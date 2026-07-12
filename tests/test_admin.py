"""管理后台端到端测试：用 Starlette TestClient 打真实 HTTP 路由，跑通审批闭环。"""

import sqlite3

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")
TOKEN = "test-admin-token"


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, active INTEGER DEFAULT 1);"
        "INSERT INTO users (name) VALUES ('alice'), ('bob');"
    )
    conn.commit()
    conn.close()

    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
    from dbmcp.snippets import SnippetStore
    svc.snippets = SnippetStore(tmp_path / "a.sqlite3")
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token=TOKEN)
    app = mcp.http_app()
    with TestClient(app) as tc:
        # 登录拿 cookie（TestClient 会话保留 cookie）
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc, svc
    svc.close()


def test_sql_import_rows(client):
    """数据导入：参数化批量 INSERT + 列校验 + 审计留痕。"""
    tc, svc = client
    r = tc.post("/admin/sql/import", data={
        "conn": "demo/main", "table": "users",
        "columns": '["name", "active"]',
        "rows": '[["frank", 1], ["grace", 0]]'})
    assert r.status_code == 200 and r.json()["inserted"] == 2
    out = svc.admin_run_sql("demo", "main", "SELECT count(*) FROM users", CALLER)
    assert out["rows"][0][0] == 4  # 原 2 + 导入 2
    # 列名不在表结构 → 拒绝（防注入面）
    r2 = tc.post("/admin/sql/import", data={
        "conn": "demo/main", "table": "users",
        "columns": '["name; DROP TABLE users --"]', "rows": '[["x"]]'})
    assert r2.status_code == 400 and "不存在" in r2.json()["error"]
    # 审计留痕
    recs = [x for x in svc.store.recent() if x["tool"] == "admin_import"]
    assert recs and "2 行" in recs[0]["sql"]


def test_expired_pending_not_in_badge(client):
    """过期的 pending 单不计入侧栏角标/顶部横幅（存储态仍是 pending，惰性过期）。"""
    tc, svc = client
    svc.execute("demo", "main", "DELETE FROM users WHERE id = 1", CALLER)
    assert "条数据变更待审批" in tc.get("/admin/audit").text
    # 把审批单改成已过期（时间格式与真实写入一致，带 UTC 时区）
    with svc.approvals._lock:
        svc.approvals._conn.execute(
            "UPDATE change_request SET expires_at = '2000-01-01T00:00:00+00:00'")
        svc.approvals._conn.commit()
    assert "条数据变更待审批" not in tc.get("/admin/audit").text


def test_approvals_list_page(client):
    tc, svc = client
    svc.execute("demo", "main", "DELETE FROM users WHERE id = 1", CALLER)
    resp = tc.get("/admin/approvals")
    assert resp.status_code == 200
    assert "待审批" in resp.text
    assert "DELETE FROM users" in resp.text


def test_detail_and_approve_flow(client):
    tc, svc = client
    r = svc.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
    cid = r["change_id"]

    # 详情页展示风险报告
    detail = tc.get(f"/admin/approvals/{cid}")
    assert detail.status_code == 200
    assert "风险报告" in detail.text
    assert "UPDATE users SET active = 0" in detail.text

    # 批准（表单 POST，303 重定向回详情）
    approve = tc.post(f"/admin/approvals/{cid}/approve",
                      data={"by": "ops@x", "note": "ok"}, follow_redirects=False)
    assert approve.status_code == 303
    assert svc.get_change(cid).status == "approved"

    # agent 带 change_id 重提 → 执行成功
    out = svc.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER, change_id=cid)
    assert out["status"] == "executed"


def test_reject_flow_returns_reason_to_agent(client):
    tc, svc = client
    r = svc.execute("demo", "main", "DELETE FROM users WHERE id = 2", CALLER)
    cid = r["change_id"]
    tc.post(f"/admin/approvals/{cid}/reject", data={"by": "ops@x", "note": "请软删除"})
    out = svc.execute("demo", "main", "DELETE FROM users WHERE id = 2", CALLER, change_id=cid)
    assert out["status"] == "rejected"
    assert "请软删除" in out["reason"]


def test_audit_page_and_filter(client):
    tc, svc = client
    svc.query("demo", "main", "SELECT 1", CALLER)
    svc.execute("demo", "main", "DELETE FROM users", CALLER)  # 生成一条 rejected
    all_page = tc.get("/admin/audit")
    assert all_page.status_code == 200
    assert "操作审计" in all_page.text
    rejected = tc.get("/admin/audit?status=rejected")
    assert "审批单" in rejected.text


