"""检验共享文案已按调用路径路由：agent（locale=en）恒英文，
后台默认（locale=zh）与既有中文界面字节一致。

覆盖模块：audit/classify.py、audit/risk.py、approvals.py、engines.py、checkup.py、
workflows.py、analysis.py（sync.py 与 service.py 在此前已是纯英文，见 CLAUDE.md）。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from dbmcp.approvals import ApprovalError, ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.audit.classify import classify
from dbmcp.audit.risk import assess
from dbmcp.config import AppConfig
from dbmcp.i18n import use_locale
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="s-i18n")

_CJK_RE = re.compile(r"[一-鿿]")


def _no_cjk(text: str) -> bool:
    return _CJK_RE.search(text) is None


# ---------------- 每个模块一条代表性断言：locale=en 英文，默认 zh 不变 ----------------

class TestClassify:
    def test_multi_statement_reason(self):
        v = classify("select 1; select 2", "mysql")
        assert v.reason == "多语句批量提交（2 条：Select、Select），按写操作进审批流"
        with use_locale("en"):
            v2 = classify("select 1; select 2", "mysql")
        assert v2.reason == ("Batch of 2 statements (Select, Select); routed to approval "
                             "as a write operation")


class TestRisk:
    def test_no_where_delete_reason(self):
        report = assess("DELETE FROM users", "mysql", lambda _t: None)
        assert report.reasons == ["无 WHERE 条件的 Delete 会影响全表所有行"]
        with use_locale("en"):
            report_en = assess("DELETE FROM users", "mysql", lambda _t: None)
        assert report_en.reasons == ["Delete without a WHERE clause affects every row in the table"]


class TestApprovals:
    def test_not_found_error(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        try:
            with pytest.raises(ApprovalError, match="审批单 #999 不存在"):
                store.get(999)
            with use_locale("en"):
                with pytest.raises(ApprovalError, match="Change #999 does not exist"):
                    store.get(999)
        finally:
            store.close()


class TestEngines:
    def test_table_not_found(self, tmp_path):
        from sqlalchemy import create_engine
        from dbmcp import engines

        db = tmp_path / "t.sqlite3"
        sqlite3.connect(db).close()
        engine = create_engine(f"sqlite:///{db}")
        with pytest.raises(ValueError, match="表 'missing' 不存在"):
            engines.sample_rows(engine, "missing", 10)
        with use_locale("en"):
            with pytest.raises(ValueError, match=r"Table 'missing' does not exist"):
                engines.sample_rows(engine, "missing", 10)


class TestWorkflows:
    def test_node_missing_input(self):
        from dbmcp.workflows import WorkflowError, compile_graph

        graph = {"nodes": [{"id": "n1", "type": "filter", "name": "f1", "cfg": {"where": "x=1"}}],
                 "edges": []}
        with pytest.raises(WorkflowError, match="节点「f1」缺少输入连线"):
            compile_graph(graph)
        with use_locale("en"):
            with pytest.raises(WorkflowError, match='Node "f1" is missing an input connection'):
                compile_graph(graph)


class TestAnalysis:
    def test_workspace_not_found(self, tmp_path):
        from dbmcp.analysis import AnalysisError, AnalysisStore

        store = AnalysisStore(tmp_path / "analysis")
        with pytest.raises(AnalysisError, match="工作区 'nope' 不存在"):
            store.drop_workspace("nope")
        with use_locale("en"):
            with pytest.raises(AnalysisError, match="Workspace 'nope' does not exist"):
                store.drop_workspace("nope")


class TestCheckup:
    def test_sqlite_integrity_check(self, tmp_path):
        from sqlalchemy import create_engine
        from dbmcp import checkup

        db = tmp_path / "c.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        engine = create_engine(f"sqlite:///{db}")

        report = checkup.run_checkup(engine, "sqlite")
        by_name = {c.name: c for c in report.checks}
        assert by_name["integrity"].title == "完整性检查"
        assert by_name["integrity"].value == "正常"

        with use_locale("en"):
            report_en = checkup.run_checkup(engine, "sqlite")
        by_name_en = {c.name: c for c in report_en.checks}
        assert by_name_en["integrity"].title == "Integrity Check"
        assert _no_cjk(by_name_en["integrity"].title)
        assert _no_cjk(by_name_en["integrity"].value)
        assert _no_cjk(by_name_en["integrity"].message)


# ---------------- MCP 协议层：agent 侧调用不泄漏中文 ----------------

@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO users (name) VALUES ('alice'), ('bob');"
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(db_file), "environment": "dev",
        "writer": {"user": "x", "password": "plain://unused"},
    }}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
    yield svc
    svc.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _all_text(result) -> str:
    """把一次 call_tool 结果里能拿到的文本都拼起来（结构化 data + 文本块）供扫描。"""
    parts = []
    for b in result.content:
        if hasattr(b, "text"):
            parts.append(b.text)
    if result.data is not None:
        parts.append(repr(result.data))
    return "\n".join(parts)


@pytest.mark.anyio
async def test_describe_table_missing_table_no_cjk(service):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        with pytest.raises(ToolError) as ei:
            await c.call_tool("describe_table",
                              {"project": "demo", "connection": "main", "table": "missing"})
    # agent_error() passes ValueError through verbatim (server.py: `isinstance(e, (QueryRejected,
    # ValueError)): return ToolError(str(e))`), so this really does exercise engines.py's
    # translated message, not some other category text.
    assert "does not exist" in str(ei.value)
    assert _no_cjk(str(ei.value)), str(ei.value)


@pytest.mark.anyio
async def test_execute_write_rejection_no_cjk(service):
    """写操作首提生成审批单：返回体（含风险报告）不得含中文。"""
    from fastmcp import Client

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        r = await c.call_tool("execute", {
            "project": "demo", "connection": "main",
            "sql": "DELETE FROM users",  # 无 WHERE，risk.reasons 里原文含中文最容易漏
            "wait_seconds": 0,
        })
    assert r.data["status"] == "approval_required"
    assert _no_cjk(_all_text(r)), _all_text(r)


@pytest.mark.anyio
async def test_db_checkup_no_cjk(service):
    from fastmcp import Client

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        r = await c.call_tool("db_checkup", {"project": "demo", "connection": "main"})
    assert _no_cjk(_all_text(r)), _all_text(r)


# ---------------- 后台侧：审批页仍按 text_language 设置渲染 ----------------

@pytest.mark.anyio
async def test_admin_approval_page_follows_admin_locale_not_caller(tmp_path):
    """agent 调 execute（locale=en）生成的审批单，后台默认设置（zh）下审批页仍显示中文
    风险理由；把 text_language 设为 en 后新生成的审批单，审批页显示英文。"""
    from starlette.testclient import TestClient

    from dbmcp.admin import mount_admin
    from dbmcp.settings import SettingsStore

    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(db_file), "environment": "dev",
        "writer": {"user": "x", "password": "plain://unused"},
    }}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
    svc.settings = SettingsStore(tmp_path / "s.sqlite3")
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token="tok")

    with use_locale("en"):
        result_zh_admin = svc.execute("demo", "main", "DELETE FROM users", CALLER)
    change_id_1 = result_zh_admin["change_id"]

    svc.save_settings({"text_language": "en"})
    with use_locale("en"):
        result_en_admin = svc.execute("demo", "main", "DELETE FROM users", CALLER)
    change_id_2 = result_en_admin["change_id"]

    with TestClient(mcp.http_app()) as tc:
        tc.post("/admin/login", data={"token": "tok"})
        page1 = tc.get(f"/admin/approvals/{change_id_1}").text
        assert "无 WHERE 条件的 Delete 会影响全表所有行" in page1

        page2 = tc.get(f"/admin/approvals/{change_id_2}").text
        assert "Delete without a WHERE clause affects every row in the table" in page2
    svc.close()
