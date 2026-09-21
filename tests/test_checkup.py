"""数据库体检的单元测试。

引擎层诊断 SQL 已在真实库上做过 e2e（MySQL 9.5 / PostgreSQL 16 / ClickHouse 25.3 /
SQLite 3.50，见 scripts/e2e_checkup.py）；这里覆盖：
- SQLite 的真实执行（完整性/碎片/日志模式/表统计）
- 阈值分支（ok/warn/critical 怎么判）：用脚本化引擎喂入构造数据，不依赖真库
- 逐项容错：一条诊断失败不会毒化其它项
- overall 对 unknown 的处理
- service 层：审计落库、Redis 拒绝
- 各引擎诊断 SQL 必须能在对应方言下被 sqlglot 解析（静态防笔误）
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
import sqlglot
from sqlalchemy import create_engine

from dbmcp import checkup
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-checkup")


# =====================================================================
# 脚本化引擎：把 SQL 片段映射到预设结果。未命中的 SQL 直接抛错——
# 测试必须显式覆盖每一条查询，否则会被当成「漏测的查询」。
# =====================================================================

class _Result:
    returns_rows = True

    def __init__(self, rows, cols=None):
        self._rows = rows
        self._cols = cols

    def keys(self):
        if self._cols:
            return self._cols
        return [f"c{i}" for i in range(len(self._rows[0]) if self._rows else 0)]

    def fetchall(self):
        return [tuple(r) for r in self._rows]


class _Conn:
    def __init__(self, owner):
        self.owner = owner

    def execute(self, sql, params=None):
        sql = str(sql)
        self.owner.queries.append(sql)
        low = sql.lower()
        if low.strip() == "select 1":  # 连接探活：脚本化引擎默认放行，死库测试另用 _DeadEngine
            return _Result([[1]])
        for route in self.owner.routes:
            frag, rows = route[0], route[1]
            cols = route[2] if len(route) > 2 else None
            if frag.lower() in low:
                return _Result(rows, cols)
        raise AssertionError(f"脚本化引擎未覆盖的 SQL: {sql}")

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Scripted:
    """假引擎：按 SQL 片段（先匹配优先）返回预设行。"""

    def __init__(self, routes: list[tuple[str, list[list]]]):
        self.routes = routes
        self.queries: list[str] = []

    def connect(self):
        return _Conn(self)


class _DeadConn:
    """连接建立就失败——模拟数据库不可达（端口拒绝/隧道断了）。"""

    def __enter__(self):
        raise _make_operational_error()

    def __exit__(self, *a):
        return False


class _DeadEngine:
    """所有连接尝试都失败。run_checkup 的探活应该一次拦下，不跑十几项诊断。"""

    def connect(self):
        return _DeadConn()


def _make_operational_error() -> Exception:
    """造一条真实的 SQLAlchemy OperationalError（带 Background 尾巴），测文本清洗。"""
    try:
        import psycopg
        inner = psycopg.OperationalError(
            "connection failed: connection to server at \"127.0.0.1\", port 55432 "
            "failed: could not receive data from server: Connection refused")
    except Exception:  # noqa: BLE001 - psycopg 不可用时退回普通异常
        return RuntimeError("connection refused")
    from sqlalchemy.exc import OperationalError
    return OperationalError("SELECT 1", {}, inner)


# =====================================================================
# fixtures
# =====================================================================

@pytest.fixture
def sqlite_db(tmp_path):
    db = tmp_path / "biz.sqlite3"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, amt REAL, note TEXT);"
        "CREATE INDEX idx_orders_amt ON orders (amt);"
    )
    con.executemany("INSERT INTO orders (amt, note) VALUES (?, ?)",
                    [(i * 1.5, f"n{i}") for i in range(200)])
    con.execute("DELETE FROM orders WHERE id % 2 = 0")
    con.commit()
    con.close()
    return str(db)


@pytest.fixture
def service(sqlite_db, tmp_path):
    cfg = AppConfig.model_validate({
        "projects": {
            "demo": {
                "connections": {
                    "main": {
                        "engine": "sqlite",
                        "database": sqlite_db,
                        "environment": "local",
                    }
                }
            }
        }
    })
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"))
    svc.data_dir = str(tmp_path / "data")
    yield svc
    svc.close()


# =====================================================================
# SQLite 真实执行
# =====================================================================

def test_sqlite_checkup_runs(sqlite_db):
    rep = checkup.run_checkup(create_engine(f"sqlite:///{sqlite_db}"), "sqlite")
    d = rep.to_dict()
    assert d["engine"] == "sqlite"
    names = {c["name"] for c in d["checks"]}
    assert names >= {"server", "integrity", "fragmentation", "journal_mode", "tables"}
    integ = next(c for c in d["checks"] if c["name"] == "integrity")
    assert integ["status"] == "ok" and integ["value"] == "正常"
    assert d["elapsed_ms"] >= 0
    assert "共" in d["summary"]


def test_unsupported_engine_degrades():
    rep = checkup.run_checkup(Scripted([]), "redis")
    assert rep.overall == "unknown"
    assert rep.checks[0].name == "unsupported"
    assert rep.checks[0].value == "redis"
    assert "redis" not in rep.checks[0].message  # 支持列表里只列支持的引擎


# =====================================================================
# MySQL 阈值分支
# =====================================================================

STATUS_VARS = {
    "Uptime": "3600", "Threads_connected": "10", "Threads_running": "3",
    "Connections": "100", "Innodb_buffer_pool_read_requests": "99000",
    "Innodb_buffer_pool_reads": "1000", "Slow_queries": "5",
    "Select_full_join": "2", "Select_range_join": "98",
    "Aborted_clients": "6", "Aborted_connects": "1", "Innodb_deadlocks": "0",
    "Created_tmp_tables": "100", "Created_tmp_disk_tables": "10",
    "Binlog_cache_use": "1000", "Binlog_cache_disk_use": "0",
}
VARIABLES = {"max_connections": "100", "version": "8.0.42", "long_query_time": "10",
             "innodb_buffer_pool_size": "134217728"}


def scripted_mysql(status=None, variables=None, extra=(), replica_rows=None):
    st = dict(STATUS_VARS, **(status or {}))
    var = dict(VARIABLES, **(variables or {}))
    routes = list(extra) + [
        ("from performance_schema.global_status", [[k, v] for k, v in st.items()]),
        ("from performance_schema.global_variables", [[k, v] for k, v in var.items()]),
        ("events_errors_summary_global_by_error", [["0"]]),
        ("where processlist_time >= :t", []),                 # 无长查询（performance_schema.threads）
        ("from performance_schema.threads", [[37]]),          # 全部线程计数（无需 PROCESS）
        ("data_lock_waits", [[0]]),                           # 行锁等待（无需 PROCESS）
        ("innodb_trx", [[0]]),                                # 退路
        ("information_schema.statistics", []),                # 无主键表
        ("coalesce(sum(coalesce(data_length", [[1048576]]),   # 数据总量（缓冲池对比）
        ("from information_schema.tables", [["db", "t", "1048576", "999"]]),
    ]
    if replica_rows is not None:
        routes.append(("show replica status", replica_rows))
    else:
        routes.append(("show replica status", []))             # 非副本：空结果
    return Scripted(routes)


def mysql_report(scripted):
    return checkup.run_checkup(scripted, "mysql", schema=None)


def by_name(rep, name):
    return next(c for c in rep.checks if c.name == name)


def test_mysql_thresholds_ok():
    """健康数据下所有可判定项都是 ok（server/大表等参考项为 info，故 overall=info）。"""
    rep = mysql_report(scripted_mysql())
    assert rep.overall == "info", rep.summary
    bad = [c.title for c in rep.checks if c.status in ("warn", "critical", "unknown")]
    assert bad == [], f"健康数据下不该有需关注/无法测量的项：{bad}"
    conn = by_name(rep, "connections")
    assert conn.status == "ok" and "10%" in conn.value
    hit = by_name(rep, "buffer_pool_hit_ratio")
    assert hit.status == "ok" and hit.value == "99.00%"
    assert by_name(rep, "deadlocks").status == "ok"
    assert by_name(rep, "long_queries").status == "ok"
    assert by_name(rep, "replication_lag").status == "info"   # 非副本


def test_mysql_connections_critical():
    rep = mysql_report(scripted_mysql(status={"Threads_connected": "96"}))  # 96%
    assert by_name(rep, "connections").status == "critical"


def test_mysql_connections_warn():
    rep = mysql_report(scripted_mysql(status={"Threads_connected": "82"}))  # 82%
    assert by_name(rep, "connections").status == "warn"


def test_mysql_buffer_pool_warn():
    rep = mysql_report(scripted_mysql(status={
        "Innodb_buffer_pool_read_requests": "9400", "Innodb_buffer_pool_reads": "600"}))
    assert by_name(rep, "buffer_pool_hit_ratio").status == "warn"  # 94%


def test_mysql_buffer_pool_critical():
    rep = mysql_report(scripted_mysql(status={
        "Innodb_buffer_pool_read_requests": "8900", "Innodb_buffer_pool_reads": "1000"}))
    assert by_name(rep, "buffer_pool_hit_ratio").status == "critical"  # 89.9%


def test_mysql_deadlocks_warn():
    """CH 9.x 移除了 Innodb_deadlocks 状态变量；错误汇总表取不到时退到状态变量。"""
    rep = mysql_report(scripted_mysql(
        status={"Innodb_deadlocks": "3"},
        extra=[("events_errors_summary_global_by_error", [])]))
    # 错误汇总表为空 → 退到状态变量
    dead = by_name(rep, "deadlocks")
    assert dead.status == "warn" and dead.value == "3 次"


def test_mysql_deadlocks_from_error_table():
    """MySQL 9.x 移除了 Innodb_deadlocks 状态变量，死锁计数来自错误汇总表。"""
    rep = mysql_report(scripted_mysql(
        status={"Innodb_deadlocks": "0"},
        extra=[("events_errors_summary_global_by_error", [["7"]])]))
    dead = by_name(rep, "deadlocks")
    assert dead.status == "warn" and dead.value == "7 次"


def test_mysql_long_query_critical():
    rep = mysql_report(scripted_mysql(
        extra=[("where processlist_time >= :t",
                [[320, "SELECT * FROM big_table", "executing"]])]))
    long_ = by_name(rep, "long_queries")
    assert long_.status == "critical"
    assert long_.details and "320s" in long_.details[0]


def test_mysql_long_queries_no_process_needed():
    """v2 权限设计：长查询走 performance_schema.threads，无 PROCESS 权限也能看到全部线程，
    且连接数远高于可见线程数时不再误判为 unknown（旧 PROCESSLIST 启发式已移除）。"""
    rep = mysql_report(scripted_mysql(status={"Threads_connected": "50"}))
    long_ = by_name(rep, "long_queries")
    assert long_.status == "ok"


def test_mysql_lock_waits_warn():
    """data_lock_waits 不需要 PROCESS 权限即可见，是首选视图。"""
    rep = mysql_report(scripted_mysql(
        extra=[("data_lock_waits", [[5]])]))
    lock = by_name(rep, "lock_waits")
    assert lock.status == "warn" and "5" in lock.value


def test_mysql_replication_lag_warn():
    """有副本且延迟 120s → warn。SHOW REPLICA STATUS 走 engine.connect，列名由路由给定。"""
    cols = ["Replica_IO_State", "Source_Host", "Source_User", "Source_Port", "Connect_Retry",
            "Source_Log_File", "Read_Source_Log_Pos", "Replica_IO_Running",
            "Replica_SQL_Running", "Seconds_Behind_Source"]
    row = ["Waiting", "1.2.3.4", "r", "3306", "60", "bin.1", "100", "Yes", "Yes", "120"]
    rep = mysql_report(scripted_mysql(
        replica_rows=None,
        extra=[("show replica status", [row], cols)]))
    lag = by_name(rep, "replication_lag")
    assert lag.status == "warn"
    assert "120" in lag.value


# =====================================================================
# PostgreSQL 阈值分支
# =====================================================================

def scripted_pg(can_see=True, max_conn="100", extra=()):
    routes = list(extra) + [
        ("select version()", [["PostgreSQL 16.15 on x86_64"]]),
        ("pg_postmaster_start_time", _started_hours_ago(2)),
        ("pg_has_role", [[1 if can_see else 0]]),
        ("current_setting('max_connections')", [[max_conn]]),
        ("select count(*) from pg_stat_activity", [[10]]),  # 连接计数（行级可见，无需 pg_monitor）
        ("from pg_stat_activity", []),                    # 明细查询：无长查询/空闲事务/等待事件
        ("temp_files, temp_bytes", [[0, 0]]),             # 临时文件落盘
        ("sum(deadlocks)", [[0]]),                     # 死锁累计
        ("from pg_stat_database", [[3000.0, 20.0]]),   # 命中率 99.34%
        ("from pg_stat_user_tables", []),              # 死元组 + 统计信息过期
        ("pg_stat_user_indexes", []),                  # 未使用索引
        ("from pg_stat_replication", []),
        ("pg_replication_slots", []),                  # 复制槽健康度
        ("pg_stat_archiver", [[100, 0, ""]]),           # WAL 归档
        ("from pg_database", [[1_000_000, 2_147_483_648 - 1_000_000]]),  # 事务回卷：剩余充足
        ("pg_database_size", [["8119 kB"]]),
        ("from pg_class", [["orders", 548864]]),
    ]
    return Scripted(routes)


def _started_hours_ago(hours):
    return [[dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)]]


def pg_report(scripted, schema=None):
    return checkup.run_checkup(scripted, "postgres", schema=schema)


def test_pg_thresholds_ok():
    """健康数据下所有可判定项都是 ok（server/库大小等参考项为 info，故 overall=info）。"""
    rep = pg_report(scripted_pg())
    assert rep.overall == "info", rep.summary
    bad = [c.title for c in rep.checks if c.status in ("warn", "critical", "unknown")]
    assert bad == [], f"健康数据下不该有需关注/无法测量的项：{bad}"
    assert by_name(rep, "connections").status == "ok"
    assert by_name(rep, "cache_hit_ratio").status == "ok"
    assert by_name(rep, "idle_in_transaction").status == "ok"
    assert by_name(rep, "long_queries").status == "ok"
    assert by_name(rep, "bloat").status == "ok"
    assert by_name(rep, "replication_lag").status == "info"   # 无流复制


def test_pg_connections_critical():
    rep = pg_report(scripted_pg(
        max_conn="100",
        extra=[("select count(*) from pg_stat_activity", [[97]])]))
    assert by_name(rep, "connections").status == "critical"


def test_pg_restricted_reader_degrades():
    """无 pg_monitor 时：连接占用/复制槽这些只看「行」的项仍能测量（v2 权限设计），
    只有需要 state/query/wait_event 列的项（空闲事务/长查询/等待事件）才 unknown，
    且报告汇总出可复制的 GRANT 模板。"""
    rep = pg_report(scripted_pg(can_see=False))
    # 行级可见的项：照常出真值
    assert by_name(rep, "connections").status == "ok", "count(*) 行级可见，不该 unknown"
    assert by_name(rep, "replication_slots").status == "info"
    assert by_name(rep, "cache_hit_ratio").status == "ok"
    assert by_name(rep, "deadlocks").status == "ok"
    assert by_name(rep, "bloat").status == "ok"
    # 需要 state 列的项：明确标 unknown 并说明缺什么
    for name in ("idle_in_transaction", "long_queries", "wait_events"):
        c = by_name(rep, name)
        assert c.status == "unknown", f"{name} 应为 unknown"
        assert c.privilege == "pg_monitor"
        assert "pg_monitor" in c.message
    # 报告汇总权限缺口 + GRANT 模板
    gaps = {g.privilege: g for g in rep.privileges}
    assert "pg_monitor" in gaps
    assert gaps["pg_monitor"].grant_sql.startswith("GRANT pg_monitor TO")
    assert "空闲事务" in gaps["pg_monitor"].affects


def test_pg_bloat_warn():
    rep = pg_report(scripted_pg(
        extra=[("from pg_stat_user_tables", [["big", "50000", "1000", "从未"]])]))
    bloat = by_name(rep, "bloat")
    assert bloat.status == "warn"
    assert "big" in bloat.value
    assert "50,000" in bloat.details[0]


def test_pg_schema_scope_used():
    """传 schema 时 bloat/大表查询要带上 schemaname 过滤。"""
    scripted = scripted_pg()
    pg_report(scripted, schema="myschema")
    assert any("schemaname = :s" in q for q in scripted.queries)
    assert any("n.nspname = :s" in q for q in scripted.queries)


def test_pg_idle_transaction_warn():
    rep = pg_report(scripted_pg(
        extra=[("state = 'idle in transaction'",
                [[101, 600, "BEGIN; SELECT ..."]])]))   # 600s 的空闲事务
    idle = by_name(rep, "idle_in_transaction")
    assert idle.status == "warn"
    assert "600s" in idle.details[0]


# =====================================================================
# v2 新增检查项
# =====================================================================

def test_mysql_tmp_tables_on_disk_warn():
    """临时表落盘 >25% → warn。"""
    rep = mysql_report(scripted_mysql(
        status={"Created_tmp_tables": "100", "Created_tmp_disk_tables": "40"}))
    c = by_name(rep, "tmp_tables_on_disk")
    assert c.status == "warn" and "40%" in c.value


def test_mysql_tmp_tables_on_disk_ok():
    rep = mysql_report(scripted_mysql(
        status={"Created_tmp_tables": "100", "Created_tmp_disk_tables": "5"}))
    assert by_name(rep, "tmp_tables_on_disk").status == "ok"


def test_mysql_no_primary_key_warn():
    rep = mysql_report(scripted_mysql(
        extra=[("information_schema.statistics",
                [["db", "orders", "1000"], ["db", "logs", "0"]])]))
    c = by_name(rep, "no_primary_key")
    assert c.status == "warn" and "2 张" in c.value
    assert any("orders" in d for d in c.details)


def test_mysql_binlog_cache_warn():
    """大事务落盘 → warn。"""
    rep = mysql_report(scripted_mysql(
        status={"Binlog_cache_use": "1000", "Binlog_cache_disk_use": "3"}))
    c = by_name(rep, "binlog_cache")
    assert c.status == "warn" and "3 次落盘" in c.value


def test_mysql_buffer_pool_vs_data_warn():
    """数据量超过缓冲池 2 倍 → warn。"""
    rep = mysql_report(scripted_mysql(
        extra=[("coalesce(sum(coalesce(data_length", [[1024**3]])]))  # 1GB 数据
    c = by_name(rep, "buffer_pool_vs_data")
    assert c.status == "warn" and "8.0 倍" in c.value


def test_mysql_replication_grant_hint():
    """无 REPLICATION CLIENT 时复制延迟标 unknown 并给出 GRANT 模板。"""
    scripted = scripted_mysql()
    # 删掉 replica status 路由，模拟无权限
    scripted.routes = [r for r in scripted.routes if "show replica status" not in r[0]]
    rep = mysql_report(scripted)
    lag = by_name(rep, "replication_lag")
    assert lag.status == "unknown" and lag.privilege == "REPLICATION CLIENT"
    gaps = {g.privilege: g for g in rep.privileges}
    assert "REPLICATION CLIENT" in gaps
    assert gaps["REPLICATION CLIENT"].grant_sql.startswith("GRANT REPLICATION CLIENT")


def test_pg_temp_files_warn():
    """临时文件落盘速率高 → warn。"""
    rep = pg_report(scripted_pg(
        extra=[("temp_files, temp_bytes", [[500, 10 * 1024**2]])]))  # 2 小时 500 个 → 250/hr
    c = by_name(rep, "temp_files")
    assert c.status == "warn" and "500" in c.value


def test_pg_wait_events_info():
    """等待事件为参考项；Lock 类等待 >=5 才升 warn。"""
    rep = pg_report(scripted_pg(
        extra=[("group by 1, 2", [["Lock", "transactionid", "2"]])]))
    c = by_name(rep, "wait_events")
    assert c.status == "info" and "transactionid" in c.value


def test_pg_wait_events_lock_warn():
    rep = pg_report(scripted_pg(
        extra=[("group by 1, 2", [["Lock", "transactionid", "7"]])]))
    assert by_name(rep, "wait_events").status == "warn"


def test_pg_stats_stale_warn():
    """统计信息超过 7 天未分析 → warn。"""
    rep = pg_report(scripted_pg(
        extra=[("coalesce(last_analyze",
                [["big", None, None], ["small", "2020-01-01", 20 * 86400 * 365]])]))
    c = by_name(rep, "stats_stale")
    assert c.status == "warn" and "2 张表" in c.value
    assert any("从未分析" in d for d in c.details)


def test_pg_unused_indexes_warn():
    """未使用索引合计 >100MB → warn。"""
    rep = pg_report(scripted_pg(
        extra=[("pg_stat_user_indexes",
                [["orders", "idx_old", "0", 200 * 1024**2]])]))
    c = by_name(rep, "unused_indexes")
    assert c.status == "warn" and "1 个从未使用" in c.value
    assert "200.0 MB" in c.details[0]


def test_pg_unused_indexes_small_is_info():
    rep = pg_report(scripted_pg(
        extra=[("pg_stat_user_indexes", [["orders", "idx_old", "0", 1024]])]))
    assert by_name(rep, "unused_indexes").status == "info"


def test_pg_replication_slot_warn():
    """复制槽滞后 >1GB → warn（非活跃槽即使滞后小也 warn）。"""
    rep = pg_report(scripted_pg(
        extra=[("pg_replication_slots",
                [["sub1", "logical", False, "reserved", -1, 2 * 1024**3]])]))
    c = by_name(rep, "replication_slots")
    assert c.status == "warn" and "2.0 GB" in c.value


def test_pg_replication_slot_critical():
    rep = pg_report(scripted_pg(
        extra=[("pg_replication_slots",
                [["sub1", "logical", True, "reserved", -1, 20 * 1024**3]])]))
    assert by_name(rep, "replication_slots").status == "critical"


def test_pg_replication_slot_inactive_warn():
    """活跃但非活跃的槽：WAL 在堆积。"""
    rep = pg_report(scripted_pg(
        extra=[("pg_replication_slots",
                [["sub1", "logical", False, "reserved", -1, 1024]])]))
    c = by_name(rep, "replication_slots")
    assert c.status == "warn" and "非活跃" in c.message


def test_pg_replication_slot_wal_status_lost():
    """wal_status=lost（官方枚举：保留的 WAL 已丢数据）→ critical，优先于滞后量判定。"""
    rep = pg_report(scripted_pg(
        extra=[("pg_replication_slots",
                [["sub1", "logical", True, "lost", 0, 1024]])]))
    c = by_name(rep, "replication_slots")
    assert c.status == "critical" and "lost" in c.message


def test_pg_replication_slot_wal_status_extended_warn():
    rep = pg_report(scripted_pg(
        extra=[("pg_replication_slots",
                [["sub1", "logical", True, "extended", 500 * 1024**2, 1024]])]))
    c = by_name(rep, "replication_slots")
    assert c.status == "warn" and "extended" in c.message


def test_pg_archiver_warn():
    """归档失败 >0 → warn。"""
    rep = pg_report(scripted_pg(
        extra=[("pg_stat_archiver", [[1000, 3, "000000010000000000000003"]])]))
    c = by_name(rep, "archiver")
    assert c.status == "warn" and "3 次失败" in c.value
    assert "000000010000000000000003" in c.details[0]


def test_pg_xid_wraparound_critical():
    """剩余事务号 <100M → critical（PG 独有的强制只读风险）。"""
    rep = pg_report(scripted_pg(
        extra=[("from pg_database", [[2_100_000_000, 47_483_648]])]))
    c = by_name(rep, "xid_wraparound")
    assert c.status == "critical" and "47M" in c.value


def test_pg_xid_wraparound_ok():
    rep = pg_report(scripted_pg())
    assert by_name(rep, "xid_wraparound").status == "ok"


# =====================================================================
# 容错：一项失败不毒化其它项
# =====================================================================

def test_one_failing_check_does_not_break_others():
    """bloat 查询失败（模拟 AmbiguousParameter）时，后续项必须照常出。"""
    scripted = Scripted([
        ("select version()", [["PostgreSQL 16.15"]]),
        ("pg_postmaster_start_time", _started_hours_ago(2)),
        ("pg_has_role", [[1]]),
        ("current_setting('max_connections')", [["100"]]),
        ("from pg_stat_activity", []),
        ("sum(deadlocks)", [[0]]),
        ("from pg_stat_database", [[3000.0, 20.0]]),
        ("from pg_stat_replication", []),
        ("pg_database_size", [["8119 kB"]]),
        ("from pg_class", [["orders", 548864]]),
    ])

    class Failing(Scripted):
        def connect(self):
            return _FailingConn(self)

    class _FailingConn(_Conn):
        def execute(self, sql, params=None):
            if "pg_stat_user_tables" in str(sql).lower():
                raise RuntimeError("AmbiguousParameter")
            return super().execute(sql, params)

    eng = Failing(scripted.routes)
    rep = checkup.run_checkup(eng, "postgres")
    assert by_name(rep, "bloat").status == "unknown"
    # 关键：后续检查没被毒化
    assert by_name(rep, "cache_hit_ratio").status == "ok"
    assert by_name(rep, "deadlocks").status == "ok"
    assert by_name(rep, "big_tables").status == "info"
    assert len(eng.queries) > 5  # 的确跑了多项，不是一失败就整体退出


# =====================================================================
# overall 与格式化
# =====================================================================

@pytest.mark.parametrize(("statuses", "expect"), [
    (["ok", "ok"], "ok"),
    (["ok", "info"], "info"),
    (["info", "unknown"], "unknown"),   # 有测不到的项不能读成「一切正常」
    (["unknown", "unknown"], "unknown"),
    (["ok", "warn", "unknown"], "warn"),
    (["info", "critical", "warn"], "critical"),
])
def test_worst_status(statuses, expect):
    checks = [checkup.Check(f"c{i}", f"t{i}", st) for i, st in enumerate(statuses)]
    assert checkup._worst(checks) == expect


# =====================================================================
# 连接探活：整库不可达时给一条明确结论，而不是十几项 OperationalError
# =====================================================================

def test_dead_db_short_circuits_to_single_critical():
    """库都连不上时，逐项报 OperationalError 只是噪音——一条 critical 说清。"""
    rep = checkup.run_checkup(_DeadEngine(), "postgres")
    assert rep.overall == "critical"
    assert len(rep.checks) == 1, "死库只该有一条连接结论，不该刷屏"
    c = rep.checks[0]
    assert (c.name, c.status) == ("connectivity", "critical")
    assert "无法连接" in c.message or "不可达" in c.message
    assert "Connection refused" in c.message  # 真实原因透传给用户
    assert rep.summary == "1 项严重（共 1 项）"
    assert rep.privileges == []  # 权限缺口要在能连上时才谈得上


def test_conn_error_text_strips_sqlalchemy_wrapper():
    """SQLAlchemy 的 OperationalError 带 Background 尾巴和 (psycopg.X) 前缀，要洗干净。"""
    e = _make_operational_error()
    text = checkup._conn_error_text(e)
    assert "Connection refused" in text
    assert "Background on SQLAlchemy" not in text
    assert not text.startswith("(")


def test_connectivity_ok_on_live_sqlite(tmp_path):
    """能连上的库探活返回 True，不影响后续诊断。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'ok.sqlite3'}")
    ok, why = checkup.connectivity_ok(eng)
    assert ok is True
    assert why == ""



