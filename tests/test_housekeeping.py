"""后台维护测试：空闲回收接线、审计/审批单保留期清理、EXPLAIN 进风险报告、单元格截断、分页。"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditRecord, AuditStore
from dbmcp.config import AppConfig
from dbmcp.engines import truncate_cell
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0")


def _record(store: AuditStore, days_ago: int) -> int:
    rid = store.record(AuditRecord(project="p", connection="c", tool="query", status="ok"))
    old_ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="milliseconds")
    with store._lock:
        store._conn.execute("UPDATE audit_log SET ts = ? WHERE id = ?", (old_ts, rid))
        store._conn.commit()
    return rid


class TestAuditRetention:
    def test_purge_old_keeps_recent(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        _record(store, days_ago=40)
        _record(store, days_ago=31)
        _record(store, days_ago=5)
        assert store.purge_old(retention_days=30) == 2
        assert store.count() == 1
        store.close()

    def test_pagination(self, tmp_path):
        store = AuditStore(tmp_path / "a.sqlite3")
        ids = [_record(store, 0) for _ in range(5)]
        page1 = store.recent(limit=2, offset=0)
        page2 = store.recent(limit=2, offset=2)
        assert [r["id"] for r in page1] == [ids[4], ids[3]]
        assert [r["id"] for r in page2] == [ids[2], ids[1]]
        assert store.count() == 5
        store.close()


class TestApprovalRetention:
    def test_purge_terminal_only(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        kw = dict(project="p", connection="c", environment="dev", engine="sqlite",
                  fingerprint="f", reason="", risk_level="MEDIUM", risk_report={},
                  agent="a", session_id="s")
        old = store.create(sql="DELETE FROM t", **kw)
        store.reject(old.id, "x")
        recent = store.create(sql="DELETE FROM t2", **kw)
        store.reject(recent.id, "x")
        # 把 old 的 created_at 改到 40 天前
        cutoff = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds")
        with store._lock:
            store._conn.execute("UPDATE change_request SET created_at = ? WHERE id = ?", (cutoff, old.id))
            store._conn.commit()
        assert store.purge_old(retention_days=30) == 1
        assert store.get(recent.id).id == recent.id  # 近期的保留
        store.close()


@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, blob TEXT);"
        "INSERT INTO users (name, blob) VALUES ('a', 'x'), ('b', NULL);"
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
            "writer": {"user": "x", "password": "plain://unused"},
            "policy": {"max_cell_chars": 20, "max_rows": 1},
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), ApprovalStore(tmp_path / "a.sqlite3"))
    yield svc
    svc.close()


class TestHousekeepOnce:
    def test_stats_shape_and_no_crash(self, service):
        stats = service.housekeep_once(retention_days=30)
        assert set(stats) == {"engines_reaped", "redis_reaped", "audit_purged",
                              "changes_purged", "notifications_purged"}

    def test_reap_wired(self, service):
        # 用过引擎后把回收阈值调成 0 → housekeep 应回收 1 个
        service.query("demo", "main", "SELECT 1", CALLER)
        service.pool._idle_reclaim_s = 0
        stats = service.housekeep_once()
        assert stats["engines_reaped"] == 1

    def test_start_stop(self, service):
        service.start_housekeeping(interval_s=3600)
        assert service._housekeeping_stop is not None
        service.close()
        assert service._housekeeping_stop is None


class TestExplainInReport:
    def test_write_ticket_contains_explain(self, service):
        r = service.execute("demo", "main", "DELETE FROM users WHERE id = 1", CALLER)
        assert r["status"] == "approval_required"
        assert "explain" in r["risk"], "风险报告应包含执行计划"
        # sqlite 的 EXPLAIN 输出是 opcode 表
        assert "Opcode" in r["risk"]["explain"] or "|" in r["risk"]["explain"]
        # 落库的审批单同样带 explain
        assert "explain" in service.get_change(r["change_id"]).risk_report


class TestCellTruncationAndHint:
    def test_truncate_cell_unit(self):
        assert truncate_cell("x" * 30, 10).startswith("xxxxxxxxxx…[已截断，原 30 字符]"[:10])
        assert "已截断" in truncate_cell("x" * 30, 10)
        assert truncate_cell("short", 10) == "short"
        assert truncate_cell(123, 10) == 123
        assert truncate_cell(None, 10) is None

    def test_query_truncates_long_cell(self, service):
        # 写入超长值（绕过审批直接用 sqlite3，避免测试绕远路）
        import sqlite3 as s3
        db = service.config.get_connection("demo", "main").database
        c = s3.connect(db); c.execute("UPDATE users SET blob = ? WHERE id = 1", ("Z" * 500,)); c.commit(); c.close()
        r = service.query("demo", "main", "SELECT blob FROM users WHERE id = 1", CALLER)
        cell = r["rows"][0][0]
        assert len(cell) < 100 and "已截断" in cell and "500" in cell

    def test_truncated_hint_for_agent(self, service):
        r = service.query("demo", "main", "SELECT * FROM users", CALLER)  # max_rows=1，表 2 行
        assert r["truncated"] is True
        assert "LIMIT/OFFSET" in r["hint"]
