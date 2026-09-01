"""引擎会话设置的回归测试。

针对真实 MySQL e2e 暴露的 bug：max_execution_time 与 READ ONLY 被逗号拼进一条
SET 语句导致 1064 语法错误。SQLite 单测跑不到 MySQL 方言，故用纯函数断言语句拆分。
"""

import datetime as dt
import decimal

import pytest

from dbmcp import __version__, engines
from dbmcp.config import ConnectionConfig, Policy
from dbmcp.engines import (
    DB_CLIENT_NAME,
    _create_readonly_engine,
    _col_categories,
    _jsonable,
    _value_category,
    make_canceller,
    mysql_read_timeout,
    mysql_session_statements,
    role_timeouts,
)


def test_db_client_name_contains_quay_version():
    assert DB_CLIENT_NAME == f"Quay({__version__})"


@pytest.mark.parametrize(
    ("engine", "client_arg"),
    [
        ("mysql", "program_name"),
        ("postgres", "application_name"),
        ("clickhouse", "client_name"),
    ],
)
def test_network_db_connections_report_quay_client(monkeypatch, engine, client_arg):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        return object()

    # MySQL 会在创建引擎后注册 connect 事件；本测试只验证传给 DBAPI 的建连参数。
    monkeypatch.setattr("dbmcp.engines.create_engine", fake_create_engine)
    monkeypatch.setattr(
        "dbmcp.engines.event.listens_for",
        lambda *_args, **_kwargs: lambda fn: fn,
    )
    cfg = ConnectionConfig(
        engine=engine,
        host="db.example",
        user="reader",
        password="plain://secret",
    )

    _create_readonly_engine(cfg, "reader", cfg.host, cfg.port)

    assert captured["connect_args"][client_arg] == DB_CLIENT_NAME


class TestJsonableBigInt:
    """大整数（雪花 ID/int64）须以字符串下发，避免前端 JSON.parse 丢精度改行。"""

    def test_bigint_beyond_js_safe_becomes_string(self):
        assert _jsonable(1726946581640544256) == "1726946581640544256"
        assert _jsonable(2 ** 60) == str(2 ** 60)
        assert _jsonable(-(2 ** 60)) == str(-(2 ** 60))

    def test_just_over_2pow53_is_string(self):
        # MAX_SAFE_INTEGER = 2^53-1；恰好超一位就转字符串
        assert _jsonable(9007199254740991) == 9007199254740991      # 边界内仍是数字
        assert _jsonable(9007199254740992) == "9007199254740992"    # 超一位 → 字符串

    def test_small_int_stays_number(self):
        assert _jsonable(42) == 42 and isinstance(_jsonable(42), int)

    def test_bool_not_treated_as_int(self):
        # bool 是 int 子类，不能被大整数逻辑吞掉
        assert _jsonable(True) is True and _jsonable(False) is False


class TestValueCategory:
    """列类型分类：只对类型明确的 Python 值给分类，字符串留空交前端推断。"""

    def test_categories(self):
        assert _value_category(1726946581640544256) == "number"   # 原始 int（下发前）→ number
        assert _value_category(3.14) == "number"
        assert _value_category(decimal.Decimal("1.5")) == "number"
        assert _value_category(True) == "bool"
        assert _value_category(dt.datetime(2026, 7, 15)) == "datetime"
        assert _value_category(dt.date(2026, 7, 15)) == "date"
        assert _value_category(dt.time(1, 2)) == "time"
        assert _value_category(b"x") == "binary"
        assert _value_category({"a": 1}) == "json"
        assert _value_category("2026-07-15") == ""   # 字符串留给前端 inferCat
        assert _value_category(None) == ""

    def test_col_categories_first_non_null(self):
        rows = [[None, "us"], [1726946581640544256, "cn"]]
        assert _col_categories(2, rows) == ["number", ""]


def test_reader_statements_are_separate():
    stmts = mysql_session_statements(timeout_s=30, readonly=True)
    assert len(stmts) == 2
    # 不能有任何一条语句同时包含变量赋值和 TRANSACTION（即不能逗号拼接）
    for s in stmts:
        assert not ("max_execution_time" in s and "TRANSACTION" in s), f"语句被错误拼接: {s}"
    assert stmts[0] == "SET SESSION max_execution_time = 30000"
    assert stmts[1] == "SET SESSION TRANSACTION READ ONLY"


