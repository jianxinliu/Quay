"""会话自查与回滚备注：agent 回溯自己过去做过的改动。

两件事合在一起：
1. `list_sessions` / `session_history` 让 agent 查自己过去的会话与其中的操作
   （可按日期、关键词、项目/连接、是否有写筛选）；
2. 写操作提交时可带 `rollback_note`（改动前的值/怎么回滚），存进审批单，
   审批人在审批页看得到，事后 agent 用 session_history 取得回来。
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditRecord, AuditStore, parse_time_filter
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

TOKEN = "test-admin-token"
CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-now")


def _rec(session_id, tool, agent="pytest/1.0", sql="SELECT 1",
         project="demo", connection="main", change_id=None):
    return AuditRecord(
        project=project, connection=connection, tool=tool, status="ok",
        agent=agent, session_id=session_id, sql=sql, change_id=change_id,
    )


def _backdate(store, session_id, iso_ts):
    """把某会话的审计记录时间改到指定时刻（构造历史数据用）。"""
    store._conn.execute("UPDATE audit_log SET ts = ? WHERE session_id = ?",
                        (iso_ts, session_id))
    store._conn.commit()


# ---------------- 时间过滤入参归一（纯函数） ----------------

class TestParseTimeFilter:
    def test_empty_is_none(self):
        assert parse_time_filter("") is None
        assert parse_time_filter(None) is None
        assert parse_time_filter("   ") is None

    def test_date_only_uses_local_midnight(self):
        """只给日期时按**本地**零点解释再转 UTC（否则东八区会漏掉当天早 8 点前的记录）。"""
        got = parse_time_filter("2026-09-08")
        expect = datetime(2026, 9, 8).astimezone().astimezone(UTC)
        assert got == expect.isoformat(timespec="milliseconds")

    def test_date_only_end_of_day_is_inclusive(self):
        got = parse_time_filter("2026-09-08", end_of_day=True)
        expect = datetime(2026, 9, 8, 23, 59, 59, 999000).astimezone().astimezone(UTC)
        assert got == expect.isoformat(timespec="milliseconds")

    def test_explicit_offset_respected(self):
        assert parse_time_filter("2026-09-08T10:00:00+08:00") == "2026-09-08T02:00:00.000+00:00"

    def test_bad_format_raises_with_hint(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            parse_time_filter("上周")

    def test_comparable_with_stored_ts(self, tmp_path):
        """归一结果必须能与 audit_log.ts 直接做字符串比较（同格式同时区）。"""
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-A", "query"))
        ts = store.recent()[0]["ts"]
        floor = parse_time_filter(datetime.now().date().isoformat())
        assert ts > floor


# ---------------- AuditStore.list_sessions 的筛选条件 ----------------

class TestListSessionsFilters:
    def test_time_window(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-old", "query"))
        _backdate(store, "sess-old", "2026-01-02T03:00:00.000+00:00")
        store.record(_rec("sess-new", "query"))

        assert [s["session_id"] for s in store.list_sessions(
            since="2026-01-01T00:00:00.000+00:00",
            until="2026-01-03T00:00:00.000+00:00")] == ["sess-old"]
        # 只给 since：老会话被排除
        assert [s["session_id"] for s in store.list_sessions(
            since="2026-06-01T00:00:00.000+00:00")] == ["sess-new"]

    def test_keyword_matches_title_note_or_sql(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.upsert_session("sess-A", "pytest/1.0", "排查订单重复扣款", "复现 #123")
        store.record(_rec("sess-A", "query", sql="SELECT * FROM payments"))
        store.record(_rec("sess-B", "query", sql="SELECT * FROM orders LIMIT 1"))

        assert [s["session_id"] for s in store.list_sessions(keyword="扣款")] == ["sess-A"]
        assert [s["session_id"] for s in store.list_sessions(keyword="#123")] == ["sess-A"]
        # 关键词也命中会话跑过的 SQL（「哪个会话动过 orders」）
        assert [s["session_id"] for s in store.list_sessions(keyword="orders")] == ["sess-B"]
        assert store.list_sessions(keyword="不存在的词") == []

    def test_keyword_on_sql_keeps_full_op_count(self, tmp_path):
        """关键词命中 SQL 时，ops 仍是该会话的全部操作数，而不是「含关键词的那几条」。"""
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-A", "query", sql="SELECT * FROM orders"))
        store.record(_rec("sess-A", "query", sql="SELECT 1"))
        assert store.list_sessions(keyword="orders")[0]["ops"] == 2

    def test_scoped_by_connection(self, tmp_path):
        """按连接筛选时，ops 只统计该连接上的操作（「我在这个库上做过什么」）。"""
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-A", "query", connection="main"))
        store.record(_rec("sess-A", "query", connection="main"))
        store.record(_rec("sess-A", "query", connection="replica"))
        store.record(_rec("sess-B", "query", connection="replica"))

        got = store.list_sessions(connection="main")
        assert [s["session_id"] for s in got] == ["sess-A"]
        assert got[0]["ops"] == 2
        assert {s["session_id"] for s in store.list_sessions(project="demo")} == {"sess-A", "sess-B"}
        assert store.list_sessions(project="other") == []

    def test_status_scopes_session_and_counts(self, tmp_path):
        """按状态筛选时，只保留有该状态操作的会话，ops 也只数这些操作。"""
        store = AuditStore(tmp_path / "a.sqlite3")
        ok = _rec("sess-A", "execute")
        failed = _rec("sess-A", "execute")
        failed.status = "rejected"
        only_failed = _rec("sess-B", "execute")
        only_failed.status = "rejected"
        for r in (ok, failed, only_failed):
            store.record(r)

        got = store.list_sessions(status="ok")
        assert [s["session_id"] for s in got] == ["sess-A"]
        assert got[0]["ops"] == 1        # 只数成功那条
        assert {s["session_id"] for s in store.list_sessions(status="rejected")} == {
            "sess-A", "sess-B"}

    def test_only_with_writes(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-read", "query"))
        store.record(_rec("sess-write", "query"))
        store.record(_rec("sess-write", "execute"))
        got = store.list_sessions(only_with_writes=True)
        assert [s["session_id"] for s in got] == ["sess-write"]
        assert got[0]["writes"] == 1

    def test_get_session(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        assert store.get_session("nope") is None
        assert store.get_session("") is None
        store.upsert_session("sess-A", "pytest/1.0", "排查订单", "复现 #123")
        assert store.get_session("sess-A")["title"] == "排查订单"


class TestAuditChangeIdColumn:
    def test_change_id_roundtrip(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        store.record(_rec("sess-A", "execute", change_id=42))
        assert store.recent()[0]["change_id"] == 42

    def test_old_db_migrates(self, tmp_path):
        """老库（无 change_id 列）打开时自动 ALTER 补列，不报错也不丢数据。"""
        db = tmp_path / "old.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(
            """CREATE TABLE audit_log (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent TEXT,
                 session_id TEXT, project TEXT NOT NULL, connection TEXT NOT NULL,
                 environment TEXT, engine TEXT, tool TEXT NOT NULL, sql TEXT,
                 fingerprint TEXT, status TEXT NOT NULL, detail TEXT,
                 row_count INTEGER, duration_ms INTEGER);
               INSERT INTO audit_log (ts, project, connection, tool, status)
               VALUES ('2026-01-01T00:00:00.000+00:00', 'demo', 'main', 'query', 'ok');"""
        )
        conn.commit()
        conn.close()

        store = AuditStore(db)
        rows = store.recent()
        assert len(rows) == 1 and rows[0]["change_id"] is None
        store.record(_rec("sess-A", "execute", change_id=7))
        assert store.recent()[0]["change_id"] == 7


# ---------------- 审批单的回滚备注 ----------------

class TestApprovalRollbackNote:
    def _create(self, store, note=""):
        return store.create(
            project="demo", connection="main", environment="dev", engine="sqlite",
            sql="UPDATE users SET active = 0 WHERE id = 1", fingerprint="fp",
            reason="下线测试账号", risk_level="MEDIUM", risk_report={},
            agent="pytest/1.0", session_id="sess-A", rollback_note=note,
        )

    def test_stored_and_returned(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        c = self._create(store, "id=1 改前 active=1；回滚 UPDATE users SET active=1 WHERE id=1")
        assert "改前 active=1" in store.get(c.id).rollback_note

    def test_defaults_to_empty(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        assert self._create(store).rollback_note == ""

    def test_old_db_migrates(self, tmp_path):
        """老库（无 rollback_note 列）打开时自动补列，老审批单读出来是空串。"""
        db = tmp_path / "old.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(
            """CREATE TABLE change_request (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                 expires_at TEXT NOT NULL, project TEXT NOT NULL, connection TEXT NOT NULL,
                 environment TEXT, engine TEXT, sql TEXT NOT NULL, fingerprint TEXT NOT NULL,
                 reason TEXT, risk_level TEXT, risk_report TEXT, agent TEXT, session_id TEXT,
                 status TEXT NOT NULL, decided_by TEXT, decided_at TEXT, decision_note TEXT);
               INSERT INTO change_request
                 (created_at, expires_at, project, connection, sql, fingerprint, status)
               VALUES ('2026-01-01T00:00:00', '2026-01-01T01:00:00', 'demo', 'main',
                       'DELETE FROM t', 'fp', 'pending');"""
        )
        conn.commit()
        conn.close()

        store = ApprovalStore(db)
        assert store.get(1).rollback_note == ""
        c = self._create(store, "有备注")
        assert store.get(c.id).rollback_note == "有备注"

    def test_get_many(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        a, b = self._create(store, "A 的备注"), self._create(store, "B 的备注")
        got = store.get_many([a.id, b.id, 999, None])
        assert set(got) == {a.id, b.id}
        assert got[a.id].rollback_note == "A 的备注"
        assert store.get_many([]) == {}


# ---------------- 服务层：会话自查 + 回滚备注闭环 ----------------

@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, active INTEGER DEFAULT 1);
           INSERT INTO users (name) VALUES ('alice'), ('bob');"""
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     ApprovalStore(tmp_path / "a.sqlite3"))
    yield svc
    svc.close()


