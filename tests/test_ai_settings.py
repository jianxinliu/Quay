"""AI 助手设置：模型列表拉取、连通性测试、设置页控件。

只测「人」的路径（后台页面 + 两个只读路由）；agent 侧不经过它们。
外部 HTTP 一律 monkeypatch，不真打接口、不真读钥匙串。
"""

import sqlite3

import httpx
import pytest
from starlette.testclient import TestClient

from dbmcp import ai
from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import DbmService
from dbmcp.settings import SettingsStore

TOKEN = "{{SECRET_3bc3e546}}"


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    sqlite3.connect(db_file).close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(db_file), "environment": "local",
    }}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     ApprovalStore(tmp_path / "a.sqlite3"))
    svc.settings = SettingsStore(tmp_path / "s.sqlite3")
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token=TOKEN)
    with TestClient(mcp.http_app()) as tc:
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc, svc
    svc.close()


class TestSettingsPage:
    def test_model_is_selectable_from_a_list(self, client):
        """模型字段是「输入框 + datalist」：拉到的列表可选，拉不到时仍能手填。"""
        tc, _ = client
        page = tc.get("/admin/settings?tab=ai").text
        assert "name='ai_model'" in page
        assert "list='ai-model-list'" in page
        assert "<datalist id='ai-model-list'>" in page
        assert "id='ai-model-refresh'" in page
        assert "id='ai-test'" in page

    def test_action_buttons_are_not_inside_the_dirty_bar(self, client):
        """两个动作按钮必须常驻可见。

        改动条 `.set-bar` 在没有未保存改动时是 opacity:0 + pointer-events:none，
        按钮放进去等于默认隐藏，用户得先随手改个字段才能发现——这是真实缺陷，
        用「按钮出现在改动条标记之前」钉住。
        """
        tc, _ = client
        for tab in ("ai", "notify"):
            page = tc.get(f"/admin/settings?tab={tab}").text
            bar = page.index("<div class='set-bar'>")
            btn = "id='ai-test'" if tab == "ai" else "id='notify-test'"
            assert page.index(btn) < bar, f"{tab}: 动作按钮落在改动条里"
            # 结果消息也要有自己的位置，别再写回改动条
            msg = "ai-act-msg" if tab == "ai" else "notify-act-msg"
            assert msg in page[:bar], f"{tab}: 动作结果应在分区内显示"

    def test_model_js_uses_admin_routes(self, client):
        tc, _ = client
        page = tc.get("/admin/settings?tab=ai").text
        assert "/admin/settings/ai/models" in page
        assert "/admin/settings/ai/probe" in page


class TestProbe:
    def test_cli_backend_reports_missing_binary(self, client):
        tc, svc = client
        svc.settings.save({"ai_provider": "claude", "ai_cli_path": "/nonexistent/claude-xyz"})
        body = tc.post("/admin/settings/ai/probe").json()
        assert body["ok"] is False
        assert "claude" in body["detail"]

    def test_api_backend_ok_with_models(self, client, monkeypatch):
        tc, svc = client
        svc.settings.save({"ai_provider": "api", "ai_api_format": "anthropic"})
        monkeypatch.setattr(ai, "_resolve_api_key", lambda _env: "sk-test")
        calls = {}

        def fake_get(url, headers=None, timeout=None):
            calls.update(url=url, headers=headers)

            class R:
                status_code = 200
                text = ""

                def json(self):
                    return {"data": [{"id": "claude-sonnet-5", "display_name": "Sonnet 5"},
                                     {"id": "claude-opus-5"}]}
            return R()

        monkeypatch.setattr("httpx.get", fake_get)
        body = tc.post("/admin/settings/ai/probe").json()
        assert body["ok"] is True and body["model_count"] == 2
        # Anthropic 走 x-api-key，不是 Bearer
        assert calls["headers"]["x-api-key"] == "sk-test"
        assert calls["url"].endswith("/v1/models")

    def test_api_backend_rejects_bad_key(self, client, monkeypatch):
        tc, svc = client
        svc.settings.save({"ai_provider": "api"})
        monkeypatch.setattr(ai, "_resolve_api_key", lambda _env: "sk-bad")

        def fake_get(url, headers=None, timeout=None):
            class R:
                status_code = 401
                text = '{"error":"invalid api key"}'
            return R()

        monkeypatch.setattr("httpx.get", fake_get)
        body = tc.post("/admin/settings/ai/probe").json()
        assert body["ok"] is False
        assert "401" in body["detail"]

    def test_missing_key_is_reported_not_raised(self, client, monkeypatch):
        tc, svc = client
        svc.settings.save({"ai_provider": "api"})
        monkeypatch.setattr(ai, "_resolve_api_key", lambda _env: "")
        body = tc.post("/admin/settings/ai/probe").json()
        assert body["ok"] is False
        assert "API key" in body["detail"]


class TestModelList:
    def test_returns_deduped_sorted_models_with_names(self, client, monkeypatch):
        tc, svc = client
        svc.settings.save({"ai_provider": "api", "ai_api_format": "openai"})
        monkeypatch.setattr(ai, "_resolve_api_key", lambda _env: "sk-test")
        seen = {}

        def fake_get(url, headers=None, timeout=None):
            seen.update(headers=headers)

            class R:
                status_code = 200
                text = ""

                def json(self):
                    return {"data": [{"id": "gpt-b"}, {"id": "gpt-a", "name": "A"},
                                     {"id": "gpt-b"}, {"id": ""}, "junk"]}
            return R()

        monkeypatch.setattr("httpx.get", fake_get)
        body = tc.get("/admin/settings/ai/models").json()
        assert body["ok"] is True
        assert [m["id"] for m in body["models"]] == ["gpt-a", "gpt-b"]
        assert body["models"][0]["name"] == "A"
        assert body["models"][1]["name"] == "gpt-b"   # 没有显示名时用 id
        assert seen["headers"]["Authorization"] == "Bearer sk-test"

    def test_cli_backend_declines(self, client):
        tc, svc = client
        svc.settings.save({"ai_provider": "codex"})
        body = tc.get("/admin/settings/ai/models").json()
        assert body["ok"] is False
        assert "手动填写" in body["error"]

    def test_network_error_returns_error_not_500(self, client, monkeypatch):
        tc, svc = client
        svc.settings.save({"ai_provider": "api"})
        monkeypatch.setattr(ai, "_resolve_api_key", lambda _env: "sk-test")

        def boom(url, headers=None, timeout=None):
            raise httpx.HTTPError("boom")

        monkeypatch.setattr("httpx.get", boom)
        body = tc.get("/admin/settings/ai/models").json()
        assert body["ok"] is False and "模型列表" in body["error"]
