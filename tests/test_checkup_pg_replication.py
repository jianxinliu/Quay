"""PG 复制延迟检查：查询只有三列，取第三列（之前取 r[3] 会在有从库的 PG 上 IndexError）。"""

from __future__ import annotations

from dbmcp import checkup


def test_pg_replication_lag_uses_third_column(monkeypatch):
    rows = [["standby1", "10.0.0.2", 0.5], ["standby2", "10.0.0.3", 95.0]]
    monkeypatch.setattr(checkup, "_rows", lambda *_a, **_kw: rows)
    c = checkup._pg_replication_lag(engine=None, can_see=True)
    assert c.status in ("warn", "critical") and "95" in c.value


def test_pg_replication_lag_no_standby_is_info(monkeypatch):
    monkeypatch.setattr(checkup, "_rows", lambda *_a, **_kw: [])
    c = checkup._pg_replication_lag(engine=None, can_see=True)
    assert c.status == "info"
