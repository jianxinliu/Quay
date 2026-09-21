"""SQLite 驱动：本地文件库，无账号概念，只读防线走 PRAGMA。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine as SAEngine

from ..config import ConnectionConfig
from .base import DbDriver, Role, SyntaxCheck, register

_SYNTAX_CHECK_STMT_NAME = "dbm_syntax_chk"  # 与 base 同名（保持与历史行为一致）


@register
class SqliteDriver(DbDriver):
    name = "sqlite"
    dialect = "sqlite"
    sa_dialect_names = ("sqlite",)
    default_port = None
    # 审批流看 VDBE 操作码（EXPLAIN）；查询台看 QUERY PLAN 行集（更紧凑易读）
    icon = "sqlite"
    has_schema_layer = False   # 一个文件就是一个库，没有「选库」这一层
    explain_prefix = "EXPLAIN "
    explain_json_prefix = "EXPLAIN QUERY PLAN "
    explain_format = "rows"

    def build_engine(
        self,
        cfg: ConnectionConfig,
        role: Role,
        host: str | None,
        port: int | None,
        schema: str | None = None,
        pool_size: int = 15,
        database: str | None = None,
    ) -> SAEngine:
        from .. import engines

        # 本地文件库：不用账号、不用连接池参数（并发无意义）
        engine = engines.create_engine(f"sqlite:///{cfg.database}", pool_pre_ping=True)

        if role == "reader":

            @engines.event.listens_for(engine, "connect")
            def _sqlite_readonly(dbapi_conn, _record):  # noqa: ANN001
                dbapi_conn.execute("PRAGMA query_only = ON")

        return engine

    def make_canceller(self, engine: SAEngine, sa_conn) -> Callable[[], None]:  # noqa: ANN001
        """SQLite：对同一底层连接调用 interrupt()（文档允许跨线程调用）。"""
        try:
            raw = sa_conn.connection.dbapi_connection
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None:
            return lambda: None

        def _cancel() -> None:
            try:
                raw.interrupt()
            except Exception:  # noqa: BLE001
                pass

        return _cancel

    def search_tables(self, engine: SAEngine, q: str, limit: int = 50) -> list[dict]:
        like = f"%{q}%"
        sql = ("SELECT '' AS s, name FROM sqlite_master WHERE type = 'table'"
               " AND name LIKE :q ORDER BY name LIMIT :n")
        with engine.connect() as conn:
            rows = conn.execute(text(sql), {"q": like, "n": limit}).fetchall()
        return [{"db": r[0] or "", "table": r[1]} for r in rows]

    def table_sizes(self, engine: SAEngine, schema: str | None = None) -> dict[str, int]:
        """dbstat 虚表需编译开启（macOS/多数发行版默认有）；无则返回空 dict。"""
        try:
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")).fetchall()
            return {str(r[0]): int(r[1] or 0) for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def estimate_row_count(self, engine: SAEngine, table: str,
                           schema: str | None = None) -> int | None:
        """SQLite 无统计表，直接 count(*)。调用方必须已校验 table 存在。"""
        try:
            preparer = engine.dialect.identifier_preparer
            qualified = (f"{preparer.quote(schema)}." if schema else "") + preparer.quote(table)
            with engine.connect() as conn:
                row = conn.execute(text(f"SELECT count(*) FROM {qualified}")).fetchone()
            return int(row[0]) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_table_ddl(self, engine: SAEngine, table: str,
                      schema: str | None = None) -> str:
        """SQLite 有 sqlite_master.sql，取服务器原文（可能多段：表 + 索引/触发器）。"""
        from ..engines import _ensure_table_exists

        _ensure_table_exists(inspect(engine), table, schema)
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE tbl_name = :t AND sql IS NOT NULL"),
                {"t": table},
            ).fetchall()
        return ";\n\n".join(str(r[0]) for r in rows)

    def syntax_check_supported(self, stmt: str) -> bool:
        return True

    def check_statement(self, conn, stmt: str) -> SyntaxCheck:  # noqa: ANN001
        """EXPLAIN 只解析不执行（SQLite 的 EXPLAIN 不带 QUERY PLAN 时仍是解析态）。"""
        from ..errors import classify_db_error, sanitize_db_message

        try:
            conn.execute(text(f"EXPLAIN {stmt}"))
        except Exception as e:  # noqa: BLE001
            if classify_db_error(e) == "sql_syntax_error":
                return SyntaxCheck(supported=True, ok=False,
                                   error=sanitize_db_message(str(e)))
            return SyntaxCheck(supported=True, ok=True)
        return SyntaxCheck(supported=True, ok=True)
