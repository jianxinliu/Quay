"""看板（/admin/dashboard）：在途操作登记簿、流量统计、连接快照。

覆盖三层：
- 纯函数（metrics）：体积估算、可读字节数；
- 存储层（AuditStore 聚合）：读写分开统计、时间桶、排行、老库迁移补列；
- 服务/HTTP：快照结构、**查询执行期间确实能在快照里看到它**（这是看板的核心承诺，
  审计记录要等操作结束才落库，答不了「此刻谁在查」）。
"""

import sqlite3
import threading

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditRecord, AuditStore
from dbmcp.config import AppConfig
from dbmcp.metrics import LiveOps, estimate_cell_bytes, estimate_result_bytes, human_bytes
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

TOKEN = "test-admin-token"
CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-dash")
EPOCH = "2000-01-01T00:00:00.000+00:00"


# ---------------- 纯函数 ----------------

class TestEstimateBytes:
    def test_ascii_string_is_its_length(self):
        assert estimate_cell_bytes("abc") == 3 + 2

    def test_utf8_counted_in_bytes_not_chars(self):
        """中文一个字 3 字节——按字符数算会把流量少报三倍。"""
        assert estimate_cell_bytes("中文") == 6 + 2

    def test_none_and_numbers_have_only_overhead(self):
        assert estimate_cell_bytes(None) == 2
        assert estimate_cell_bytes(42) == len("42") + 2

    def test_containers_recurse(self):
        """JSON 列 / bytes 包装是嵌套结构，必须递归求和而不是当成一个标量。"""
        got = estimate_cell_bytes({"k": "abcd"})
        assert got == estimate_cell_bytes("k") + estimate_cell_bytes("abcd")

    def test_result_counts_header_once_and_every_cell(self):
        cols, rows = ["id", "name"], [[1, "ab"], [2, "cd"]]
        expect = sum(estimate_cell_bytes(c) for c in cols) + sum(
            estimate_cell_bytes(v) for row in rows for v in row)
        assert estimate_result_bytes(cols, rows) == expect

    def test_empty_result_is_zero(self):
        assert estimate_result_bytes([], []) == 0

    def test_human_bytes(self):
        assert human_bytes(0) == "0 B"
        assert human_bytes(999) == "999 B"
        assert human_bytes(1536) == "1.5 KB"
        assert human_bytes(5 * 1024 * 1024) == "5.0 MB"


class TestLiveOps:
    def test_begin_shows_up_and_end_removes(self):
        live = LiveOps()
        op = live.begin("demo", "main", "query", agent="a", session_id="s", sql="SELECT 1")
        snap = live.snapshot()
        assert len(snap) == 1 and snap[0]["tool"] == "query" and snap[0]["sql"] == "SELECT 1"
        assert snap[0]["elapsed_ms"] >= 0
        live.end(op)
        assert live.snapshot() == [] and live.count() == 0

    def test_track_ends_even_on_exception(self):
        live = LiveOps()
        with pytest.raises(RuntimeError), live.track("demo", "main", "query"):
            raise RuntimeError("boom")
        assert live.count() == 0

    def test_long_sql_is_clipped(self):
        live = LiveOps(max_sql_chars=10)
        live.begin("demo", "main", "query", sql="S" * 200)
        assert live.snapshot()[0]["sql"] == "S" * 10 + "…"

    def test_longest_running_first(self):
        """看板最关心「哪条卡住了」，所以跑得最久的排最前。"""
        live = LiveOps()
        live.begin("demo", "main", "query", sql="new")
        old_id = live.begin("demo", "main", "query", sql="old")
        live._ops[old_id].started_at -= 10   # 假装它已经跑了 10 秒
        assert [o["sql"] for o in live.snapshot()] == ["old", "new"]

    def test_end_unknown_id_is_noop(self):
        LiveOps().end(999)


# ---------------- 审计聚合 ----------------

@pytest.fixture
def store(tmp_path):
    s = AuditStore(tmp_path / "audit.sqlite3")
    yield s
    s.close()


def _rec(**kw):
    base = dict(project="demo", connection="main", tool="query", status="ok",
                agent="pytest/1.0", session_id="sess-dash")
    base.update(kw)
    return AuditRecord(**base)


