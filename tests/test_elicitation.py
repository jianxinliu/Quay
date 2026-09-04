"""elicitation 快捷审批测试：走真实 MCP 协议（in-memory），客户端用 handler 模拟人。"""

import sqlite3

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import DbmService


def make_service(tmp_path, environment: str):
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
            "engine": "sqlite", "database": str(db_file), "environment": environment,
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    service = DbmService(
        cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3")
    )
    service.data_dir = str(tmp_path / "data")
    service.base_url = "http://127.0.0.1:8100"
    return service


SEEN: list[dict] = []   # 记下弹窗正文与 schema，供「一屏能看完 / 无必填项」的断言用


def approve_handler(message, response_type, params, context):
    """模拟人直接点 Accept：不填任何字段（decision 有默认值，客户端可以什么都不回）。"""
    SEEN.append({"message": message, "schema": params.requestedSchema})

    async def _inner():
        return ElicitResult(action="accept", content={})
    return _inner()


def deny_handler(message, response_type, params, context):
    async def _inner():
        return ElicitResult(action="decline")
    return _inner()


def deny_via_field_handler(message, response_type, params, context):
    """点了 Accept 但把字段选成 deny：按字段的意思驳回。"""
    async def _inner():
        return ElicitResult(action="accept", content={"decision": "deny"})
    return _inner()


@pytest.mark.anyio
async def test_elicitation_approve_executes_immediately(tmp_path):
    svc = make_service(tmp_path, environment="dev")  # dev → elicitation 默认开
    mcp = build_mcp(svc)
    try:
        async with Client(mcp, elicitation_handler=approve_handler) as c:
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main",
                "sql": "UPDATE users SET active = 0 WHERE id = 1", "reason": "t",
            })
            assert r.data["status"] == "executed"
            assert r.data["affected_rows"] == 1
            # 审批单留痕：elicitation 批准后立即核销
            change = svc.get_change(r.data["change_id"])
            assert change.status == "consumed"
            assert change.decided_by.startswith("elicitation:")
    finally:
        svc.close()


@pytest.mark.anyio
async def test_elicitation_form_has_no_required_field(tmp_path):
    """弹窗不能有必填项：否则客户端显示「Value: not set / This field is required」，
    人得先去把字段填上才允许 Accept，多一步且看着像出错。"""
    svc = make_service(tmp_path, environment="dev")
    mcp = build_mcp(svc)
    SEEN.clear()
    try:
        async with Client(mcp, elicitation_handler=approve_handler) as c:
            await c.call_tool("execute", {
                "project": "demo", "connection": "main",
                "sql": "UPDATE users SET active = 0 WHERE id = 1",
            })
        schema = SEEN[-1]["schema"]
        assert not schema.get("required")
        assert schema["properties"]["decision"]["default"] == "approve"
    finally:
        svc.close()


@pytest.mark.anyio
async def test_elicitation_message_stays_one_screen(tmp_path):
    """超长语句（如整份同步计划带 CREATE TABLE）必须被截断：客户端把 message 原样打在
    终端里，撑满屏幕会把 Accept/Decline 顶出可视区，人根本没法操作。"""
    svc = make_service(tmp_path, environment="dev")
    mcp = build_mcp(svc)
    SEEN.clear()
    long_sql = "UPDATE users SET name = 'x' WHERE id = 1 AND (" + " OR ".join(
        f"id = {i}" for i in range(200)) + ")"
    try:
        async with Client(mcp, elicitation_handler=approve_handler) as c:
            await c.call_tool("execute", {
                "project": "demo", "connection": "main", "sql": long_sql,
            })
        message = SEEN[-1]["message"]
        assert len(message.splitlines()) <= 12
        assert len(message) < 800
        assert "已截断" in message
        assert "/admin/approvals/" in message   # 完整内容去审批页看
    finally:
        svc.close()


@pytest.mark.anyio
async def test_elicitation_deny_field_rejects(tmp_path):
    """人点了 Accept 但把字段选成 deny：按字段的意思驳回，不能当批准执行。"""
    svc = make_service(tmp_path, environment="dev")
    mcp = build_mcp(svc)
    try:
        async with Client(mcp, elicitation_handler=deny_via_field_handler) as c:
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main",
                "sql": "DELETE FROM users WHERE id = 2",
            })
            assert r.data["status"] == "rejected"
    finally:
        svc.close()


@pytest.mark.anyio
async def test_elicitation_deny_rejects(tmp_path):
    svc = make_service(tmp_path, environment="dev")
    mcp = build_mcp(svc)
    try:
        async with Client(mcp, elicitation_handler=deny_handler) as c:
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main",
                "sql": "DELETE FROM users WHERE id = 2",
            })
            assert r.data["status"] == "rejected"
            assert "会话内拒绝" in svc.get_change(r.data["change_id"]).decision_note
            # 数据没被删
            q = await c.call_tool("query", {"project": "demo", "connection": "main",
                                            "sql": "SELECT count(*) FROM users"})
            # query 现在返回紧凑 TSV 文本：末行是唯一数据行（count 值）
            body = [ln for ln in q.data.splitlines() if not ln.startswith("#")]
            assert body[-1] == "2"
    finally:
        svc.close()


@pytest.mark.anyio
async def test_prod_environment_skips_elicitation(tmp_path):
    svc = make_service(tmp_path, environment="prod")  # prod → elicitation 默认关
    mcp = build_mcp(svc)
    try:
        async with Client(mcp, elicitation_handler=approve_handler) as c:
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main",
                "sql": "UPDATE users SET active = 0 WHERE id = 1",
                "wait_seconds": 0,  # 不等人决策，直接断言首提结果
            })
            # 即使客户端支持 elicitation，prod 也走审批单流程
            assert r.data["status"] == "approval_required"
            assert svc.get_change(r.data["change_id"]).status == "pending"
    finally:
        svc.close()


@pytest.mark.anyio
async def test_client_without_elicitation_falls_back(tmp_path):
    svc = make_service(tmp_path, environment="dev")
    mcp = build_mcp(svc)
    try:
        async with Client(mcp) as c:  # 无 handler → 客户端不支持 elicitation
            r = await c.call_tool("execute", {
                "project": "demo", "connection": "main",
                "sql": "UPDATE users SET active = 0 WHERE id = 1",
                "wait_seconds": 0,
            })
            assert r.data["status"] == "approval_required"  # 回退审批单
    finally:
        svc.close()


@pytest.mark.anyio
async def test_export_table_returns_download_link_without_file_content(tmp_path):
    svc = make_service(tmp_path, environment="dev")
    mcp = build_mcp(svc)
    try:
        async with Client(mcp) as c:
            tools = await c.list_tools()
            assert {"list_databases", "export_table"} <= {tool.name for tool in tools}
            result = await c.call_tool("export_table", {
                "project": "demo",
                "connection": "main",
                "table": "users",
                "fields": ["id", "name"],
                "limit": 1,
                "format": "csv",
            })
            assert result.data["row_count"] == 1
            assert result.data["download_url"].startswith(
                "http://127.0.0.1:8100/exports/"
            )
            assert result.data["byte_size"] > 0
            assert "不要读取" in result.data["agent_instruction"]
            assert all(block.type != "resource" for block in result.content)
            # tool 文本/结构化结果都不能夹带导出正文
            assert all("id,name" not in getattr(block, "text", "") for block in result.content)
    finally:
        svc.close()
