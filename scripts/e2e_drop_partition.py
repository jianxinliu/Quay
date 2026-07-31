"""真实 MySQL e2e：验证 MySQL 无括号 `DROP PARTITION p1, p2` 的分类 + 执行闭环。

背景（CLAUDE.md 经验教训「sqlglot DROP PARTITION 无括号」）：MySQL 正确语法是**不带括号**的
逗号列表，但 sqlglot 只认带括号——无括号原文喂 sqlglot 会 ParseError（classify/lint 误判、
后台查询台直接拒执行），带括号又被 MySQL 按 1064 拒绝。修法是仅在解析判定前临时补括号
（normalize_sql_for_parse），执行永远走用户原文。

本脚本用真实 MySQL 证明：
  1) classify 对无括号原文判为 Alter 写操作（不再 ParseError）；
  2) run_write 发无括号原文真能删掉分区（执行路径用原文，MySQL 接受）；
  3) 发 sqlglot 会 re-render 出的带括号形式，MySQL 报 1064（佐证「必须发原文」）。

用 docker dbm-mysql-test（127.0.0.1:13306, root/123456, testdb），非生产数据。
"""

from __future__ import annotations

import os

from dbmcp.audit.classify import classify
from dbmcp.config import ConnectionConfig, Policy, WriterAccount
from dbmcp.engines import _create_readonly_engine, run_query, run_write

HOST = os.environ.get("DBM_E2E_MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("DBM_E2E_MYSQL_PORT", "13306"))
USER = os.environ.get("DBM_E2E_MYSQL_USER", "root")
PW = os.environ.get("DBM_E2E_MYSQL_PW", "123456")
DB = os.environ.get("DBM_E2E_MYSQL_DB", "testdb")

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
ok = True


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok
    ok = ok and cond
    print(f"  [{PASS if cond else FAIL}] {name}{(' — ' + extra) if extra else ''}")


cfg = ConnectionConfig(
    engine="mysql", environment="dev", host=HOST, port=PORT, database=DB,
    user=USER, password=f"plain://{PW}",
    writer=WriterAccount(user=USER, password=f"plain://{PW}"),
    policy=Policy(statement_timeout_s=30, write_timeout_s=600),
)
writer = _create_readonly_engine(cfg, "writer", cfg.host, cfg.port)
reader = _create_readonly_engine(cfg, "reader", cfg.host, cfg.port)


def partitions() -> set[str]:
    rows = run_query(
        reader,
        "SELECT partition_name FROM information_schema.partitions "
        f"WHERE table_schema='{DB}' AND table_name='dbm_part_e2e' "
        "AND partition_name IS NOT NULL",
        max_rows=100,
    ).rows
    return {r[0] for r in rows}


# 干净起点：建一张按天 RANGE 分区的表
run_write(writer, "DROP TABLE IF EXISTS dbm_part_e2e")
run_write(
    writer,
    "CREATE TABLE dbm_part_e2e (id INT, d INT) "
    "PARTITION BY RANGE (d) ("
    " PARTITION p20260702 VALUES LESS THAN (20260703),"
    " PARTITION p20260703 VALUES LESS THAN (20260704),"
    " PARTITION p20260704 VALUES LESS THAN (20260705),"
    " PARTITION p20260705 VALUES LESS THAN (20260706),"
    " PARTITION pmax VALUES LESS THAN MAXVALUE)",
)
check("初始 5 个分区", partitions() == {
    "p20260702", "p20260703", "p20260704", "p20260705", "pmax"}, str(partitions()))

# 1) classify：无括号原文判为写操作（不再 ParseError）
no_paren = "ALTER TABLE dbm_part_e2e DROP PARTITION p20260702, p20260703, p20260704"
v = classify(no_paren, "mysql")
check("classify 判为 Alter 写操作（非 ParseError）",
      v.readonly is False and v.statement_kind == "Alter",
      f"readonly={v.readonly} kind={v.statement_kind}")

# 2) run_write 发无括号原文 → MySQL 真删掉 3 个分区
run_write(writer, no_paren)
after = partitions()
check("无括号原文执行后 3 个分区被删",
      after == {"p20260705", "pmax"}, str(after))

# 3) sqlglot re-render 的带括号形式 → MySQL 报 1064（证明必须发原文，不能发归一化副本）
paren_form = "ALTER TABLE dbm_part_e2e DROP PARTITION (p20260705)"
raised_1064 = False
try:
    run_write(writer, paren_form)
except Exception as e:  # noqa: BLE001
    raised_1064 = "1064" in str(e) or "syntax" in str(e).lower()
check("带括号形式被 MySQL 1064 拒（佐证必须发原文）", raised_1064,
      "带括号未报错" if not raised_1064 else "")
check("p20260705 仍在（带括号那条没执行成功）", "p20260705" in partitions(), str(partitions()))

# 清理
run_write(writer, "DROP TABLE IF EXISTS dbm_part_e2e")
print()
print("总体:", PASS if ok else FAIL)
raise SystemExit(0 if ok else 1)