def test_empty_report_is_unknown():
    assert checkup._worst([]) == "unknown"


@pytest.mark.parametrize(("n", "expect"), [
    (0, "0 B"), (1023, "1023 B"), (2048, "2.0 KB"), (1048576, "1.0 MB"),
    (1073741824, "1.0 GB"), (2.5 * 1024**4, "2.5 TB"),
])
def test_human_bytes(n, expect):
    assert checkup._human_bytes(n) == expect


def test_human_duration_and_rate():
    assert checkup._human_duration(3600) == "1 小时 0 分"
    assert checkup._human_duration(90000) == "1 天 1 小时"
    assert checkup._rate_per_hour(60, 3600) == 60.0
    assert checkup._rate_per_hour(None, 3600) is None
    assert checkup._rate_per_hour(10, 0) is None


# =====================================================================
# service 层
# =====================================================================

def test_service_checkup_sqlite(service):
    rep = service.db_checkup("demo", "main", CALLER)
    assert rep["engine"] == "sqlite"
    assert rep["overall"] in ("ok", "info")
    recs = [r for r in service.store.recent(limit=500) if r.get("tool") == "checkup"]
    assert recs and recs[0]["status"] == "ok"


def test_service_checkup_scope_used(service):
    """schema 参数要传到引擎层（默认库兜底）。"""
    service.db_checkup("demo", "main", CALLER)
    rec = next(r for r in service.store.recent(limit=100) if r.get("tool") == "checkup")
    # sqlite 连接无独立库名概念，scope 兜底成配置的 database（文件路径）
    assert rec["sql"] and rec["sql"].endswith("biz.sqlite3")


