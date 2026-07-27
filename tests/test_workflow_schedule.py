"""cron 匹配 / 下拉转 cron / 两张 SQLite 表 CRUD 单测。"""

from datetime import datetime

import pytest

from dbmcp.workflows import (
    WorkflowRunStore,
    WorkflowScheduleStore,
    cron_from_dropdown,
    cron_matches,
)


# ---------- cron_matches ----------

class TestCronMatches:
    def test_wildcard_matches_every_minute(self):
        dt = datetime(2026, 7, 27, 12, 34)
        assert cron_matches("* * * * *", dt) is True

    def test_star_slash_5_matches_multiples_of_5(self):
        for m in [0, 5, 10, 55]:
            assert cron_matches("*/5 * * * *", datetime(2026, 7, 27, 12, m)) is True
        for m in [1, 4, 7, 59]:
            assert cron_matches("*/5 * * * *", datetime(2026, 7, 27, 12, m)) is False

    def test_daily_at_9_30(self):
        assert cron_matches("30 9 * * *", datetime(2026, 7, 27, 9, 30)) is True
        assert cron_matches("30 9 * * *", datetime(2026, 7, 27, 9, 31)) is False
        assert cron_matches("30 9 * * *", datetime(2026, 7, 27, 10, 30)) is False

    def test_weekly_monday(self):
        # cron 里 1=周一（0=周日），Python weekday(): 周一=0 → 加 1 mod 7 = 1
        mon = datetime(2026, 7, 27)  # 周一
        sun = datetime(2026, 7, 26)  # 周日
        assert cron_matches("0 9 * * 1", datetime(mon.year, mon.month, mon.day, 9, 0)) is True
        assert cron_matches("0 9 * * 1", datetime(sun.year, sun.month, sun.day, 9, 0)) is False

    def test_weekly_sunday_both_0_and_7(self):
        sun = datetime(2026, 7, 26)  # 周日
        assert cron_matches("0 9 * * 0", datetime(sun.year, sun.month, sun.day, 9, 0)) is True
        assert cron_matches("0 9 * * 7", datetime(sun.year, sun.month, sun.day, 9, 0)) is True

    def test_monthly_1st(self):
        assert cron_matches("0 8 1 * *", datetime(2026, 7, 1, 8, 0)) is True
        assert cron_matches("0 8 1 * *", datetime(2026, 7, 2, 8, 0)) is False

    def test_range_and_list(self):
        # 分钟 5-10 且 15,30,45
        assert cron_matches("5-10 * * * *", datetime(2026, 7, 27, 12, 7)) is True
        assert cron_matches("5-10 * * * *", datetime(2026, 7, 27, 12, 11)) is False
        assert cron_matches("15,30,45 * * * *", datetime(2026, 7, 27, 12, 30)) is True
        assert cron_matches("15,30,45 * * * *", datetime(2026, 7, 27, 12, 31)) is False

    def test_day_and_dow_union_semantics(self):
        """Unix cron 语义：day 与 dow 都非 * 时任一命中即算匹配。"""
        # 每月 1 号或者每周日的 9:00 都触发
        assert cron_matches("0 9 1 * 0", datetime(2026, 7, 1, 9, 0)) is True    # 1 号（不管周几）
        assert cron_matches("0 9 1 * 0", datetime(2026, 7, 26, 9, 0)) is True   # 周日（不管几号）
        assert cron_matches("0 9 1 * 0", datetime(2026, 7, 15, 9, 0)) is False  # 都不是

    def test_malformed_expressions(self):
        # 字段数不对
        assert cron_matches("* * * *", datetime.now()) is False
        # 越界
        assert cron_matches("60 * * * *", datetime.now()) is False
        # 空字段
        assert cron_matches("* * * * ", datetime.now()) is False
        # 非数字
        assert cron_matches("abc * * * *", datetime.now()) is False


# ---------- cron_from_dropdown ----------