class TestServiceListSessions:
    def test_lists_own_sessions_and_marks_current(self, service):
        other = CallerInfo(agent="other-agent/1.0", session_id="sess-other")
        service.begin_session(CALLER, "排查订单", "复现 #123")
        service.query("demo", "main", "SELECT 1", CALLER)
        service.query("demo", "main", "SELECT 1", other)

        got = service.list_agent_sessions(CALLER)
        assert [s["session_id"] for s in got] == ["sess-now"]
        assert got[0]["title"] == "排查订单" and got[0]["current"] is True
        # 别的 agent 的会话要显式开 all_agents 才出现
        assert {s["session_id"] for s in service.list_agent_sessions(CALLER, all_agents=True)} == {
            "sess-now", "sess-other"}

    def test_unregistered_session_has_empty_title(self, service):
        service.query("demo", "main", "SELECT 1", CALLER)
        assert service.list_agent_sessions(CALLER)[0]["title"] == ""

    def test_date_and_keyword_filters(self, service):
        service.query("demo", "main", "SELECT * FROM users", CALLER)
        old = CallerInfo(agent="pytest/1.0", session_id="sess-old")
        service.query("demo", "main", "SELECT 1", old)
        _backdate(service.store, "sess-old", "2020-01-01T00:00:00.000+00:00")

        today = datetime.now().date().isoformat()
        assert [s["session_id"] for s in service.list_agent_sessions(CALLER, since=today)] == [
            "sess-now"]
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        assert [s["session_id"] for s in service.list_agent_sessions(
            CALLER, until=yesterday)] == ["sess-old"]
        assert [s["session_id"] for s in service.list_agent_sessions(
            CALLER, keyword="users")] == ["sess-now"]

    def test_writes_only(self, service):
        service.query("demo", "main", "SELECT 1", CALLER)
        writer = CallerInfo(agent="pytest/1.0", session_id="sess-write")
        service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", writer)
        got = service.list_agent_sessions(CALLER, writes_only=True)
        assert [s["session_id"] for s in got] == ["sess-write"]

    def test_writes_only_with_status_ok_means_really_changed(self, service):
        """「真正改成过数据的会话」= writes_only + status=ok（只提交没批的不算）。"""
        attempted = CallerInfo(agent="pytest/1.0", session_id="sess-attempted")
        service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", attempted)

        done = CallerInfo(agent="pytest/1.0", session_id="sess-done")
        r = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 2", done)
        service.approve_change(r["change_id"], decided_by="alice@ops")
        service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 2", done,
                        change_id=r["change_id"])

        # 不带 status：两个会话都「有写操作」，但其中一个其实没落库
        assert {s["session_id"] for s in service.list_agent_sessions(CALLER, writes_only=True)} == {
            "sess-attempted", "sess-done"}
        got = service.list_agent_sessions(CALLER, writes_only=True, status="ok")
        assert [s["session_id"] for s in got] == ["sess-done"]

    def test_bad_status_raises(self, service):
        with pytest.raises(ValueError, match="Unsupported status"):
            service.list_agent_sessions(CALLER, status="success")

    def test_bad_date_raises(self, service):
        with pytest.raises(ValueError):
            service.list_agent_sessions(CALLER, since="上周")