def test_service_checkup_rejects_redis(service):
    service.config = AppConfig.model_validate({
        "projects": {"demo": {"connections": {"cache": {
            "engine": "redis", "host": "127.0.0.1", "port": 6379, "environment": "local",
        }}}}
    })
    with pytest.raises(ValueError, match="Redis"):
        service.db_checkup("demo", "cache", CALLER)


# =====================================================================
# 静态校验：各引擎诊断 SQL 必须能在对应方言下解析（防笔误）
# =====================================================================

def test_checkup_sql_parses():
    dialect = {"mysql": "mysql", "postgres": "postgres",
               "sqlite": "sqlite", "clickhouse": "clickhouse"}
    samples = {
        "mysql": [
            "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status"
            " WHERE VARIABLE_NAME IN ('Uptime','Threads_connected')",
            "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_variables"
            " WHERE VARIABLE_NAME IN ('max_connections','version')",
            "SELECT SUM_ERROR_RAISED FROM performance_schema.events_errors_summary_global_by_error"
            " WHERE ERROR_NAME = 'ER_LOCK_DEADLOCK'",
            "SELECT PROCESSLIST_TIME, LEFT(COALESCE(PROCESSLIST_INFO,''), 80), PROCESSLIST_STATE"
            " FROM performance_schema.threads"
            " WHERE PROCESSLIST_TIME >= 60"
            " AND PROCESSLIST_COMMAND NOT IN ('Sleep', 'Daemon')"
            " AND PROCESSLIST_ID IS NOT NULL"
            " ORDER BY PROCESSLIST_TIME DESC LIMIT 5",
            "SELECT COUNT(*) FROM performance_schema.data_lock_waits",
            "SELECT COALESCE(sum(COALESCE(data_length,0)+COALESCE(index_length,0)), 0)"
            " FROM information_schema.tables WHERE table_schema = DATABASE()"
            " AND table_schema NOT IN ('mysql','sys')",
            "SELECT t.table_schema, t.table_name, COALESCE(t.table_rows, 0)"
            " FROM information_schema.tables t"
            " LEFT JOIN information_schema.statistics s"
            " ON s.table_schema = t.table_schema AND s.table_name = t.table_name AND s.non_unique = 0"
            " WHERE t.table_type = 'BASE TABLE' AND t.table_schema = DATABASE()"
            " AND t.table_schema NOT IN ('mysql','sys') AND s.table_name IS NULL"
            " ORDER BY t.table_rows DESC LIMIT 10",
            "SELECT table_schema, table_name, COALESCE(data_length,0)+COALESCE(index_length,0)"
            " AS bytes, table_rows FROM information_schema.tables"
            " WHERE table_schema = DATABASE() AND table_schema NOT IN ('mysql','sys')"
            " ORDER BY bytes DESC LIMIT 5",
        ],
        "postgres": [
            "SELECT version()",
            "SELECT pg_postmaster_start_time()",
            "SELECT pg_has_role(current_user, 'pg_monitor', 'member')",
            "SELECT current_setting('max_connections')::int",
            "SELECT count(*) FROM pg_stat_activity",
            "SELECT pid, extract(epoch FROM (now() - xact_start))::int, left(query, 80)"
            " FROM pg_stat_activity WHERE state = 'idle in transaction'"
            " AND now() - xact_start > make_interval(secs => 300) ORDER BY xact_start LIMIT 5",
            "SELECT pid, extract(epoch FROM (now() - query_start))::int, left(query, 80)"
            " FROM pg_stat_activity WHERE state = 'active' AND query_start IS NOT NULL"
            " AND now() - query_start > make_interval(secs => 60)"
            " ORDER BY query_start DESC LIMIT 5",
            "SELECT wait_event_type, wait_event, count(*) FROM pg_stat_activity"
            " WHERE wait_event IS NOT NULL AND pid <> pg_backend_pid()"
            " GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5",
            "SELECT temp_files, temp_bytes FROM pg_stat_database"
            " WHERE datname = current_database()",
            "SELECT sum(blks_hit), sum(blks_read) FROM pg_stat_database"
            " WHERE datname = current_database()",
            "SELECT sum(deadlocks) FROM pg_stat_database WHERE datname = current_database()",
            "SELECT relname, n_dead_tup, n_live_tup, COALESCE(last_autovacuum::text, 'x')"
            " FROM pg_stat_user_tables WHERE n_dead_tup >= 10000"
            " AND schemaname = current_schema() ORDER BY n_dead_tup DESC LIMIT 5",
            "SELECT relname, COALESCE(last_analyze, last_autoanalyze) AS last_an,"
            " extract(epoch FROM (now() - COALESCE(last_analyze, last_autoanalyze)))::int"
            " FROM pg_stat_user_tables WHERE schemaname = current_schema()"
            " ORDER BY last_an NULLS FIRST LIMIT 10",
            "SELECT s.relname, s.indexrelname, s.idx_scan,"
            " COALESCE(pg_relation_size(s.indexrelid), 0)"
            " FROM pg_stat_user_indexes s"
            " JOIN pg_index i ON i.indexrelid = s.indexrelid"
            " WHERE s.schemaname = current_schema()"
            " AND s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary"
            " ORDER BY 4 DESC LIMIT 10",
            "SELECT application_name, client_addr, COALESCE(extract(epoch FROM write_lag),0)"
            " FROM pg_stat_replication ORDER BY 3 DESC NULLS LAST LIMIT 5",
            "SELECT slot_name, slot_type, active, wal_status,"
            " COALESCE(safe_wal_size, -1),"
            " COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0)"
            " FROM pg_replication_slots ORDER BY 6 DESC LIMIT 5",
            "SELECT archived_count, failed_count, COALESCE(last_failed_wal, '')"
            " FROM pg_stat_archiver",
            "SELECT max(age(datfrozenxid)), 2147483648::bigint - max(age(datfrozenxid))"
            " FROM pg_database",
            "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relkind IN ('r','p') AND n.nspname = current_schema()"
            " AND n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY 2 DESC LIMIT 5",
        ],
        "clickhouse": [
            "SELECT version()",
            "SELECT uptime()",
            "SELECT name, path, total_space, free_space, unreserved_space,"
            " is_read_only, is_broken FROM system.disks"
            " ORDER BY (free_space / NULLIF(total_space,0)) ASC LIMIT 5",
            "SELECT metric, value FROM system.metrics"
            " WHERE metric IN ('Query','Merge','BackgroundMergesAndMutationsPoolTask')",
            "SELECT event, value FROM system.events"
            " WHERE event IN ('FailedQuery','FailedSelectQuery','FailedInsertQuery')",
            "SELECT database, table, queue_size, merges_in_queue, absolute_delay,"
            " is_session_expired, log_max_index, log_pointer"
            " FROM system.replicas WHERE is_readonly = 0"
            " ORDER BY queue_size DESC LIMIT 5",
            "SELECT database, table, count() AS parts FROM system.parts WHERE active"
            " AND database = currentDatabase() GROUP BY database, table HAVING parts >= 150"
            " ORDER BY parts DESC LIMIT 5",
            "SELECT count() FROM system.mutations WHERE NOT is_done",
            "SELECT database, table, sum(bytes_on_disk) AS bytes, sum(rows) AS rows"
            " FROM system.parts WHERE active AND database = currentDatabase()"
            " GROUP BY database, table ORDER BY bytes DESC LIMIT 5",
        ],
    }
    problems = []
    for eng, sqls in samples.items():
        for sql in sqls:
            try:
                sqlglot.parse_one(sql, read=dialect[eng])
            except sqlglot.errors.SqlglotError as e:
                problems.append(f"{eng}: {e}: {sql[:80]}")
    assert not problems, "诊断 SQL 解析失败：\n" + "\n".join(problems)


