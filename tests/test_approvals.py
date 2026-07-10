"""审批流集成测试：拒绝—重提 + change_id 放行的完整闭环与各拒绝路径。

用 SQLite 作为目标库；writer 账号在 sqlite 下与 reader 相同连接（仅去掉只读 PRAGMA）。
"""

import sqlite3

import pytest

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.metadata import MetadataCache
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")


@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, active INTEGER DEFAULT 1);
        INSERT INTO users (name) VALUES ('alice'), ('bob'), ('carol');
        """
    )
    conn.commit()
    conn.close()

    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
            # sqlite 下 writer 复用同库；writer 引擎不加只读 PRAGMA
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    store = AuditStore(tmp_path / "audit.sqlite3")
    approvals = ApprovalStore(tmp_path / "audit.sqlite3")
    svc = DbmService(cfg, store, approvals)
    # 元数据缓存复用 service 的引擎池
    svc.metadata = MetadataCache(tmp_path / "audit.sqlite3", svc.pool)
    yield svc
    svc.close()


class TestHappyPath:
    def test_full_approval_cycle(self, service):
        # 1. 首次提交写操作 → 被拒绝并生成审批单
        r1 = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
        assert r1["status"] == "approval_required"
        change_id = r1["change_id"]
        assert r1["risk"]["level"] in ("MEDIUM", "HIGH", "CRITICAL")

        # 2. 未审批就重提 → 拒绝
        r2 = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1",
                             CALLER, change_id=change_id)
        assert r2["status"] == "rejected"
        assert "尚未审批" in r2["reason"]

        # 3. 人工批准
        change = service.approve_change(change_id, decided_by="alice@ops", note="ok")
        assert change.status == "approved"

        # 4. 带 change_id 重提 → 执行
        r3 = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1",
                             CALLER, change_id=change_id)
        assert r3["status"] == "executed"
        assert r3["affected_rows"] == 1

        # 5. 数据确实被改
        result = service.query("demo", "main", "SELECT active FROM users WHERE id = 1", CALLER)
        assert result["rows"][0][0] == 0

    def test_one_time_consumption(self, service):
        r = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 2", CALLER)
        cid = r["change_id"]
        service.approve_change(cid, "alice@ops")
        assert service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 2",
                               CALLER, change_id=cid)["status"] == "executed"
        # 再次使用同一审批单 → 拒绝
        again = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 2",
                                CALLER, change_id=cid)
        assert again["status"] == "rejected"
        assert "只能执行一次" in again["reason"]


class TestRejectionPaths:
    def test_human_reject(self, service):
        r = service.execute("demo", "main", "DELETE FROM users WHERE id = 3", CALLER)
        cid = r["change_id"]
        service.reject_change(cid, "bob@ops", note="请改成软删除")
        out = service.execute("demo", "main", "DELETE FROM users WHERE id = 3",
                              CALLER, change_id=cid)
        assert out["status"] == "rejected"
        assert "请改成软删除" in out["reason"]

    def test_fingerprint_mismatch(self, service):
        # 审批的是 id=1，重提改成 id=2 → 指纹不符，执行的应是存储的 SQL，但这里直接拒绝
        r = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
        cid = r["change_id"]
        service.approve_change(cid, "alice@ops")
        out = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 2",
                              CALLER, change_id=cid)
        assert out["status"] == "rejected"
        assert "不一致" in out["reason"]
        # 确认 id=2 没被改
        assert service.query("demo", "main", "SELECT active FROM users WHERE id = 2", CALLER)["rows"][0][0] == 1

    def test_executes_stored_sql_not_resubmitted(self, service):
        """核心不变量：即便重提文本与审批一致，执行的也是审批单存储的 SQL。"""
        r = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1", CALLER)
        cid = r["change_id"]
        change = service.get_change(cid)
        assert change.sql == "UPDATE users SET active = 0 WHERE id = 1"
        service.approve_change(cid, "alice@ops")
        out = service.execute("demo", "main", "UPDATE users SET active = 0 WHERE id = 1",
                              CALLER, change_id=cid)
        assert out["status"] == "executed"

    def test_wrong_connection(self, service):
        r = service.execute("demo", "main", "DELETE FROM users WHERE id = 1", CALLER)
        cid = r["change_id"]
        service.approve_change(cid, "alice@ops")
        with pytest.raises(KeyError):
            service.execute("demo", "nonexistent", "DELETE FROM users WHERE id = 1",
                            CALLER, change_id=cid)


class TestReadonlyViaExecute:
    def test_readonly_executes_directly(self, service):
        r = service.execute("demo", "main", "SELECT count(*) AS c FROM users", CALLER)
        assert r["status"] == "executed"
        assert r["readonly"] is True
        assert r["rows"][0][0] == 3


class TestAudit:
    def test_approval_required_is_audited(self, service):
        service.execute("demo", "main", "DELETE FROM users", CALLER)
        log = service.store.recent()[0]
        assert log["tool"] == "execute"
        assert log["status"] == "rejected"
        assert "审批单" in log["detail"]
