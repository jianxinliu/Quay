"""真实库 e2e：数据库体检（db_checkup）在 MySQL / PostgreSQL / ClickHouse / SQLite 上的闭环验证。

为什么需要真库：体检 SQL 是方言相关的（PG 的 pg_stat_* 视图权限模型、MySQL 9.x 移除了
Innodb_deadlocks 状态变量改走 performance_schema 错误汇总表、CH 25.x 的 system.replicas
没了 relative_delay 列），SQLite 单测覆盖不了「视图/列是否真的存在、权限是否真的够」。

环境变量（全部可省，省略的引擎跳过）：
  DBM_E2E_MYSQL_HOST/PORT/USER/PW/DB        默认 127.0.0.1:13306 root/123456 testdb
  DBM_E2E_PG_HOST/PORT/USER/PW/DB           默认 127.0.0.1:15432 postgres/123456 testdb
  DBM_E2E_CH_HOST/PORT/USER/PW/DB           默认 127.0.0.1:19000 ch/123456 testdb
  DBM_E2E_ONLY=mysql,postgres               只跑指定引擎

验证内容：
  1) 每个引擎都能跑完一整份报告（不抛异常），且检查项数量符合预期；
  2) overall 与各项 status 自洽（overall = 最严重项）；
  3) unknown 项都带可读原因（权限/无副本等），而不是空白；
  4) MySQL/PG 的长查询能被真实检出（在另一个会话里跑 SLEEP/pg_sleep，再体检）。

用法：uv run python scripts/e2e_checkup.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

from dbmcp import checkup
from dbmcp.config import ConnectionConfig, Policy

PASS, FAIL, SKIP = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m", "\033[33mSKIP\033[0m"
ok = True

# 各引擎最少应有的检查项数量（少一项说明有检查没跑起来，多半是 SQL/视图对不上）
MIN_CHECKS = {
    "mysql": 16,
    "postgres": 16,
    "clickhouse": 8,
    "sqlite": 5,
}

# 受限只读账号（无 pg_monitor / 无 PROCESS）：验证 v2 权限设计——行级可见的项仍能测量，
# 需要 state/会话明细的项标 unknown 并汇总 GRANT 模板。省略则跳过该场景。
RESTRICTED = {
    "postgres": ("probe_reader", "probe123"),   # 无 pg_monitor
    "mysql": ("probe_r", "probe123"),           # 只有 SELECT，无 PROCESS
}


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


def conn_cfg(engine: str) -> ConnectionConfig:
    E = env
    if engine == "mysql":
        return ConnectionConfig(
            engine="mysql", environment="dev", host=E("MYSQL", "HOST", "127.0.0.1"),
            port=int(E("MYSQL", "PORT", "13306")), database=E("MYSQL", "DB", "testdb"),
            user=E("MYSQL", "USER", "root"), password=f"plain://{E('MYSQL', 'PW', '123456')}",
            policy=Policy(statement_timeout_s=30))
    if engine == "postgres":
        return ConnectionConfig(
            engine="postgres", environment="dev", host=E("PG", "HOST", "127.0.0.1"),
            port=int(E("PG", "PORT", "15432")), database=E("PG", "DB", "testdb"),
            user=E("PG", "USER", "postgres"), password=f"plain://{E('PG', 'PW', '123456')}",
            policy=Policy(statement_timeout_s=30))
    if engine == "clickhouse":
        return ConnectionConfig(
            engine="clickhouse", environment="dev", host=E("CH", "HOST", "127.0.0.1"),
            port=int(E("CH", "PORT", "19000")), database=E("CH", "DB", "testdb"),
            user=E("CH", "USER", "ch"), password=f"plain://{E('CH', 'PW', '123456')}",
            policy=Policy(statement_timeout_s=30))
    return ConnectionConfig(
        engine="sqlite", environment="dev", database=os.environ.get(
            "DBM_E2E_SQLITE_DB", ":memory:"),
        policy=Policy(statement_timeout_s=30))


def run_engine(engine: str) -> None:
    from dbmcp.engines import _create_readonly_engine

    cfg = conn_cfg(engine)
    if engine != "sqlite" and not reachable(cfg.host, cfg.port):
        print(f"  [{SKIP}] {engine}：{cfg.host}:{cfg.port} 不可达，跳过")
        return
    print(f"\n=== {engine} ===")
    if engine == "mysql":
        # 清掉前几次 e2e 残留的 SLEEP 长查询——它们是真的长查询，会让「无意外 critical」
        # 这条断言误失败。只在这个一次性的测试容器里做，且只杀 >120s 的旧线程。
        _cleanup_mysql_sleeps(cfg)
    if engine == "sqlite" and cfg.database == ":memory:":
        # 内存库至少建张表，让大表/完整性检查有东西可看
        import sqlite3
        import tempfile
        db = os.path.join(tempfile.mkdtemp(), "e2e.sqlite3")
        con = sqlite3.connect(db)
        con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT);")
        con.executemany("INSERT INTO t (s) VALUES (?)", [(f"n{i}",) for i in range(100)])
        con.commit()
        con.close()
        cfg = cfg.model_copy(update={"database": db})

    engine_obj = _create_readonly_engine(cfg, "reader", cfg.host, cfg.port)
    rep = checkup.run_checkup(engine_obj, engine)
    rep.to_dict()  # 序列化必须不崩（前端/MCP 拿到的就是这个 dict）

    for c in rep.checks:
        marker = {"warn": "⚠ ", "critical": "✖ ", "unknown": "? "}.get(c.status, "  ")
        print(f"    {marker}[{c.status:8}] {c.title} = {c.value}")
        if c.status == "unknown":
            print(f"            原因：{c.message}")
        for detail in c.details[:3]:
            print(f"            - {detail}")

    check(f"{engine} 跑完整份报告未抛异常", len(rep.checks) > 0, f"{len(rep.checks)} 项")
    check(f"{engine} 检查项数量符合预期",
          len(rep.checks) >= MIN_CHECKS.get(engine, 3),
          f">= {MIN_CHECKS.get(engine, 3)}")
    check(f"{engine} overall 与各项自洽",
          rep.overall == checkup._worst(rep.checks), f"overall={rep.overall}")
    unknown = [c for c in rep.checks if c.status == "unknown" and not c.message]
    check(f"{engine} 每个未知项都写了原因", not unknown,
          f"{len(unknown)} 项无原因" if unknown else "")
    critical = [c for c in rep.checks if c.status == "critical"]
    check(f"{engine} 无意外 critical", not critical,
          ", ".join(c.title for c in critical) if critical else "")

    # 受限只读账号场景：权限重新设计的真实验证
    if engine in RESTRICTED:
        check_restricted(engine, cfg)

    # 长查询真实检出（mysql/postgres）：另起一个会话跑 sleep，同时体检
    if engine in ("mysql", "postgres"):
        check_long_query(engine, cfg)


def check_restricted(engine: str, cfg: ConnectionConfig) -> None:
    """用受限账号体检：行级可见的项必须出真值，需要会话明细的项必须 unknown + 权限提示。"""
    from dbmcp.engines import _create_readonly_engine

    user, pw = RESTRICTED[engine]
    if engine == "postgres":
        # probe_reader 无 pg_monitor：pg_stat_activity 的行可见、state 列为 NULL
        restricted = cfg.model_copy(update={"user": user,
                                           "password": f"plain://{pw}"})
        must_work = ("connections", "replication_slots", "cache_hit_ratio",
                     "deadlocks", "bloat", "stats_stale", "unused_indexes")
        must_unknown = ("idle_in_transaction", "long_queries", "wait_events")
        priv = "pg_monitor"
    else:
        # probe_r 只有 SELECT、无 PROCESS：performance_schema.threads 仍可见全部线程
        restricted = cfg.model_copy(update={"user": user,
                                           "password": f"plain://{pw}"})
        must_work = ("long_queries", "lock_waits", "connections",
                     "tmp_tables_on_disk", "buffer_pool_hit_ratio")
        must_unknown = ()
        priv = None
    print(f"  --- 受限账号 {user} ---")
    eng = _create_readonly_engine(restricted, "reader", restricted.host, restricted.port)
    rep = checkup.run_checkup(eng, engine)
    for c in rep.checks:
        if c.status != "ok" and c.status != "info":
            print(f"    [{c.status:8}] {c.title} = {c.value}")
    for name in must_work:
        c = next((x for x in rep.checks if x.name == name), None)
        check(f"{engine}/{user} {name} 受限账号仍可测量", c is not None and c.status != "unknown",
              c.value if c else "缺这项")
    for name in must_unknown:
        c = next((x for x in rep.checks if x.name == name), None)
        cond = c is not None and c.status == "unknown" and c.privilege == priv
        check(f"{engine}/{user} {name} 无权限时标 unknown 并提示 {priv}", cond,
              f"{c.message[:60]}..." if c else "缺这项")
    if priv:
        gaps = {g.privilege for g in rep.privileges}
        check(f"{engine}/{user} 报告含 GRANT 模板", priv in gaps, str(sorted(gaps)))


def _cleanup_mysql_sleeps(cfg: ConnectionConfig) -> None:
    """清理前次 e2e 残留的长查询线程（共享测试容器才有这问题）。"""
    try:
        import pymysql
        conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                               password=cfg.password.replace("plain://", ""),
                               connect_timeout=5, read_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT PROCESSLIST_ID FROM performance_schema.threads"
                            " WHERE PROCESSLIST_TIME > 120 AND PROCESSLIST_COMMAND = 'Query'"
                            " AND CONNECTION_ID() <> PROCESSLIST_ID")
                for (pid,) in cur.fetchall():
                    cur.execute(f"KILL CONNECTION {int(pid)}")
        finally:
            conn.close()
    except Exception as ex:  # noqa: BLE001
        print(f"  [SKIP] 清理残留长查询失败（不影响主流程）：{type(ex).__name__}")


def check_long_query(engine: str, cfg: ConnectionConfig) -> None:
    """在独立会话里跑一条 >= 阈值的慢查询，体检必须能在长查询存活期间检出它。"""
    from dbmcp.engines import _create_readonly_engine

    sleep_s = max(checkup.LONG_QUERY_WARN_S + 20, 80)
    if engine == "mysql":
        cmd = ["mysql", "-h", cfg.host, "-P", str(cfg.port), "-u", cfg.user,
               f"-p{cfg.password.replace('plain://', '')}", "-e", f"SELECT SLEEP({sleep_s})"]
    else:
        cmd = ["docker", "exec", os.environ.get("DBM_E2E_PG_CONTAINER", "dbm-pg-test"),
               "psql", "-U", cfg.user, "-d", cfg.database, "-c",
               f"SELECT pg_sleep({sleep_s});"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError as ex:
        print(f"  [{SKIP}] {engine} 长查询检出：缺客户端（{ex}）")
        return
    try:
        time.sleep(checkup.LONG_QUERY_WARN_S + 8)  # 等它超过阈值
        eng = _create_readonly_engine(cfg, "reader", cfg.host, cfg.port)
        rep = checkup.run_checkup(eng, engine)
        long_ = next((c for c in rep.checks if c.name == "long_queries"), None)
        cond = long_ is not None and long_.status in ("warn", "critical") and long_.details
        check(f"{engine} 真实检出长查询", cond,
              long_.value if long_ else "没有 long_queries 检查项")
        if cond:
            print(f"            - {long_.details[0]}")
    finally:
        proc.kill()
        proc.wait()


def main() -> None:
    only = os.environ.get("DBM_E2E_ONLY", "")
    engines = only.split(",") if only else ["mysql", "postgres", "clickhouse", "sqlite"]
    for engine in engines:
        engine = engine.strip()
        if engine in checkup.supported_engines() or engine == "sqlite":
            run_engine(engine)
        else:
            print(f"  [{SKIP}] 未知引擎：{engine}")
    print()
    print(f"{PASS} 全部通过" if ok else f"{FAIL} 有失败项")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