def test_writer_has_no_readonly():
    stmts = mysql_session_statements(timeout_s=10, readonly=False)
    assert stmts == ["SET SESSION max_execution_time = 10000"]
    assert all("READ ONLY" not in s for s in stmts)


def test_sa_pool_kwargs_preserves_default_and_scales():
    from dbmcp.engines import _sa_pool_kwargs
    # 15（默认）还原历史值：pool_size=5 + overflow=10
    assert _sa_pool_kwargs(15) == {"pool_size": 5, "max_overflow": 10}
    # 小于 5：全常驻、无 overflow
    assert _sa_pool_kwargs(3) == {"pool_size": 3, "max_overflow": 0}
    # 大：5 常驻，其余全 overflow；总数 = pool_size + max_overflow
    k = _sa_pool_kwargs(100)
    assert k == {"pool_size": 5, "max_overflow": 95}
    assert k["pool_size"] + k["max_overflow"] == 100


def test_role_timeouts_reader_uses_statement_timeout():
    policy = Policy(statement_timeout_s=30, write_timeout_s=600)
    stmt, op = role_timeouts(policy, readonly=True)
    assert (stmt, op) == (30, 30)


def test_role_timeouts_writer_uses_write_timeout():
    """writer 的操作超时放大到 write_timeout_s，根治大 DELETE 的 2013 断连。"""
    policy = Policy(statement_timeout_s=30, write_timeout_s=600)
    stmt, op = role_timeouts(policy, readonly=False)
    assert stmt == 30          # 语句超时（喂 max_execution_time，只约束 SELECT）不变
    assert op == 600           # socket/写超时放大


def test_reader_socket_read_timeout_exceeds_max_execution_time():
    """reader 的 socket read_timeout 必须严格大于 max_execution_time(=stmt)，否则长 SELECT
    被 socket 抢先超时打成 2013 Lost connection，而非服务端干净的 3024 超时。"""
    stmt, op = role_timeouts(Policy(statement_timeout_s=30, write_timeout_s=600), readonly=True)
    rt = mysql_read_timeout(stmt, op, readonly=True)
    assert rt > stmt           # 有宽限，socket 不会抢先
    assert rt == 45            # 30 + 15 宽限
    # 调大读取超时后仍保持宽限，长 SELECT 能跑完
    assert mysql_read_timeout(300, 300, readonly=True) == 315


def test_writer_socket_read_timeout_is_write_timeout():
    """writer 无 max_execution_time 约束，socket read_timeout 直接用 write_timeout_s（本身即上限）。"""
    assert mysql_read_timeout(30, 600, readonly=False) == 600


# ---------- make_canceller：按方言生成正确的 DB 层取消 ----------


class _FakeConn:
    """模拟 with engine.connect() 的上下文，记录 exec_driver_sql 调用。"""

    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def exec_driver_sql(self, sql, *args):
        self._sink.append(sql)


class _FakeEngine:
    def __init__(self, dialect_name, sink):
        self.dialect = type("D", (), {"name": dialect_name})()
        self._sink = sink

    def connect(self):
        return _FakeConn(self._sink)


def _sa_conn_with(raw):
    """构造带 .connection.dbapi_connection = raw 的假 SQLAlchemy 连接。"""
    inner = type("Inner", (), {"dbapi_connection": raw})()
    return type("SAConn", (), {"connection": inner})()


def test_make_canceller_mysql_kills_query():
    sink = []
    engine = _FakeEngine("mysql", sink)
    raw = type("Raw", (), {"thread_id": lambda self: 4242})()
    cancel = make_canceller(engine, _sa_conn_with(raw))
    cancel()
    assert sink == ["KILL QUERY 4242"]


def test_make_canceller_postgres_cancels_backend():
    sink = []
    engine = _FakeEngine("postgresql", sink)
    info = type("Info", (), {"backend_pid": 777})()
    raw = type("Raw", (), {"info": info})()
    cancel = make_canceller(engine, _sa_conn_with(raw))
    cancel()
    assert sink == ["SELECT pg_cancel_backend(777)"]


def test_make_canceller_sqlite_interrupts():
    interrupted = []
    raw = type("Raw", (), {"interrupt": lambda self: interrupted.append(True)})()
    engine = _FakeEngine("sqlite", [])
    cancel = make_canceller(engine, _sa_conn_with(raw))
    cancel()
    assert interrupted == [True]