def test_unknown_change_404(client):
    tc, _ = client
    assert tc.get("/admin/approvals/9999").status_code == 404


def test_index_redirects(client):
    tc, _ = client
    resp = tc.get("/admin", follow_redirects=False)
    assert resp.status_code in (307, 302, 303)
    assert "/admin/approvals" in resp.headers["location"]


class TestSqlConsole:
    """查询台：页面渲染 + 静态资源 + 元信息 + 读/写执行 + 导出。"""

    def test_page_renders_console_app(self, client):
        tc, _ = client
        r = tc.get("/admin/sql")
        assert r.status_code == 200
        assert "查询台" in r.text
        assert "/admin/static/console.js" in r.text
        assert "/admin/static/vue.global.prod.js" in r.text
        assert "/admin/static/monaco/vs/loader.js" in r.text

    def test_static_assets_served_without_auth(self, client):
        tc, _ = client
        for path in ("/admin/static/console.js", "/admin/static/vue.global.prod.js",
                     "/admin/static/monaco/vs/loader.js"):
            r = tc.get(path)
            assert r.status_code == 200, path
            assert "javascript" in r.headers["content-type"], path
        # 目录穿越防护：resolve 后越出静态根 → 404
        assert tc.get("/admin/static/%2e%2e/%2e%2e/pyproject.toml").status_code in (400, 404)
        assert tc.get("/admin/static/nope.js").status_code == 404

    def test_connections_endpoint(self, client):
        tc, _ = client
        d = tc.get("/admin/sql/connections").json()
        assert d["ok"] and any(c["value"] == "demo/main" for c in d["connections"])

    def test_databases_endpoint_sqlite_empty(self, client):
        tc, _ = client
        d = tc.get("/admin/sql/databases", params={"conn": "demo/main"}).json()
        assert d["ok"] and d["databases"] == []

    def test_ddl_endpoint(self, client):
        tc, _ = client
        d = tc.get("/admin/sql/ddl", params={"conn": "demo/main", "table": "users"}).json()
        assert d["ok"] and "CREATE TABLE" in d["ddl"] and "users" in d["ddl"]

    def test_tables_endpoint_includes_sizes(self, client):
        tc, _ = client
        d = tc.get("/admin/sql/tables", params={"conn": "demo/main"}).json()
        assert d["ok"] and "users" in d["tables"]
        # sizes 为 dict（sqlite 无 dbstat 支持时为空，不阻断）
        assert isinstance(d["sizes"], dict)

    def test_ddl_missing_table(self, client):
        tc, _ = client
        d = tc.get("/admin/sql/ddl", params={"conn": "demo/main", "table": "nope"}).json()
        assert not d["ok"] and "不存在" in d["error"]

    def test_tables_and_table_meta(self, client):
        tc, _ = client
        tbls = tc.get("/admin/sql/tables", params={"conn": "demo/main"}).json()
        assert tbls["ok"] and "users" in tbls["tables"]
        meta = tc.get("/admin/sql/table", params={"conn": "demo/main", "table": "users"}).json()
        assert meta["ok"]
        assert [c["name"] for c in meta["columns"]] == ["id", "name", "active"]

    def test_run_read_returns_rows(self, client):
        tc, _ = client
        d = tc.post("/admin/sql/run",
                    data={"conn": "demo/main", "sql": "SELECT id, name FROM users ORDER BY id"}).json()
        assert d["ok"] and d["kind"] == "read"
        assert d["columns"] == ["id", "name"]
        assert len(d["rows"]) == 2

    def test_run_write_confirm_flow(self, client):
        tc, svc = client
        # 未确认 → 风险报告，不执行
        d1 = tc.post("/admin/sql/run",
                     data={"conn": "demo/main", "sql": "DELETE FROM users WHERE id=1"}).json()
        assert d1["ok"] and d1["kind"] == "confirm" and "risk" in d1
        assert svc.query("demo", "main", "SELECT count(*) AS c FROM users", CALLER)["rows"][0][0] == 2
        # 确认 → writer 直接执行
        d2 = tc.post("/admin/sql/run",
                     data={"conn": "demo/main", "sql": "DELETE FROM users WHERE id=1", "confirm": "1"}).json()
        assert d2["ok"] and d2["kind"] == "write" and d2["affected_rows"] == 1
        assert svc.query("demo", "main", "SELECT count(*) AS c FROM users", CALLER)["rows"][0][0] == 1

    def test_format_endpoint(self, client):
        tc, _ = client
        d = tc.post("/admin/sql/format",
                    data={"conn": "demo/main", "sql": "select 1 from users"}).json()
        assert d["ok"] and "SELECT" in d["sql"]

    def test_export_csv_download(self, client):
        tc, _ = client
        r = tc.post("/admin/sql/export",
                    data={"conn": "demo/main", "sql": "SELECT id, name FROM users", "format": "csv"})
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert r.content.startswith(b"\xef\xbb\xbf")
        assert b"id,name" in r.content

    def test_export_rejects_write(self, client):
        tc, _ = client
        r = tc.post("/admin/sql/export",
                    data={"conn": "demo/main", "sql": "DELETE FROM users", "format": "csv"})
        assert r.status_code == 400
        assert not r.json()["ok"]

    def test_sql_page_requires_auth_static_public(self, client):
        tc, _ = client
        tc.get("/admin/logout")
        # 页面需鉴权
        assert tc.get("/admin/sql", follow_redirects=False).status_code == 303
        assert tc.get("/admin/sql/connections", follow_redirects=False).status_code == 303
        # 静态资源公开（Monaco worker 用 data-URI importScripts，带 cookie 会 303 崩）
        assert tc.get("/admin/static/console.js").status_code == 200


