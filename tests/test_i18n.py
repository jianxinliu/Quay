"""语言机制：agent 恒英文（MCP 中间件），后台按设置（guard），服务层只管 t()。"""

from __future__ import annotations

import anyio
import pytest

from dbmcp.i18n import current_locale, register, t, use_locale

register({"test.hello": ("你好，{name}", "Hello, {name}"), "test.plain": ("纯文本", "plain")})


class TestCatalog:
    def test_default_zh_and_switch(self):
        assert current_locale() == "zh"
        assert t("test.hello", name="A") == "你好，A"
        with use_locale("en"):
            assert t("test.hello", name="A") == "Hello, A"
            assert t("test.plain") == "plain"
        assert t("test.plain") == "纯文本"          # 退出后恢复

    def test_unknown_key_and_bad_locale_do_not_raise(self):
        assert t("nope.missing") == "nope.missing"
        with use_locale("fr"):                       # 非法值按中文
            assert t("test.plain") == "纯文本"
        assert t("test.hello") == "你好，{name}"      # 缺参数：原样返回，不抛

    def test_duplicate_key_with_different_text_rejected(self):
        with pytest.raises(ValueError):
            register({"test.plain": ("别的", "other")})
        register({"test.plain": ("纯文本", "plain")})   # 同文案重复登记无害

    def test_locale_propagates_into_worker_threads(self):
        """服务层在 anyio.to_thread 里跑，contextvar 必须跟进线程，否则 agent 会收到中文。"""
        async def main():
            with use_locale("en"):
                return await anyio.to_thread.run_sync(lambda: t("test.plain"))
        assert anyio.run(main) == "plain"


class TestEntryPoints:
    def test_mcp_tool_call_sees_english(self):
        from fastmcp import Client, FastMCP

        from dbmcp.server import _AgentLocale

        mcp = FastMCP("x")
        mcp.add_middleware(_AgentLocale())

        @mcp.tool
        def hello() -> str:
            return t("test.plain")

        async def main():
            async with Client(mcp) as c:
                return (await c.call_tool("hello")).content[0].text
        assert anyio.run(main) == "plain"
        assert t("test.plain") == "纯文本"     # 中间件退出后不影响进程默认

    def test_admin_guard_follows_setting(self, tmp_path):
        import sqlite3

        from starlette.responses import PlainTextResponse
        from starlette.testclient import TestClient

        from dbmcp.admin import mount_admin
        from dbmcp.approvals import ApprovalStore
        from dbmcp.audit.log import AuditStore
        from dbmcp.config import AppConfig
        from dbmcp.server import build_mcp
        from dbmcp.service import DbmService
        from dbmcp.settings import SettingsStore

        db = tmp_path / "b.sqlite3"
        sqlite3.connect(db).close()
        cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db), "environment": "dev"}}}}})
        svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
        svc.settings = SettingsStore(tmp_path / "s.sqlite3")
        mcp = build_mcp(svc)
        mount_admin(mcp, svc, admin_token="tok")

        # 挂一条走 guard 的探针路由，读当前语言下的文案
        from dbmcp.admin.context import build_context
        ctx = build_context(mcp, svc, "tok")

        @mcp.custom_route("/admin/_probe_locale", methods=["GET"])
        @ctx.guard
        async def _probe(_req):  # noqa: ANN001, ANN202
            return PlainTextResponse(t("test.plain"))

        with TestClient(mcp.http_app()) as tc:
            tc.post("/admin/login", data={"token": "tok"})
            assert tc.get("/admin/_probe_locale").text == "纯文本"
            svc.save_settings({"text_language": "en"})
            assert tc.get("/admin/_probe_locale").text == "plain"
            svc.save_settings({"text_language": "zh"})
            assert tc.get("/admin/_probe_locale").text == "纯文本"
        svc.close()