class TestTrafficSummary:
    def test_reads_and_writes_counted_separately(self, store):
        """写工具的 row_count 是「影响行数」，和读出的行数加在一起毫无意义。"""
        store.record(_rec(tool="query", row_count=10, result_bytes=500))
        store.record(_rec(tool="execute", status="ok", row_count=3))
        got = store.traffic_summary(EPOCH)
        assert got["rows_read"] == 10
        assert got["rows_written"] == 3
        assert got["writes"] == 1
        assert got["bytes_read"] == 500

    def test_rejected_write_not_counted_as_written(self, store):
        """首提生成审批单那条是 rejected（没落库），不能算作改过数据。"""
        store.record(_rec(tool="execute", status="rejected", row_count=99))
        got = store.traffic_summary(EPOCH)
        assert got["rows_written"] == 0
        assert got["rejected"] == 1 and got["writes"] == 1

    def test_status_and_timing_rollup(self, store):
        store.record(_rec(status="ok", duration_ms=10))
        store.record(_rec(status="error", duration_ms=30))
        got = store.traffic_summary(EPOCH)
        assert (got["ops"], got["ok"], got["error"]) == (2, 1, 1)
        assert got["avg_ms"] == 20 and got["max_ms"] == 30

    def test_empty_window_is_all_zero_not_none(self, store):
        """SUM() 在空表上返回 NULL——不归零的话看板会显示 "null"。"""
        got = store.traffic_summary("2999-01-01T00:00:00.000+00:00")
        assert got["ops"] == 0 and got["bytes_read"] == 0 and got["avg_ms"] == 0

    def test_since_filters_out_older(self, store):
        store.record(_rec())
        store._conn.execute("UPDATE audit_log SET ts = '2020-01-01T00:00:00.000+00:00'")
        store._conn.commit()
        assert store.traffic_summary("2021-01-01T00:00:00.000+00:00")["ops"] == 0
        assert store.traffic_summary(EPOCH)["ops"] == 1


class TestTrafficSeries:
    def test_hour_buckets(self, store):
        store.record(_rec(result_bytes=100))
        store.record(_rec(result_bytes=200))
        series = store.traffic_series(EPOCH, bucket="hour")
        assert len(series) == 1
        assert len(series[0]["bucket"]) == 13   # 'YYYY-MM-DDTHH'
        assert series[0]["ops"] == 2 and series[0]["bytes_read"] == 300

    def test_minute_buckets_are_finer(self, store):
        store.record(_rec())
        assert len(store.traffic_series(EPOCH, bucket="minute")[0]["bucket"]) == 16

    def test_failed_counted_per_bucket(self, store):
        store.record(_rec(status="error"))
        assert store.traffic_series(EPOCH)[0]["failed"] == 1

    def test_day_buckets_for_long_windows(self, store):
        store.record(_rec())
        assert len(store.traffic_series(EPOCH, bucket="day")[0]["bucket"]) == 10

    def test_bad_bucket_rejected(self, store):
        with pytest.raises(ValueError, match="聚合粒度"):
            store.traffic_series(EPOCH, bucket="week")


class TestTopGroups:
    def test_ranked_by_ops(self, store):
        store.record(_rec(connection="a"))
        store.record(_rec(connection="b"))
        store.record(_rec(connection="b"))
        got = store.top_groups("connection", EPOCH)
        assert [g["name"] for g in got] == ["b", "a"]
        assert got[0]["ops"] == 2

    def test_column_whitelisted(self, store):
        """列名要拼进 SQL，只能来自白名单。"""
        with pytest.raises(ValueError, match="不可分组"):
            store.top_groups("sql; DROP TABLE audit_log", EPOCH)

    def test_blank_names_skipped(self, store):
        store.record(_rec(session_id=""))
        assert store.top_groups("session_id", EPOCH) == []


def test_old_db_gets_result_bytes_column(tmp_path):
    """老库没有 result_bytes 列（CREATE TABLE IF NOT EXISTS 不补列），须自动 ALTER。"""
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE audit_log (
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent TEXT,
             session_id TEXT, project TEXT NOT NULL, connection TEXT NOT NULL,
             environment TEXT, engine TEXT, tool TEXT NOT NULL, sql TEXT,
             fingerprint TEXT, status TEXT NOT NULL, detail TEXT,
             row_count INTEGER, duration_ms INTEGER);"""
    )
    conn.commit()
    conn.close()

    store = AuditStore(path)
    store.record(_rec(result_bytes=42))
    assert store.traffic_summary(EPOCH)["bytes_read"] == 42
    store.close()


# ---------------- 服务层 ----------------

@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);
           INSERT INTO users (name) VALUES ('alice'), ('bob');"""
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     ApprovalStore(tmp_path / "a.sqlite3"))
    yield svc
    svc.close()