class TestSnippets:
    """SQL 片段库：保存 / 列表 / 更新 / 删除。"""

    def test_save_list_update_delete(self, client):
        tc, _ = client
        # 保存
        r = tc.post("/admin/sql/snippets/save", data={
            "title": "日活", "note": "每日", "sql": "SELECT count(*) FROM users",
            "connection": "demo/main"}).json()
        assert r["ok"] and r["snippet"]["id"] > 0
        sid = r["snippet"]["id"]
        # 列表
        lst = tc.get("/admin/sql/snippets").json()
        assert lst["ok"] and any(s["id"] == sid for s in lst["snippets"])
        # 更新标题/备注
        u = tc.post("/admin/sql/snippets/save", data={
            "id": sid, "title": "日活V2", "note": "改了", "sql": "SELECT 1"}).json()
        assert u["ok"] and u["snippet"]["title"] == "日活V2"
        # 删除
        d = tc.post("/admin/sql/snippets/delete", data={"id": sid}).json()
        assert d["ok"]
        assert not any(s["id"] == sid for s in tc.get("/admin/sql/snippets").json()["snippets"])

    def test_save_requires_title(self, client):
        tc, _ = client
        r = tc.post("/admin/sql/snippets/save", data={"title": "", "sql": "SELECT 1"})
        assert r.status_code == 400 and not r.json()["ok"]

    def test_delete_missing_returns_error(self, client):
        tc, _ = client
        r = tc.post("/admin/sql/snippets/delete", data={"id": "99999"})
        assert r.status_code == 400 and not r.json()["ok"]

    def test_snippet_routes_require_auth(self, client):
        tc, _ = client
        tc.get("/admin/logout")
        assert tc.get("/admin/sql/snippets", follow_redirects=False).status_code == 303


class TestAuth:
    def _fresh_app(self, tmp_path):
        import sqlite3
        db = tmp_path / "biz.sqlite3"
        c = sqlite3.connect(db); c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)"); c.commit(); c.close()
        cfg = AppConfig.model_validate(
            {"projects": {"demo": {"connections": {"main": {
                "engine": "sqlite", "database": str(db), "environment": "dev"}}}}}
        )
        svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
        mcp = build_mcp(svc)
        mount_admin(mcp, svc, admin_token=TOKEN)
        return svc, mcp

    def test_unauthenticated_redirects_to_login(self, tmp_path):
        svc, mcp = self._fresh_app(tmp_path)
        with TestClient(mcp.http_app()) as tc:
            for path in ("/admin/approvals", "/admin/audit", "/admin"):
                r = tc.get(path, follow_redirects=False)
                assert r.status_code == 303, path
                assert r.headers["location"] == "/admin/login"
        svc.close()

    def test_wrong_token_rejected(self, tmp_path):
        svc, mcp = self._fresh_app(tmp_path)
        with TestClient(mcp.http_app()) as tc:
            r = tc.post("/admin/login", data={"token": "wrong"})
            assert r.status_code == 401
            # 没拿到 cookie，仍然被挡
            assert tc.get("/admin/approvals", follow_redirects=False).status_code == 303
        svc.close()

    def test_login_then_access_then_logout(self, tmp_path):
        svc, mcp = self._fresh_app(tmp_path)
        with TestClient(mcp.http_app()) as tc:
            tc.post("/admin/login", data={"token": TOKEN})
            assert tc.get("/admin/approvals").status_code == 200
            tc.get("/admin/logout")
            assert tc.get("/admin/approvals", follow_redirects=False).status_code == 303
        svc.close()

    def test_login_page_accessible_without_auth(self, tmp_path):
        svc, mcp = self._fresh_app(tmp_path)
        with TestClient(mcp.http_app()) as tc:
            r = tc.get("/admin/login")
            assert r.status_code == 200
            assert "管理 token" in r.text
        svc.close()


