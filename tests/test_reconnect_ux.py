"""连接不可用时的可操作性：健康位接口 + 错误分类透传 + 人工重连。

背景（使用反馈）：连接断了以后 ① 不会自动恢复（退避用尽就永久放弃）；
② 页面上只有左树右键菜单里藏着一个「重连数据库」，人找不到。
这里守住修好之后的契约：健康位查得到、错误带得出分类、重连按得动。
"""

import sqlite3
import time

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import error_payload, mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.health import ConnectionUnavailable, Health
from dbmcp.jobs import JobManager
from dbmcp.server import build_mcp
from dbmcp.service import DbmService, QueryRejected

TOKEN = "test-admin-token"


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
                       "INSERT INTO users (name) VALUES ('alice');")
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
    svc.data_dir = str(tmp_path / "data")
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token=TOKEN)
    with TestClient(mcp.http_app()) as tc:
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc, svc
    svc.close()


class TestHealthRoute:
    def test_healthy_connections_are_omitted(self, client):
        tc, _ = client
        d = tc.get("/admin/sql/health").json()
        assert d["ok"] is True and d["conns"] == {}

    def test_reports_unavailable_with_retry_countdown(self, client):
        tc, svc = client
        svc.health._entries[("demo", "main")] = Health(
            state="unavailable", fail_count=2, last_error="lost connection",
            next_retry_at=time.monotonic() + 42,
        )
        entry = tc.get("/admin/sql/health").json()["conns"]["demo/main"]
        assert entry["state"] == "unavailable"
        assert 30 <= entry["retry_in_s"] <= 42     # 前端据此显示「约 N 秒后自动重试」
        assert entry["last_error"] == "lost connection"

    def test_requires_auth(self, client):
        tc, _ = client
        tc.get("/admin/logout")
        assert tc.get("/admin/sql/health", follow_redirects=False).status_code in (303, 401)


class TestErrorPayload:
    def test_connection_unavailable_tagged(self):
        p = error_payload(ConnectionUnavailable("断了", retry_after_s=5, state="unavailable"))
        assert p["error_kind"] == "connection_unavailable"

    def test_exhausted_tagged(self):
        p = error_payload(ConnectionUnavailable("没了", state="exhausted"))
        assert p["error_kind"] == "connection_exhausted"

    def test_raw_connection_error_tagged(self):
        """健康位还没来得及打标的第一次失败，也要能给出重连入口。"""
        p = error_payload(RuntimeError("(2013, 'Lost connection to MySQL server')"))
        assert p["error_kind"] == "connection_error"

    def test_business_rejection_has_no_kind(self):
        p = error_payload(QueryRejected("已拒绝：写操作需审批"))
        assert "error_kind" not in p and p["error"] == "已拒绝：写操作需审批"

    def test_credentials_never_leak_to_page(self):
        p = error_payload(RuntimeError("boom mysql+pymysql://root:s3cret@db:3306/app"))
        assert "s3cret" not in p["error"]


class TestJobErrorKind:
    """连接类错误要能穿过后台任务队列到达前端（查询台走的是 run_async + 轮询）。"""

    def test_error_kind_propagates_through_job(self):
        mgr = JobManager(ttl_s=5)

        def boom(_register):
            e = RuntimeError("连接不可用")
            e.dbm_error_kind = "connection_exhausted"
            raise e

        jid = mgr.submit(("p", "c"), boom)
        for _ in range(100):
            snap = mgr.get(jid)
            if snap["status"] != "running":
                break
            time.sleep(0.01)
        assert snap["status"] == "error"
        assert snap["error_kind"] == "connection_exhausted"

    def test_plain_error_has_empty_kind(self):
        mgr = JobManager(ttl_s=5)
        jid = mgr.submit(("p", "c"), lambda _r: (_ for _ in ()).throw(ValueError("语法错")))
        for _ in range(100):
            snap = mgr.get(jid)
            if snap["status"] != "running":
                break
            time.sleep(0.01)
        assert snap["status"] == "error" and snap["error_kind"] == ""


class TestReconnectRoute:
    def test_reconnect_recovers_exhausted_connection(self, client):
        tc, svc = client
        svc.health._entries[("demo", "main")] = Health(
            state="exhausted", fail_count=7, last_error="lost connection",
            next_retry_at=time.monotonic() + 300,
        )
        d = tc.post("/admin/sql/reconnect", data={"conn": "demo/main"}).json()
        assert d["ok"] is True
        assert tc.get("/admin/sql/health").json()["conns"] == {}

    def test_reconnect_unknown_connection_reports_error(self, client):
        tc, _ = client
        d = tc.post("/admin/sql/reconnect", data={"conn": "demo/nope"}).json()
        assert d["ok"] is False and d["error"]
