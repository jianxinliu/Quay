"""审批等待闭环：agent 提交写操作后服务端等人决策，批准即自动执行。

去掉的是原来最别扭的两步——「人回 CLI 说一句已批准」和「agent 再重提一次 SQL」。
覆盖三条路径：
- 后台「仅批准」→ 等待中的 agent 自动带 change_id 重提、核销执行；
- 后台「批准并立即执行」→ 变更当场落地，agent 从 exec_result 收结果、不再重提；
- 超时 / 被拒绝 → agent 拿到明确状态，审批单语义（TTL、一次性核销）不变。
"""

import sqlite3
from collections.abc import Callable

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from dbmcp.approvals import ApprovalError, ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")
WRITE_SQL = "UPDATE users SET active = 0 WHERE id = 1"


def make_service(tmp_path) -> DbmService:
    """prod 环境：elicitation 默认关，写操作必然走审批单，与客户端能力无关。"""
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
            "engine": "sqlite", "database": str(db_file), "environment": "prod",
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    svc = DbmService(
        cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3")
    )
    svc.data_dir = str(tmp_path / "data")
    svc.base_url = "http://127.0.0.1:8100"
    return svc


def active_of(svc: DbmService, user_id: int) -> int:
    path = svc.config.get_connection("demo", "main").database
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()[0]


async def decide_when_pending(svc: DbmService, action: Callable[[int], object]) -> None:
    """等审批单一出现就替「人」做决策，模拟用户在后台点按钮。"""
    for _ in range(200):
        pending = svc.list_changes("pending")
        if pending:
            action(pending[0].id)
            return
        await anyio.sleep(0.02)
    raise AssertionError("审批单一直没出现")


# ---------- 存储层：执行结果回填 + 老库迁移 ----------

def test_record_execution_roundtrip(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite3")
    change = store.create(
        project="demo", connection="main", environment="prod", engine="sqlite",
        sql=WRITE_SQL, fingerprint="fp", reason="", risk_level="HIGH",
        risk_report={}, agent="pytest", session_id="s1",
    )
    assert store.get(change.id).exec_result is None
    store.record_execution(change.id, {"affected_rows": 3, "duration_ms": 7,
                                       "executed_by": "admin-ui"})
    assert store.get(change.id).exec_result == {"affected_rows": 3, "duration_ms": 7,
                                                "executed_by": "admin-ui"}
    store.close()


def test_old_db_migrates_exec_result_column(tmp_path):
    """exec_result 是后加的列：老库打开时自动 ALTER，不能崩也不能丢数据。"""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE change_request (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL, expires_at TEXT NOT NULL, project TEXT NOT NULL,"
        " connection TEXT NOT NULL, environment TEXT, engine TEXT, sql TEXT NOT NULL,"
        " fingerprint TEXT NOT NULL, reason TEXT, risk_level TEXT, risk_report TEXT,"
        " agent TEXT, session_id TEXT, status TEXT NOT NULL, decided_by TEXT,"
        " decided_at TEXT, decision_note TEXT);"
        "INSERT INTO change_request (created_at, expires_at, project, connection, sql,"
        " fingerprint, status) VALUES ('2026-01-01T00:00:00+00:00',"
        " '2099-01-01T00:00:00+00:00', 'demo', 'main', 'DELETE FROM t', 'fp', 'pending');"
    )
    conn.commit()
    conn.close()

    store = ApprovalStore(db)
    old = store.get(1)
    assert old.sql == "DELETE FROM t" and old.exec_result is None
    store.record_execution(1, {"affected_rows": 1})
    assert store.get(1).exec_result == {"affected_rows": 1}
    store.close()


# ---------- 服务层：审批链接 + 批准并执行 ----------

def test_approval_required_carries_clickable_url(tmp_path):
    """首提返回里要有审批页直达链接，agent 贴给用户即可，省掉「自己去后台找」。"""
    svc = make_service(tmp_path)
    try:
        result = svc.execute("demo", "main", WRITE_SQL, CALLER)
        assert result["status"] == "approval_required"
        assert result["approval_url"] == \
            f"http://127.0.0.1:8100/admin/approvals/{result['change_id']}"
        assert result["approval_url"] in result["message"]
    finally:
        svc.close()


def test_approve_and_execute_runs_stored_sql(tmp_path):
    svc = make_service(tmp_path)
    try:
        cid = svc.execute("demo", "main", WRITE_SQL, CALLER)["change_id"]
        out = svc.approve_and_execute_change(cid, decided_by="human@localhost", note="ok")

        assert out["status"] == "executed" and out["affected_rows"] == 1
        assert out["executed_by"] == "admin-ui"
        assert active_of(svc, 1) == 0                      # 真的改了库
        change = svc.get_change(cid)
        assert change.status == "consumed"                 # 一次性核销
        assert change.decided_by == "human@localhost"
        assert change.exec_result["affected_rows"] == 1    # 回填给等待中的 agent
    finally:
        svc.close()


def test_approve_and_execute_refuses_non_pending(tmp_path):
    """已决策的审批单不能再「批准并执行」（沿用 approve 的状态机校验）。"""
    svc = make_service(tmp_path)
    try:
        cid = svc.execute("demo", "main", WRITE_SQL, CALLER)["change_id"]
        svc.reject_change(cid, decided_by="human", note="不批")
        with pytest.raises(ApprovalError):
            svc.approve_and_execute_change(cid, decided_by="human")
        assert active_of(svc, 1) == 1  # 没执行
    finally:
        svc.close()


# ---------- MCP 层：execute 内联等待 / wait_for_change ----------

@pytest.mark.anyio
async def test_execute_waits_then_executes_after_approval(tmp_path):
    """人在后台点「仅批准」→ 等待中的 execute 自动重提执行，无需人回会话说一句。"""
    svc = make_service(tmp_path)
    mcp = build_mcp(svc)
    try:
        async with Client(mcp) as c, anyio.create_task_group() as tg:
            tg.start_soon(decide_when_pending, svc,
                          lambda cid: svc.approve_change(cid, decided_by="human", note="ok"))
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main", "sql": WRITE_SQL,
                "wait_seconds": 20,
            })
        assert r.data["status"] == "executed" and r.data["affected_rows"] == 1
        assert active_of(svc, 1) == 0
        assert svc.get_change(r.data["change_id"]).status == "consumed"
    finally:
        svc.close()


