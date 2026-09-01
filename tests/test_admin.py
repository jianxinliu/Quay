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
    svc.data_dir = str(tmp_path / "data")
    svc.base_url = "http://testserver"
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


def test_foreign_host_blocked_at_guard(client):
    """C2：非本机 Host（DNS rebinding）在 guard 层被 403 拦下，先于认证。"""
    tc, _ = client
    # 已登录、但伪造攻击者 Host → 403（Host 校验先于 auth）
    r = tc.get("/admin/approvals", headers={"host": "attacker.example.com"})
    assert r.status_code == 403
    # 跨站 Origin 的写请求同样被拦
    w = tc.post("/admin/sql/run", data={"conn": "demo/main", "sql": "SELECT 1"},
                headers={"origin": "http://evil.com"})
    assert w.status_code == 403
    # 正常本机请求（conftest 允许 testserver）仍放行
    assert tc.get("/admin/approvals").status_code == 200


def test_sql_reconnect_recovers_exhausted(client):
    """查询台「重连数据库」：连接被判 exhausted 后，HTTP 路由强制重建使其恢复可用。"""
    tc, svc = client
    from dbmcp.health import Health

    svc.health._entries[("demo", "main")] = Health(state="exhausted", fail_count=5,
                                                    last_error="lost connection")
    r = tc.post("/admin/sql/reconnect", data={"conn": "demo/main"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["engine"] == "sqlite"
    assert svc.health.get("demo", "main") is None


def test_sql_reconnect_bad_conn_returns_error(client):
    tc, _ = client
    r = tc.post("/admin/sql/reconnect", data={"conn": "demo/nope"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_no_auth_mode_skips_login(tmp_path):
    """--no-auth：跳过认证，本机测试脚手架用；Host 校验仍在。"""
    import sqlite3
    from dbmcp.admin import mount_admin
    from dbmcp.approvals import ApprovalStore
    from dbmcp.audit.log import AuditStore
    from dbmcp.config import AppConfig
    from dbmcp.server import build_mcp
    from dbmcp.service import DbmService
    from dbmcp.snippets import SnippetStore

    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript("CREATE TABLE t (id INTEGER);")
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(db_file), "environment": "dev",
    }}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     ApprovalStore(tmp_path / "a.sqlite3"))
    svc.snippets = SnippetStore(tmp_path / "a.sqlite3")
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token="unused", no_auth=True)
    with TestClient(mcp.http_app()) as tc:
        # 未登录直接访问受保护路由 → 200（跳过 auth）
        assert tc.get("/admin/approvals").status_code == 200
        # 铃铛 API 未登录也直接返回 JSON
        assert tc.get("/admin/notifications/unread_count").json()["ok"] is True
        # 但外来 Host 仍被 403（Host 校验先于 auth 检查）
        assert tc.get("/admin/approvals",
                      headers={"host": "attacker.example.com"}).status_code == 403
    svc.close()


def test_search_tables_and_lint(client):
    """全局表搜索（sqlite sqlite_master LIKE）+ sqlglot 语法检查接口。"""
    tc, svc = client
    r = tc.get("/admin/sql/search_tables?conn=demo/main&q=use")
    assert r.status_code == 200
    assert any(x["table"] == "users" for x in r.json()["results"])
    assert tc.get("/admin/sql/search_tables?conn=demo/main&q=").json()["results"] == []
    # lint：合法 SQL 无错；语法错误返回行列；词法错误（引号不闭合）也有定位
    ok = tc.post("/admin/sql/lint", data={"sql": "SELECT 1 FROM users", "dialect": "sqlite"})
    assert ok.json()["errors"] == []
    bad = tc.post("/admin/sql/lint", data={"sql": "SELEC 1 FRM users", "dialect": "mysql"}).json()
    assert bad["errors"] and bad["errors"][0]["line"] == 1
    tok = tc.post("/admin/sql/lint", data={"sql": "SELECT 'abc FROM t", "dialect": "mysql"}).json()
    assert tok["errors"] and tok["errors"][0]["message"]


def test_lint_blank_line_separated_statements(client):
    """空行分隔的多条 SQL（无分号）不应误报——每块独立解析，合法块不标错。
    修的坑：sqlglot 只认分号，整体解析会把两条当成一条、在下一条起始处误报红波浪线。"""
    tc, _ = client
    # 两条合法 SELECT 用空行隔开、第一条无分号 → 不应有任何错误
    two = "SELECT max(id) FROM users\n\nSELECT 1"
    assert tc.post("/admin/sql/lint", data={"sql": two, "dialect": "mysql"}).json()["errors"] == []
    # 真正的错误落在出错的那一块（第 3 行），而不是别处
    bad = "SELECT 1 FROM users\n\nSELEC bad syntax here"
    errs = tc.post("/admin/sql/lint", data={"sql": bad, "dialect": "mysql"}).json()["errors"]
    assert errs and errs[0]["line"] == 3


def test_lint_mysql_drop_partition_no_paren(client):
    """回归：MySQL 无括号 DROP PARTITION（正确语法）不应被编辑器标红。
    sqlglot 只认带括号，归一化后 lint 须返回空错误列表。"""
    tc, _ = client
    sql = "ALTER TABLE ad_event DROP PARTITION p20260702, p20260703, p20260704"
    assert tc.post("/admin/sql/lint", data={"sql": sql, "dialect": "mysql"}).json()["errors"] == []


