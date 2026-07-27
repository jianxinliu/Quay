"""service 层调度器集成测试：CRUD + tick 触发 + 富通知发送。"""

import sqlite3
import time

import pytest

pytest.importorskip("duckdb")

from dbmcp.analysis import AnalysisStore  # noqa: E402
from dbmcp.audit.log import AuditStore  # noqa: E402
from dbmcp.config import AppConfig  # noqa: E402
from dbmcp.notify import Notifier  # noqa: E402
from dbmcp.service import CallerInfo, DbmService  # noqa: E402
from dbmcp.workflows import WorkflowRunStore, WorkflowScheduleStore, WorkflowStore  # noqa: E402

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")


class CapturingNotifier(Notifier):
    """测试用：把所有 send 调用记录到列表，供断言用。"""

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, title, body, meta=None):
        self.sent.append({"title": title, "body": body, "meta": meta or {}})


@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        "INSERT INTO users (name, age) VALUES ('alice', 30), ('bob', 25);"
    )
    conn.commit(); conn.close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(db_file), "environment": "local",
    }}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     notifier=CapturingNotifier())
    svc.analysis = AnalysisStore(tmp_path / "analysis")
    svc.workflows = WorkflowStore(tmp_path / "wf.sqlite3")
    svc.schedules = WorkflowScheduleStore(tmp_path / "sched.sqlite3")
    svc.runs = WorkflowRunStore(tmp_path / "runs.sqlite3")
    svc.data_dir = str(tmp_path / "data")
    yield svc
    svc.close()


def _mk_wf(svc, name="wf1"):
    """建一个最小 workflow：一个 source 从 demo/main 拉 users。"""
    g = {"nodes": [
        {"id": "a", "type": "source", "name": "u", "x": 0, "y": 0,
         "cfg": {"conn": "demo/main", "sql": "SELECT * FROM users"}}],
        "edges": []}
    svc.workflow_save(name, "ws1", "", CALLER, graph=g)


class TestScheduleCRUD:
    def test_upsert_requires_existing_workflow(self, service):
        with pytest.raises(ValueError, match="不存在"):
            service.workflow_schedule_upsert("nope", "interval", "5")

    def test_upsert_and_get(self, service):
        _mk_wf(service, "wf1")
        s = service.workflow_schedule_upsert("wf1", "interval", "5")
        assert s["name"] == "wf1" and s["enabled"] is True
        got = service.workflow_schedule_get("wf1")
        assert got["cron_type"] == "interval"

    def test_delete(self, service):
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "interval", "5")
        service.workflow_schedule_delete("wf1")
        assert service.workflow_schedule_get("wf1") is None

    def test_list(self, service):
        _mk_wf(service, "wf1")
        _mk_wf(service, "wf2")
        service.workflow_schedule_upsert("wf1", "interval", "5")
        service.workflow_schedule_upsert("wf2", "daily", "09:00")
        assert {x["name"] for x in service.workflow_schedule_list()} == {"wf1", "wf2"}


