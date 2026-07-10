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
from typing import Any, Literal

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine as SAEngine

from .config import ConnectionConfig
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
    """按 (project, connection, role) 缓存引擎，并托管其 SSH 隧道生命周期。"""

    def __init__(self, idle_reclaim_s: int = DEFAULT_IDLE_RECLAIM_S):
        self._entries: dict[tuple[str, str, Role], _PooledEngine] = {}
        self._lock = threading.Lock()
        self._idle_reclaim_s = idle_reclaim_s

    def get(
        self,
        project: str,
        connection: str,
        cfg: ConnectionConfig,
        role: Role = "reader",
    ) -> SAEngine:
        key = (project, connection, role)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry.tunnel is None or entry.tunnel.is_alive()):
                entry.last_used = time.monotonic()
                return entry.engine
            if entry is not None:
                # 隧道已死，连引擎一起回收后重建
                entry.dispose()
                del self._entries[key]
            entry = _build_pooled_engine(cfg, role)
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

    def dispose(self) -> None:
        with self._lock:
            for entry in self._entries.values():
                entry.dispose()
            self._entries.clear()


def _build_pooled_engine(cfg: ConnectionConfig, role: Role) -> _PooledEngine:
    tunnel: SSHTunnel | None = None
    host, port = cfg.host, cfg.port

    # SQLite 无网络，跳板不适用
    if cfg.engine != "sqlite" and cfg.jump_hosts:
        default_port = 3306 if cfg.engine == "mysql" else 5432
        tunnel = open_tunnel(cfg.host, cfg.port or default_port, cfg.jump_hosts, cfg.ssh_options)
        host, port = "127.0.0.1", tunnel.local_port

    try:
        engine = _create_readonly_engine(cfg, role, host, port)
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
) -> SAEngine:
    timeout_s = cfg.policy.statement_timeout_s

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
    readonly = role == "reader"

    if cfg.engine == "mysql":
        from sqlalchemy.engine import URL

        url = URL.create(
            "mysql+pymysql",
            username=user,
            password=password,
            host=host,
            port=port or 3306,
            database=cfg.database,
        )
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 5,
                "read_timeout": timeout_s,
                "write_timeout": timeout_s,
            },
        )

        statements = mysql_session_statements(timeout_s, readonly)

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
        options = f"-c statement_timeout={timeout_s * 1000}"
        if readonly:
            options += " -c default_transaction_read_only=on"
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5, "options": options},
        )

    raise UnsupportedEngineError(f"引擎 {cfg.engine!r} 暂不支持直连查询（Redis 适配在 M4）")


def run_query(
    engine: SAEngine, sql: str, max_rows: int, max_cell_chars: int = 4096
) -> QueryResult:
    """执行只读 SQL（调用方必须先通过 classify 判定），行数截到 max_rows，单元格截到 max_cell_chars。"""
    start = dt.datetime.now()
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        if result.returns_rows:
            columns = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [
                [truncate_cell(_jsonable(v), max_cell_chars) for v in row]
                for row in fetched[:max_rows]
            ]
        else:
            columns, rows, truncated = [], [], False
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns, rows, len(rows), truncated, duration_ms)


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


def run_write(engine: SAEngine, sql: str) -> QueryResult:
    """执行写 SQL（调用方必须确保已通过审批），返回受影响行数。事务自动提交。"""
    start = dt.datetime.now()
    with engine.begin() as conn:
        result = conn.execute(text(sql))
        affected = result.rowcount if result.rowcount is not None else -1
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns=[], rows=[], row_count=affected, truncated=False, duration_ms=duration_ms)


def explain(engine: SAEngine, sql: str, engine_kind: str) -> str | None:
    """取执行计划文本，供风险评估参考。失败返回 None（不阻断主流程）。"""
    prefix = "EXPLAIN "  # 不带 ANALYZE，避免真实执行写语句
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(prefix + sql)).fetchall()
        return "\n".join(" | ".join(str(v) for v in row) for row in rows)
    except Exception:
        return None


def list_tables(engine: SAEngine) -> list[str]:
    return sorted(inspect(engine).get_table_names())


def describe_table(engine: SAEngine, table: str) -> dict:
    insp = inspect(engine)
    _ensure_table_exists(insp, table)
    columns = [
        {
            "name": c["name"],
            "type": str(c["type"]),
            "nullable": c.get("nullable", True),
            "default": _jsonable(c.get("default")),
            "comment": c.get("comment"),
        }
        for c in insp.get_columns(table)
    ]
    indexes = [
        {"name": i.get("name"), "columns": i.get("column_names"), "unique": i.get("unique")}
        for i in insp.get_indexes(table)
    ]
    pk = insp.get_pk_constraint(table)
    return {"table": table, "columns": columns, "indexes": indexes, "primary_key": pk.get("constrained_columns", [])}


def sample_rows(
    engine: SAEngine, table: str, limit: int, max_cell_chars: int = 4096
) -> QueryResult:
    insp = inspect(engine)
    _ensure_table_exists(insp, table)
    # 表名经存在性校验后再用方言引用符包裹，杜绝注入
    preparer = engine.dialect.identifier_preparer
    quoted = preparer.quote(table)
    return run_query(engine, f"SELECT * FROM {quoted}", max_rows=limit, max_cell_chars=max_cell_chars)


def _ensure_table_exists(insp, table: str) -> None:  # noqa: ANN001
    names = insp.get_table_names()
    if table not in names:
        raise ValueError(f"表 {table!r} 不存在，可用表: {', '.join(sorted(names)) or '（无）'}")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_base64__": base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)
