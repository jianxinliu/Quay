"""权限管理的 HTTP 层：连接过滤、两段式确认、prod 闸门、指纹绑定、密码不外发。

这里刻意用**连不上的** PostgreSQL 连接配置——预览、prod 闸门、指纹校验三道关卡
全部发生在真正建连之前，所以不需要真库就能把闸门逻辑测严实；真正的执行路径由
scripts/e2e_privileges.py 对真实 PG / MySQL 跑。
"""

import sqlite3

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import DbmService

TOKEN = "test-admin-token"


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    sqlite3.connect(db_file).close()
    cfg = AppConfig.model_validate({"projects": {
        # sqlite 不支持权限管理，必须被连接列表过滤掉
        "demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev"}}},
        "pg": {"connections": {
            "dev1": {"engine": "postgres", "host": "127.0.0.1", "port": 1,
                     "database": "app", "user": "ro", "password": "plain://x",
                     "environment": "dev",
                     "writer": {"user": "root", "password": "plain://y"}},
            "prod1": {"engine": "postgres", "host": "127.0.0.1", "port": 1,
                      "database": "app", "user": "ro", "password": "plain://x",
                      "environment": "prod",
                      "writer": {"user": "root", "password": "plain://y"}},
            "noadmin": {"engine": "postgres", "host": "127.0.0.1", "port": 1,
                        "database": "app", "user": "ro", "password": "plain://x",
                        "environment": "dev"},
        }},
        "my": {"connections": {"m1": {
            "engine": "mysql", "host": "127.0.0.1", "port": 1, "database": "shop",
            "user": "ro", "password": "plain://x", "environment": "dev",
            "writer": {"user": "root", "password": "plain://y"}}}},
    }})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
    svc.data_dir = str(tmp_path / "data")
    svc.base_url = "http://testserver"
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token=TOKEN)
    with TestClient(mcp.http_app()) as tc:
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc, svc
    svc.close()


def _run(tc, **body):
    return tc.post("/admin/privileges/run", json=body).json()


class TestPlacement:
    """权限管理是**查询台里的弹窗**，不是独立页面——账号与授权是某个库自己的功能，
    跟着当前连接走；挂成一级导航的话人得换页面再重新选一遍连接，上下文就丢了。"""

    def test_console_loads_the_panel_component(self, client):
        tc, _ = client
        html = tc.get("/admin/sql").text
        assert "/admin/static/privileges.js" in html
        assert "/admin/static/privileges.css" in html

    def test_no_standalone_page(self, client):
        tc, _ = client
        assert tc.get("/admin/privileges").status_code == 404

    def test_no_top_level_nav_entry(self, client):
        tc, _ = client
        assert 'href="/admin/privileges"' not in tc.get("/admin/audit").text

    def test_data_routes_require_auth(self, client):
        tc, _ = client
        tc.get("/admin/logout")
        r = tc.get("/admin/privileges/users?conn=pg/dev1", follow_redirects=False)
        assert r.status_code in (302, 303, 307)


class TestConnectionListing:
    def test_only_supported_engines(self, client):
        tc, _ = client
        d = tc.get("/admin/privileges/connections").json()
        assert d["ok"]
        values = [c["value"] for c in d["connections"]]
        assert "demo/main" not in values          # sqlite 不支持
        assert set(values) == {"my/m1", "pg/dev1", "pg/noadmin", "pg/prod1"}

    def test_reports_writer_presence_and_admin_user(self, client):
        tc, _ = client
        conns = {c["value"]: c for c in tc.get("/admin/privileges/connections").json()["connections"]}
        assert conns["pg/dev1"]["has_writer"] and conns["pg/dev1"]["admin_user"] == "root"
        # 没配 writer 的连接要能在页面上被看出来（只能看、不能改）
        assert conns["pg/noadmin"]["has_writer"] is False
        assert conns["pg/noadmin"]["admin_user"] == "ro"


