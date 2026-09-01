"""真实 PostgreSQL / MySQL e2e：用户与权限管理的完整闭环。

覆盖的不变量（单测测不出、必须真机跑）：
  1. 目录查询在真库上能跑通——PG 走 pg_roles / aclexplode，MySQL 走 mysql.user /
     SHOW GRANTS，方言写法只有真库认；
  2. 授权**真的生效**：新建账号 → 连上去查表被拒 → GRANT → 同一条查询成功 →
     REVOKE → 又被拒。不验证「效果」的权限功能等于没验证；
  3. PG 的 ALTER DEFAULT PRIVILEGES 对**之后新建**的表生效（这正是
     「GRANT ON ALL TABLES 之后新表又没权限」那个坑的解药）；
  4. **密码不落审计**：CREATE USER 执行后，audit_log 里那条记录的 sql 必须是脱敏版。

用法（两个容器都是一次性测试库，非生产）：
    docker run -d --name dbm-pg-test    -e POSTGRES_PASSWORD=123456 -e POSTGRES_DB=testdb -p 15432:5432 postgres:17
    docker run -d --name dbm-mysql-test -e MYSQL_ROOT_PASSWORD=123456 -e MYSQL_DATABASE=testdb -p 13306:3306 mysql:8.4
    uv run python scripts/e2e_privileges.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import text

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.engines import _create_readonly_engine
from dbmcp.service import CallerInfo, DbmService

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
CALLER = CallerInfo(agent="e2e-privileges", session_id="e2e")
ok_all = True


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"  [{PASS if cond else FAIL}] {name}{(' — ' + extra) if extra else ''}")


PG = dict(engine="postgres", host=os.environ.get("DBM_E2E_PG_HOST", "127.0.0.1"),
          port=int(os.environ.get("DBM_E2E_PG_PORT", "15432")), database="testdb",
          user="postgres", password="plain://123456", environment="dev",
          writer={"user": "postgres", "password": "plain://123456"})
MY = dict(engine="mysql", host=os.environ.get("DBM_E2E_MYSQL_HOST", "127.0.0.1"),
          port=int(os.environ.get("DBM_E2E_MYSQL_PORT", "13306")), database="testdb",
          user="root", password="plain://123456", environment="dev",
          writer={"user": "root", "password": "plain://123456"})


def make_service(tmp: Path) -> DbmService:
    cfg = AppConfig.model_validate({"projects": {"e2e": {"connections": {"pg": PG, "my": MY}}}})
    db = tmp / "audit.sqlite3"
    return DbmService(cfg, AuditStore(db), ApprovalStore(db))


def run_dcl(svc: DbmService, conn: str, action: str, params: dict) -> dict:
    """走完整两段式：先预览拿指纹，再带指纹确认执行。"""
    card = svc.admin_run_dcl("e2e", conn, action, params, CALLER)
    return svc.admin_run_dcl("e2e", conn, action, params, CALLER, confirm=True,
                             expect_fingerprint=card["fingerprint"])


def probe_as(engine_kind: str, user: str, pw: str, sql: str) -> tuple[bool, str]:
    """用**新账号自己**连上去跑一条 SQL，验证授权到底有没有生效。"""
    from dbmcp.config import ConnectionConfig
    src = PG if engine_kind == "postgres" else MY
    cfg = ConnectionConfig.model_validate({**{k: v for k, v in src.items() if k != "writer"},
                                           "user": user, "password": f"plain://{pw}"})
    eng = _create_readonly_engine(cfg, "reader", cfg.host, cfg.port)
    try:
        with eng.connect() as c:
            c.execute(text(sql))
        return True, ""
    except Exception as e:  # noqa: BLE001
        from dbmcp.errors import classify_db_error
        return False, classify_db_error(e)
    finally:
        eng.dispose()


def seed(engine_kind: str, statements: list[str]) -> None:
    from dbmcp.config import ConnectionConfig
    src = PG if engine_kind == "postgres" else MY
    cfg = ConnectionConfig.model_validate(src)
    eng = _create_readonly_engine(cfg, "writer", cfg.host, cfg.port)
    with eng.begin() as c:
        for s in statements:
            c.execute(text(s))
    eng.dispose()


def test_postgres(svc: DbmService) -> None:
    print("\n=== PostgreSQL ===")
    who = "e2e_" + uuid.uuid4().hex[:8]
    pw = "Probe#" + uuid.uuid4().hex[:8]
    seed("postgres", ["DROP TABLE IF EXISTS priv_a", "CREATE TABLE priv_a(id int)",
                      "INSERT INTO priv_a VALUES (1)"])

    users = svc.admin_list_db_users("e2e", "pg", CALLER)
    names = [r[users["columns"].index("name")] for r in users["rows"]]
    check("列出角色（pg_roles）", "postgres" in names, f"{len(names)} 个角色")
    check("下发权限白名单", "SELECT" in users["privileges"]["table"])

    run_dcl(svc, "pg", "create_user", {"name": who, "password": pw})
    users2 = svc.admin_list_db_users("e2e", "pg", CALLER)
    names2 = [r[users2["columns"].index("name")] for r in users2["rows"]]
    check("新建角色后出现在列表里", who in names2)

    okc, kind = probe_as("postgres", who, pw, "SELECT 1")
    check("新角色能登录", okc, kind)
    okc, kind = probe_as("postgres", who, pw, "SELECT * FROM priv_a")
    check("授权前读表被拒", (not okc) and kind == "permission_denied", kind)

    run_dcl(svc, "pg", "grant", {"privileges": ["USAGE"], "level": "schema",
                                 "grantee": who, "schema": "public"})
    run_dcl(svc, "pg", "grant", {"privileges": ["SELECT"], "level": "all_tables",
                                 "grantee": who, "schema": "public"})
    okc, kind = probe_as("postgres", who, pw, "SELECT * FROM priv_a")
    check("GRANT 后读表成功", okc, kind)

    grants = svc.admin_db_user_grants("e2e", "pg", who, CALLER)
    tbl = [g for g in grants["groups"] if g["title"] == "表 / 视图"][0]
    check("授权明细里能看到这张表", any("priv_a" in str(r) for r in tbl["rows"]),
          f"{tbl['row_count']} 行")

    matrix = svc.admin_privilege_matrix("e2e", "pg", "public", CALLER)
    mi = {c: i for i, c in enumerate(matrix["columns"])}
    hit = [r for r in matrix["rows"] if r[mi["table"]] == "priv_a" and r[mi["grantee"]] == who]
    check("权限矩阵含新授权", bool(hit))
    check("权限矩阵带属主（空 ACL 才解释得清）",
          all(r[mi["owner"]] for r in matrix["rows"]))

    # 默认权限：GRANT ON ALL TABLES 只管现有表，新建的表仍无权限——这正是那个坑
    seed("postgres", ["DROP TABLE IF EXISTS priv_b", "CREATE TABLE priv_b(id int)"])
    okc, kind = probe_as("postgres", who, pw, "SELECT * FROM priv_b")
    check("ALL TABLES 授权对**之后**新建的表无效（复现坑）",
          (not okc) and kind == "permission_denied", kind)
    run_dcl(svc, "pg", "default_privileges",
            {"privileges": ["SELECT"], "schema": "public", "grantee": who,
             "obj_type": "TABLES", "for_role": "postgres"})
    seed("postgres", ["DROP TABLE IF EXISTS priv_c", "CREATE TABLE priv_c(id int)"])
    okc, kind = probe_as("postgres", who, pw, "SELECT * FROM priv_c")
    check("设默认权限后，新建表自动有权限", okc, kind)

    run_dcl(svc, "pg", "revoke", {"privileges": ["SELECT"], "level": "all_tables",
                                  "grantee": who, "schema": "public"})
    okc, kind = probe_as("postgres", who, pw, "SELECT * FROM priv_a")
    check("REVOKE 后读表又被拒", (not okc) and kind == "permission_denied", kind)

    # 收尾：清掉默认权限与属主依赖，再删角色
    run_dcl(svc, "pg", "revoke_default_privileges",
            {"privileges": ["SELECT"], "schema": "public", "grantee": who,
             "obj_type": "TABLES", "for_role": "postgres"})
    run_dcl(svc, "pg", "revoke", {"privileges": ["SELECT"], "level": "all_tables",
                                  "grantee": who, "schema": "public"})
    run_dcl(svc, "pg", "revoke", {"privileges": ["USAGE"], "level": "schema",
                                  "grantee": who, "schema": "public"})
    run_dcl(svc, "pg", "drop_user", {"name": who})
    users3 = svc.admin_list_db_users("e2e", "pg", CALLER)
    check("删除后角色消失",
          who not in [r[users3["columns"].index("name")] for r in users3["rows"]])
    seed("postgres", ["DROP TABLE IF EXISTS priv_a", "DROP TABLE IF EXISTS priv_b",
                      "DROP TABLE IF EXISTS priv_c"])
    return pw


def test_mysql(svc: DbmService) -> str:
    print("\n=== MySQL ===")
    who = "e2e_" + uuid.uuid4().hex[:8]
    pw = "Probe#" + uuid.uuid4().hex[:8]
    seed("mysql", ["DROP TABLE IF EXISTS priv_a", "CREATE TABLE priv_a(id int)",
                   "INSERT INTO priv_a VALUES (1)"])

    users = svc.admin_list_db_users("e2e", "my", CALLER)
    ni, hi = users["columns"].index("name"), users["columns"].index("host")
    check("列出账号（mysql.user）", any(r[ni] == "root" for r in users["rows"]),
          f"{users['row_count']} 个账号")

    run_dcl(svc, "my", "create_user", {"name": who, "password": pw, "host": "%"})
    users2 = svc.admin_list_db_users("e2e", "my", CALLER)
    check("新建账号带 host 出现在列表里",
          any(r[ni] == who and r[hi] == "%" for r in users2["rows"]))

    okc, kind = probe_as("mysql", who, pw, "SELECT * FROM priv_a")
    check("授权前读表被拒", (not okc) and kind == "permission_denied", kind)

    run_dcl(svc, "my", "grant", {"privileges": ["SELECT"], "level": "database",
                                 "grantee": who, "database": "testdb", "host": "%"})
    okc, kind = probe_as("mysql", who, pw, "SELECT * FROM priv_a")
    check("GRANT 后读表成功", okc, kind)

    grants = svc.admin_db_user_grants("e2e", "my", who, CALLER, host="%")
    text_all = str(grants["groups"][0]["rows"])
    check("SHOW GRANTS 里能看到这条授权", "SELECT" in text_all and "testdb" in text_all)

    matrix = svc.admin_privilege_matrix("e2e", "my", "testdb", CALLER)
    check("权限矩阵含库级授权", any(who in str(r) for r in matrix["rows"]))

    run_dcl(svc, "my", "revoke", {"privileges": ["SELECT"], "level": "database",
                                  "grantee": who, "database": "testdb", "host": "%"})
    okc, kind = probe_as("mysql", who, pw, "SELECT * FROM priv_a")
    check("REVOKE 后读表又被拒", (not okc) and kind == "permission_denied", kind)

    run_dcl(svc, "my", "drop_user", {"name": who, "host": "%"})
    users3 = svc.admin_list_db_users("e2e", "my", CALLER)
    check("删除后账号消失", not any(r[ni] == who for r in users3["rows"]))
    seed("mysql", ["DROP TABLE IF EXISTS priv_a"])
    return pw


def test_audit_has_no_password(svc: DbmService, secrets: list[str]) -> None:
    print("\n=== 审计（红线 2：密码不落审计）===")
    rows = svc.store.recent(limit=500)
    dcl = [r for r in rows if r.get("tool") == "admin_dcl"]
    check("权限变更都落了审计", len(dcl) >= 6, f"{len(dcl)} 条")
    blob = " ".join(str(r) for r in rows)
    check("审计里没有任何明文密码", all(s not in blob for s in secrets))
    creates = [r for r in dcl if "CREATE" in (r.get("sql") or "")]
    check("CREATE 语句在审计里是脱敏版",
          bool(creates) and all("***" in r["sql"] for r in creates),
          creates[0]["sql"] if creates else "")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        svc = make_service(Path(td))
        secrets = []
        try:
            secrets.append(test_postgres(svc))
            secrets.append(test_mysql(svc))
            test_audit_has_no_password(svc, secrets)
        finally:
            svc.close()
    print("\n" + ("\033[32m全部通过\033[0m" if ok_all else "\033[31m有失败项\033[0m"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