@pytest.mark.anyio
async def test_execute_wait_reports_backend_execution(tmp_path):
    """人点「批准并立即执行」→ agent 收到执行结果，且不会再重提（重提会被拒）。"""
    svc = make_service(tmp_path)
    mcp = build_mcp(svc)
    try:
        async with Client(mcp) as c, anyio.create_task_group() as tg:
            tg.start_soon(decide_when_pending, svc,
                          lambda cid: svc.approve_and_execute_change(cid, decided_by="human"))
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main", "sql": WRITE_SQL,
                "wait_seconds": 20,
            })
        assert r.data["status"] == "executed"
        assert r.data["affected_rows"] == 1
        assert "管理后台" in r.data["message"]
        assert active_of(svc, 1) == 0
        # 只执行了一次：审批单已核销，agent 再重提会被拒
        again = svc.execute("demo", "main", WRITE_SQL, CALLER, change_id=r.data["change_id"])
        assert again["status"] == "rejected"
    finally:
        svc.close()


@pytest.mark.anyio
async def test_execute_wait_timeout_keeps_approval_open(tmp_path):
    """等待超时不是失败：审批单仍有效，agent 据此提醒用户并继续等。"""
    svc = make_service(tmp_path)
    mcp = build_mcp(svc)
    try:
        async with Client(mcp) as c:
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main", "sql": WRITE_SQL,
                "wait_seconds": 1,
            })
        assert r.data["status"] == "approval_required"
        assert r.data["waited_seconds"] == 1
        assert "wait_for_change" in r.data["message"]
        assert svc.get_change(r.data["change_id"]).status == "pending"
        assert active_of(svc, 1) == 1
    finally:
        svc.close()


@pytest.mark.anyio
async def test_execute_wait_disabled_returns_immediately(tmp_path):
    """wait_seconds=0（或设置为 0）：保持原有的「立即返回审批单号」行为。"""
    svc = make_service(tmp_path)
    mcp = build_mcp(svc)
    try:
        async with Client(mcp) as c:
            with anyio.fail_after(5):  # 不该有任何等待
                r = await c.call_tool("execute", {
                    "project": "demo", "connection": "main", "sql": WRITE_SQL,
                    "wait_seconds": 0,
                })
        assert r.data["status"] == "approval_required"
        assert "waited_seconds" not in r.data
    finally:
        svc.close()


@pytest.mark.anyio
async def test_execute_wait_returns_rejection_reason(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc)
    try:
        async with Client(mcp) as c, anyio.create_task_group() as tg:
            tg.start_soon(decide_when_pending, svc,
                          lambda cid: svc.reject_change(cid, decided_by="human", note="影响面太大"))
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main", "sql": WRITE_SQL,
                "wait_seconds": 20,
            })
        assert r.data["status"] == "rejected" and r.data["reason"] == "影响面太大"
        assert active_of(svc, 1) == 1
    finally:
        svc.close()


@pytest.mark.anyio
async def test_wait_for_change_returns_on_decision(tmp_path):
    """独立等待工具：execute 超时后接着等，批准即返回 approved 供 agent 重提。"""
    svc = make_service(tmp_path)
    mcp = build_mcp(svc)
    try:
        cid = svc.execute("demo", "main", WRITE_SQL, CALLER)["change_id"]
        async with Client(mcp) as c, anyio.create_task_group() as tg:
            tg.start_soon(decide_when_pending, svc,
                          lambda i: svc.approve_change(i, decided_by="human", note="go"))
            r = await c.call_tool("wait_for_change", {"change_id": cid, "timeout_seconds": 20})
        assert r.data["status"] == "approved" and r.data["decision_note"] == "go"
        assert r.data["change_id"] == cid
    finally:
        svc.close()


@pytest.mark.anyio
async def test_wait_for_change_times_out_as_pending(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc)
    try:
        cid = svc.execute("demo", "main", WRITE_SQL, CALLER)["change_id"]
        async with Client(mcp) as c:
            r = await c.call_tool("wait_for_change", {"change_id": cid, "timeout_seconds": 1})
        assert r.data["status"] == "pending" and r.data["timed_out"] is True
    finally:
        svc.close()


@pytest.mark.anyio
async def test_wait_for_change_unknown_id_errors_fast(tmp_path):
    """不存在的审批单立刻报错，不能空等到超时。"""
    svc = make_service(tmp_path)
    mcp = build_mcp(svc)
    try:
        async with Client(mcp) as c:
            with pytest.raises(ToolError), anyio.fail_after(5):
                await c.call_tool("wait_for_change", {"change_id": 9999,
                                                      "timeout_seconds": 60})
    finally:
        svc.close()
