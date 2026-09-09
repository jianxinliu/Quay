"""系统设置页的结构：账本行、开关、护栏分区、「已改」标记、改动条。

这页是**护栏控制台**而不是偏好面板——所以断言的重点是：
护栏项和普通偏好在结构上分得开、偏离默认值的项能被标出来、
开关的值真能被表单提交（复选框本身不带 name，值由隐藏 input 承载）。
"""

import sqlite3

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import DbmService
from dbmcp.settings import DEFAULTS, SettingsStore

TOKEN = "test-admin-token"
FORM_TABS = ("general", "db", "redis", "ai", "notify")
PLAIN_TABS = ("connections", "ssh", "info")


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


class TestShell:
    def test_every_tab_renders(self, client):
        tc, _ = client
        for tab in FORM_TABS + PLAIN_TABS:
            r = tc.get(f"/admin/settings?tab={tab}")
            assert r.status_code == 200, tab
            assert "set-page" in r.text, tab

    def test_assets_loaded_and_not_cached(self, client):
        tc, _ = client
        page = tc.get("/admin/settings").text
        assert "/admin/static/settings.css" in page
        assert "/admin/static/settings.js" in page
        for path in ("settings.css", "settings.js"):
            r = tc.get(f"/admin/static/{path}")
            assert r.status_code == 200
            # 自家静态文件必须 no-cache，否则改完前端浏览器还跑旧的
            assert r.headers["cache-control"] == "no-cache"

    def test_nav_marks_current_and_groups_tabs(self, client):
        tc, _ = client
        page = tc.get("/admin/settings?tab=redis").text
        assert "class='on' href='/admin/settings?tab=redis'" in page
        # 分组之间有分隔（八个 tab 平铺读不出关系）
        assert page.count("<span class='sep'></span>") >= 2

    def test_unknown_tab_falls_back_to_general(self, client):
        tc, _ = client
        page = tc.get("/admin/settings?tab=nope").text
        assert "class='on' href='/admin/settings?tab=general'" in page

    def test_form_tabs_have_a_change_bar(self, client):
        """保存按钮不再钉在长表单最底下，改成有改动才浮出的条。"""
        tc, _ = client
        for tab in FORM_TABS:
            page = tc.get(f"/admin/settings?tab={tab}").text
            assert "set-bar" in page and "保存改动" in page, tab

    def test_plain_tabs_have_no_change_bar(self, client):
        """连接管理/SSH/系统信息各有自己的表单与按钮，不该再出现一个全局保存条。"""
        tc, _ = client
        for tab in PLAIN_TABS:
            assert "set-bar" not in tc.get(f"/admin/settings?tab={tab}").text, tab

    def test_only_form_tabs_get_search(self, client):
        tc, _ = client
        assert "set-search" in tc.get("/admin/settings?tab=db").text
        assert "set-search" not in tc.get("/admin/settings?tab=info").text


class TestRows:
    def test_guard_section_is_visually_separated(self, client):
        """改错护栏有后果，改错字号没有——这个区别由版式说出来。"""
        tc, _ = client
        page = tc.get("/admin/settings?tab=db").text
        assert "set-sec guard" in page
        assert "给 Agent 的护栏" in page
        # 外观类的分区不带 guard
        assert "set-sec'" in tc.get("/admin/settings?tab=general").text

    def test_toggle_value_is_carried_by_a_hidden_input(self, client):
        """复选框本身不带 name：未勾选的复选框根本不进 FormData，
        而保存接口要的是显式的 true/false。"""
        tc, _ = client
        page = tc.get("/admin/settings?tab=db").text
        assert "<input type='hidden' name='sql_minimap' value='true'>" in page
        assert "data-field='sql_minimap'" in page

    def test_number_rows_carry_unit_and_readout_slot(self, client):
        tc, _ = client
        page = tc.get("/admin/settings?tab=db").text
        assert "data-kind='tokens'" in page      # 字符 → ≈token
        assert "data-kind='bytes'" in page       # 字节 → MB

    def test_changed_marker_only_on_non_default_values(self, client):
        tc, svc = client
        assert "set-tag" not in tc.get("/admin/settings?tab=db").text
        svc.save_settings({"sync_max_rows": DEFAULTS["sync_max_rows"] + 1})
        page = tc.get("/admin/settings?tab=db").text
        assert page.count("set-tag") == 1
        assert "已改" in page

    def test_changed_marker_clears_when_restored(self, client):
        tc, svc = client
        svc.save_settings({"sql_font_size": 20})
        assert "set-tag" in tc.get("/admin/settings?tab=db").text
        svc.save_settings({"sql_font_size": DEFAULTS["sql_font_size"]})
        assert "set-tag" not in tc.get("/admin/settings?tab=db").text

    def test_long_explanations_are_folded(self, client):
        """说明控制在两行内，长解释折进「展开说明」，否则整页全是灰字。"""
        tc, _ = client
        page = tc.get("/admin/settings?tab=db").text
        assert "set-more" in page and "展开说明" in page

    def test_ai_prompt_section_is_folded_by_default(self, client):
        """几十行的提示词文本框默认收起，别把真正要调的旋钮挤出屏幕。"""
        tc, _ = client
        assert "set-sec fold" in tc.get("/admin/settings?tab=ai").text


class TestSaving:
    def test_toggle_off_round_trips(self, client):
        tc, svc = client
        assert svc.get_settings()["sql_minimap"] is True
        tc.post("/admin/settings/save", data={"sql_minimap": "false"})
        assert svc.get_settings()["sql_minimap"] is False
        page = tc.get("/admin/settings?tab=db").text
        assert "<input type='hidden' name='sql_minimap' value='false'>" in page

    def test_every_rendered_field_is_a_known_setting(self, client):
        """页面上出现的 name 必须都在 DEFAULTS 里，否则保存时会被白名单静默丢掉。"""
        import re

        tc, _ = client
        extra = {"ai_api_key", "ai_api_key_clear", "notify_primary"}  # 非设置项/特殊处理
        for tab in FORM_TABS:
            page = tc.get(f"/admin/settings?tab={tab}").text
            for name in set(re.findall(r"name='([a-z_0-9]+)'", page)):
                assert name in DEFAULTS or name in extra, f"{tab}: {name}"