# =====================================================================
# MCP 协议层（工具发现 + 结构化返回）
# =====================================================================

@pytest.mark.anyio
async def test_mcp_tool_registered_and_callable(service):
    """走真实 MCP 协议：工具能被发现、调用，返回结构化诊断报告。"""
    from fastmcp import Client

    from dbmcp.server import build_mcp

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert "db_checkup" in names

        r = await c.call_tool("db_checkup",
                              {"project": "demo", "connection": "main"})
        rep = r.data
        assert rep["engine"] == "sqlite"
        assert rep["overall"] in ("ok", "info")
        assert any(c_["name"] == "integrity" for c_ in rep["checks"])
        # 体检说明随首次调用附给 agent（与其它工具一致）
        assert r.data is not None


@pytest.mark.anyio
async def test_mcp_tool_rejects_redis(service, tmp_path):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from dbmcp.server import build_mcp

    service.config = AppConfig.model_validate({
        "projects": {"demo": {"connections": {"cache": {
            "engine": "redis", "host": "127.0.0.1", "port": 6379, "environment": "local",
        }}}}
    })
    mcp = build_mcp(service)
    async with Client(mcp) as c:
        with pytest.raises(ToolError) as ei:
            await c.call_tool("db_checkup", {"project": "demo", "connection": "cache"})
        assert "Redis" in str(ei.value)