class TestCronFromDropdown:
    def test_interval_small(self):
        assert cron_from_dropdown("interval", "5") == "*/5 * * * *"

    def test_interval_hour_multiple(self):
        assert cron_from_dropdown("interval", "60") == "0 */1 * * *"
        assert cron_from_dropdown("interval", "180") == "0 */3 * * *"

    def test_interval_illegal(self):
        # 大于 59 但不是 60 倍数
        with pytest.raises(ValueError, match="interval"):
            cron_from_dropdown("interval", "75")
        # 超过一天
        with pytest.raises(ValueError, match="interval"):
            cron_from_dropdown("interval", "1440")
        # 非整数
        with pytest.raises(ValueError, match="interval"):
            cron_from_dropdown("interval", "abc")
        # 0/负数
        with pytest.raises(ValueError, match="interval"):
            cron_from_dropdown("interval", "0")

    def test_daily(self):
        assert cron_from_dropdown("daily", "09:30") == "30 9 * * *"
        assert cron_from_dropdown("daily", "23:00") == "0 23 * * *"

    def test_weekly(self):
        assert cron_from_dropdown("weekly", "1 09:30") == "30 9 * * 1"
        assert cron_from_dropdown("weekly", "0 08:00") == "0 8 * * 0"

    def test_monthly(self):
        assert cron_from_dropdown("monthly", "15 09:00") == "0 9 15 * *"

    def test_cron_passthrough(self):
        assert cron_from_dropdown("cron", "*/10 * * * *") == "*/10 * * * *"
        with pytest.raises(ValueError, match="5 字段"):
            cron_from_dropdown("cron", "* * * *")

    def test_hhmm_illegal(self):
        with pytest.raises(ValueError, match="HH:MM"):
            cron_from_dropdown("daily", "25:00")
        with pytest.raises(ValueError, match="HH:MM"):
            cron_from_dropdown("daily", "9:60")
        with pytest.raises(ValueError, match="HH:MM"):
            cron_from_dropdown("daily", "abc")

    def test_unknown_type(self):
        with pytest.raises(ValueError, match="未知调度类型"):
            cron_from_dropdown("secondly", "5")


# ---------- WorkflowScheduleStore ----------

class TestScheduleStore:
    def test_upsert_and_get(self, tmp_path):
        store = WorkflowScheduleStore(tmp_path / "s.sqlite3")
        s = store.upsert("wf1", "interval", "5")
        assert s["name"] == "wf1" and s["cron_type"] == "interval"
        assert s["enabled"] is True and s["notify_on"] == "failure"
        assert s["attach_kinds"] == ["summary"]

    def test_upsert_overwrites(self, tmp_path):
        store = WorkflowScheduleStore(tmp_path / "s.sqlite3")
        store.upsert("wf1", "interval", "5")
        s = store.upsert("wf1", "daily", "09:00", enabled=False,
                         notify_on="always", attach_kinds=["summary", "markdown_table"])
        assert s["cron_type"] == "daily" and s["enabled"] is False
        assert set(s["attach_kinds"]) == {"summary", "markdown_table"}

    def test_reject_bad_cron_or_notify_on(self, tmp_path):
        store = WorkflowScheduleStore(tmp_path / "s.sqlite3")
        with pytest.raises(ValueError):
            store.upsert("wf1", "interval", "999")
        with pytest.raises(ValueError, match="notify_on"):
            store.upsert("wf1", "daily", "09:00", notify_on="weekly")
        with pytest.raises(ValueError, match="attach_kinds"):
            store.upsert("wf1", "daily", "09:00", attach_kinds=["png"])

    def test_list_and_enabled(self, tmp_path):
        store = WorkflowScheduleStore(tmp_path / "s.sqlite3")
        store.upsert("a", "interval", "5")
        store.upsert("b", "interval", "5", enabled=False)
        assert {x["name"] for x in store.list()} == {"a", "b"}
        assert {x["name"] for x in store.list_enabled()} == {"a"}

    def test_delete(self, tmp_path):
        store = WorkflowScheduleStore(tmp_path / "s.sqlite3")
        store.upsert("a", "interval", "5")
        store.delete("a")
        assert store.get("a") is None

    def test_mark_ran(self, tmp_path):
        store = WorkflowScheduleStore(tmp_path / "s.sqlite3")
        store.upsert("a", "interval", "5")
        store.mark_ran("a", "ok")
        s = store.get("a")
        assert s["last_status"] == "ok" and s["last_run_at"]


