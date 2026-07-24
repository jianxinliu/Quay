"""通知：管理后台 HTTP 端点 + 设置页 + 从设置动态组装 Notifier。"""

from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.inbox import InboxNotifier, InboxStore
from dbmcp.notify import NotifierRouter, build_from_settings
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService
from dbmcp.settings import SettingsStore
from dbmcp.snippets import SnippetStore

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")
TOKEN = "test-admin-token"


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO users (name) VALUES ('a'), ('b');"
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(db_file), "environment": "dev",
        "writer": {"user": "x", "password": "plain://unused"},
    }}}}})

    # 与 serve 同款装配：Inbox + SettingsStore + NotifierRouter
    inbox_store = InboxStore(tmp_path / "a.sqlite3")
    settings_store = SettingsStore(tmp_path / "a.sqlite3")
    inbox_notifier = InboxNotifier(inbox_store)

    def _make():
        return build_from_settings(settings_store.get_all(), inbox=inbox_notifier)

    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     ApprovalStore(tmp_path / "a.sqlite3"),
                     notifier=NotifierRouter(_make))
    svc.inbox = inbox_store
    svc.settings = settings_store
    svc.snippets = SnippetStore(tmp_path / "a.sqlite3")
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token=TOKEN)
    app = mcp.http_app()
    with TestClient(app) as tc:
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc, svc
    svc.close()


class TestInboxEndpoints:
    def test_unread_count_starts_at_zero(self, client):
        tc, _ = client
        r = tc.get("/admin/notifications/unread_count")
        assert r.json() == {"ok": True, "count": 0}

    def test_approval_created_writes_inbox(self, client):
        tc, svc = client
        # agent 提交写 → 生成审批单 → 触发通知 → InboxNotifier 写库
        r = svc.execute("demo", "main", "DELETE FROM users WHERE id=1", CALLER)
        assert r["status"] == "approval_required"
        c = tc.get("/admin/notifications/unread_count").json()["count"]
        assert c == 1
        lst = tc.get("/admin/notifications/list").json()["items"]
        assert len(lst) == 1
        assert lst[0]["kind"] == "approval_created"
        assert f"#{r['change_id']}" in lst[0]["title"]
        assert lst[0]["meta"]["change_id"] == r["change_id"]
        # deeplink 直达审批详情页
        expected = f"/admin/approvals/{r['change_id']}"
        assert expected in lst[0]["meta"]["deeplink"]

    def test_deeplink_uses_configured_base_url(self, client):
        tc, svc = client
        # 改基址（例如 Docker/反代场景）
        tc.post("/admin/settings/save", data={
            "admin_base_url": "https://quay.example.com",
        })
        r = svc.execute("demo", "main", "DELETE FROM users WHERE id=1", CALLER)
        item = tc.get("/admin/notifications/list").json()["items"][0]
        assert item["meta"]["deeplink"] == (
            f"https://quay.example.com/admin/approvals/{r['change_id']}")

    def test_mark_read_single(self, client):
        tc, svc = client
        svc.execute("demo", "main", "DELETE FROM users WHERE id=1", CALLER)
        item_id = tc.get("/admin/notifications/list").json()["items"][0]["id"]
        r = tc.post("/admin/notifications/mark_read", data={"ids": str(item_id)})
        assert r.json() == {"ok": True, "updated": 1}
        assert tc.get("/admin/notifications/unread_count").json()["count"] == 0
        # 幂等
        r2 = tc.post("/admin/notifications/mark_read", data={"ids": str(item_id)})
        assert r2.json()["updated"] == 0

    def test_mark_all_read(self, client):
        tc, svc = client
        svc.execute("demo", "main", "DELETE FROM users WHERE id=1", CALLER)
        svc.execute("demo", "main", "DELETE FROM users WHERE id=2", CALLER)
        assert tc.get("/admin/notifications/unread_count").json()["count"] == 2
        r = tc.post("/admin/notifications/mark_read", data={"all": "1"})
        assert r.json()["updated"] == 2
        assert tc.get("/admin/notifications/unread_count").json()["count"] == 0

    def test_list_unread_only_filter(self, client):
        tc, svc = client
        svc.execute("demo", "main", "DELETE FROM users WHERE id=1", CALLER)
        svc.execute("demo", "main", "DELETE FROM users WHERE id=2", CALLER)
        first_id = tc.get("/admin/notifications/list").json()["items"][-1]["id"]
        tc.post("/admin/notifications/mark_read", data={"ids": str(first_id)})
        items = tc.get("/admin/notifications/list?unread=1").json()["items"]
        assert len(items) == 1

    def test_test_notification_button(self, client):
        tc, _ = client
        r = tc.post("/admin/notifications/test")
        assert r.json() == {"ok": True}
        # 落一条内推
        items = tc.get("/admin/notifications/list").json()["items"]
        assert items[0]["kind"] == "test"


