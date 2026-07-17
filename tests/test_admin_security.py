"""安全修复回归：C2（Host/Origin 校验防 DNS rebinding/CSRF）、C3（prod 写二次闸门）、
H1（确认指纹绑定，防「看 A 批 B」）。"""

import sqlite3

import pytest

from dbmcp.admin import _allowed_hosts, _hostname_of, _local_request_ok
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.classify import fingerprint
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.service import CallerInfo, DbmService, QueryRejected

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")


# ---------- C2：Host / Origin 校验（纯函数） ----------

class _FakeReq:
    def __init__(self, method="GET", headers=None):
        self.method = method
        self.headers = headers or {}


class TestHostOriginGuard:
    def test_hostname_strips_port_and_scheme_and_brackets(self):
        assert _hostname_of("127.0.0.1:8100") == "127.0.0.1"
        assert _hostname_of("http://localhost:8100/admin") == "localhost"
        assert _hostname_of("[::1]:8100") == "::1"
        assert _hostname_of("EVIL.COM") == "evil.com"

    def test_default_allowlist_is_loopback(self):
        allowed = _allowed_hosts()
        assert "127.0.0.1" in allowed and "localhost" in allowed and "::1" in allowed

    def test_env_extends_allowlist(self, monkeypatch):
        monkeypatch.setenv("DBM_ADMIN_ALLOWED_HOSTS", "my.host, other.host")
        allowed = _allowed_hosts()
        assert "my.host" in allowed and "other.host" in allowed
        assert "127.0.0.1" in allowed  # 默认仍在

    def test_loopback_host_allowed(self):
        assert _local_request_ok(_FakeReq(headers={"host": "127.0.0.1:8100"})) is True

    def test_foreign_host_blocked_dns_rebinding(self):
        # DNS rebinding：浏览器带的是攻击者域名的 Host → 拒绝
        assert _local_request_ok(_FakeReq(headers={"host": "attacker.example.com"})) is False

    def test_missing_host_blocked(self):
        assert _local_request_ok(_FakeReq(headers={})) is False

    def test_cross_origin_post_blocked(self):
        # 写请求带跨站 Origin → 拒绝（纵深防御补 SameSite）
        req = _FakeReq(method="POST",
                       headers={"host": "127.0.0.1:8100", "origin": "http://evil.com"})
        assert _local_request_ok(req) is False

    def test_same_origin_post_allowed(self):
        req = _FakeReq(method="POST",
                       headers={"host": "127.0.0.1:8100", "origin": "http://127.0.0.1:8100"})
        assert _local_request_ok(req) is True

    def test_post_without_origin_allowed(self):
        # 非浏览器客户端（curl/脚本）不带 Origin —— 已过 Host + 认证，放行
        req = _FakeReq(method="POST", headers={"host": "localhost:8100"})
        assert _local_request_ok(req) is True


# ---------- C3 / H1：admin_run_sql 写路径二次闸门 ----------

def _make_service(tmp_path, environment: str):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        "INSERT INTO users (name, age) VALUES ('alice', 30), ('bob', 25);"
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": environment,
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    return DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                      ApprovalStore(tmp_path / "a.sqlite3"))


WRITE_SQL = "UPDATE users SET age = 99 WHERE id = 1"


class TestProdWriteGate:
    def test_confirm_report_carries_prod_and_fingerprint(self, tmp_path):
        svc = _make_service(tmp_path, "prod")
        try:
            r = svc.admin_run_sql("demo", "main", WRITE_SQL, CALLER, confirm=False)
            assert r["kind"] == "confirm"
            assert r["prod"] is True
            assert r["expect_text"] == "main"           # 需输入的连接名
            assert r["fingerprint"] == fingerprint(WRITE_SQL, "sqlite")
        finally:
            svc.close()

    def test_prod_write_confirm_executes_without_connection_name(self, tmp_path):
        # 便利优先：prod 写只需人工二次确认（confirm=True），不再要求输入连接名
        svc = _make_service(tmp_path, "prod")
        try:
            r = svc.admin_run_sql("demo", "main", WRITE_SQL, CALLER, confirm=True)
            assert r["kind"] == "write"
            out = svc.admin_run_sql("demo", "main", "SELECT age FROM users WHERE id=1", CALLER)
            assert out["rows"][0][0] == 99
        finally:
            svc.close()

    def test_local_write_confirm_executes(self, tmp_path):
        svc = _make_service(tmp_path, "local")
        try:
            r = svc.admin_run_sql("demo", "main", WRITE_SQL, CALLER, confirm=True)
            assert r["kind"] == "write"
        finally:
            svc.close()


class TestConfirmFingerprintBinding:
    def test_mismatched_fingerprint_rejected(self, tmp_path):
        svc = _make_service(tmp_path, "local")
        try:
            with pytest.raises(QueryRejected):
                svc.admin_run_sql("demo", "main", WRITE_SQL, CALLER, confirm=True,
                                  expect_fingerprint="deadbeef")
            out = svc.admin_run_sql("demo", "main", "SELECT age FROM users WHERE id=1", CALLER)
            assert out["rows"][0][0] == 30   # 未执行
        finally:
            svc.close()

    def test_matching_fingerprint_executes(self, tmp_path):
        svc = _make_service(tmp_path, "local")
        try:
            fp = fingerprint(WRITE_SQL, "sqlite")
            r = svc.admin_run_sql("demo", "main", WRITE_SQL, CALLER, confirm=True,
                                  expect_fingerprint=fp)
            assert r["kind"] == "write"
        finally:
            svc.close()

    def test_prod_fingerprint_binding_still_enforced(self, tmp_path):
        # 移除连接名闸门后，H1 指纹绑定在 prod 上仍然生效
        svc = _make_service(tmp_path, "prod")
        try:
            fp = fingerprint(WRITE_SQL, "sqlite")
            # 指纹不一致 → 拒
            with pytest.raises(QueryRejected):
                svc.admin_run_sql("demo", "main", WRITE_SQL, CALLER, confirm=True,
                                  expect_fingerprint="deadbeef")
            # 指纹一致 → 执行
            r = svc.admin_run_sql("demo", "main", WRITE_SQL, CALLER, confirm=True,
                                  expect_fingerprint=fp)
            assert r["kind"] == "write"
        finally:
            svc.close()
