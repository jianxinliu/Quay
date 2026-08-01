"""会话回溯：agent 用 begin_session 声明会话名字/简介后，后台可按 agent + 会话过滤
其跑过的 SQL，并把「需审批（写）/不需审批（读）」分开。

覆盖三层：AuditStore 会话表与读写过滤（纯逻辑）、DbmService.begin_session、
以及审计页 HTTP 的会话/读写筛选。
"""

import sqlite3

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditRecord, AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

TOKEN = "test-admin-token"


def _rec(session_id, tool, agent="claude/1.0", status="ok"):
    return AuditRecord(
        project="demo", connection="main", tool=tool, status=status,
        agent=agent, session_id=session_id, sql=f"-- {tool}",
    )


# ---------------- AuditStore：会话表 + 读写过滤 ----------------

class TestAuditStoreSessions:
    def test_upsert_and_list_session(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.upsert_session("sess-A", "claude/1.0", "排查订单", "复现 #123")
        # 该会话跑了 2 条读 + 1 条写
        store.record(_rec("sess-A", "query"))
        store.record(_rec("sess-A", "sample_rows"))
        store.record(_rec("sess-A", "execute"))

        sessions = store.list_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s["session_id"] == "sess-A"
        assert s["title"] == "排查订单"
        assert s["note"] == "复现 #123"
        assert s["agent"] == "claude/1.0"
        assert s["ops"] == 3
        assert s["writes"] == 1  # 仅 execute 计入需审批的写

    def test_upsert_is_idempotent_and_updates(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.upsert_session("sess-A", "claude/1.0", "旧名字", "")
        store.upsert_session("sess-A", "claude/1.0", "新名字", "补充简介")
        store.record(_rec("sess-A", "query"))
        s = store.list_sessions()[0]
        assert s["title"] == "新名字"
        assert s["note"] == "补充简介"

    def test_empty_session_id_not_stored(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.upsert_session("", "claude/1.0", "无会话id", "")
        # 无记录，也不报错
        assert store.list_sessions() == []

    def test_list_sessions_covers_unregistered(self, tmp_path):
        """没调 begin_session 的会话也应出现在列表里（title 为空），只是没名字。"""
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-X", "query"))
        s = store.list_sessions()[0]
        assert s["session_id"] == "sess-X"
        assert s["title"] is None
        assert s["ops"] == 1

    def test_list_sessions_filter_by_agent(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-A", "query", agent="claude/1.0"))
        store.record(_rec("sess-B", "query", agent="codex/1.0"))
        got = store.list_sessions(agent="codex/1.0")
        assert [s["session_id"] for s in got] == ["sess-B"]

    def test_rw_filter_write_only(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-A", "query"))
        store.record(_rec("sess-A", "execute"))
        store.record(_rec("sess-A", "admin_execute"))
        write_rows = store.recent(filters={"rw": "write"})
        assert {r["tool"] for r in write_rows} == {"execute", "admin_execute"}
        assert store.count({"rw": "write"}) == 2

    def test_rw_filter_read_only(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-A", "query"))
        store.record(_rec("sess-A", "sample_rows"))
        store.record(_rec("sess-A", "execute"))
        read_rows = store.recent(filters={"rw": "read"})
        assert {r["tool"] for r in read_rows} == {"query", "sample_rows"}

    def test_session_and_rw_combined(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-A", "query"))
        store.record(_rec("sess-A", "execute"))
        store.record(_rec("sess-B", "execute"))
        rows = store.recent(filters={"session_id": "sess-A", "rw": "write"})
        assert len(rows) == 1 and rows[0]["session_id"] == "sess-A"

    def test_purge_removes_orphaned_sessions(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.upsert_session("sess-A", "claude/1.0", "会话A", "")
        # 无任何 audit_log 引用 sess-A → purge 后元信息被清
        store.purge_old(retention_days=0)
        assert store.list_sessions() == []
        # agent_session 表已空
        rows = store._conn.execute("SELECT count(*) FROM agent_session").fetchone()
        assert rows[0] == 0


# ---------------- DbmService.begin_session ----------------

@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY);")
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "local"}}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"))
    yield svc
    svc.close()


class TestServiceBeginSession:
    def test_begin_session_registers(self, service):
        caller = CallerInfo(agent="claude/1.0", session_id="sess-A")
        out = service.begin_session(caller, "排查订单", "复现 #123")
        assert out["session_id"] == "sess-A"
        assert out["title"] == "排查订单"
        # 未跑 SQL 时 list_sessions 靠 audit_log 无记录，但 begin_session 登记本身应已入库
        registered = service.store._conn.execute(
            "SELECT title, note FROM agent_session WHERE session_id='sess-A'").fetchone()
        assert registered[0] == "排查订单" and registered[1] == "复现 #123"

    def test_empty_title_rejected(self, service):
        caller = CallerInfo(agent="claude/1.0", session_id="sess-A")
        with pytest.raises(ValueError):
            service.begin_session(caller, "   ")

    def test_no_session_id_warns(self, service):
        caller = CallerInfo(agent="claude/1.0", session_id="")
        out = service.begin_session(caller, "无会话id")
        assert out["session_id"] == ""
        assert "无法按会话归类" in out["note"]

    def test_session_associates_subsequent_sql(self, service):
        """begin_session 后跑的 query 自动按 session_id 归到该会话名下。"""
        caller = CallerInfo(agent="claude/1.0", session_id="sess-A")
        service.begin_session(caller, "排查订单", "")
        service.query("demo", "main", "SELECT 1", caller)
        sessions = service.store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["title"] == "排查订单"
        assert sessions[0]["ops"] == 1


# ---------------- 审计页 HTTP：会话 / 读写筛选 ----------------

@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO users (name) VALUES ('alice');"
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
            "writer": {"user": "x", "password": "plain://unused"}}}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
    svc.data_dir = str(tmp_path / "data")
    svc.base_url = "http://testserver"
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token=TOKEN)
    app = mcp.http_app()
    with TestClient(app) as tc:
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc, svc
    svc.close()


class TestAuditPageSessionFilter:
    def _seed(self, svc):
        svc.store.upsert_session("sess-A", "claude/1.0", "排查订单重复扣款", "复现 #123")
        svc.store.record(_rec("sess-A", "query"))
        svc.store.record(_rec("sess-A", "execute"))
        svc.store.record(_rec("sess-B", "query", agent="codex/1.0"))

    def test_session_dropdown_shows_title(self, client):
        tc, svc = client
        self._seed(svc)
        html = tc.get("/admin/audit").text
        assert "排查订单重复扣款" in html          # 会话名字进了下拉/表格
        assert "全部会话" in html                   # 会话筛选下拉存在
        assert "需审批（写）" in html                # 读写筛选下拉存在

    def test_filter_by_session(self, client):
        tc, svc = client
        self._seed(svc)
        html = tc.get("/admin/audit?session_id=sess-A").text
        # 回溯提示条出现，且统计到 1 次写
        assert "正在回溯会话" in html
        assert "排查订单重复扣款" in html
        # 只匹配到 sess-A 的 2 条（sess-B 被过滤掉；codex 仅出现在筛选下拉里）
        assert "共 2 条" in html
        # 分页/表体只剩 sess-A：其它会话的写记录不参与统计
        assert "共 3 条" not in html

    def test_filter_by_session_and_write(self, client):
        tc, svc = client
        self._seed(svc)
        # 只看该会话里需审批的写：应含 execute，不含 query
        html = tc.get("/admin/audit?session_id=sess-A&rw=write").text
        assert "execute" in html
        # query 工具名不应出现在工具列（只剩写）
        assert ">query<" not in html
