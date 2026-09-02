"""真实 MySQL / PostgreSQL e2e：跨连接表同步（sync_table）。

覆盖的不变量（SQLite 单测测不出、必须真机跑）：
  1. **同引擎建表用原文**：MySQL → MySQL 时目标表保留 ENGINE / CHARSET / 表注释 / 二级索引；
  2. **跨引擎转写的 DDL 真能执行**：MySQL → PG 的近似 DDL 直接发给 PG 必须建表成功
     （本地只能验证"字符串长得像"，方言到底认不认只有真库说了算）；
  3. **值保真**：DECIMAL 精度、datetime、JSON、中文、超 2^53 的大整数在目标库与源库逐一相等
     ——复制路径刻意绕开了 run_query 的 JSON 化/截断，这条就是在验证那个决定；
  4. 破坏性模式（data=replace / ddl=recreate）行为正确；
  5. 目标是 prod 连接时一律拒绝。

用法（两个一次性测试容器，非生产）：
    docker run -d --name dbm-pg-test    -e POSTGRES_PASSWORD=123456 -e POSTGRES_DB=testdb -p 15432:5432 postgres:17
    docker run -d --name dbm-mysql-test -e MYSQL_ROOT_PASSWORD=123456 -e MYSQL_DATABASE=testdb -p 13306:3306 mysql:8.4
    uv run python scripts/e2e_sync.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import text

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.engines import _create_readonly_engine
from dbmcp.service import CallerInfo, DbmService, QueryRejected
from dbmcp.sync import SyncSpec

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
CALLER = CallerInfo(agent="e2e-sync", session_id="e2e")
SRC_DB, DST_DB = "dbm_sync_src", "dbm_sync_dst"
ok_all = True


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"  [{PASS if cond else FAIL}] {name}{(' — ' + extra) if extra else ''}")


MY_HOST = os.environ.get("DBM_E2E_MYSQL_HOST", "127.0.0.1")
MY_PORT = int(os.environ.get("DBM_E2E_MYSQL_PORT", "13306"))
PG_HOST = os.environ.get("DBM_E2E_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("DBM_E2E_PG_PORT", "15432"))

MY_ROOT = {"user": "root", "password": "plain://123456"}


def mysql_conn(database: str, environment: str) -> dict:
    return {"engine": "mysql", "host": MY_HOST, "port": MY_PORT, "database": database,
            "environment": environment, **MY_ROOT, "writer": dict(MY_ROOT)}


def pg_conn(environment: str) -> dict:
    return {"engine": "postgres", "host": PG_HOST, "port": PG_PORT, "database": "testdb",
            "environment": environment, "user": "postgres", "password": "plain://123456",
            "writer": {"user": "postgres", "password": "plain://123456"}}


# 源表刻意做得"脏"一点：无符号大整数主键、DECIMAL、带 collation 的 varchar、JSON、
# 中文注释、二级索引——跨引擎转写踩的雷全在这些成分上。
SRC_DDL = """
CREATE TABLE orders (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT NOT NULL DEFAULT 0,
  amount DECIMAL(12,4) NOT NULL DEFAULT 0.0000,
  channel VARCHAR(32) COLLATE utf8mb4_general_ci DEFAULT NULL,
  payload JSON DEFAULT NULL,
  title VARCHAR(64) DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_created (user_id, created_at),
  KEY idx_channel (channel)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单表'
"""

BIG_ID = 9007199254740993          # 2^53 + 1：JSON 化会丢精度的那个量级
ROWS = [
    (1, 100, "12.3456", "web", '{"k": 1}', "中文标题", "2026-01-01 10:00:00"),
    (2, 100, "0.0001", "app", '{"k": 2}', "second", "2026-01-02 10:00:00"),
    (3, 200, "9999.9999", None, None, "third", "2026-01-03 10:00:00"),
    (4, 200, "5.5000", "web", '{"nested": {"a": [1,2]}}', "fourth", "2026-01-04 10:00:00"),
    (BIG_ID, 300, "1.0000", "api", None, "big id", "2026-01-05 10:00:00"),
]


def seed(cfg: AppConfig) -> None:
    """在 MySQL 建源库/目标库与源表数据；PG 侧清掉可能残留的表。"""
    my = cfg.get_connection("e2e", "my_src")
    root = _create_readonly_engine(my.model_copy(update={"database": None}), "writer",
                                   MY_HOST, MY_PORT)
    with root.begin() as conn:
        for db in (SRC_DB, DST_DB):
            conn.execute(text(f"DROP DATABASE IF EXISTS {db}"))
            conn.execute(text(f"CREATE DATABASE {db} DEFAULT CHARSET utf8mb4"))
    src = _create_readonly_engine(my, "writer", MY_HOST, MY_PORT)
    with src.begin() as conn:
        conn.execute(text(SRC_DDL))
        conn.execute(
            text("INSERT INTO orders (id,user_id,amount,channel,payload,title,created_at)"
                 " VALUES (:a,:b,:c,:d,:e,:f,:g)"),
            [dict(zip("abcdefg", r, strict=True)) for r in ROWS],
        )
    root.dispose()
    src.dispose()

    pg = _create_readonly_engine(cfg.get_connection("e2e", "pg_dst"), "writer", PG_HOST, PG_PORT)
    with pg.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS orders"))
        conn.execute(text("DROP TABLE IF EXISTS orders_pg"))
    pg.dispose()


def make_service(tmp: Path) -> DbmService:
    cfg = AppConfig.model_validate({"projects": {"e2e": {"connections": {
        "my_src": mysql_conn(SRC_DB, "dev"),
        "my_dst": mysql_conn(DST_DB, "local"),
        "pg_dst": pg_conn("local"),
        "my_prod": mysql_conn(DST_DB, "prod"),
    }}}})
    db = tmp / "audit.sqlite3"
    return DbmService(cfg, AuditStore(db), ApprovalStore(db))


def run_sync(svc: DbmService, spec: SyncSpec) -> dict:
    """提交 → 人工批准 → 带 change_id 重提执行，走的就是 agent 那条完整路径。"""
    submitted = svc.sync_table(spec, CALLER, reason="e2e")
    if submitted.get("status") != "approval_required":
        return submitted
    svc.approve_change(submitted["change_id"], decided_by="e2e")
    return svc.sync_table(spec, CALLER, change_id=submitted["change_id"])


def q(svc: DbmService, conn_name: str, sql: str) -> list[list]:
    cfg = svc.config.get_connection("e2e", conn_name)
    return svc._read("e2e", conn_name, cfg, sql, CALLER, 100, mask=False)["rows"]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dbm-e2e-sync-"))
    svc = make_service(tmp)
    print(f"\n临时数据目录: {tmp}")
    try:
        seed(svc.config)
    except Exception as e:
        print(f"[{FAIL}] 建测试库失败（容器起了吗？）: {type(e).__name__}: {e}")
        return 1

    base = dict(source_project="e2e", source_connection="my_src", source_table="orders",
                target_project="e2e")

    print("\n[1] 计划预览（dry_run 不占审批单）")
    plan = svc.sync_table(SyncSpec(**base, target_connection="my_dst", target_table="orders"),
                          CALLER, dry_run=True)
    check("dry_run 返回计划", plan["status"] == "planned")
    check("目标表尚不存在", plan["target_exists"] is False)
    check("列齐全", plan["columns"] == ["id", "user_id", "amount", "channel", "payload",
                                        "title", "created_at"])
    check("未生成审批单", svc.list_changes() == [])

    print("\n[2] MySQL → MySQL：同引擎建表用源库原文")
    out = run_sync(svc, SyncSpec(**base, target_connection="my_dst", target_table="orders"))
    check("执行成功", out.get("status") == "executed", str(out.get("reason", "")))
    check("复制 5 行", out.get("affected_rows") == 5, str(out.get("affected_rows")))
    ddl = svc.get_table_ddl("e2e", "my_dst", "orders", CALLER)
    check("保留 ENGINE=InnoDB", "ENGINE=InnoDB" in ddl)
    check("保留表注释", "订单表" in ddl)
    check("保留二级索引 idx_channel", "idx_channel" in ddl)
    check("保留 UNSIGNED 主键", "unsigned" in ddl.lower())

    print("\n[3] 值保真（DECIMAL / datetime / JSON / 中文 / 大整数）")
    src_rows = q(svc, "my_src", "SELECT id,amount,payload,title,created_at FROM orders ORDER BY id")
    dst_rows = q(svc, "my_dst", "SELECT id,amount,payload,title,created_at FROM orders ORDER BY id")
    check("逐行逐列与源库一致", src_rows == dst_rows,
          f"src={src_rows[:1]} dst={dst_rows[:1]}")
    big = q(svc, "my_dst", f"SELECT amount FROM orders WHERE id = {BIG_ID}")
    check("大整数主键行存在且金额精确", big and str(big[0][0]) == "1.0000", str(big))
    dec = q(svc, "my_dst", "SELECT amount FROM orders WHERE id = 3")
    check("DECIMAL 精度未丢", dec and str(dec[0][0]) == "9999.9999", str(dec))

    print("\n[4] MySQL → PostgreSQL：跨引擎转写的 DDL 真能在 PG 上执行")
    spec_pg = SyncSpec(**base, target_connection="pg_dst", target_table="orders_pg")
    submitted = svc.sync_table(spec_pg, CALLER, reason="e2e cross")
    check("计划带近似 DDL 告警",
          any("近似 DDL" in w for w in submitted.get("warnings", [])),
          str(submitted.get("warnings")))
    svc.approve_change(submitted["change_id"], decided_by="e2e")
    out = svc.sync_table(spec_pg, CALLER, change_id=submitted["change_id"])
    check("PG 建表 + 写入成功", out.get("status") == "executed", str(out.get("reason", "")))
    check("复制 5 行", out.get("affected_rows") == 5, str(out.get("affected_rows")))
    pg_rows = q(svc, "pg_dst", "SELECT id, amount, title FROM orders_pg ORDER BY id")
    check("PG 侧大整数主键保真",
          any(str(r[0]) == str(BIG_ID) for r in pg_rows), str(pg_rows))
    check("PG 侧 DECIMAL 保真",
          any(str(r[1]) == "9999.9999" for r in pg_rows), str(pg_rows))
    check("PG 侧中文保真", any(r[2] == "中文标题" for r in pg_rows), str(pg_rows))

    print("\n[5] WHERE / ORDER BY / limit 收窄 + 源侧还有更多的探测")
    spec = SyncSpec(**base, target_connection="my_dst", target_table="orders_top",
                    where="user_id = 200", order_by="id DESC", limit=1)
    out = run_sync(svc, spec)
    check("只同步 1 行", out.get("affected_rows") == 1, str(out.get("affected_rows")))
    check("如实告知源侧还有更多", out.get("source_truncated") is True)
    top = q(svc, "my_dst", "SELECT id FROM orders_top")
    check("取的是 ORDER BY 后的第一行", top and int(top[0][0]) == 4, str(top))

    print("\n[6] 破坏性模式")
    spec = SyncSpec(**base, target_connection="my_dst", target_table="orders_top",
                    ddl="skip", data="replace", where="user_id = 100")
    out = run_sync(svc, spec)
    check("replace 执行成功", out.get("status") == "executed", str(out.get("reason", "")))
    rows = q(svc, "my_dst", "SELECT id FROM orders_top ORDER BY id")
    check("目标表被清空后只剩新数据", [int(r[0]) for r in rows] == [1, 2], str(rows))

    spec = SyncSpec(**base, target_connection="my_dst", target_table="orders_top",
                    ddl="recreate", data="append", where="id = 3")
    out = run_sync(svc, spec)
    check("recreate 走 drop→create→copy",
          [s["step"] for s in out.get("steps", [])] == ["drop", "create", "copy"],
          str(out.get("steps")))
    rows = q(svc, "my_dst", "SELECT id FROM orders_top")
    check("重建后只有新同步的一行", [int(r[0]) for r in rows] == [3], str(rows))

    print("\n[7] 守卫")
    try:
        svc.sync_table(SyncSpec(**base, target_connection="my_prod", target_table="orders"),
                       CALLER, dry_run=True)
        check("拒绝向 prod 同步", False, "居然没拒绝")
    except QueryRejected as e:
        check("拒绝向 prod 同步", "生产环境" in str(e), str(e)[:60])

    tools = {r["tool"] for r in svc.store.recent(limit=200)}
    check("源读与目标写都落了审计", {"sync_read", "sync_write"} <= tools, str(sorted(tools)))

    svc.close()
    print(f"\n{'全部通过' if ok_all else '有失败项'}\n")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