# =====================================================================
# report_to_markdown：服务端权威 Markdown 渲染（供 AI 诊断当上下文）
# =====================================================================

def _sample_report_dict():
    return {
        "engine": "mysql", "version": "8.0.36", "scope": "shop",
        "started_at": "2026-09-20T00:00:00+00:00", "elapsed_ms": 320,
        "overall": "warn", "summary": "1 项需关注、1 项正常（共 2 项）",
        "dimensions": [{"name": d[0], "title": d[1]} for d in checkup.DIMENSIONS],
        "checks": [
            {"name": "conn", "title": "连接占用", "status": "warn", "value": "82%",
             "message": "连接数偏高", "details": ["db: 40 / 48"], "dimension": "capacity",
             "privilege": ""},
            {"name": "hit", "title": "缓存命中率", "status": "ok", "value": "99.1%",
             "message": "", "details": [], "dimension": "performance", "privilege": ""},
            {"name": "long", "title": "长查询", "status": "unknown", "value": "",
             "message": "权限不足", "details": [], "dimension": "performance",
             "privilege": "PROCESS"},
        ],
        "privileges": [
            {"privilege": "PROCESS", "grant_sql": "GRANT PROCESS ON *.* TO 'u'@'%';",
             "affects": ["长查询"]},
        ],
    }


