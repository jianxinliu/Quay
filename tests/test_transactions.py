"""Staged SQL batches are reviewed once and executed atomically."""

import sqlite3

import pytest

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.service import CallerInfo, DbmService, QueryRejected


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "data.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT)")
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(path), "environment": "dev",
    }}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"),
                     ApprovalStore(tmp_path / "audit.sqlite3"))
    yield svc, path
    svc.close()


def test_agent_stages_without_writing_then_one_approval_executes_whole_batch(service):
    svc, path = service
    caller = CallerInfo("agent", "session-a")
    tid = svc.begin_transaction("demo", "main", caller)["transaction_id"]
    svc.add_transaction_sql(tid, "INSERT INTO items VALUES (1, 'a')", caller)
    svc.add_transaction_sql(tid, "INSERT INTO items VALUES (2, 'b')", caller)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    preview = svc.preview_transaction(tid, caller)
    assert preview["count"] == 2
    pending = svc.commit_transaction(tid, caller, reason="batch")
    assert pending["status"] == "approval_required"
    change = svc.approvals.get(pending["change_id"])
    assert "INSERT INTO items VALUES (1" in change.sql
    assert "INSERT INTO items VALUES (2" in change.sql
    svc.approvals.approve(change.id, "human")
    result = svc.execute("demo", "main", change.sql, caller, change_id=change.id)
    assert result["status"] == "executed"
    statement_logs = [r for r in svc.store.recent() if r["tool"] == "transaction_statement"]
    assert len(statement_logs) == 2
    assert {r["sql"] for r in statement_logs} == {
        "INSERT INTO items VALUES (1, 'a')", "INSERT INTO items VALUES (2, 'b')"}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT label FROM items ORDER BY id").fetchall() == [("a",), ("b",)]


def test_failed_batch_rolls_back_and_other_owner_cannot_use_draft(service):
    svc, path = service
    owner = CallerInfo("agent", "session-a")
    stranger = CallerInfo("agent", "session-b")
    tid = svc.begin_transaction("demo", "main", owner)["transaction_id"]
    with pytest.raises(QueryRejected):
        svc.add_transaction_sql(tid, "INSERT INTO items VALUES (1, 'x')", stranger)
    svc.add_transaction_sql(tid, "INSERT INTO items VALUES (1, 'x')", owner)
    svc.add_transaction_sql(tid, "INSERT INTO items VALUES (1, 'duplicate')", owner)
    pending = svc.commit_transaction(tid, owner)
    svc.approvals.approve(pending["change_id"], "human")
    change = svc.approvals.get(pending["change_id"])
    with pytest.raises(Exception):
        svc.execute("demo", "main", change.sql, owner, change_id=change.id)
    assert len([r for r in svc.store.recent() if r["tool"] == "transaction_statement"
                and r["status"] == "error"]) == 2
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def test_admin_preview_confirm_binding_and_discard(service):
    svc, path = service
    admin = CallerInfo("admin-ui", "browser-session")
    tid = svc.begin_transaction("demo", "main", admin)["transaction_id"]
    svc.add_transaction_sql(tid, "INSERT INTO items VALUES (1, 'a')", admin)
    svc.add_transaction_sql(tid, "INSERT INTO items VALUES (2, 'b')", admin)
    preview = svc.preview_transaction(tid, admin, admin=True)
    assert preview["kind"] == "confirm"
    with pytest.raises(QueryRejected):
        svc.commit_transaction(tid, admin, admin=True, expect_fingerprint="wrong")
    result = svc.commit_transaction(tid, admin, admin=True,
                                    expect_fingerprint=preview["fingerprint"])
    assert result["kind"] == "write"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    another = svc.begin_transaction("demo", "main", admin)["transaction_id"]
    svc.add_transaction_sql(another, "INSERT INTO items VALUES (3, 'c')", admin)
    assert svc.rollback_transaction(another, admin)["status"] == "discarded"
    with pytest.raises(QueryRejected):
        svc.preview_transaction(another, admin)


def test_housekeeping_expires_abandoned_drafts(service):
    svc, _ = service
    caller = CallerInfo("agent", "session-a")
    tid = svc.begin_transaction("demo", "main", caller)["transaction_id"]
    svc._transaction_drafts[tid]["updated"] -= 3601
    assert svc.housekeep_once()["transactions_reaped"] == 1
    with pytest.raises(QueryRejected):
        svc.add_transaction_sql(tid, "INSERT INTO items VALUES (1, 'a')", caller)


def test_admin_draft_rejects_syntax_error_before_any_statement_runs(service):
    svc, path = service
    admin = CallerInfo("admin-ui", "browser-session")
    tid = svc.begin_transaction("demo", "main", admin)["transaction_id"]
    svc.add_transaction_sql(tid, "INSERT INTO items VALUES (1, 'valid')", admin)
    with pytest.raises(QueryRejected):
        svc.add_transaction_sql(tid, "INSER INTO items VALUES (2, 'bad')", admin)
    assert svc.preview_transaction(tid, admin, admin=True)["count"] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


@pytest.mark.anyio
async def test_mcp_transaction_tool_stages_and_returns_one_change(service):
    from fastmcp import Client

    from dbmcp.server import build_mcp

    svc, path = service
    async with Client(build_mcp(svc)) as client:
        assert "transaction" in {tool.name for tool in await client.list_tools()}
        started = (await client.call_tool("transaction", {
            "action": "begin", "project": "demo", "connection": "main",
        })).data
        tid = started["transaction_id"]
        for i in (1, 2):
            await client.call_tool("transaction", {
                "action": "add", "project": "demo", "connection": "main",
                "transaction_id": tid, "sql": f"INSERT INTO items VALUES ({i}, 'v{i}')",
            })
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
        pending = (await client.call_tool("transaction", {
            "action": "commit", "project": "demo", "connection": "main",
            "transaction_id": tid, "wait_seconds": 0,
        })).data
        assert pending["status"] == "approval_required"
        assert pending["change_id"] > 0