# ---------- WorkflowRunStore ----------

class TestRunStore:
    def test_start_and_finish_ok(self, tmp_path):
        store = WorkflowRunStore(tmp_path / "r.sqlite3")
        rid = store.start("wf1", triggered_by="schedule")
        store.finish(rid, "ok", steps=[{"name": "s1", "ok": True}],
                     output_preview={"cols": ["a"], "rows": [[1]]},
                     xlsx_path="wf/1/output.xlsx")
        r = store.get(rid)
        assert r["status"] == "ok" and r["triggered_by"] == "schedule"
        assert r["steps"][0]["name"] == "s1"
        assert r["output_preview"]["rows"] == [[1]]
        assert r["xlsx_path"] == "wf/1/output.xlsx"

    def test_finish_failed_carries_error(self, tmp_path):
        store = WorkflowRunStore(tmp_path / "r.sqlite3")
        rid = store.start("wf1")
        store.finish(rid, "failed", error="boom")
        r = store.get(rid)
        assert r["status"] == "failed" and r["error"] == "boom"

    def test_list_by_name_orders_desc(self, tmp_path):
        store = WorkflowRunStore(tmp_path / "r.sqlite3")
        ids = [store.start("wf1") for _ in range(3)]
        for rid in ids:
            store.finish(rid, "ok")
        rows = store.list_by_name("wf1")
        assert [r["id"] for r in rows] == list(reversed(ids))

    def test_running_for(self, tmp_path):
        store = WorkflowRunStore(tmp_path / "r.sqlite3")
        rid = store.start("wf1")
        assert len(store.running_for("wf1")) == 1
        store.finish(rid, "ok")
        assert store.running_for("wf1") == []

    def test_sweep_stale_running(self, tmp_path):
        """服务重启：1 小时前还标 running → 标 failed。"""
        store = WorkflowRunStore(tmp_path / "r.sqlite3")
        rid = store.start("wf1")
        # 手工把 started_at 改到 2 小时前
        store._conn.execute(  # noqa: SLF001
            "UPDATE workflow_run SET started_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", rid))
        store._conn.commit()  # noqa: SLF001
        n = store.sweep_stale_running(older_than_hours=1)
        assert n == 1
        r = store.get(rid)
        assert r["status"] == "failed" and "重启中断" in r["error"]

    def test_sweep_leaves_fresh_running(self, tmp_path):
        """刚起的 running 不动。"""
        store = WorkflowRunStore(tmp_path / "r.sqlite3")
        rid = store.start("wf1")
        assert store.sweep_stale_running(older_than_hours=1) == 0
        assert store.get(rid)["status"] == "running"

    def test_purge_older_than_returns_xlsx_paths(self, tmp_path):
        store = WorkflowRunStore(tmp_path / "r.sqlite3")
        rid_old = store.start("wf1")
        store.finish(rid_old, "ok", xlsx_path="wf/1/output.xlsx")
        store._conn.execute(  # noqa: SLF001
            "UPDATE workflow_run SET started_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", rid_old))
        store._conn.commit()  # noqa: SLF001
        rid_new = store.start("wf1")
        store.finish(rid_new, "ok")
        paths = store.purge_older_than(days=1)
        assert paths == ["wf/1/output.xlsx"]
        assert store.get(rid_old) is None
        assert store.get(rid_new) is not None