class TestConnectionAdminUI:
    def _app(self, tmp_path, monkeypatch):
        # 内存 keyring
        import sys, types
        store = {}
        mod = types.ModuleType("keyring"); errmod = types.ModuleType("keyring.errors")
        errmod.PasswordDeleteError = type("E", (Exception,), {})
        mod.errors = errmod
        mod.set_password = lambda s, a, v: store.__setitem__((s, a), v)
        mod.get_password = lambda s, a: store.get((s, a))
        mod.delete_password = lambda s, a: store.pop((s, a), None)
        monkeypatch.setitem(sys.modules, "keyring", mod)
        monkeypatch.setitem(sys.modules, "keyring.errors", errmod)

        cfg_path = tmp_path / "conn.yaml"
        cfg_path.write_text("projects: {}\n")
        cfg = AppConfig.model_validate({"projects": {}})
        svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                         ApprovalStore(tmp_path / "a.sqlite3"), config_path=str(cfg_path))
        mcp = build_mcp(svc)
        mount_admin(mcp, svc, admin_token=TOKEN)
        return svc, mcp, cfg_path, store

    def test_create_connection_via_form(self, tmp_path, monkeypatch):
        svc, mcp, cfg_path, store = self._app(tmp_path, monkeypatch)
        with TestClient(mcp.http_app()) as tc:
            tc.post("/admin/login", data={"token": TOKEN})
            r = tc.post("/admin/connections/save", data={
                "project": "local", "connection": "db1", "engine": "mysql",
                "environment": "dev", "host": "127.0.0.1", "port": "3306",
                "database": "app", "user": "root", "password": "secret123",
                "max_rows": "500", "jump_hosts": "", "ssh_options_extra": "",
                "force_privileged": "1",  # 跳过真连探测（本测试验证写回/keyring，非权限门）
            }, follow_redirects=False)
            assert r.status_code == 303
            # 落库、密码进 keyring、文件无明文
            assert svc.config.get_connection("local", "db1").host == "127.0.0.1"
            assert "secret123" in store.values()
            assert "secret123" not in cfg_path.read_text()
            # 列表页可见
            page = tc.get("/admin/connections")
            assert "local/db1" in page.text
        svc.close()

    def test_delete_connection_via_form(self, tmp_path, monkeypatch):
        svc, mcp, cfg_path, store = self._app(tmp_path, monkeypatch)
        with TestClient(mcp.http_app()) as tc:
            tc.post("/admin/login", data={"token": TOKEN})
            tc.post("/admin/connections/save", data={
                "project": "local", "connection": "db1", "engine": "sqlite",
                "environment": "local", "database": "/tmp/x.db", "max_rows": "10",
            })
            tc.post("/admin/connections/delete", data={"project": "local", "connection": "db1"})
            assert "local" not in svc.config.projects
        svc.close()

    def test_bad_config_shows_error(self, tmp_path, monkeypatch):
        svc, mcp, cfg_path, store = self._app(tmp_path, monkeypatch)
        with TestClient(mcp.http_app()) as tc:
            tc.post("/admin/login", data={"token": TOKEN})
            r = tc.post("/admin/connections/save", data={
                "project": "local", "connection": "db1", "engine": "mysql",
                "environment": "dev", "max_rows": "500",  # mysql 缺 host
            })
            assert r.status_code == 400
            assert "失败" in r.text
        svc.close()
