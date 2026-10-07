"""Small default tool surface and on-demand access to the remaining capabilities."""

import sqlite3

import pytest
from fastmcp import Client

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import DbmService


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "db.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT)")
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(path), "environment": "dev",
    }}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"),
                     ApprovalStore(tmp_path / "audit.sqlite3"))
    yield svc
    svc.close()


@pytest.mark.anyio
async def test_catalog_hides_rare_tools_and_exposes_description_schema_and_call(service):
    async with Client(build_mcp(service)) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert len(names) == 10
        assert {"list_projects", "query", "execute", "list_capabilities",
                "capability_detail", "call_capability"} <= names
        assert "list_tables" not in names
        assert "sync_table" not in names
        catalog = (await client.call_tool("list_capabilities", {})).data
        assert any(item["name"] == "list_tables" and item["summary"] for item in catalog)
        detail = (await client.call_tool("capability_detail", {"name": "list_tables"})).data
        assert detail["name"] == "list_tables"
        assert "project" in detail["input_schema"]["properties"]
        result = (await client.call_tool("call_capability", {"name": "list_tables",
                              "arguments": {"project": "demo", "connection": "main"}})).data
        assert "items" in str(result)
        assert service.store.recent()[0]["session_id"]


@pytest.mark.anyio
async def test_first_call_hint_survives_gateway_dispatch(service):
    from dbmcp.guide import FIRST_CALL_GUIDE

    async with Client(build_mcp(service)) as client:
        result = await client.call_tool("call_capability", {"name": "list_tables",
            "arguments": {"project": "demo", "connection": "main"}})
        assert any(FIRST_CALL_GUIDE in getattr(part, "text", "") for part in result.content)


@pytest.mark.anyio
async def test_catalog_rejects_unknown_and_recursive_capability(service):
    async with Client(build_mcp(service)) as client:
        with pytest.raises(Exception, match="Unknown capability"):
            await client.call_tool("capability_detail", {"name": "missing"})
        with pytest.raises(Exception, match="cannot be called"):
            await client.call_tool("call_capability", {"name": "call_capability",
                                "arguments": {"name": "list_projects"}})