class TestPreviewIsNotExecution:
    """第一段只回语句，绝不碰数据库——连接指向 127.0.0.1:1（必定连不上）仍应成功返回。"""

    def test_grant_preview(self, client):
        tc, _ = client
        d = _run(tc, conn="pg/dev1", action="grant",
                 params={"privileges": ["SELECT"], "level": "all_tables",
                         "grantee": "drama", "schema": "public"})
        assert d["ok"] and d["kind"] == "confirm"
        assert d["sql"] == 'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO "drama"'
        assert d["prod"] is False and d["fingerprint"]

    def test_mysql_preview_uses_account_form(self, client):
        tc, _ = client
        d = _run(tc, conn="my/m1", action="grant",
                 params={"privileges": ["SELECT"], "level": "database",
                         "grantee": "app", "database": "shop", "host": "10.0.%"})
        assert d["sql"] == "GRANT SELECT ON `shop`.* TO 'app'@'10.0.%'"

    def test_password_never_in_response(self, client):
        tc, _ = client
        d = _run(tc, conn="pg/dev1", action="create_user",
                 params={"name": "app", "password": "hunter2"})
        assert "hunter2" not in str(d)
        assert d["sql"] == 'CREATE ROLE "app" WITH LOGIN PASSWORD ***'
        assert d["has_secret"] is True

    def test_unsupported_engine_rejected(self, client):
        tc, _ = client
        d = _run(tc, conn="demo/main", action="create_user",
                 params={"name": "a", "password": "b"})
        assert not d["ok"] and "sqlite" in d["error"]

    def test_injection_in_name_rejected(self, client):
        tc, _ = client
        d = _run(tc, conn="pg/dev1", action="drop_user",
                 params={"name": 'x"; DROP ROLE root; --'})
        assert not d["ok"] and "不允许的字符" in d["error"]

    def test_injection_in_privilege_rejected(self, client):
        tc, _ = client
        d = _run(tc, conn="pg/dev1", action="grant",
                 params={"privileges": ["SELECT; DROP ROLE root"], "level": "all_tables",
                         "grantee": "app", "schema": "public"})
        assert not d["ok"] and "白名单" in d["error"]

    def test_unknown_action_rejected(self, client):
        tc, _ = client
        d = _run(tc, conn="pg/dev1", action="make_superuser", params={"name": "app"})
        assert not d["ok"] and "未知的权限操作" in d["error"]


class TestGates:
    """三道闸门都在建连之前生效——被拒时连接不上的库也不会被触碰。"""

    def test_prod_requires_connection_name(self, client):
        tc, _ = client
        params = {"privileges": ["SELECT"], "level": "all_tables",
                  "grantee": "app", "schema": "public"}
        card = _run(tc, conn="pg/prod1", action="grant", params=params)
        assert card["prod"] is True and card["expect_text"] == "prod1"
        # 不输连接名 → 拒
        d = _run(tc, conn="pg/prod1", action="grant", params=params, confirm="1",
                 expect_fingerprint=card["fingerprint"])
        assert not d["ok"] and "输入连接名" in d["error"]
        # 输错 → 拒
        d2 = _run(tc, conn="pg/prod1", action="grant", params=params, confirm="1",
                  confirm_text="prod", expect_fingerprint=card["fingerprint"])
        assert not d2["ok"] and "输入连接名" in d2["error"]

    def test_dev_needs_no_connection_name(self, client):
        """非生产不设连接名闸门——过了闸门才会去建连，所以这里应该是「连不上」而不是「被拒」。"""
        tc, _ = client
        card = _run(tc, conn="pg/dev1", action="grant",
                    params={"privileges": ["SELECT"], "level": "all_tables",
                            "grantee": "app", "schema": "public"})
        d = _run(tc, conn="pg/dev1", action="grant",
                 params={"privileges": ["SELECT"], "level": "all_tables",
                         "grantee": "app", "schema": "public"},
                 confirm="1", expect_fingerprint=card["fingerprint"])
        assert not d["ok"] and "输入连接名" not in d["error"]

    def test_fingerprint_binding_blocks_switched_statement(self, client):
        """H1：确认卡片上看到的是 SELECT，提交时参数被换成 ALL——指纹对不上必须拒。"""
        tc, _ = client
        card = _run(tc, conn="pg/dev1", action="grant",
                    params={"privileges": ["SELECT"], "level": "all_tables",
                            "grantee": "app", "schema": "public"})
        d = _run(tc, conn="pg/dev1", action="grant",
                 params={"privileges": ["ALL"], "level": "all_tables",
                         "grantee": "app", "schema": "public"},
                 confirm="1", expect_fingerprint=card["fingerprint"])
        assert not d["ok"] and "指纹不一致" in d["error"]

    def test_missing_writer_account_rejected(self, client):
        tc, _ = client
        card = _run(tc, conn="pg/noadmin", action="grant",
                    params={"privileges": ["SELECT"], "level": "all_tables",
                            "grantee": "app", "schema": "public"})
        d = _run(tc, conn="pg/noadmin", action="grant",
                 params={"privileges": ["SELECT"], "level": "all_tables",
                         "grantee": "app", "schema": "public"},
                 confirm="1", expect_fingerprint=card["fingerprint"])
        assert not d["ok"] and "writer" in d["error"]


class TestAuditTaxonomy:
    def test_dcl_counts_as_write_in_audit_filter(self):
        """审计页的读/写过滤要把权限变更算作写操作。"""
        from dbmcp.audit.log import _WRITE_TOOLS
        assert "admin_dcl" in _WRITE_TOOLS
