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
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine as SAEngine

from .config import ConnectionConfig
from .secrets import resolve_secret


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
class EnginePool:
    """按 (project, connection) 缓存只读 SQLAlchemy Engine。"""

    _engines: dict[tuple[str, str], SAEngine] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, project: str, connection: str, cfg: ConnectionConfig) -> SAEngine:
        key = (project, connection)
        with self._lock:
            engine = self._engines.get(key)
            if engine is None:
                engine = _create_readonly_engine(cfg)
                self._engines[key] = engine
            return engine

    def dispose(self) -> None:
        with self._lock:
            for engine in self._engines.values():
                engine.dispose()
            self._engines.clear()


def _create_readonly_engine(cfg: ConnectionConfig) -> SAEngine:
    timeout_s = cfg.policy.statement_timeout_s

    if cfg.engine == "sqlite":
        engine = create_engine(f"sqlite:///{cfg.database}", pool_pre_ping=True)

        @event.listens_for(engine, "connect")
        def _sqlite_readonly(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA query_only = ON")

        return engine

    password = resolve_secret(cfg.password or "")

    if cfg.engine == "mysql":
        from sqlalchemy.engine import URL

        url = URL.create(
            "mysql+pymysql",
            username=cfg.user,
            password=password,
            host=cfg.host,
            port=cfg.port or 3306,
            database=cfg.database,
        )
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 5,
                "read_timeout": timeout_s,
                "write_timeout": timeout_s,
                # max_execution_time 只约束 SELECT；READ ONLY 是数据库层第二道防线
                "init_command": (
                    f"SET SESSION max_execution_time={timeout_s * 1000},"
                    " SESSION TRANSACTION READ ONLY"
                ),
            },
        )

    if cfg.engine == "postgres":
        from sqlalchemy.engine import URL

        url = URL.create(
            "postgresql+psycopg",
            username=cfg.user,
            password=password,
            host=cfg.host,
            port=cfg.port or 5432,
            database=cfg.database,
        )
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 5,
                "options": (
                    f"-c statement_timeout={timeout_s * 1000}"
                    " -c default_transaction_read_only=on"
                ),
            },
        )

    raise UnsupportedEngineError(f"引擎 {cfg.engine!r} 暂不支持直连查询（Redis 适配在 M4）")


def run_query(engine: SAEngine, sql: str, max_rows: int) -> QueryResult:
    """执行只读 SQL（调用方必须先通过 classify 判定），结果集截断到 max_rows。"""
    start = dt.datetime.now()
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        if result.returns_rows:
            columns = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
        else:
            columns, rows, truncated = [], [], False
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns, rows, len(rows), truncated, duration_ms)


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


def sample_rows(engine: SAEngine, table: str, limit: int) -> QueryResult:
    insp = inspect(engine)
    _ensure_table_exists(insp, table)
    # 表名经存在性校验后再用方言引用符包裹，杜绝注入
    preparer = engine.dialect.identifier_preparer
    quoted = preparer.quote(table)
    return run_query(engine, f"SELECT * FROM {quoted}", max_rows=limit)


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
