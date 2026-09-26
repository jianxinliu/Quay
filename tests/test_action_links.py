"""通知里的一次性审批链接：令牌签发/作废、只在开了设置且选了外部渠道时才发、
落地页不走 cookie 认证但仍受 Host 校验，GET 无副作用、POST 用一次即作废。"""

from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.notify import build_bark_payload, build_feishu_payload, build_wecom_payload
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService
from dbmcp.settings import SettingsStore

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")
TOKEN = "admin-token"


class CaptureNotifier:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, title: str, body: str, meta: dict | None = None) -> None:
        self.sent.append({"title": title, "body": body, "meta": dict(meta or {})})


@pytest.fixture
def svc(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
                       "INSERT INTO users (name) VALUES ('alice'), ('bob');")
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(db_file), "environment": "dev",
        "writer": {"user": "x", "password": "plain://unused"},
    }}}}})
    s = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"),
                   notifier=CaptureNotifier())
    s.settings = SettingsStore(tmp_path / "s.sqlite3")
    yield s
    s.close()


def _pending_change(s: DbmService) -> int:
    out = s.execute("demo", "main", "DELETE FROM users WHERE id = 1", CALLER)
    assert out["status"] == "approval_required"
    return int(out["change_id"])


class TestStoreTokens:
    def test_issue_redeem_once(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        cid = store.create(project="p", connection="c", environment="dev", engine="sqlite",
                           sql="DELETE FROM t", fingerprint="f", reason="", risk_level="HIGH",
                           risk_report={}, agent="a", session_id="s").id
        tok = store.issue_action_token(cid)
        assert store.action_token_matches(cid, tok) and not store.action_token_matches(cid, tok + "x")
        assert store.action_token_matches(cid, "") is False
        assert store.redeem_action_token(cid, tok) is True
        assert store.redeem_action_token(cid, tok) is False      # 用过即作废
        assert store.action_token_matches(cid, tok) is False

    def test_decision_invalidates_token(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        cid = store.create(project="p", connection="c", environment="dev", engine="sqlite",
                           sql="DELETE FROM t", fingerprint="f", reason="", risk_level="HIGH",
                           risk_report={}, agent="a", session_id="s").id
        tok = store.issue_action_token(cid)
        store.approve(cid, decided_by="admin")   # 后台批过的单，通知里的链接不该还能点
        assert store.action_token_matches(cid, tok) is False
        with pytest.raises(Exception):
            store.issue_action_token(cid)        # 非 pending 不再签发

    def test_old_db_gets_column(self, tmp_path):
        db = tmp_path / "old.sqlite3"
        ApprovalStore(db).close()
        c = sqlite3.connect(db)
        c.execute("ALTER TABLE change_request DROP COLUMN action_token")
        c.commit(); c.close()
        store = ApprovalStore(db)   # 迁移补列
        cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(change_request)")}
        assert "action_token" in cols
        store.close()


class TestServiceIssuesLink:
    def test_off_by_default(self, svc):
        _pending_change(svc)
        meta = svc.notifier.sent[-1]["meta"]
        assert "deeplink" in meta and "action_url" not in meta

    def test_requires_external_channel(self, svc):
        svc.save_settings({"notify_action_links": True})   # 主渠道仍是 none
        _pending_change(svc)
        assert "action_url" not in svc.notifier.sent[-1]["meta"]

    def test_link_issued_with_channel(self, svc):
        svc.save_settings({"notify_action_links": True, "notify_primary": "bark",
                           "notify_bark_key": "k", "admin_base_url": "https://quay.example"})
        cid = _pending_change(svc)
        url = svc.notifier.sent[-1]["meta"]["action_url"]
        assert url.startswith(f"https://quay.example/admin/approvals/{cid}/act?t=")
        tok = url.split("t=", 1)[1]
        assert svc.approvals.action_token_matches(cid, tok)


class TestPayloads:
    def test_bark_prefers_action_url(self):
        assert build_bark_payload("t", "b", url="https://x/a", action_url="https://x/act")["url"] == "https://x/act"
        assert build_bark_payload("t", "b", url="https://x/a")["url"] == "https://x/a"

    def test_wecom_and_feishu_add_action_link(self):
        w = build_wecom_payload("t", "b", url="https://x/a", action_url="https://x/act")
        assert "[前往处理](https://x/a)" in w["markdown"]["content"]
        assert "[一键批准 / 拒绝](https://x/act)" in w["markdown"]["content"]
        f = build_feishu_payload("t", "b", url="https://x/a", action_url="https://x/act")
        hrefs = [n["href"] for n in f["content"]["post"]["zh_cn"]["content"][0] if n["tag"] == "a"]
        assert hrefs == ["https://x/a", "https://x/act"]
        # 没有 action_url 时形状与以前一致
        assert "一键批准" not in build_wecom_payload("t", "b", url="https://x/a")["markdown"]["content"]


@pytest.fixture
def web(svc):
    svc.save_settings({"notify_action_links": True, "notify_primary": "wecom",
                       "notify_wecom_webhook": "https://wecom.example/hook",
                       "admin_base_url": "http://testserver"})
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token=TOKEN)
    with TestClient(mcp.http_app()) as tc:
        yield tc, svc


def _link(svc) -> tuple[int, str]:
    cid = _pending_change(svc)
    url = svc.notifier.sent[-1]["meta"]["action_url"]
    return cid, url.split("t=", 1)[1]


class TestActRoutes:
    def test_get_renders_without_login_and_has_no_side_effect(self, web):
        tc, svc = web
        cid, tok = _link(svc)
        r = tc.get(f"/admin/approvals/{cid}/act?t={tok}")     # 未登录
        assert r.status_code == 200 and f"审批单 #{cid}" in r.text and "DELETE FROM users" in r.text
        assert "/admin/login" not in r.headers.get("location", "")
        assert svc.approvals.get(cid).effective_status() == "pending"   # GET 不决策
        assert svc.approvals.action_token_matches(cid, tok)              # GET 不作废

    def test_wrong_or_missing_token_403(self, web):
        tc, svc = web
        cid, tok = _link(svc)
        assert tc.get(f"/admin/approvals/{cid}/act?t=nope").status_code == 403
        assert tc.get(f"/admin/approvals/{cid}/act").status_code == 403
        assert tc.post(f"/admin/approvals/{cid}/act", data={"t": "nope", "decision": "approve"}).status_code == 403
        assert svc.approvals.get(cid).effective_status() == "pending"

    def test_post_approve_once(self, web):
        tc, svc = web
        cid, tok = _link(svc)
        r = tc.post(f"/admin/approvals/{cid}/act", data={"t": tok, "decision": "approve"})
        assert r.status_code == 200 and "已批准" in r.text
        c = svc.approvals.get(cid)
        assert c.status == "approved" and c.decided_by == "notify-link"
        # 重放同一链接：令牌已作废
        assert tc.post(f"/admin/approvals/{cid}/act", data={"t": tok, "decision": "reject"}).status_code == 403
        assert tc.get(f"/admin/approvals/{cid}/act?t={tok}").status_code == 403

    def test_post_reject_with_note(self, web):
        tc, svc = web
        cid, tok = _link(svc)
        r = tc.post(f"/admin/approvals/{cid}/act", data={"t": tok, "decision": "reject", "note": "先别删"})
        assert r.status_code == 200 and "已拒绝" in r.text
        c = svc.approvals.get(cid)
        assert c.status == "rejected" and c.decision_note == "先别删"

    def test_bad_decision_value_rejected_and_token_kept(self, web):
        tc, svc = web
        cid, tok = _link(svc)
        assert tc.post(f"/admin/approvals/{cid}/act", data={"t": tok, "decision": "execute"}).status_code == 403
        assert svc.approvals.action_token_matches(cid, tok)   # 非法动作不消耗令牌

    def test_spoofed_host_blocked(self, web):
        tc, svc = web
        cid, tok = _link(svc)
        r = tc.get(f"/admin/approvals/{cid}/act?t={tok}", headers={"host": "attacker.example.com"})
        assert r.status_code == 403
        r = tc.post(f"/admin/approvals/{cid}/act", data={"t": tok, "decision": "approve"},
                    headers={"host": "attacker.example.com"})
        assert r.status_code == 403 and svc.approvals.get(cid).effective_status() == "pending"

    def test_backend_decision_first_then_link_dead(self, web):
        tc, svc = web
        cid, tok = _link(svc)
        svc.approve_change(cid, decided_by="admin@localhost")
        assert tc.get(f"/admin/approvals/{cid}/act?t={tok}").status_code == 403