def test_settings_info_tab(client):
    """系统信息 tab：展示路径/运行时/token 指引；token 明文绝不出现在页面。"""
    tc, svc = client
    r = tc.get("/admin/settings?tab=info")
    assert r.status_code == 200
    body = r.text
    for s in ("系统信息", "SQLite 库", "keyring 服务名", "登录 Token", "DBM_ADMIN_TOKEN"):
        assert s in body
    assert TOKEN not in body  # 安全：不泄露登录 token 明文


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


def test_sql_run_async_job_fields_and_cancel(client):
    """异步查询：run_async 返回 job_id；job 轮询带排队位置/计时字段；cancel 端点可用。

    串行/取消的完整行为由 test_jobs.py 单测覆盖，这里验证 HTTP 层接线正确。
    """
    import time as _t

    tc, svc = client
    r = tc.post("/admin/sql/run_async", data={"conn": "demo/main", "sql": "SELECT * FROM users"})
    assert r.status_code == 200 and r.json()["ok"]
    job_id = r.json()["job_id"]

    payload = None
    for _ in range(200):
        payload = tc.get(f"/admin/sql/job?id={job_id}").json()
        assert "elapsed_ms" in payload
        if payload["status"] in ("done", "error", "canceled"):
            break
        _t.sleep(0.01)
    assert payload["status"] == "done"
    assert payload["result"]["rows"]

    # cancel：未知/已结束的任务返回 ok=False（不抛错）
    assert tc.post("/admin/sql/cancel", data={"id": job_id}).json()["ok"] is False
    assert tc.post("/admin/sql/cancel", data={"id": "nope"}).json()["ok"] is False

    # 过期/丢失的 job 轮询给出友好提示
    assert tc.get("/admin/sql/job?id=missing").json()["ok"] is False


def test_approvals_list_page(client):
    tc, svc = client
    svc.execute("demo", "main", "DELETE FROM users WHERE id = 1", CALLER)
    resp = tc.get("/admin/approvals")
    assert resp.status_code == 200
    assert "待审批" in resp.text
    assert "DELETE FROM users" in resp.text


def test_temporary_exports_page_download_and_delete(client):
    tc, svc = client
    item = svc._save_mcp_export(
        b"\xef\xbb\xbfid,name\r\n1,alice\r\n",
        "text/csv; charset=utf-8",
        "csv",
        {
            "project": "demo", "connection": "main", "database": None,
            "table": "users", "fields": ["id", "name"], "row_count": 1,
            "requested_limit": 1, "truncated": False, "format": "csv",
            "masked_columns": [],
        },
    )

    page = tc.get("/admin/exports")
    assert page.status_code == 200
    assert item["filename"] in page.text
    assert "临时导出" in page.text

    preview = tc.get(f"/admin/exports/{item['token']}/preview")
    assert preview.status_code == 200
    assert "alice" in preview.text

    download = tc.get(item["download_url"])
    assert download.status_code == 200
    assert download.content.startswith(b"\xef\xbb\xbfid,name")

    deleted = tc.post(
        "/admin/exports/delete",
        data={"token": item["token"]},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert svc.resolve_mcp_export(item["token"], item["filename"]) is None


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


def test_detail_renders_explain_with_column_headers(client):
    """执行计划要带表头：只有一堆值，审批人不知道每列是什么。"""
    tc, svc = client
    cid = svc.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)["change_id"]
    detail = tc.get(f"/admin/approvals/{cid}").text
    assert "执行计划（EXPLAIN）" in detail
    assert "<th>opcode</th>" in detail          # sqlite EXPLAIN 的列名
    assert "<th>addr</th>" in detail


def test_explain_html_falls_back_to_pre_for_legacy_text_plan():
    """老审批单里存的是无列名的纯文本计划，仍原样展示（不误把首行当表头）。"""
    from dbmcp.admin import _explain_html
    out = _explain_html({"explain": "1 | SIMPLE | users | ALL"})
    assert "<pre>" in out and "SIMPLE | users" in out
    assert "<th>" not in out


def test_explain_html_translates_no_info_plan():
    """MySQL 对点查更新的无信息量输出转成人话（新旧两种格式都认）。"""
    from dbmcp.admin import _explain_html
    msg = "Plan not executable by iterator executor"
    for risk in ({"explain": msg},
                 {"explain": {"columns": ["EXPLAIN"], "rows": [[msg]]}}):
        assert "优化器无需生成可展示的查询计划" in _explain_html(risk)


def test_approve_and_execute_button_lands_the_change(client):
    """后台「批准并立即执行」：人点一次就落地，agent 只需从等待中收结果。"""
    tc, svc = client
    cid = svc.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)["change_id"]

    # 详情页两个按钮都在
    detail = tc.get(f"/admin/approvals/{cid}")
    assert "批准并立即执行" in detail.text and "仅批准" in detail.text

    resp = tc.post(f"/admin/approvals/{cid}/approve",
                   data={"by": "ops@x", "note": "ok", "exec": "1"}, follow_redirects=False)
    assert resp.status_code == 303
    change = svc.get_change(cid)
    assert change.status == "consumed"                    # 已核销，agent 不必也不能再重提
    assert change.exec_result["affected_rows"] == 1
    with sqlite3.connect(svc.config.get_connection("demo", "main").database) as conn:
        assert conn.execute("SELECT active FROM users WHERE id = 1").fetchone()[0] == 0

    # 决策卡片回显执行结果
    assert "影响 1 行" in tc.get(f"/admin/approvals/{cid}").text