def test_report_to_markdown_has_engine_scope_summary_and_checks():
    md = checkup.report_to_markdown(_sample_report_dict())
    assert "# 数据库体检报告" in md
    assert "mysql 8.0.36" in md
    assert "范围：shop" in md
    assert "1 项需关注、1 项正常（共 2 项）" in md
    # 检查项按维度分组、带状态标签与值
    assert "[需关注] **连接占用** — 82%" in md
    assert "连接数偏高" in md
    assert "db: 40 / 48" in md
    assert "[正常] **缓存命中率** — 99.1%" in md
    assert "[无法测量] **长查询**" in md


def test_report_to_markdown_includes_privilege_gaps_and_grant():
    md = checkup.report_to_markdown(_sample_report_dict())
    assert "权限缺口" in md
    assert "缺 PROCESS 权限" in md
    assert "GRANT PROCESS ON *.* TO 'u'@'%';" in md


def test_report_to_markdown_skips_empty_dimensions_and_handles_minimal_report():
    md = checkup.report_to_markdown({"engine": "sqlite"})
    assert "sqlite" in md
    # 没有检查项时不应输出空的维度标题
    assert "## " not in md


def test_report_to_markdown_does_not_escape_backticks_in_grant_block():
    """GRANT 块要保留 ``` 围栏，AI 才知道那是 SQL。"""
    md = checkup.report_to_markdown(_sample_report_dict())
    assert "```sql" in md