class TestRunScheduled:
    def test_creates_run_record_and_marks_ok(self, service):
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "interval", "5", notify_on="always")
        service._run_scheduled("wf1")  # noqa: SLF001
        runs = service.workflow_runs_list("wf1")
        assert len(runs) == 1
        assert runs[0]["status"] == "ok"
        assert runs[0]["triggered_by"] == "schedule"
        # notify_on=always → notifier 应被调
        assert len(service.notifier.sent) == 1
        assert "wf1" in service.notifier.sent[0]["title"]

    def test_notify_on_failure_only(self, service):
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "interval", "5", notify_on="failure")
        service._run_scheduled("wf1")  # noqa: SLF001
        # ok → 不发通知
        assert service.notifier.sent == []

    def test_notify_on_none_never_sends(self, service):
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "interval", "5", notify_on="none")
        service._run_scheduled("wf1")  # noqa: SLF001
        assert service.notifier.sent == []

    def test_failed_run_updates_status(self, service):
        # 建一个失败的 workflow（sql 里引用不存在的表）
        g = {"nodes": [
            {"id": "a", "type": "source", "name": "u", "x": 0, "y": 0,
             "cfg": {"conn": "demo/main", "sql": "SELECT * FROM not_exist"}}],
            "edges": []}
        service.workflow_save("fail_wf", "ws_fail", "", CALLER, graph=g)
        service.workflow_schedule_upsert("fail_wf", "interval", "5", notify_on="failure")
        service._run_scheduled("fail_wf")  # noqa: SLF001
        runs = service.workflow_runs_list("fail_wf")
        assert runs[0]["status"] == "failed"
        assert runs[0]["error"]
        assert len(service.notifier.sent) == 1
        assert "失败" in service.notifier.sent[0]["title"]

    def test_skip_when_already_running(self, service):
        """同名 workflow 已 running → 跳过本次调度。"""
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "interval", "5", notify_on="always")
        # 手动塞一条 running 记录（模拟上一次没完成）
        service.runs.start("wf1", triggered_by="schedule")
        service._run_scheduled("wf1")  # noqa: SLF001
        # 应该跳过：仍只有那条手塞的 running
        runs = service.workflow_runs_list("wf1")
        assert len(runs) == 1
        assert runs[0]["status"] == "running"
        assert service.notifier.sent == []

    def test_xlsx_saved_when_attach_kinds_includes_xlsx(self, service, tmp_path):
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "interval", "5",
                                          attach_kinds=["summary", "xlsx_link"])
        service._run_scheduled("wf1")  # noqa: SLF001
        runs = service.workflow_runs_list("wf1")
        r = runs[0]
        assert r["xlsx_path"] and r["xlsx_path"].endswith("output.xlsx")
        # 文件真的存在
        from pathlib import Path
        assert (Path(service.data_dir) / r["xlsx_path"]).is_file()

    def test_xlsx_not_saved_without_kind(self, service):
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "interval", "5", attach_kinds=["summary"])
        service._run_scheduled("wf1")  # noqa: SLF001
        assert service.workflow_runs_list("wf1")[0]["xlsx_path"] is None


class TestStartSchedulerLifecycle:
    def test_sweeps_stale_running_on_start(self, service):
        """启动时把 1h 前 running 的记录标 failed。"""
        _mk_wf(service, "wf1")
        rid = service.runs.start("wf1", triggered_by="schedule")
        # 手改 started_at 到 2h 前
        service.runs._conn.execute(  # noqa: SLF001
            "UPDATE workflow_run SET started_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (rid,))
        service.runs._conn.commit()  # noqa: SLF001
        service.start_scheduler(interval_s=3600)  # 长间隔 → tick 不会触发
        assert service.runs.get(rid)["status"] == "failed"

    def test_tick_fires_matched_schedule(self, service, monkeypatch):
        """cron_matches 判命中时应触发 _run_scheduled。"""
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "cron", "* * * * *")  # 每分钟
        called: list[str] = []
        monkeypatch.setattr(service, "_run_scheduled",
                            lambda name: called.append(name))
        service._scheduler_tick()  # noqa: SLF001
        # 后台线程可能还没跑到，短等一下
        time.sleep(0.3)
        assert called == ["wf1"]

    def test_tick_dedups_within_minute(self, service, monkeypatch):
        """同分钟内多次 tick 只触发一次（防 30s tick 在一分钟内跑两次）。"""
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "cron", "* * * * *")
        called: list[str] = []
        monkeypatch.setattr(service, "_run_scheduled",
                            lambda name: called.append(name))
        service._scheduler_tick()  # noqa: SLF001
        service._scheduler_tick()  # noqa: SLF001
        time.sleep(0.3)
        assert called == ["wf1"]

    def test_tick_skips_disabled(self, service, monkeypatch):
        _mk_wf(service, "wf1")
        service.workflow_schedule_upsert("wf1", "cron", "* * * * *", enabled=False)
        called: list[str] = []
        monkeypatch.setattr(service, "_run_scheduled",
                            lambda name: called.append(name))
        service._scheduler_tick()  # noqa: SLF001
        time.sleep(0.2)
        assert called == []