class TestServiceSessionHistory:
    def test_defaults_to_current_session(self, service):
        service.begin_session(CALLER, "排查订单", "复现 #123")
        service.query("demo", "main", "SELECT 1", CALLER)
        out = service.session_history(CALLER)
        assert out["session_id"] == "sess-now"
        assert out["title"] == "排查订单" and out["note"] == "复现 #123"
        assert out["count"] == 1
        assert out["operations"][0]["tool"] == "query"
        assert out["operations"][0]["connection"] == "demo/main"

    def test_requires_session_id_when_client_has_none(self, service):
        anon = CallerInfo(agent="pytest/1.0", session_id="")
        with pytest.raises(ValueError, match="list_sessions"):
            service.session_history(anon)

    def test_write_op_carries_change_id_and_rollback_note(self, service):
        """核心用例：改动前的旧值随审批单存下来，事后能从会话记录里取回。"""
        note = "id=1 改前 active=1；回滚 UPDATE users SET active = 1 WHERE id = 1"
        r1 = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1",
                             CALLER, reason="下线测试账号", rollback_note=note)
        cid = r1["change_id"]
        service.approve_change(cid, decided_by="alice@ops")
        r2 = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1",
                             CALLER, change_id=cid)
        assert r2["status"] == "executed"

        ops = service.session_history(CALLER, writes_only=True)["operations"]
        # 两条：首提（生成审批单，rejected）与核销执行（ok），都关联同一张审批单
        assert {o["change_id"] for o in ops} == {cid}
        executed = [o for o in ops if o["status"] == "ok"][0]
        assert executed["rollback_note"] == note
        assert executed["approval_status"] == "consumed"
        assert executed["row_count"] == 1

    def test_status_filters_to_executed_only(self, service):
        r = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
        service.approve_change(r["change_id"], decided_by="alice@ops")
        service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER,
                        change_id=r["change_id"])

        out = service.session_history(CALLER, writes_only=True)
        assert out["status_counts"] == {"ok": 1, "rejected": 1}   # 首提那条是 rejected

        done = service.session_history(CALLER, writes_only=True, status="ok")
        assert [o["status"] for o in done["operations"]] == ["ok"]
        assert done["operations"][0]["row_count"] == 1

    def test_bad_status_rejected_with_options(self, service):
        with pytest.raises(ValueError, match="Unsupported status"):
            service.session_history(CALLER, status="executed")

    def test_writes_only_filters_reads(self, service):
        service.query("demo", "main", "SELECT 1", CALLER)
        service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
        assert len(service.session_history(CALLER)["operations"]) == 2
        assert [o["tool"] for o in service.session_history(
            CALLER, writes_only=True)["operations"]] == ["execute"]

    def test_long_sql_truncated(self, service):
        long_sql = "SELECT 1 -- " + "x" * (service.HISTORY_SQL_MAX_CHARS + 500)
        service.query("demo", "main", long_sql, CALLER)
        sql = service.session_history(CALLER, fields="sql")["operations"][0]["sql"]
        assert sql.endswith("... (truncated)")
        assert len(sql) < len(long_sql)

    def test_failed_op_detail_is_opt_in(self, service):
        service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
        op = service.session_history(CALLER)["operations"][0]
        assert op["status"] == "rejected"
        assert "detail" not in op          # 明细默认不给
        op = service.session_history(CALLER, fields="status,detail")["operations"][0]
        assert "审批单" in op["detail"]