# ---------------------------------------------------------------------------
# 实例级体检合并（merge_reports）
# ---------------------------------------------------------------------------

def _rep(*checks: checkup.Check, engine: str = "mysql") -> checkup.CheckupReport:
    r = checkup.CheckupReport(engine=engine)
    r.checks = list(checks)
    return r


class TestMergeReports:
    def test_empty_reports_gives_unknown_report(self):
        m = checkup.merge_reports("mysql", [])
        assert m.overall == "unknown"
        assert m.scope == ""
        assert len(m.checks) == 1 and m.checks[0].name == "no_databases"

    def test_scope_says_all_databases(self):
        m = checkup.merge_reports("mysql", [("a", _rep()), ("b", _rep())])
        assert m.scope == "全体 2 个库"

    def test_instance_scope_checks_collapse_to_one_without_prefix(self):
        """连接占用这类全局计数器在哪个库查都一样——并成一条，不加 [库名]。"""
        a = _rep(checkup.Check("connections", "连接占用", "warn", "10 / 100",
                               instance_scope=True))
        b = _rep(checkup.Check("connections", "连接占用", "warn", "12 / 100",
                               instance_scope=True))
        m = checkup.merge_reports("mysql", [("db1", a), ("db2", b)])
        assert len(m.checks) == 1
        assert not m.checks[0].title.startswith("[")
        assert m.checks[0].title == "连接占用"

    def test_per_db_check_keeps_the_worst_with_db_prefix(self):
        a = _rep(checkup.Check("big_tables", "大表 TOP5", "ok", "db1"))
        b = _rep(checkup.Check("big_tables", "大表 TOP5", "critical", "db2"))
        m = checkup.merge_reports("mysql", [("db1", a), ("db2", b)])
        assert len(m.checks) == 1
        assert m.checks[0].title == "[db2] 大表 TOP5"
        assert m.checks[0].status == "critical"
        assert m.overall == "critical"

    def test_identical_unmarked_checks_also_collapse(self):
        """没标 instance_scope 但各库值完全相同（如单库引擎的检查）也并成一条。"""
        a = _rep(checkup.Check("big_tables", "大表 TOP5", "ok", "1 MB"))
        b = _rep(checkup.Check("big_tables", "大表 TOP5", "ok", "1 MB"))
        m = checkup.merge_reports("mysql", [("db1", a), ("db2", b)])
        assert len(m.checks) == 1 and not m.checks[0].title.startswith("[")

    def test_mixed_report_is_compact(self):
        """21 个库的实例不应该把连接占用复制 21 遍（真实 MySQL e2e 的回归点）。"""
        reps = []
        for i in range(21):
            reps.append((f"db{i}", _rep(
                checkup.Check("connections", "连接占用", "ok", "1 / 100",
                              instance_scope=True),
                checkup.Check("big_tables", "大表 TOP5", "ok" if i else "warn", f"db{i}"),
            )))
        m = checkup.merge_reports("mysql", reps)
        assert len(m.checks) == 2, "实例级指标必须去重"
        assert m.overall == "warn"
        assert m.checks[1].title == "[db0] 大表 TOP5"

    def test_privilege_gaps_merged_without_duplicates(self):
        a = _rep()
        a.privileges = [checkup.PrivilegeGap("pg_monitor", "GRANT ...", ["长查询", "空闲事务"])]
        b = _rep()
        b.privileges = [checkup.PrivilegeGap("pg_monitor", "GRANT ...", ["空闲事务", "等待事件"])]
        m = checkup.merge_reports("postgres", [("d1", a), ("d2", b)])
        assert len(m.privileges) == 1
        assert m.privileges[0].affects == ["长查询", "空闲事务", "等待事件"]

    def test_version_taken_from_first_report(self):
        a = _rep(); a.version = "MySQL 9.5.0"
        m = checkup.merge_reports("mysql", [("d1", a), ("d2", _rep())])
        assert m.version == "MySQL 9.5.0"

    def test_serializable_and_renderable(self):
        a = _rep(checkup.Check("connections", "连接占用", "warn", "82%",
                               instance_scope=True))
        m = checkup.merge_reports("mysql", [("d1", a)])
        d = m.to_dict()
        assert d["scope"] == "全体 1 个库"
        assert "全体 1 个库" in checkup.report_to_markdown(d)