def test_plain_approve_leaves_execution_to_agent(client):
    """「仅批准」按钮（不带 exec）保持原语义：不执行，等 agent 重提。"""
    tc, svc = client
    cid = svc.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)["change_id"]
    tc.post(f"/admin/approvals/{cid}/approve", data={"by": "ops@x", "note": "ok"})
    assert svc.get_change(cid).status == "approved"
    with sqlite3.connect(svc.config.get_connection("demo", "main").database) as conn:
        assert conn.execute("SELECT active FROM users WHERE id = 1").fetchone()[0] == 1


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


class TestSshIdentitiesAndHops:
    """SSH 证书库 + 结构化跳板：后台渲染与 HTTP 增删改闭环（用带 config_path 的实例）。"""

    @pytest.fixture
    def ssh_client(self, tmp_path):
        key = tmp_path / "prod_key"
        key.write_text("KEY")
        key.chmod(0o600)
        cfg_path = tmp_path / "conn.yaml"
        cfg_path.write_text("projects: {}\n", encoding="utf-8")
        from dbmcp.config import load_config
        svc = DbmService(load_config(cfg_path), AuditStore(tmp_path / "a.sqlite3"),
                         ApprovalStore(tmp_path / "a.sqlite3"), config_path=str(cfg_path))
        mcp = build_mcp(svc)
        mount_admin(mcp, svc, admin_token=TOKEN)
        with TestClient(mcp.http_app()) as tc:
            tc.post("/admin/login", data={"token": TOKEN})
            yield tc, svc, str(key), cfg_path
        svc.close()

    def test_identity_crud_and_reference(self, ssh_client):
        tc, svc, key, cfg_path = ssh_client
        # 建证书
        r = tc.post("/admin/ssh-identities/save",
                    data={"name": "prod-bastion", "key_path": key}, follow_redirects=False)
        assert r.status_code == 303
        assert "prod-bastion" in svc.config.ssh_identities
        # SSH 配置 tab 展示它
        page = tc.get("/admin/settings?tab=ssh")
        assert "prod-bastion" in page.text and "SSH 配置库" in page.text
        # 建一个多跳连接：第一跳引用证书，第二跳内联 key
        # httpx 用「列表值」表示重复表单键 → 平行数组按行对齐
        r = tc.post("/admin/connections/save", data={
            "project": "local", "connection": "db1", "engine": "mysql",
            "environment": "dev", "host": "h", "port": "3306", "database": "d",
            "user": "u", "password": "p", "force_privileged": "1",
            "hop_host": ["b1", "b2"], "hop_user": ["alice", ""], "hop_port": ["", "2222"],
            "hop_identity": ["prod-bastion", ""], "hop_key_path": ["", key],
            "ssh_options_extra": "", "max_rows": "500", "mask_columns": "",
        }, follow_redirects=False)
        assert r.status_code == 303, r.text
        hops = svc.config.get_connection("local", "db1").jump_hosts
        assert [h.label() for h in hops] == ["alice@b1", "b2:2222"]
        assert hops[0].identity == "prod-bastion" and hops[1].key_path == key
        # 编辑表单回填跳板行（host 值出现在表单）
        form = tc.get("/admin/settings?tab=connections&edit=local/db1")
        assert "value='b1'" in form.text and "value='b2'" in form.text
        # 被引用的证书拒删
        r = tc.post("/admin/ssh-identities/delete",
                    data={"name": "prod-bastion"}, follow_redirects=False)
        assert r.status_code == 400 and "引用" in r.text

    def test_full_ssh_config_and_identity_only_hop(self, ssh_client):
        """#4：SSH 配置带 host/user/port；跳板仅引用配置（不写 host）时继承这些字段。"""
        tc, svc, key, cfg_path = ssh_client
        # 存一条完整 SSH 配置（含主机/用户/端口）
        r = tc.post("/admin/ssh-identities/save", data={
            "name": "bastion-full", "key_path": key,
            "host": "jump.example.com", "user": "ops", "port": "2200",
        }, follow_redirects=False)
        assert r.status_code == 303
        ident = svc.config.ssh_identities["bastion-full"]
        assert ident.host == "jump.example.com" and ident.user == "ops" and ident.port == 2200
        # 建连接：跳板只引用配置、host/user/port 全留空
        r = tc.post("/admin/connections/save", data={
            "project": "local", "connection": "db2", "engine": "mysql",
            "environment": "dev", "host": "h", "port": "3306", "database": "d",
            "user": "u", "password": "p", "force_privileged": "1",
            "hop_host": [""], "hop_user": [""], "hop_port": [""],
            "hop_identity": ["bastion-full"], "hop_key_path": [""],
            "ssh_options_extra": "", "max_rows": "500", "mask_columns": "",
        }, follow_redirects=False)
        assert r.status_code == 303, r.text
        hops = svc.config.get_connection("local", "db2").jump_hosts
        assert len(hops) == 1 and hops[0].identity == "bastion-full" and not hops[0].host
        # 解析后从配置继承 host/user/port
        from dbmcp.tunnel import resolve_jump_hosts
        resolved = resolve_jump_hosts(hops, svc.config.ssh_identities)
        assert resolved[0].host == "jump.example.com"
        assert resolved[0].user == "ops" and resolved[0].port == 2200

    def test_identity_delete_when_unreferenced(self, ssh_client):
        tc, svc, key, cfg_path = ssh_client
        tc.post("/admin/ssh-identities/save", data={"name": "id1", "key_path": key})
        r = tc.post("/admin/ssh-identities/delete", data={"name": "id1"}, follow_redirects=False)
        assert r.status_code == 303 and "id1" not in svc.config.ssh_identities

    def test_bad_key_path_shows_error(self, ssh_client):
        tc, svc, key, cfg_path = ssh_client
        r = tc.post("/admin/ssh-identities/save",
                    data={"name": "x", "key_path": "/no/such/key"}, follow_redirects=False)
        assert r.status_code == 400 and "不存在" in r.text