def _sqlite_engine() -> object:
    import sqlalchemy as sa
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
    return engine


def test_explain_returns_columns_with_rows():
    """执行计划必须带列名——审批人看一堆裸值不知道每列是什么。"""
    plan = engines.explain(_sqlite_engine(), "UPDATE t SET name = 'b' WHERE id = 1", "sqlite")
    assert plan["columns"][:2] == ["addr", "opcode"]
    assert plan["rows"]
    assert all(len(row) == len(plan["columns"]) for row in plan["rows"])


def test_explain_mysql_forces_traditional_format():
    """MySQL 9 默认 TREE 格式解释不了 DML，必须显式要传统表格式（真实 MySQL 9.5 上验证过）。"""
    captured = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt):
            captured.append(str(stmt))
            raise RuntimeError("stop here")  # 只关心发出去的语句

    engine = type("E", (), {"connect": lambda self: _Conn()})()
    assert engines.explain(engine, "UPDATE t SET a = 1", "mysql") is None
    assert captured == ["EXPLAIN FORMAT=TRADITIONAL UPDATE t SET a = 1"]
    captured.clear()
    engines.explain(engine, "UPDATE t SET a = 1", "postgres")
    assert captured == ["EXPLAIN UPDATE t SET a = 1"]


def test_explain_failure_returns_none():
    """取计划失败不阻断主流程（审批单照样生成）。"""
    assert engines.explain(_sqlite_engine(), "NOT SQL AT ALL", "sqlite") is None


def test_explain_caps_rows_and_cell_chars(monkeypatch):
    monkeypatch.setattr(engines, "PLAN_MAX_ROWS", 2)
    monkeypatch.setattr(engines, "PLAN_MAX_CELL_CHARS", 2)
    plan = engines.explain(_sqlite_engine(), "UPDATE t SET name = 'b' WHERE id = 1", "sqlite")
    assert len(plan["rows"]) == 2
    assert all(v is None or len(v) <= 3 for row in plan["rows"] for v in row)  # 2 + 省略号


def test_make_canceller_missing_id_is_noop():
    """取不到连接标识时返回空操作，cancel 不抛错。"""
    sink = []
    engine = _FakeEngine("mysql", sink)
    raw = type("Raw", (), {})()  # 无 thread_id
    cancel = make_canceller(engine, _sa_conn_with(raw))
    cancel()  # 不应抛
    assert sink == []


class TestPostgresDatabaseFallback:
    """PG 未绑 database 时，reader 与 writer 必须落到**同一个库**。

    真机 drama-u 暴露：libpq 在 dbname 缺省时用**连接账号自己的名字**当库名，于是
    reader 连 `drama`、writer 连 `root` —— 页面报
    `FATAL: database "root" does not exist`。库不存在只是吵；真正危险的是库恰好存在，
    审批通过的写会安静地打进另一个库。
    """

    @staticmethod
    def _cfg(database=None):
        return ConnectionConfig.model_validate({
            "engine": "postgres", "host": "127.0.0.1", "port": 5432, "database": database,
            "user": "drama", "password": "plain://x", "environment": "dev",
            "writer": {"user": "root", "password": "plain://y"},
        })

    def test_falls_back_to_reader_account_name(self):
        assert engines.pg_database_name(self._cfg()) == "drama"

    def test_bound_database_wins(self):
        assert engines.pg_database_name(self._cfg("shortdrama")) == "shortdrama"

    def test_reader_and_writer_agree_when_unbound(self):
        cfg = self._cfg()
        urls = {}
        for role in ("reader", "writer"):
            eng = _create_readonly_engine(cfg, role, cfg.host, cfg.port)
            urls[role] = (eng.url.username, eng.url.database)
            eng.dispose()
        assert urls["reader"] == ("drama", "drama")
        # 修复前这里是 ("root", "root")——写会跑到另一个库去
        assert urls["writer"] == ("root", "drama")

    def test_reader_and_writer_agree_when_bound(self):
        cfg = self._cfg("shortdrama")
        for role in ("reader", "writer"):
            eng = _create_readonly_engine(cfg, role, cfg.host, cfg.port)
            assert eng.url.database == "shortdrama"
            eng.dispose()
