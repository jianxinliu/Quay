"""管理后台端到端测试：用 Starlette TestClient 打真实 HTTP 路由，跑通审批闭环。"""

import sqlite3

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")


@pytest.fixture
def client(tmp_path):
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
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
    mcp = build_mcp(svc)
    mount_admin(mcp, svc)
    app = mcp.http_app()
    with TestClient(app) as tc:
        yield tc, svc
    svc.close()


def test_approvals_list_page(client):
    tc, svc = client
    svc.execute("demo", "main", "DELETE FROM users WHERE id = 1", CALLER)
    resp = tc.get("/admin/approvals")
    assert resp.status_code == 200
    assert "待审批" in resp.text
    assert "DELETE FROM users" in resp.text


def test_detail_and_approve_flow(client):
    tc, svc = client
    r = svc.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
    cid = r["change_id"]

    # 详情页展示风险报告
    detail = tc.get(f"/admin/approvals/{cid}")
    assert detail.status_code == 200
    assert "风险报告" in detail.text
    assert "UPDATE users SET active = 0" in detail.text

    # 批准（表单 POST，303 重定向回详情）
    approve = tc.post(f"/admin/approvals/{cid}/approve",
                      data={"by": "ops@x", "note": "ok"}, follow_redirects=False)
    assert approve.status_code == 303
    assert svc.get_change(cid).status == "approved"

    # agent 带 change_id 重提 → 执行成功
    out = svc.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER, change_id=cid)
    assert out["status"] == "executed"


def test_reject_flow_returns_reason_to_agent(client):
    tc, svc = client
    r = svc.execute("demo", "main", "DELETE FROM users WHERE id = 2", CALLER)
    cid = r["change_id"]
    tc.post(f"/admin/approvals/{cid}/reject", data={"by": "ops@x", "note": "请软删除"})
    out = svc.execute("demo", "main", "DELETE FROM users WHERE id = 2", CALLER, change_id=cid)
    assert out["status"] == "rejected"
    assert "请软删除" in out["reason"]


def test_audit_page_and_filter(client):
    tc, svc = client
    svc.query("demo", "main", "SELECT 1", CALLER)
    svc.execute("demo", "main", "DELETE FROM users", CALLER)  # 生成一条 rejected
    all_page = tc.get("/admin/audit")
    assert all_page.status_code == 200
    assert "操作审计" in all_page.text
    rejected = tc.get("/admin/audit?status=rejected")
    assert "审批单" in rejected.text


def test_unknown_change_404(client):
    tc, _ = client
    assert tc.get("/admin/approvals/9999").status_code == 404


def test_index_redirects(client):
    tc, _ = client
    resp = tc.get("/admin", follow_redirects=False)
    assert resp.status_code in (307, 302)
    assert "/admin/approvals" in resp.headers["location"]