class TestPostgresMultiDatabase:
    """查询台的 db= 参数要一路透传到引擎池。PG 一条连接只绑一个 database，
    「换库」只能换连接——漏传一处，就会出现「树里选了 shop、SQL 却在 testdb 上跑」。
    真实多库行为由 scripts 里的真机验证覆盖，这里守的是参数不掉链子。"""

    def test_db_param_reaches_pool(self, client):
        tc, svc = client
        seen = []
        orig = svc.pool.get

        def spy(project, connection, cfg, role="reader", schema=None, database=None, **kw):
            seen.append(database)
            return orig(project, connection, cfg, role=role, schema=schema, **kw)

        svc.pool.get = spy
        try:
            tc.get("/admin/sql/tables?conn=demo/main&db=shop")
            tc.get("/admin/sql/table?conn=demo/main&table=users&db=shop")
            tc.get("/admin/sql/ddl?conn=demo/main&table=users&db=shop")
            tc.post("/admin/sql/run", data={"conn": "demo/main", "sql": "SELECT 1", "db": "shop"})
        finally:
            svc.pool.get = orig
        assert seen, "没有任何一次 pool.get 被调用"
        assert all(d == "shop" for d in seen), seen

    def test_no_db_param_keeps_connection_default(self, client):
        tc, svc = client
        seen = []
        orig = svc.pool.get

        def spy(project, connection, cfg, role="reader", schema=None, database=None, **kw):
            seen.append(database)
            return orig(project, connection, cfg, role=role, schema=schema, **kw)

        svc.pool.get = spy
        try:
            tc.post("/admin/sql/run", data={"conn": "demo/main", "sql": "SELECT 1"})
        finally:
            svc.pool.get = orig
        assert seen and all(d is None for d in seen), seen

    def test_server_databases_route(self, client):
        tc, _ = client
        # sqlite 没有库的概念 → 空列表，但路由本身要在
        d = tc.get("/admin/sql/server_databases?conn=demo/main").json()
        assert d["ok"] and d["databases"] == []


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

    def test_parallel_flag_bypasses_connection_serial(self, client):
        """连接串行只约束编辑器 query（同连接忙时拒绝）；数据 tab（parallel=1）用独立 key 不受限。"""
        import threading
        tc, svc = client
        gate = threading.Event()
        orig = svc.admin_run_sql

        def slow(project, connection, sql, *a, **k):
            if "SLEEPMARK" in sql:
                gate.wait(3)  # 占住连接串行名额，直到测试放行
            return orig(project, connection, sql, *a, **k)

        svc.admin_run_sql = slow
        try:
            r1 = tc.post("/admin/sql/run_async",
                         data={"conn": "demo/main", "sql": "SELECT 1 -- SLEEPMARK"}).json()
            assert r1["ok"] and r1["job_id"]  # 编辑器查询占住连接
            # 同连接再来一条编辑器查询 → 忙时拒绝
            r2 = tc.post("/admin/sql/run_async",
                         data={"conn": "demo/main", "sql": "SELECT 2"}).json()
            assert r2["ok"] is False and "正在执行" in r2["error"]
            # 同连接的数据 tab（parallel=1）→ 不占串行名额，直接受理
            r3 = tc.post("/admin/sql/run_async",
                         data={"conn": "demo/main", "sql": "SELECT * FROM users", "parallel": "1"}).json()
            assert r3["ok"] and r3["job_id"]
        finally:
            gate.set()
            svc.admin_run_sql = orig

    def test_explain_write_uses_writer_role(self, client):
        """写语句的 EXPLAIN 必须走 writer 账号（reader 只读账号无 DELETE 权限会被 DB 拒，MySQL 1142）。

        sqlite 下 writer 与 reader 同库，无法复现权限拒绝，故用 spy 断言选到的角色。
        """
        tc, svc = client
        seen = []
        orig = svc.pool.get

        def spy(project, connection, cfg, role="reader", schema=None, **kw):
            seen.append(role)
            return orig(project, connection, cfg, role=role, schema=schema, **kw)

        svc.pool.get = spy
        try:
            dw = tc.post("/admin/sql/explain",
                         data={"conn": "demo/main", "sql": "DELETE FROM users WHERE id=1"}).json()
            assert dw["ok"], dw
            assert seen == ["writer"]
            seen.clear()
            dr = tc.post("/admin/sql/explain",
                         data={"conn": "demo/main", "sql": "SELECT * FROM users"}).json()
            assert dr["ok"], dr
            assert seen == ["reader"]
        finally:
            svc.pool.get = orig

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

    def test_unauthenticated_api_call_returns_json_401(self, tmp_path):
        """前端 fetch（Accept: application/json）鉴权失败 → 401 JSON，而非 303 到登录页 HTML。
        回归：会话过期时查询台 POST 会被静默重定向拿到登录页 HTML，
        前端 r.json() 报出「Unexpected token '<', "<!doctype "...」的误导错误。"""
        svc, mcp = self._fresh_app(tmp_path)
        with TestClient(mcp.http_app()) as tc:
            r = tc.post("/admin/sql/run_async",
                        data={"conn": "demo/main", "sql": "SELECT 1"},
                        headers={"Accept": "application/json"},
                        follow_redirects=False)
            assert r.status_code == 401
            assert "application/json" in r.headers.get("content-type", "")
            body = r.json()
            assert body["ok"] is False and "登录" in body["error"]
            # 页面导航（非 JSON）仍走 303 到登录页
            nav = tc.get("/admin/sql", follow_redirects=False)
            assert nav.status_code == 303 and nav.headers["location"] == "/admin/login"
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
        import sys
        import types
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