class TestServiceHistoryFields:
    def test_sql_not_returned_by_default(self, service):
        service.query("demo", "main", "SELECT * FROM users", CALLER)
        out = service.session_history(CALLER)
        assert "sql" not in out["operations"][0]
        assert out["fields"] == list(service.HISTORY_DEFAULT_FIELDS)
        assert "fields=" in out["hint"]     # 提示 agent 怎么把 SQL 取出来

    def test_explicit_fields_returns_exactly_those(self, service):
        service.query("demo", "main", "SELECT * FROM users", CALLER)
        op = service.session_history(CALLER, fields="ts,sql")["operations"][0]
        assert set(op) == {"ts", "sql"}
        # 审计里存的是实际发给 DB 的 SQL（读查询会被注入兜底 LIMIT）
        assert op["sql"].startswith("SELECT * FROM users")

    def test_all_returns_everything_present(self, service):
        service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1",
                        CALLER, rollback_note="改前 active=1")
        out = service.session_history(CALLER, fields="all")
        op = out["operations"][0]
        assert {"sql", "detail", "change_id", "rollback_note", "fingerprint"} <= set(op)
        assert "hint" not in out

    def test_unknown_field_rejected_with_options(self, service):
        service.query("demo", "main", "SELECT 1", CALLER)
        with pytest.raises(ValueError, match="Unsupported field"):
            service.session_history(CALLER, fields="ts,secret_column")

    def test_empty_values_are_omitted(self, service):
        """只读操作没有审批单：change_id/rollback_note 等空列不占位。"""
        service.query("demo", "main", "SELECT 1", CALLER)
        op = service.session_history(CALLER)["operations"][0]
        assert "change_id" not in op and "rollback_note" not in op

    def test_rollback_note_surfaced_in_change_status(self, service):
        r = service.execute("demo", "main", "DELETE FROM users WHERE id = 2", CALLER,
                            rollback_note="id=2 是 bob；回滚需重新 INSERT")
        from dbmcp.service import change_status_payload
        payload = change_status_payload(service.get_change(r["change_id"]))
        assert payload["rollback_note"].startswith("id=2 是 bob")

    def test_no_rollback_note_means_no_key(self, service):
        r = service.execute("demo", "main", "DELETE FROM users WHERE id = 2", CALLER)
        from dbmcp.service import change_status_payload
        assert "rollback_note" not in change_status_payload(service.get_change(r["change_id"]))