class TestBellStaticAssets:
    """铃铛 UI 的静态资源可访问 + 关键类/函数已挂进 admin.js/admin-chrome.css。

    铃铛 JS 是 vanilla（没有单测框架），这里断言的是"关键实现已加载到全站外壳"，
    保证任何服务端渲染页 / SPA 页面都能拿到铃铛脚本。真实交互靠人工验证/浏览器 e2e。
    """

    def test_admin_js_has_bell_bootstrap(self, client):
        tc, _ = client
        r = tc.get("/admin/static/admin.js")
        assert r.status_code == 200
        # 关键实现：类名 / SSE 端点 / 未读 API
        assert "dbm-bell" in r.text
        assert "/admin/notifications/stream" in r.text
        assert "/admin/notifications/unread_count" in r.text
        assert "/admin/notifications/mark_read" in r.text
        # deeplink 单条点击跳转
        assert "data-href" in r.text

    def test_admin_chrome_css_has_bell_styles(self, client):
        tc, _ = client
        r = tc.get("/admin/static/admin-chrome.css")
        assert r.status_code == 200
        assert ".dbm-bell" in r.text
        assert ".dbm-panel" in r.text
        assert ".dbm-item.unread" in r.text

    def test_admin_pages_load_bell_script(self, client):
        """所有走 _shell 的页面都加载 admin.js，铃铛就都挂上了。"""
        tc, _ = client
        for path in ("/admin/audit", "/admin/approvals",
                     "/admin/settings?tab=notify", "/admin/sql", "/admin/redis"):
            r = tc.get(path)
            assert r.status_code == 200, path
            assert "/admin/static/admin.js" in r.text, path
            assert "/admin/static/admin-chrome.css" in r.text, path

    def test_login_page_does_not_load_bell(self, client):
        """登录页不用铃铛（用户还没认证）。"""
        tc, _ = client
        tc.get("/admin/logout")
        r = tc.get("/admin/login")
        # 登录页是独立模板 _login_page，不加载 admin.js/admin-chrome.css
        assert "/admin/static/admin.js" not in r.text


class TestNotifySettingsPage:
    def test_notify_tab_renders(self, client):
        tc, _ = client
        r = tc.get("/admin/settings?tab=notify")
        assert r.status_code == 200
        body = r.text
        assert "主外部渠道" in body
        assert "notify_primary" in body
        assert "Bark" in body and "企业微信" in body and "飞书" in body

    def test_save_notify_settings_switches_channel(self, client):
        tc, svc = client
        # 初始 primary=none
        assert svc.get_settings()["notify_primary"] == "none"
        # 切到 wecom
        r = tc.post("/admin/settings/save", data={
            "notify_primary": "wecom",
            "notify_wecom_webhook": "https://x/wh",
        })
        assert r.status_code == 200
        assert svc.get_settings()["notify_primary"] == "wecom"
        assert svc.get_settings()["notify_wecom_webhook"] == "https://x/wh"

    def test_invalid_primary_falls_back_to_default(self, client):
        tc, svc = client
        r = tc.post("/admin/settings/save", data={"notify_primary": "email"})
        assert r.status_code == 200
        # 未知值被 _validate 折回默认（none）
        assert svc.get_settings()["notify_primary"] == "none"


class TestRouterLivePickupsSettingsChange:
    """改设置 → 下一次 send 立即用新配置（NotifierRouter 每次读 settings）。"""

    def test_switch_from_none_to_bark_starts_sending(self, client, monkeypatch):
        tc, svc = client
        calls: list[str] = []

        # patch webhook 底层 urlopen，观察是否被调
        import dbmcp.notify
        original = dbmcp.notify.urllib.request.urlopen

        class _Resp:
            def __enter__(self): return type("R", (), {"status": 200, "read": lambda: b""})()
            def __exit__(self, *a): return None

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            calls.append(req.full_url)
            return _Resp()

        monkeypatch.setattr(dbmcp.notify.urllib.request, "urlopen", fake_urlopen)

        # 先 primary=none：只落 inbox，不外呼
        svc.notifier.send("t", "b")
        # 等异步线程跑完
        import time
        time.sleep(0.1)
        assert calls == []

        # 切到 bark
        tc.post("/admin/settings/save", data={
            "notify_primary": "bark",
            "notify_bark_server": "https://x",
            "notify_bark_key": "abc",
        })
        svc.notifier.send("t2", "b2")
        # webhook 是异步线程，稍等
        for _ in range(40):
            if calls:
                break
            time.sleep(0.02)
        assert calls == ["https://x/abc"]

        # 恢复
        monkeypatch.setattr(dbmcp.notify.urllib.request, "urlopen", original)