def test_ai_route_gated_when_disabled(client):
    """AI 关闭时 /admin/sql/ai 直接 403（门禁），不触达 provider。"""
    from dbmcp.settings import SettingsStore
    tc, svc = client
    svc.settings = SettingsStore(":memory:")
    svc.save_settings({"ai_enabled": "false"})  # 默认已改为开，显式关
    r = tc.post("/admin/sql/ai", data={"conn": "demo/main", "question": "统计用户数"})
    assert r.status_code == 403
    assert r.json()["ok"] is False


def test_ai_route_generates_when_enabled(client, monkeypatch):
    """开启后：回填 SQL + 解释 + session_id（用假 ai.generate_sql，不真调 CLI）。"""
    from dbmcp import ai
    from dbmcp.settings import SettingsStore
    tc, svc = client
    svc.settings = SettingsStore(":memory:")
    svc.save_settings({"ai_enabled": "true"})
    monkeypatch.setattr(ai, "generate_sql",
                        lambda **kw: ai.AIResult(sql="SELECT count(*) FROM users",
                                                 explanation="走全表", session_id="sid-9"))
    r = tc.post("/admin/sql/ai",
                data={"conn": "demo/main", "question": "统计用户数",
                      "tables": '["users"]', "explain": "1"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    # 路由会用 sqlglot 美化 SQL，故断言语义而非精确字符串
    assert "COUNT(*)" in d["sql"].upper() and "USERS" in d["sql"].upper()
    assert d["explanation"] == "走全表"
    assert d["session_id"] == "sid-9"


def test_workflow_new_page_and_list_route(client):
    """新页 GET /admin/workflows 返 HTML shell（含 Vue 挂载点）；老 JSON 迁到 /list。"""
    tc, _ = client
    r = tc.get("/admin/workflows")
    assert r.status_code == 200
    assert 'id="wf-app"' in r.text and "workflows.js" in r.text
    # 老 JSON 端点被搬到 /list
    r2 = tc.get("/admin/workflows/list")
    assert r2.status_code == 200 and r2.json()["ok"] is True


def test_workflow_preview_columns_http(client, tmp_path):
    """preview_columns 路由：成功返回列/类型；参数缺失 400；graph 非法 400。"""
    import json as _json

    from dbmcp.analysis import AnalysisStore
    tc, svc = client
    svc.analysis = AnalysisStore(tmp_path / "analysis")
    g = {"nodes": [
        {"id": "a", "type": "source", "name": "u",
         "cfg": {"conn": "demo/main", "sql": "SELECT * FROM users"}},
        {"id": "b", "type": "filter", "name": "flt", "cfg": {"where": "id >= 1"}}],
        "edges": [{"from": "a", "to": "b", "port": "in"}]}
    r = tc.post("/admin/workflows/preview_columns",
                data={"workspace": "ws1", "node": "b", "graph": _json.dumps(g)})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert {c["name"] for c in d["columns"]} >= {"id", "name"}
    # 参数缺失
    r2 = tc.post("/admin/workflows/preview_columns", data={"node": "b"})
    assert r2.status_code == 400
    # graph 非法 JSON
    r3 = tc.post("/admin/workflows/preview_columns",
                 data={"workspace": "ws1", "node": "b", "graph": "not-json"})
    assert r3.status_code == 400


def test_workflow_preview_node_http(client, tmp_path):
    """preview_node 路由：拿前 N 行数据，走 analysis 沙箱。"""
    import json as _json

    from dbmcp.analysis import AnalysisStore
    tc, svc = client
    svc.analysis = AnalysisStore(tmp_path / "analysis")
    g = {"nodes": [{"id": "a", "type": "source", "name": "u",
                    "cfg": {"conn": "demo/main", "sql": "SELECT * FROM users"}}],
         "edges": []}
    r = tc.post("/admin/workflows/preview_node",
                data={"workspace": "ws1", "node": "a", "graph": _json.dumps(g), "limit": "5"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert set(d["columns"]) >= {"id", "name"}
    assert d["row_count"] >= 1


def test_workflow_workspaces_http(client, tmp_path):
    """workspaces 列表路由：analysis 未初始化时应友好错误，初始化后能列出。"""
    from dbmcp.analysis import AnalysisStore
    tc, svc = client
    # 未初始化 analysis → analysis_overview 抛
    r = tc.get("/admin/workflows/workspaces")
    assert r.status_code == 400
    svc.analysis = AnalysisStore(tmp_path / "analysis")
    r2 = tc.get("/admin/workflows/workspaces")
    assert r2.status_code == 200 and r2.json()["ok"] is True


def test_workflow_workspace_create_http(client, tmp_path):
    """workspace_create 路由：建成功后能被 workspaces 列表返回；重名幂等。"""
    from dbmcp.analysis import AnalysisStore
    tc, svc = client
    svc.analysis = AnalysisStore(tmp_path / "analysis")
    # 空名 400
    r0 = tc.post("/admin/workflows/workspace_create", data={"name": ""})
    assert r0.status_code == 400
    # 建成功
    r = tc.post("/admin/workflows/workspace_create", data={"name": "ws_test"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # 列表能看到
    ws = tc.get("/admin/workflows/workspaces").json()["workspaces"]
    assert any(w["workspace"] == "ws_test" for w in ws)
    # 幂等（DuckDB 里再连一次即可，不报错）
    r2 = tc.post("/admin/workflows/workspace_create", data={"name": "ws_test"})
    assert r2.status_code == 200


def test_workflow_ai_gated_and_generates(client, monkeypatch):
    """流程 AI 路由：未开启 403；开启后返回校验通过的 graph（假 ai.generate_workflow）。"""
    from dbmcp import ai
    from dbmcp.settings import SettingsStore
    tc, svc = client
    svc.settings = SettingsStore(":memory:")
    # 关闭 → 403（默认已改为开，显式关来测门禁）
    svc.save_settings({"ai_enabled": "false"})
    r = tc.post("/admin/workflows/ai", data={"conn": "demo/main", "question": "聚合"})
    assert r.status_code == 403
    # 开启 + 假 AI 返回合法 graph
    svc.save_settings({"ai_enabled": "true"})
    good = {"nodes": [
        {"id": "a", "type": "source", "name": "src", "cfg": {"conn": "demo/main", "sql": "SELECT id FROM users"}},
        {"id": "b", "type": "output", "name": "out", "cfg": {"limit": 5}}],
        "edges": [{"from": "a", "to": "b", "port": "in"}]}
    monkeypatch.setattr(ai, "generate_workflow", lambda **kw: (dict(good), "sid"))
    r2 = tc.post("/admin/workflows/ai",
                 data={"conn": "demo/main", "question": "输出用户", "tables": '["users"]'})
    assert r2.status_code == 200
    d = r2.json()
    assert d["ok"] is True
    assert [n["name"] for n in d["graph"]["nodes"]] == ["src", "out"]
    assert all("x" in n and "y" in n for n in d["graph"]["nodes"])  # 已排版


def test_workflow_ai_modify_mode_forwards_current_graph(client, monkeypatch):
    """修改模式：前端传 current_graph 时，走 ai.generate_workflow 的 current_graph 参数。"""
    import json as _json
    from dbmcp import ai, engines
    from dbmcp.settings import SettingsStore
    tc, svc = client
    svc.settings = SettingsStore(":memory:")
    svc.save_settings({"ai_enabled": "true"})
    monkeypatch.setattr(engines, "get_table_ddl",
                        lambda *a, **k: "CREATE TABLE users(id INT)")
    good = {"nodes": [
        {"id": "a", "type": "source", "name": "src", "cfg": {"conn": "demo/main", "sql": "SELECT 1"}},
        {"id": "b", "type": "output", "name": "out", "cfg": {"limit": 5}}],
        "edges": [{"from": "a", "to": "b", "port": "in"}]}
    captured = {}
    def fake(**kw):
        captured.update(kw)
        return dict(good), "sid"
    monkeypatch.setattr(ai, "generate_workflow", fake)
    cur = {"nodes": [{"id": "z", "type": "source", "name": "old",
                      "cfg": {"conn": "demo/main", "sql": "SELECT * FROM users"}}], "edges": []}
    r = tc.post("/admin/workflows/ai",
                data={"conn": "demo/main", "question": "改", "tables": '["users"]',
                      "current_graph": _json.dumps(cur)})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert captured.get("current_graph") == cur
    # 空 nodes 视为新建（不设 current_graph）
    captured.clear()
    r = tc.post("/admin/workflows/ai",
                data={"conn": "demo/main", "question": "新", "tables": '["users"]',
                      "current_graph": _json.dumps({"nodes": [], "edges": []})})
    assert r.status_code == 200
    assert captured.get("current_graph") is None


# ==============================================================
# PR-2：调度 / 运行历史 / xlsx 下载 HTTP 路由
# ==============================================================


def _prep_pr2_service(svc, tmp_path):
    """给 client fixture 的 svc 补上 workflows/schedules/runs/analysis 并建一个 workflow。"""
    from dbmcp.analysis import AnalysisStore
    from dbmcp.service import CallerInfo as CI
    from dbmcp.workflows import WorkflowRunStore, WorkflowScheduleStore, WorkflowStore
    svc.analysis = AnalysisStore(tmp_path / "analysis")
    svc.workflows = WorkflowStore(tmp_path / "wf.sqlite3")
    svc.schedules = WorkflowScheduleStore(tmp_path / "sched.sqlite3")
    svc.runs = WorkflowRunStore(tmp_path / "runs.sqlite3")
    svc.data_dir = str(tmp_path / "data")
    caller = CI(agent="pytest/1.0", session_id="s1")
    g = {"nodes": [{"id": "a", "type": "source", "name": "u", "x": 0, "y": 0,
                    "cfg": {"conn": "demo/main", "sql": "SELECT * FROM users"}}],
         "edges": []}
    svc.workflow_save("wf_pr2", "ws_pr2", "", caller, graph=g)


def test_workflow_schedule_get_upsert_delete(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    # 空 name → 400
    r0 = tc.post("/admin/workflows/schedule",
                 data={"name": "", "cron_type": "interval", "cron_value": "5"})
    assert r0.status_code == 400
    # workflow 不存在 → 400
    r1 = tc.post("/admin/workflows/schedule",
                 data={"name": "nope", "cron_type": "interval", "cron_value": "5"})
    assert r1.status_code == 400
    # 建
    r2 = tc.post("/admin/workflows/schedule",
                 data={"name": "wf_pr2", "cron_type": "interval", "cron_value": "5",
                       "notify_on": "always", "attach_kinds": "summary,markdown_table"})
    assert r2.status_code == 200 and r2.json()["ok"] is True
    got = r2.json()["schedule"]
    assert got["notify_on"] == "always"
    assert set(got["attach_kinds"]) == {"summary", "markdown_table"}
    # 读
    r3 = tc.get("/admin/workflows/schedule?name=wf_pr2")
    assert r3.status_code == 200 and r3.json()["schedule"]["cron_type"] == "interval"
    # 改（改 cron_value + enabled=0）
    r4 = tc.post("/admin/workflows/schedule",
                 data={"name": "wf_pr2", "cron_type": "daily", "cron_value": "09:00",
                       "enabled": "0"})
    assert r4.status_code == 200
    assert r4.json()["schedule"]["enabled"] is False
    # 删
    r5 = tc.post("/admin/workflows/schedule/delete", data={"name": "wf_pr2"})
    assert r5.status_code == 200
    r6 = tc.get("/admin/workflows/schedule?name=wf_pr2")
    assert r6.status_code == 200 and r6.json()["schedule"] is None


def test_workflow_schedule_rejects_bad_cron(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    r = tc.post("/admin/workflows/schedule",
                data={"name": "wf_pr2", "cron_type": "interval", "cron_value": "9999"})
    assert r.status_code == 400
    r2 = tc.post("/admin/workflows/schedule",
                 data={"name": "wf_pr2", "cron_type": "daily", "cron_value": "25:00"})
    assert r2.status_code == 400


def test_workflow_runs_and_detail(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    # 触发一次调度产生 run 记录
    svc.workflow_schedule_upsert("wf_pr2", "interval", "5", notify_on="none")
    svc._run_scheduled("wf_pr2")  # noqa: SLF001
    r1 = tc.get("/admin/workflows/runs?name=wf_pr2")
    assert r1.status_code == 200
    runs = r1.json()["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "ok"
    run_id = runs[0]["id"]
    r2 = tc.get(f"/admin/workflows/runs/{run_id}/detail")
    assert r2.status_code == 200
    assert r2.json()["run"]["id"] == run_id
    # 不存在的 id → 404
    r3 = tc.get("/admin/workflows/runs/99999/detail")
    assert r3.status_code == 404


def test_workflow_run_detail_page_shell(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    svc.workflow_schedule_upsert("wf_pr2", "interval", "5", notify_on="none")
    svc._run_scheduled("wf_pr2")  # noqa: SLF001
    run_id = svc.workflow_runs_list("wf_pr2")[0]["id"]
    r = tc.get(f"/admin/workflows/runs/{run_id}")
    assert r.status_code == 200
    assert 'id="wf-app"' in r.text and "workflows.js" in r.text


def test_workflow_run_xlsx_download(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    svc.workflow_schedule_upsert("wf_pr2", "interval", "5",
                                  attach_kinds=["summary", "xlsx_link"])
    svc._run_scheduled("wf_pr2")  # noqa: SLF001
    run_id = svc.workflow_runs_list("wf_pr2")[0]["id"]
    r = tc.get(f"/admin/workflows/runs/{run_id}/download/output.xlsx")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "spreadsheet" in ct
    assert r.content[:2] == b"PK"  # xlsx 是 zip


def test_workflow_run_xlsx_download_missing(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    # 未含 xlsx_link → 没落文件
    svc.workflow_schedule_upsert("wf_pr2", "interval", "5", attach_kinds=["summary"])
    svc._run_scheduled("wf_pr2")  # noqa: SLF001
    run_id = svc.workflow_runs_list("wf_pr2")[0]["id"]
    r = tc.get(f"/admin/workflows/runs/{run_id}/download/output.xlsx")
    assert r.status_code == 404


def test_workflow_running_route_default_schedule_only(client, tmp_path):
    """GET /admin/workflows/running：默认 triggered_by=schedule 过滤，manual 记录不出现。"""
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    # 起 3 条 running：2 schedule + 1 manual；1 已完成
    r_sched1 = svc.runs.start("wf_pr2", "schedule")
    r_sched2 = svc.runs.start("wf_other", "schedule")
    svc.runs.start("wf_manual", "manual")
    r_done = svc.runs.start("wf_done", "schedule")
    svc.runs.finish(r_done, "ok")

    r = tc.get("/admin/workflows/running")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    ids = {row["id"] for row in body["runs"]}
    assert ids == {r_sched1, r_sched2}
    # 每行字段齐全
    row = body["runs"][0]
    for key in ("id", "name", "triggered_by", "started_at", "elapsed_s"):
        assert key in row
    assert row["triggered_by"] == "schedule"


def test_workflow_running_route_all(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    svc.runs.start("wf_pr2", "schedule")
    svc.runs.start("wf_manual", "manual")
    r = tc.get("/admin/workflows/running?triggered_by=all")
    assert r.status_code == 200
    assert len(r.json()["runs"]) == 2


def test_workflow_running_route_empty(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    r = tc.get("/admin/workflows/running")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["runs"] == []


# ==============================================================
# 定时任务全局管理页：GET /schedules + POST /schedule/trigger
# ==============================================================


def test_workflow_schedules_list_route(client, tmp_path):
    """GET /admin/workflows/schedules → 所有 schedule 附 workflow_exists 标记。"""
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    # 场景 1：空
    r0 = tc.get("/admin/workflows/schedules")
    assert r0.status_code == 200 and r0.json()["schedules"] == []
    # 建两条 schedule：wf_pr2 存在、ghost 对应 workflow 不存在
    svc.workflow_schedule_upsert("wf_pr2", "interval", "5", enabled=True)
    # 直接 upsert ghost（先建 workflow 再删）
    from dbmcp.service import CallerInfo as CI
    caller = CI(agent="pytest/1.0", session_id="s1")
    g = {"nodes": [{"id": "a", "type": "source", "name": "u", "x": 0, "y": 0,
                    "cfg": {"conn": "demo/main", "sql": "SELECT * FROM users"}}],
         "edges": []}
    svc.workflow_save("ghost", "ws_pr2", "", caller, graph=g)
    svc.workflow_schedule_upsert("ghost", "daily", "09:30", enabled=False)
    svc.workflow_delete("ghost")
    r = tc.get("/admin/workflows/schedules")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    rows = {s["name"]: s for s in body["schedules"]}
    assert rows["wf_pr2"]["workflow_exists"] is True
    assert rows["wf_pr2"]["enabled"] is True
    assert rows["ghost"]["workflow_exists"] is False
    # running 字段存在（本例都无运行中实例）
    assert "running" in rows["wf_pr2"]


def test_workflow_schedule_trigger_route_success(client, tmp_path):
    """POST /admin/workflows/schedule/trigger → 后台跑一次，入 workflow_run 表。"""
    import time
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    svc.workflow_schedule_upsert("wf_pr2", "interval", "5", enabled=True)
    r = tc.post("/admin/workflows/schedule/trigger", data={"name": "wf_pr2"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # 后台线程跑完（本例 SQL 小、~10ms）
    for _ in range(30):
        runs = svc.workflow_runs_list("wf_pr2")
        if runs and runs[0].get("status") in ("ok", "failed"):
            break
        time.sleep(0.05)
    runs = svc.workflow_runs_list("wf_pr2")
    assert len(runs) == 1
    assert runs[0]["triggered_by"] == "schedule"
    assert runs[0]["status"] == "ok"


def test_workflow_schedule_trigger_route_missing_name(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    r = tc.post("/admin/workflows/schedule/trigger", data={"name": ""})
    assert r.status_code == 400 and r.json()["ok"] is False


def test_workflow_schedule_trigger_route_no_schedule(client, tmp_path):
    """workflow 存在但没建 schedule 时拒绝。"""
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    r = tc.post("/admin/workflows/schedule/trigger", data={"name": "wf_pr2"})
    assert r.status_code == 400
    assert "调度配置" in r.json()["error"]


def test_workflow_schedule_trigger_route_no_workflow(client, tmp_path):
    tc, svc = client
    _prep_pr2_service(svc, tmp_path)
    # schedule 系统不允许对不存在的 workflow 建，这里模拟：直接下探到 store
    svc.schedules._conn.execute(  # noqa: SLF001
        "INSERT INTO workflow_schedule (name, cron_type, cron_value, enabled, notify_on,"
        " attach_kinds, notify_channels, created_at, updated_at)"
        " VALUES ('ghost', 'interval', '5', 1, 'failure', '[\"summary\"]', '[]', "
        "'2020-01-01T00:00:00', '2020-01-01T00:00:00')")
    svc.schedules._conn.commit()  # noqa: SLF001
    r = tc.post("/admin/workflows/schedule/trigger", data={"name": "ghost"})
    assert r.status_code == 400
    assert "workflow" in r.json()["error"].lower() or "不存在" in r.json()["error"]


def test_workflows_js_no_chinese_in_vue_bindings():
    """回归：Vue 的 :xxx="..." 是 JS 表达式，把中文短语当值会在浏览器抛
    SyntaxError: Unexpected identifier '中文'。静态检测所有 :attr="..." 值中
    出现的中文字符（含标点），撞上就报错并给出改法提示（去掉冒号或用 '字符串'）。
    """
    import re
    from pathlib import Path
    js = Path("src/dbmcp/static/workflows.js").read_text(encoding="utf-8")
    # 匹配所有 :some-attr="..." 中的值（不匹配 http:// 之类）
    pattern = re.compile(r'[\s\'"]:(\w[\w-]*)="([^"]*)"')
    offenders = []
    for m in pattern.finditer(js):
        attr, val = m.group(1), m.group(2)
        # 允许值里含引号包裹的中文（是合法 JS 字符串字面量）；只报"裸中文"
        # 快速判：去掉所有 '...' / "..." 包裹的字符串字面量后仍有中文 = 有裸中文
        stripped = re.sub(r"'[^']*'|\"[^\"]*\"", "", val)
        if re.search(r"[一-鿿　-〿＀-￯]", stripped):
            offenders.append(f":{attr}=\"{val}\"")
    assert not offenders, (
        "workflows.js 的 Vue :attr 绑定里有裸中文（会被解释为 JS 表达式 → SyntaxError）："
        f"\n  {chr(10).join(offenders)}\n"
        "改法：去掉冒号（静态值 title=\"...\"），或把中文加单/双引号变成字符串字面量。"
    )


def test_workflows_js_openschedule_ignores_event_object():
    """回归：openSchedule 的 nameOverride 参数必须先判 typeof === 'string'
    再用；否则详情页 @click="openSchedule" 会把 MouseEvent 当 name 传进去，
    浏览器会把 {isTrusted:true,...} 送到后端报"workflow xxx 不存在"。
    """
    from pathlib import Path
    js = Path("src/dbmcp/static/workflows.js").read_text(encoding="utf-8")
    # 找 openSchedule 方法体，确认它对 nameOverride 做了字符串类型判定
    idx = js.find("openSchedule: function")
    assert idx > 0, "openSchedule 方法不见了"
    # 截前 400 字符看方法体开头
    body = js[idx:idx + 400]
    assert 'typeof nameOverride === "string"' in body or \
           "typeof nameOverride==='string'" in body.replace(" ", ""), (
        "openSchedule 必须先判 nameOverride 是字符串再用，否则详情页 @click 传"
        "的 MouseEvent 会被当成 workflow name 送到后端。"
    )