class TestDashboardSnapshot:
    def test_shape(self, service):
        snap = service.dashboard_snapshot()
        assert set(snap) >= {"generated_at", "window", "uptime_s", "connections",
                             "live", "traffic", "series", "top", "sessions", "approvals"}
        assert snap["connections"]["configured"] == 1
        assert snap["connections"]["items"][0]["connection"] == "main"
        assert snap["live"] == {"count": 0, "ops": []}

    def test_sessions_use_fixed_recent_days_not_the_window(self, service):
        """会话列表不跟随统计窗口：选 1 小时就看不见昨天的会话，选 30 天又会翻出陈年会话。"""
        for window in ("1h", "30d"):
            snap = service.dashboard_snapshot(window)
            assert snap["session_days"] == service.DASHBOARD_SESSION_DAYS

    def test_bad_window_rejected(self, service):
        with pytest.raises(ValueError, match="统计窗口"):
            service.dashboard_snapshot("all")

    def test_bucket_granularity_follows_window(self, service):
        """一律按小时的话：1 小时窗口只剩一两个点，30 天窗口有 720 根柱子。"""
        assert service.dashboard_snapshot("1h")["bucket"] == "minute"
        assert service.dashboard_snapshot("24h")["bucket"] == "hour"
        assert service.dashboard_snapshot("30d")["bucket"] == "day"

    def test_query_traffic_lands_in_snapshot(self, service):
        service.query("demo", "main", "SELECT * FROM users", CALLER)
        traffic = service.dashboard_snapshot()["traffic"]
        assert traffic["ops"] == 1
        assert traffic["rows_read"] == 2
        assert traffic["bytes_read"] > 0        # 结果体积被记进了审计

    def test_pooled_engine_shows_up_after_query(self, service):
        before = service.dashboard_snapshot()["connections"]["pooled_engines"]
        service.query("demo", "main", "SELECT 1", CALLER)
        item = service.dashboard_snapshot()["connections"]["items"][0]
        assert before == 0 and item["engines"] == 1 and item["state"] == "ok"

    def test_unhealthy_connection_reported(self, service):
        service.health.mark_failed("demo", "main", "OperationalError: gone away")
        conns = service.dashboard_snapshot()["connections"]
        assert conns["unhealthy"] == 1
        assert conns["items"][0]["state"] == "unavailable"
        assert "gone away" in conns["items"][0]["last_error"]

    def test_running_query_is_visible_while_it_runs(self, service, monkeypatch):
        """看板的核心承诺：操作**还没结束**时就能看到它。

        审计要等操作结束才落库，所以只有在途登记簿答得了「此刻谁在查」。这里在查询
        执行到一半时（真正在 run_query 里面）去照一张快照，断言它已经在里面了。
        """
        from dbmcp import engines

        seen = {}
        real = engines.run_query

        def spy(*a, **kw):
            seen["snapshot"] = service.dashboard_snapshot()["live"]
            return real(*a, **kw)

        monkeypatch.setattr(engines, "run_query", spy)
        service.query("demo", "main", "SELECT * FROM users", CALLER)

        live = seen["snapshot"]
        assert live["count"] == 1
        op = live["ops"][0]
        assert op["tool"] == "query" and op["connection"] == "main"
        assert op["sql"].startswith("SELECT * FROM users")
        assert op["session_id"] == "sess-dash" and op["agent"] == "pytest/1.0"
        # 查询结束后必须清干净，否则看板会永远显示一条幽灵查询
        assert service.dashboard_snapshot()["live"]["count"] == 0

    def test_failed_query_also_unregisters(self, service):
        """出错路径也得注销——只在成功分支清理是最容易漏的那种泄漏。"""
        with pytest.raises(Exception):
            service.query("demo", "main", "SELECT * FROM no_such_table", CALLER)
        assert service.live.count() == 0

    def test_live_registry_is_thread_safe(self, service):
        def worker():
            for _ in range(20):
                service.query("demo", "main", "SELECT 1", CALLER)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert service.live.count() == 0
        assert service.dashboard_snapshot()["traffic"]["ops"] == 80


# ---------------- HTTP ----------------

@pytest.fixture
def client(service):
    mcp = build_mcp(service)
    mount_admin(mcp, service, admin_token=TOKEN)
    with TestClient(mcp.http_app()) as tc:
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc


class TestDashboardHttp:
    def test_page_renders_mount_point_and_assets(self, client):
        page = client.get("/admin/dashboard").text
        assert 'id="dash"' in page
        assert "/admin/static/dashboard.js" in page
        assert "/admin/static/dashboard.css" in page

    def test_echarts_loaded_before_the_page_script(self, client):
        """echarts 是 UMD 包，必须同步加载且早于用它的脚本，否则 window.echarts 是 undefined。"""
        page = client.get("/admin/dashboard").text
        vendor = page.index("/admin/static/echarts.min.js")
        assert "defer" not in page[vendor - 40:vendor]      # vendor 必须同步加载
        assert vendor < page.index("/admin/static/dashboard.js")

    def test_nav_links_to_dashboard(self, client):
        assert 'href="/admin/dashboard"' in client.get("/admin/audit").text

    def test_data_endpoint(self, client, service):
        service.query("demo", "main", "SELECT * FROM users", CALLER)
        body = client.get("/admin/dashboard/data?window=24h").json()
        assert body["window"] == "24h"
        assert body["traffic"]["ops"] >= 1
        assert body["connections"]["items"][0]["connection"] == "main"

    def test_bad_window_is_400_not_500(self, client):
        r = client.get("/admin/dashboard/data?window=forever")
        assert r.status_code == 400 and r.json()["ok"] is False

    def test_requires_auth(self, service):
        mcp = build_mcp(service)
        mount_admin(mcp, service, admin_token=TOKEN)
        with TestClient(mcp.http_app()) as tc:   # 不登录
            r = tc.get("/admin/dashboard/data",
                       headers={"Accept": "application/json"})
            assert r.status_code == 401

    def test_static_assets_are_no_cache(self, client):
        """自家静态文件必须 no-cache，否则改完前端浏览器还跑旧的（见 CLAUDE.md 教训）。"""
        for path in ("dashboard.js", "dashboard.css"):
            r = client.get(f"/admin/static/{path}")
            assert r.status_code == 200
            assert r.headers["cache-control"] == "no-cache"
