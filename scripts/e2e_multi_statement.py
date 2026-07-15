"""真实 MySQL e2e：验证 run_write 多语句批量（拆分 + 单事务逐条执行）。

复现用户失败场景：ALTER TABLE ... ; UPDATE ...; UPDATE ...; —— 单条 execute 发
多语句会被 pymysql 打成 1064；本脚本证明拆分后各条真实执行、影响行数累加。

用 docker dbm-mysql-test（127.0.0.1:13306, root/123456, testdb），非生产数据。
"""

from __future__ import annotations

import os

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

# 干净起点
run_write(writer, "DROP TABLE IF EXISTS dbm_multi_e2e")

# 1) 建表 + 批量插入（多语句，一次 run_write）——迁移的典型形态
res = run_write(
    writer,
    "CREATE TABLE dbm_multi_e2e (id INT PRIMARY KEY, category VARCHAR(32), note VARCHAR(255));"
    "INSERT INTO dbm_multi_e2e (id, category) VALUES (1, 'a'), (2, 'a'), (3, 'b');"
    "UPDATE dbm_multi_e2e SET note='设备节流' WHERE category='a';",
)
print(f"批量1 affected_rows={res.row_count}")
# INSERT 3 行 + UPDATE 2 行 = 5（CREATE 不计）
check("多语句批量执行成功且影响行数累加", res.row_count == 5, f"got {res.row_count}")

rows = run_query(reader, "SELECT count(*) FROM dbm_multi_e2e", max_rows=10).rows
check("3 行已插入", rows[0][0] == 3, f"got {rows[0][0]}")
rows = run_query(reader,
                 "SELECT count(*) FROM dbm_multi_e2e WHERE note='设备节流'", max_rows=10).rows
check("UPDATE 生效（2 行 note 被填）", rows[0][0] == 2, f"got {rows[0][0]}")

# 2) ALTER + 回填 UPDATE（用户原始失败案例的结构：DDL + DML 混合）
res = run_write(
    writer,
    "ALTER TABLE dbm_multi_e2e ADD COLUMN description VARCHAR(1024) NOT NULL DEFAULT '';"
    "UPDATE dbm_multi_e2e SET description='单台设备被填充的广告' WHERE category='a';"
    "UPDATE dbm_multi_e2e SET description='其它' WHERE category='b';",
)
print(f"批量2 (ALTER+UPDATE) affected_rows={res.row_count}")
check("ALTER+回填 UPDATE 批量成功", res.row_count == 3, f"got {res.row_count}")
rows = run_query(reader,
                 "SELECT description FROM dbm_multi_e2e WHERE id=1", max_rows=10).rows
check("新列已回填", rows[0][0] == "单台设备被填充的广告", f"got {rows[0][0]!r}")

# 3) 单语句仍正常（split 对单条是 no-op）
res = run_write(writer, "UPDATE dbm_multi_e2e SET note='x' WHERE id=3")
check("单语句仍正常", res.row_count == 1, f"got {res.row_count}")

# 4) 语句内含分号的字符串字面量不被误拆
res = run_write(writer, "UPDATE dbm_multi_e2e SET note='a;b;c' WHERE id=1")
rows = run_query(reader, "SELECT note FROM dbm_multi_e2e WHERE id=1", max_rows=10).rows
check("字符串内分号不误拆", rows[0][0] == "a;b;c", f"got {rows[0][0]!r}")

# 清理
run_write(writer, "DROP TABLE IF EXISTS dbm_multi_e2e")
print()
print("总体:", PASS if ok else FAIL)
raise SystemExit(0 if ok else 1)