# ---------------- MCP 工具注册 + 审批页展示 ----------------

@pytest.fixture
def client(tmp_path, service):
    mcp = build_mcp(service)
    mount_admin(mcp, service, admin_token=TOKEN)
    with TestClient(mcp.http_app()) as tc:
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc, mcp


class TestToolsAndAdmin:
    def test_approval_page_shows_rollback_note(self, client, service):
        tc, _ = client
        r = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1",
                            CALLER, rollback_note="id=1 改前 active=1")
        page = tc.get(f"/admin/approvals/{r['change_id']}").text
        assert "回滚参考" in page
        assert "id=1 改前 active=1" in page

    def test_approval_page_without_note(self, client, service):
        tc, _ = client
        r = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
        assert "回滚参考" not in tc.get(f"/admin/approvals/{r['change_id']}").text


@pytest.mark.anyio
async def test_tools_over_mcp_protocol(service):
    """走真实 MCP 协议调一遍：工具注册、参数、返回结构都对得上。"""
    from fastmcp import Client

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        names = {item["name"] for item in (await c.call_tool("list_capabilities", {})).data}
        assert {"list_sessions", "session_history"} <= names

        await c.call_tool("begin_session", {"title": "排查订单重复扣款"})
        r = await c.call_tool("execute", {
            "project": "demo", "connection": "main",
            "sql": "UPDATE users SET active = 0 WHERE id = 1",
            "rollback_note": "id=1 改前 active=1；回滚 UPDATE users SET active=1 WHERE id=1",
            "wait_seconds": 0,
        })
        cid = r.data["change_id"]

        sessions = await c.call_tool("list_sessions", {"writes_only": True})
        sids = [s["session_id"] for s in sessions.data]
        assert len(sids) == 1 and sessions.data[0]["title"] == "排查订单重复扣款"

        hist = await c.call_tool("session_history", {
            "session_id": sids[0], "writes_only": True})
        op = hist.data["operations"][0]
        assert hist.data["status_counts"] == {"rejected": 1}   # 只提交了、还没批
        assert op["change_id"] == cid
        assert op["rollback_note"].startswith("id=1 改前 active=1")
        assert "sql" not in op                      # 默认不带 SQL 原文
        assert "sql" in (await c.call_tool("session_history", {
            "session_id": sids[0], "fields": "sql"})).data["operations"][0]

        # 只看真正落地的改动：这单还没批准，故为空
        assert (await c.call_tool("session_history", {
            "session_id": sids[0], "writes_only": True, "status": "ok"})).data["count"] == 0
