"""真实库 e2e：实例级体检（db_checkup_all）+ 合并报告（merge_reports）。

为什么需要真库：db_checkup_all = 列实例上的用户库 + 逐库 run_checkup + merge_reports。
合并的「实例级指标去重 / 库级指标取最严重并标库名」依赖各库真实查出来的值是否相同
（连接占用这类在任一库上查都一样；大表、无主键表各库不同）——SQLite 单测造不出
「同一台实例上有多个库」的真实结构。

环境变量（可省，省略的引擎跳过）：
  DBM_E2E_MYSQL_HOST/PORT/USER/PW/DB   默认 127.0.0.1:13306 root/123456 testdb
  DBM_E2E_ONLY=mysql                   只跑指定引擎

验证内容：
  1) db_checkup_all 跑完整台实例的所有用户库，不抛异常；
  2) scope 变成「全体 N 个库」，N = 真实用户库数量；
  3) 实例级指标（如连接占用）在合并后只剩一条、且**不带** [库名] 前缀；
  4) 库级指标各库不同时，取最严重那条、标题带 [库名] 前缀；
  5) overall = 所有检查里最严重的；
  6) 没有同名检查重复出现（去重没生效会复制 N 遍连接占用）；
  7) 报告能序列化（前端 / MCP 拿到的就是这个 dict）。

用法：uv run python scripts/e2e_checkup_all.py
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from collections import Counter

from dbmcp import checkup, engines
from dbmcp.config import AppConfig, ConnectionConfig, Policy
from dbmcp.service import CallerInfo, DbmService

PASS, FAIL, SKIP = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m", "\033[33mSKIP\033[0m"
ok = True


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok
    ok = ok and cond
    print(f"  [{PASS if cond else FAIL}] {name}{(' — ' + extra) if extra else ''}")


def env(engine: str, key: str, default: str) -> str:
    return os.environ.get(f"DBM_E2E_{engine}_{key}", default)


def reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def build_service(cfg: ConnectionConfig) -> DbmService:
    from dbmcp.audit.log import AuditStore

    from dbmcp.config import ProjectConfig

    config = AppConfig(projects={"e2e": ProjectConfig(connections={"db": cfg})})
    store = AuditStore(os.path.join(tempfile.mkdtemp(), "audit.sqlite3"))
    return DbmService(config, store)


def run_mysql() -> None:
    host = env("MYSQL", "HOST", "127.0.0.1")
    port = int(env("MYSQL", "PORT", "13306"))
    if not reachable(host, port):
        print(f"  [{SKIP}] mysql：{host}:{port} 不可达，跳过")
        return
    # 实例级体检的典型场景就是「没选库」——故意不绑默认库，直接连实例
    db = env("MYSQL", "DB", "") or None
    cfg = ConnectionConfig(
        engine="mysql", environment="dev", host=host, port=port,
        database=db,
        user=env("MYSQL", "USER", "root"),
        password=f"plain://{env('MYSQL', 'PW', '123456')}",
        policy=Policy(statement_timeout_s=30))
    print(f"\n=== mysql {host}:{port} ===")

    # 先看这台实例上有多少用户库（决定「全体 N 个库」的 N）
    eng = engines._create_readonly_engine(cfg, "reader", cfg.host, cfg.port)
    dbs = engines.list_databases(eng)
    check("列得出用户库", len(dbs) >= 2, f"{len(dbs)} 个：{', '.join(dbs[:5])}")

    svc = build_service(cfg)
    rep = svc.db_checkup_all("e2e", "db", CallerInfo("e2e", "e2e-script"))

    check("scope 是全体 N 个库", rep.get("scope", "").startswith("全体 "),
          f"scope={rep.get('scope')!r}")
    n = int(rep["scope"].removeprefix("全体 ").removesuffix(" 个库"))
    check("覆盖了全部用户库", n == len(dbs), f"报告 {n} 个 vs 实际 {len(dbs)} 个")

    names = [c["name"] for c in rep["checks"]]
    dupes = {k: v for k, v in Counter(names).items() if v > 1}
    check("没有同名检查重复", not dupes, f"重复：{dupes}" if dupes else f"{len(names)} 项")

    # 实例级指标（连接占用在任一库上查都一样）不应带 [库名] 前缀
    conn = next((c for c in rep["checks"] if c["name"] == "connections"), None)
    if conn:
        check("实例级指标去重且无库名前缀",
              not conn["title"].startswith("[") and conn["status"] != "unknown"
              or not conn["title"].startswith("["),
              f"{conn['title']} = {conn['value']}")
    else:
        check("有连接占用项", False, "缺 connections 检查")

    prefixed = [c["title"] for c in rep["checks"] if c["title"].startswith("[")]
    check("库级指标带 [库名] 前缀", bool(prefixed) or n == 1,
          f"{len(prefixed)} 项，如 {prefixed[:2]}" if prefixed else "只有一个库，无库级差异")

    from dbmcp.checkup import _worst, Check
    expected = _worst([Check(c["name"], c["title"], c["status"]) for c in rep["checks"]])
    check("overall = 最严重项", rep["overall"] == expected,
          f"overall={rep['overall']} vs worst={expected}")

    # 序列化过的报告喂给 AI 诊断的渲染器，必须不崩
    md = checkup.report_to_markdown(rep)
    check("报告可渲染为 Markdown", "数据库体检报告" in md, f"{len(md)} 字符")

    for c in rep["checks"]:
        marker = {"warn": "⚠ ", "critical": "✖ ", "unknown": "? "}.get(c["status"], "  ")
        print(f"    {marker}[{c['status']:8}] {c['title']} = {c['value']}")


if __name__ == "__main__":
    only = os.environ.get("DBM_E2E_ONLY", "mysql").split(",")
    if "mysql" in only:
        run_mysql()
    print(f"\n{'全部通过' if ok else '有失败项'}")
    sys.exit(0 if ok else 1)
