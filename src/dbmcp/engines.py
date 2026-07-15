"""SQL 引擎适配：基于 SQLAlchemy Core，统一 MySQL / PostgreSQL / SQLite。

数据库层的第二道只读防线（写操作在 M3 走独立的 writer 账号连接）：
- MySQL:    init_command 设置 SESSION TRANSACTION READ ONLY + max_execution_time
- Postgres: options 设置 default_transaction_read_only=on + statement_timeout
- SQLite:   连接建立时 PRAGMA query_only=ON
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, Literal

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine as SAEngine

from .config import ConnectionConfig, SshIdentity
from .secrets import resolve_secret
from .tunnel import SSHTunnel, open_tunnel

# 写操作在审批通过后用 writer 账号建独立引擎；日常查询用 reader。
Role = Literal["reader", "writer"]

DEFAULT_IDLE_RECLAIM_S = 600  # 隧道 + 引擎空闲 10 分钟回收


class UnsupportedEngineError(Exception):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    duration_ms: int
    # 每列的权威类型分类（number/string/datetime/date/time/bool/json/binary/""），
    # 由原始 Python 值类型推断——供前端类型图标用，尤其大整数以字符串传输后仍标为 number。
    column_types: list[str] = field(default_factory=list)


@dataclass
class _PooledEngine:
    engine: SAEngine
    tunnel: SSHTunnel | None
    last_used: float

    def dispose(self) -> None:
        self.engine.dispose()
        if self.tunnel is not None:
            self.tunnel.close()


class EnginePool:
    """按 (project, connection, role, schema) 缓存引擎，并托管其 SSH 隧道生命周期。

    schema 是查询台的「执行 schema」上下文：MySQL 以该库为默认库建独立引擎、
    PG 设 search_path——比在共享连接上执行 USE 干净（不污染池内会话状态）。
    """

    def __init__(self, idle_reclaim_s: int = DEFAULT_IDLE_RECLAIM_S):
        self._entries: dict[tuple[str, str, Role, str], _PooledEngine] = {}
        self._lock = threading.Lock()
        self._idle_reclaim_s = idle_reclaim_s
        # SSH 证书库（名字→证书）的活引用，建隧道时解析每跳的 identity。
        # service 构造时指向 AppConfig.ssh_identities（同一 dict，原地增删即时可见）。
        self.identities: dict[str, SshIdentity] | None = None

    def get(
        self,
        project: str,
        connection: str,
        cfg: ConnectionConfig,
        role: Role = "reader",
        schema: str | None = None,
        identities: dict[str, "SshIdentity"] | None = None,
    ) -> SAEngine:
        key = (project, connection, role, schema or "")
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry.tunnel is None or entry.tunnel.is_alive()):
                entry.last_used = time.monotonic()
                return entry.engine
            if entry is not None:
                # 隧道已死，连引擎一起回收后重建
                entry.dispose()
                del self._entries[key]
            entry = _build_pooled_engine(cfg, role, schema, identities or self.identities)
            self._entries[key] = entry
            return entry.engine

    def reap_idle(self) -> int:
        """回收空闲超过阈值的引擎与隧道，返回回收数量。"""
        now = time.monotonic()
        reaped = 0
        with self._lock:
            for key in list(self._entries):
                entry = self._entries[key]
                if now - entry.last_used >= self._idle_reclaim_s:
                    entry.dispose()
                    del self._entries[key]
                    reaped += 1
        return reaped

    def dispose_connection(self, project: str, connection: str) -> None:
        """回收某连接的所有角色引擎与隧道（配置变更后强制重建）。"""
        with self._lock:
            for key in [k for k in self._entries if k[0] == project and k[1] == connection]:
                self._entries.pop(key).dispose()

    def dispose(self) -> None:
        with self._lock:
            for entry in self._entries.values():
                entry.dispose()
            self._entries.clear()


def build_probe_engine(
    cfg: ConnectionConfig, role: Role = "reader",
    identities: dict[str, "SshIdentity"] | None = None,
) -> _PooledEngine:
    """用给定配置临时建连（含隧道），不入池。调用方用完必须 .dispose()。

    用于"测试连接/权限探测"——针对表单里尚未保存的配置，不影响 EnginePool。
    """
    return _build_pooled_engine(cfg, role, identities=identities)


def _build_pooled_engine(
    cfg: ConnectionConfig, role: Role, schema: str | None = None,
    identities: dict[str, "SshIdentity"] | None = None,
) -> _PooledEngine:
    tunnel: SSHTunnel | None = None
    host, port = cfg.host, cfg.port

    # SQLite 无网络，跳板不适用
    if cfg.engine != "sqlite" and cfg.jump_hosts:
        default_port = 3306 if cfg.engine == "mysql" else 5432
        tunnel = open_tunnel(cfg.host, cfg.port or default_port,
                             cfg.jump_hosts, cfg.ssh_options, identities)
        host, port = "127.0.0.1", tunnel.local_port

    try:
        engine = _create_readonly_engine(cfg, role, host, port, schema)
    except Exception:
        if tunnel is not None:
            tunnel.close()
        raise
    return _PooledEngine(engine=engine, tunnel=tunnel, last_used=time.monotonic())


def _resolve_account(cfg: ConnectionConfig, role: Role) -> tuple[str, str]:
    """返回 (user, password_ref)。writer 角色要求配置了 writer 账号。"""
    if role == "writer":
        if cfg.writer is None:
            raise UnsupportedEngineError("该连接未配置 writer 账号，无法执行写操作")
        return cfg.writer.user, cfg.writer.password
    return cfg.user or "", cfg.password or ""


def role_timeouts(policy, readonly: bool) -> tuple[int, int]:  # noqa: ANN001
    """按角色算 (语句超时, socket/写操作超时)。

    - 语句超时（stmt）：始终用 policy.statement_timeout_s，喂 MySQL max_execution_time（只约束 SELECT）。
    - 操作超时（op）：reader 用语句超时；writer 用 policy.write_timeout_s——放大给大
      DELETE/UPDATE 留足时间，否则 reader 的 30s socket read_timeout 会把长写打成
      pymysql 2013「Lost connection ... read operation timed out」。MySQL 用作
      socket read/write_timeout；PG 用作 writer 的 statement_timeout。
    """
    stmt = policy.statement_timeout_s
    return stmt, (stmt if readonly else policy.write_timeout_s)


# reader 的 socket read_timeout 要比服务端 max_execution_time(=stmt_timeout_s) 宽这么多秒。
_READ_SOCKET_GRACE_S = 15


def mysql_read_timeout(stmt_timeout_s: int, op_timeout_s: int, readonly: bool) -> int:
    """MySQL 连接的 socket read_timeout（秒）。

    - reader：max_execution_time(=stmt_timeout_s) 才是长 SELECT 的真实上限；socket read_timeout
      必须比它宽 _READ_SOCKET_GRACE_S 秒，否则二者相等时 socket 常抢先超时 → pymysql 报
      2013「Lost connection ... read operation timed out」，而不是让 max_execution_time 干净地以
      3024「超过最大执行时间」中断。放宽 socket 后，服务端超时先触发、错误信息也清晰；用户调大
      连接的读取超时（statement_timeout_s）时长 SELECT 也能真正跑完而不被 socket 误杀。
    - writer：op(=write_timeout_s) 本身就是真实上限（写操作不受 max_execution_time 约束），直接用。
    """
    return stmt_timeout_s + _READ_SOCKET_GRACE_S if readonly else op_timeout_s


def mysql_session_statements(timeout_s: int, readonly: bool) -> list[str]:
    """MySQL 建连后要逐条执行的会话设置语句。

    关键：max_execution_time（变量赋值）与 SET TRANSACTION READ ONLY 是两条
    互不兼容的独立语句，绝不能用逗号拼进一条 SET —— 真实 MySQL 会报 1064 语法错误
    （SQLite 单测发现不了，需要真实 MySQL e2e 才暴露）。
    """
    statements = [f"SET SESSION max_execution_time = {timeout_s * 1000}"]
    if readonly:
        # 会话默认只读，作为数据库层第二道防线（写操作报 1792）
        statements.append("SET SESSION TRANSACTION READ ONLY")
    return statements


def _create_readonly_engine(
    cfg: ConnectionConfig,
    role: Role,
    host: str | None,
    port: int | None,
    schema: str | None = None,
) -> SAEngine:
    """schema：查询台执行 schema 上下文。MySQL 覆盖默认库；PG 设 search_path。"""
    readonly = role == "reader"
    stmt_timeout_s, op_timeout_s = role_timeouts(cfg.policy, readonly)

    if cfg.engine == "sqlite":
        engine = create_engine(f"sqlite:///{cfg.database}", pool_pre_ping=True)

        if role == "reader":
            @event.listens_for(engine, "connect")
            def _sqlite_readonly(dbapi_conn, _record):  # noqa: ANN001
                dbapi_conn.execute("PRAGMA query_only = ON")

        return engine

    user, password_ref = _resolve_account(cfg, role)
    password = resolve_secret(password_ref)
    # writer 引擎不施加数据库层只读约束（写操作已经过审批）

    if cfg.engine == "mysql":
        from sqlalchemy.engine import URL

        url = URL.create(
            "mysql+pymysql",
            username=user,
            password=password,
            host=host,
            port=port or 3306,
            database=schema or cfg.database,
        )
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 5,
                # reader 的 read_timeout 加宽限，让服务端 max_execution_time 先于 socket 超时
                # （否则二者相等时长 SELECT 被打成 2013 Lost connection，而非干净的超时错误）
                "read_timeout": mysql_read_timeout(stmt_timeout_s, op_timeout_s, readonly),
                "write_timeout": op_timeout_s,
            },
        )

        # max_execution_time 只对 SELECT 生效，用语句超时；socket 层已按角色区分
        statements = mysql_session_statements(stmt_timeout_s, readonly)

        @event.listens_for(engine, "connect")
        def _mysql_session_setup(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            try:
                for stmt in statements:
                    cursor.execute(stmt)
            finally:
                cursor.close()

        return engine

    if cfg.engine == "postgres":
        from sqlalchemy.engine import URL

        url = URL.create(
            "postgresql+psycopg",
            username=user,
            password=password,
            host=host,
            port=port or 5432,
            database=cfg.database,
        )
        # PG 的 statement_timeout 对所有语句生效（含写）；writer 用写超时，reader 用语句超时
        options = f"-c statement_timeout={op_timeout_s * 1000}"
        if readonly:
            options += " -c default_transaction_read_only=on"
        if schema:
            options += f" -c search_path={schema}"
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5, "options": options},
        )

    raise UnsupportedEngineError(f"引擎 {cfg.engine!r} 暂不支持直连查询（Redis 适配在 M4）")


_PAGINATE_DIALECTS = {"mysql": "mysql", "postgres": "postgres", "sqlite": "sqlite"}


def paginate_sql(sql: str, engine_kind: str, limit: int, offset: int) -> tuple[str, bool, bool]:
    """给缺 LIMIT 的顶层 SELECT/UNION 注入 LIMIT/OFFSET。

    返回 (新SQL, 是否已分页, 是否带 ORDER BY)——无 ORDER BY 的 LIMIT/OFFSET 翻页
    顺序不稳定（可能重复/漏行），调用方据此提示使用者。

    分页兜底——防止 `SELECT * FROM 大表` 把全表拉进客户端把 DB/进程跑挂。
    用户自带 LIMIT 则尊重不改；SHOW/DESCRIBE/EXPLAIN 等非 SELECT 不动；解析失败也不动。
    """
    import sqlglot  # noqa: PLC0415
    from sqlglot import exp  # noqa: PLC0415

    dialect = _PAGINATE_DIALECTS.get(engine_kind)
    try:
        expr = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return sql, False, False
    if not isinstance(expr, (exp.Select, exp.Union)):
        return sql, False, False
    ordered = expr.args.get("order") is not None
    if expr.args.get("limit"):
        return sql, False, ordered
    expr = expr.limit(limit)
    if offset:
        expr = expr.offset(offset)
    return expr.sql(dialect=dialect), True, ordered


def make_canceller(engine: SAEngine, sa_conn) -> Callable[[], None]:  # noqa: ANN001
    """为一次正在执行的查询构造「取消函数」：在 DB 层中断它，而不是杀线程。

    - MySQL：新开一条连接执行 `KILL QUERY <连接id>`（同账号可杀自己的查询）
    - Postgres：`pg_cancel_backend(<pid>)`
    - SQLite：对同一底层连接调用 interrupt()（文档允许跨线程调用）

    取不到连接标识时返回空操作（cancel 变成对排队任务生效、对运行中无害）。
    """
    name = engine.dialect.name
    try:
        raw = sa_conn.connection.dbapi_connection
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None:
        return lambda: None

    if name == "mysql":
        try:
            cid = int(raw.thread_id())
        except Exception:  # noqa: BLE001
            return lambda: None

        def _cancel_mysql() -> None:
            with engine.connect() as c:
                c.exec_driver_sql(f"KILL QUERY {cid}")

        return _cancel_mysql

    if name in ("postgresql", "postgres"):
        pid = None
        try:
            pid = raw.info.backend_pid  # psycopg3
        except Exception:  # noqa: BLE001
            try:
                pid = raw.get_backend_pid()  # psycopg2
            except Exception:  # noqa: BLE001
                pid = None
        if pid is None:
            return lambda: None

        def _cancel_pg() -> None:
            with engine.connect() as c:
                c.exec_driver_sql(f"SELECT pg_cancel_backend({int(pid)})")

        return _cancel_pg

    if name == "sqlite":
        def _cancel_sqlite() -> None:
            try:
                raw.interrupt()
            except Exception:  # noqa: BLE001
                pass

        return _cancel_sqlite

    return lambda: None


def run_query(
    engine: SAEngine, sql: str, max_rows: int, max_cell_chars: int = 4096,
    on_start: Callable[[Callable[[], None]], None] | None = None,
) -> QueryResult:
    """执行只读 SQL（调用方必须先通过 classify 判定），行数截到 max_rows，单元格截到 max_cell_chars。

    注：不用流式游标（pymysql SSCursor 提前关闭 + 连接池复用会「commands out of sync」）；
    改由调用方注入 LIMIT 兜底（paginate_sql）限制 DB 返回行数，缓冲游标即可安全。

    on_start：拿到连接后回调一次，传入该查询的取消函数（供上层串行队列的 cancel 使用）。
    """
    start = dt.datetime.now()
    with engine.connect() as conn:
        if on_start is not None:
            on_start(make_canceller(engine, conn))
        result = conn.execute(text(sql))
        if result.returns_rows:
            columns = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            page = fetched[:max_rows]
            # 列类型分类须在 _jsonable 前用原始 Python 值算（大整数一旦转字符串就丢了类型）
            column_types = _col_categories(len(columns), page)
            rows = [
                [truncate_cell(_jsonable(v), max_cell_chars) for v in row]
                for row in page
            ]
        else:
            columns, rows, truncated, column_types = [], [], False, []
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns, rows, len(rows), truncated, duration_ms, column_types)


def truncate_cell(value: Any, max_chars: int) -> Any:
    """超长字符串单元格截断并标注原始长度（含 bytes 的 base64 包装形式）。"""
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"…[已截断，原 {len(value)} 字符]"
    if isinstance(value, dict) and "__bytes_base64__" in value:
        b64 = value["__bytes_base64__"]
        if isinstance(b64, str) and len(b64) > max_chars:
            return {"__bytes_base64__": b64[:max_chars] + f"…[已截断，原 {len(b64)} 字符]"}
    return value


def estimate_row_count(engine: SAEngine, engine_kind: str, table: str) -> int | None:
    """表行数量级估算。优先用引擎统计（避免大表全表 count），取不到再退回精确 count。

    - MySQL:    information_schema.tables.table_rows（引擎维护的近似值）
    - Postgres: pg_class.reltuples（analyze 后的近似值，-1 表示未统计）
    - SQLite:   无统计表，直接 count(*)
    调用方必须已校验 table 存在。返回 None 表示无法估算。
    """
    try:
        with engine.connect() as conn:
            if engine_kind == "mysql":
                row = conn.execute(
                    text(
                        "SELECT table_rows FROM information_schema.tables"
                        " WHERE table_schema = DATABASE() AND table_name = :t"
                    ),
                    {"t": table},
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else None
            if engine_kind == "postgres":
                row = conn.execute(
                    text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"),
                    {"t": table},
                ).fetchone()
                if not row or row[0] is None or row[0] < 0:
                    return None
                return int(row[0])
            if engine_kind == "sqlite":
                preparer = engine.dialect.identifier_preparer
                row = conn.execute(text(f"SELECT count(*) FROM {preparer.quote(table)}")).fetchone()
                return int(row[0]) if row else None
    except Exception:
        return None
    return None


def collect_table_meta(engine: SAEngine, engine_kind: str, table: str) -> dict:
    """采集单表的结构 + 索引 + 行数估算，供元数据缓存与风险评估使用。"""
    info = describe_table(engine, table)
    info["row_estimate"] = estimate_row_count(engine, engine_kind, table)
    return info


def run_write(
    engine: SAEngine, sql: str,
    on_start: Callable[[Callable[[], None]], None] | None = None,
) -> QueryResult:
    """执行写 SQL（调用方必须确保已通过审批），返回受影响行数。事务自动提交。

    on_start：拿到连接后回调一次，传入取消函数（KILL QUERY），供串行队列 cancel 中断大写操作。

    多语句批量：按分号拆成单条、在同一事务里逐条执行（pymysql/psycopg 单条 execute
    不支持多语句），受影响行数为各 DML 语句 rowcount 之和。注意 MySQL 的 DDL 会隐式
    提交，ALTER+DML 混合批次无法整体回滚（MySQL 本身限制）。
    """
    from .workflows import split_statements  # 懒加载避免与 workflows 循环导入

    stmts = split_statements(sql) or [sql]
    start = dt.datetime.now()
    affected = 0
    with engine.begin() as conn:
        if on_start is not None:
            on_start(make_canceller(engine, conn))
        for stmt in stmts:
            result = conn.execute(text(stmt))
            rc = result.rowcount if result.rowcount is not None else -1
            if rc > 0:
                affected += rc
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns=[], rows=[], row_count=affected, truncated=False, duration_ms=duration_ms)


def search_tables(engine: SAEngine, engine_kind: str, q: str, limit: int = 50) -> list[dict]:
    """跨库按名模糊搜表（查询台 ⌘P 跳转）。返回 [{db, table}]，全程参数化。"""
    like = f"%{q}%"
    if engine_kind == "mysql":
        sql = ("SELECT table_schema, table_name FROM information_schema.tables"
               " WHERE table_name LIKE :q AND table_schema NOT IN"
               " ('mysql','information_schema','performance_schema','sys')"
               " ORDER BY table_schema, table_name LIMIT :n")
    elif engine_kind == "postgres":
        sql = ("SELECT schemaname, tablename FROM pg_catalog.pg_tables"
               " WHERE tablename LIKE :q AND schemaname NOT IN ('pg_catalog','information_schema')"
               " ORDER BY schemaname, tablename LIMIT :n")
    else:  # sqlite
        sql = ("SELECT '' AS s, name FROM sqlite_master WHERE type = 'table'"
               " AND name LIKE :q ORDER BY name LIMIT :n")
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"q": like, "n": limit}).fetchall()
    return [{"db": r[0] or "", "table": r[1]} for r in rows]


def insert_rows(engine: SAEngine, table: str, columns: list[str],
                rows: list[list], schema: str | None = None) -> QueryResult:
    """参数化批量 INSERT（单事务，全部成功或整体回滚）。

    表名/列名由调用方经表结构校验后传入；此处用 SQLAlchemy 构造以正确按方言加引号，
    值全部走绑定参数——导入数据永不拼接 SQL。
    """
    import sqlalchemy as sa

    start = dt.datetime.now()
    t = sa.table(table, *[sa.column(c) for c in columns], schema=schema or None)
    params = [dict(zip(columns, r, strict=False)) for r in rows]
    with engine.begin() as conn:
        conn.execute(sa.insert(t), params)
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns=[], rows=[], row_count=len(rows), truncated=False,
                       duration_ms=duration_ms)


def explain(engine: SAEngine, sql: str, engine_kind: str) -> str | None:
    """取执行计划文本，供风险评估参考。失败返回 None（不阻断主流程）。"""
    prefix = "EXPLAIN "  # 不带 ANALYZE，避免真实执行写语句
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(prefix + sql)).fetchall()
        return "\n".join(" | ".join(str(v) for v in row) for row in rows)
    except Exception:
        return None


# 列库/schema 时过滤掉系统库，减少噪音
_SYSTEM_SCHEMAS = {
    "information_schema", "performance_schema", "mysql", "sys", "pg_catalog", "pg_toast",
}


def list_databases(engine: SAEngine) -> list[str]:
    """列出可选的库 / schema（MySQL 是数据库，PG 是 schema），过滤系统库。

    用于未绑定默认库的连接：先选库，再在该库下列表（避免 no-database 反射崩溃）。
    """
    names = inspect(engine).get_schema_names()
    return sorted(n for n in names if n and n.lower() not in _SYSTEM_SCHEMAS)


def list_tables(engine: SAEngine, schema: str | None = None) -> list[str]:
    return sorted(inspect(engine).get_table_names(schema=schema))


def describe_table(engine: SAEngine, table: str, schema: str | None = None) -> dict:
    insp = inspect(engine)
    _ensure_table_exists(insp, table, schema)
    columns = [
        {
            "name": c["name"],
            "type": str(c["type"]),
            "nullable": c.get("nullable", True),
            "default": _jsonable(c.get("default")),
            "comment": c.get("comment"),
        }
        for c in insp.get_columns(table, schema=schema)
    ]
    indexes = [
        {"name": i.get("name"), "columns": i.get("column_names"), "unique": i.get("unique")}
        for i in insp.get_indexes(table, schema=schema)
    ]
    pk = insp.get_pk_constraint(table, schema=schema)
    return {"table": table, "schema": schema, "columns": columns, "indexes": indexes,
            "primary_key": pk.get("constrained_columns", [])}


def table_sizes(engine: SAEngine, engine_kind: str, schema: str | None = None) -> dict[str, int]:
    """按表返回存储容量（字节，数据+索引），供树右侧分级展示。

    一次查询拿整个库（不逐表），取不到（引擎不支持/权限不足）返回空 dict 不阻断。
    """
    try:
        with engine.connect() as conn:
            if engine_kind == "mysql":
                rows = conn.execute(text(
                    "SELECT table_name, COALESCE(data_length,0)+COALESCE(index_length,0)"
                    " FROM information_schema.tables"
                    " WHERE table_schema = COALESCE(:s, DATABASE())"), {"s": schema}).fetchall()
            elif engine_kind == "postgres":
                rows = conn.execute(text(
                    "SELECT c.relname, pg_total_relation_size(c.oid)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE c.relkind IN ('r','p')"
                    " AND n.nspname = COALESCE(:s, current_schema())"), {"s": schema}).fetchall()
            elif engine_kind == "sqlite":
                # dbstat 虚表需编译开启（macOS/多数发行版默认有）；无则走 except 返回空
                rows = conn.execute(text(
                    "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")).fetchall()
            else:
                return {}
        return {str(r[0]): int(r[1] or 0) for r in rows}
    except Exception:
        return {}


def get_table_ddl(engine: SAEngine, engine_kind: str, table: str, schema: str | None = None) -> str:
    """取建表语句。MySQL/SQLite 取服务器原文；PG 无内建语句，反射拼近似 DDL。

    表名先经存在性校验，再用方言引用符包裹，杜绝注入。
    """
    insp = inspect(engine)
    _ensure_table_exists(insp, table, schema)
    preparer = engine.dialect.identifier_preparer

    if engine_kind == "mysql":
        q = (preparer.quote(schema) + "." if schema else "") + preparer.quote(table)
        with engine.connect() as conn:
            row = conn.execute(text(f"SHOW CREATE TABLE {q}")).fetchone()
        return str(row[1]) if row and len(row) > 1 else ""

    if engine_kind == "sqlite":
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE tbl_name = :t AND sql IS NOT NULL"),
                {"t": table},
            ).fetchall()
        return ";\n\n".join(str(r[0]) for r in rows)

    # PG 等：无 SHOW CREATE TABLE，由反射信息生成近似 DDL（注释标明非服务器原文）
    info = describe_table(engine, table, schema)
    body = []
    for c in info["columns"]:
        line = f"  {c['name']} {c['type']}"
        if not c.get("nullable", True):
            line += " NOT NULL"
        if c.get("default") is not None:
            line += f" DEFAULT {c['default']}"
        body.append(line)
    if info.get("primary_key"):
        body.append("  PRIMARY KEY (" + ", ".join(info["primary_key"]) + ")")
    qname = f"{schema}.{table}" if schema else table
    out = ["-- 由表结构反射生成的近似 DDL（该引擎无 SHOW CREATE TABLE）",
           f"CREATE TABLE {qname} (", ",\n".join(body), ");"]
    for i in info.get("indexes", []):
        uniq = "UNIQUE " if i.get("unique") else ""
        cols = ", ".join(i.get("columns") or [])
        out.append(f"CREATE {uniq}INDEX {i['name']} ON {qname} ({cols});")
    return "\n".join(out)


def sample_rows(
    engine: SAEngine, table: str, limit: int, max_cell_chars: int = 4096
) -> QueryResult:
    insp = inspect(engine)
    _ensure_table_exists(insp, table)
    # 表名经存在性校验后再用方言引用符包裹，杜绝注入
    preparer = engine.dialect.identifier_preparer
    quoted = preparer.quote(table)
    return run_query(engine, f"SELECT * FROM {quoted}", max_rows=limit, max_cell_chars=max_cell_chars)


def _ensure_table_exists(insp, table: str, schema: str | None = None) -> None:  # noqa: ANN001
    names = insp.get_table_names(schema=schema)
    if table not in names:
        raise ValueError(f"表 {table!r} 不存在，可用表: {', '.join(sorted(names)) or '（无）'}")


# JS Number.MAX_SAFE_INTEGER = 2^53-1；超过它的整数（雪花 ID/int64）在前端 JSON.parse
# 时会被 double 近似而丢精度（末位改变），必须以字符串下发；列类型另由 column_types 标注。
_JS_SAFE_INT = 9007199254740991


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, float, str)):
        return value
    if isinstance(value, int):
        # 超 JS 安全整数范围 → 转字符串保精度（不影响小整数，仍是数字）
        return str(value) if abs(value) > _JS_SAFE_INT else value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_base64__": base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)


def _value_category(v: Any) -> str:
    """由原始 Python 值推断列类型分类，与前端 COL_GLYPH 词表对齐。

    只对**类型明确**的 Python 值给出分类；字符串返回 ""（未知），让前端按内容推断
    （保留查询台对日期串/JSON 串的既有识别，不回退）。大整数虽以字符串下发，但这里
    看到的是原始 int → 归类 number，前端图标据此显示 #。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float, decimal.Decimal)):
        return "number"
    if isinstance(v, dt.datetime):
        return "datetime"
    if isinstance(v, dt.date):
        return "date"
    if isinstance(v, dt.time):
        return "time"
    if isinstance(v, (bytes, bytearray)):
        return "binary"
    if isinstance(v, (dict, list)):
        return "json"
    return ""  # 字符串等：交给前端按内容推断


def _col_categories(ncols: int, rows: list) -> list[str]:
    """每列取首个非空原始值推断类型分类；全空列为 ""。"""
    cats = [""] * ncols
    for j in range(ncols):
        for row in rows:
            if row[j] is not None:
                cats[j] = _value_category(row[j])
                break
    return cats
