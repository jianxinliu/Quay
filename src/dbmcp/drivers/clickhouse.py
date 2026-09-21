"""ClickHouse 驱动：native 协议，只读防线走 URL query 参数。

本期 ClickHouse 只作只读分析：readonly=1 是数据库层第二道防线（reader 上任何写/DDL
报 Code 164「Cannot execute query in readonly mode」）。实测 per-query 的 settings dict
不生效，只有 URL query 参数才真正落到 system.settings.readonly（须用真实 ClickHouse 才验得出）。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine as SAEngine

from ..config import ConnectionConfig
from ..secrets import resolve_secret
from .base import DB_CLIENT_NAME, DbDriver, Role, SyntaxCheck, register, resolve_account, role_timeouts

_CH_EXPLAINABLE_HEADS = ("select", "with")

_SYNTAX_CHECK_STMT_NAME = "dbm_syntax_chk"


@register
class ClickhouseDriver(DbDriver):
    name = "clickhouse"
    dialect = "clickhouse"
    sa_dialect_names = ("clickhouse",)
    default_port = 9000
    icon = "clickhouse"
    sync_target = False        # 本项目只读（不配 writer），不能做同步目标
    ai_sql = False               # 方言覆盖不全，生成的 SQL 跑不了，先关掉
    explain_prefix = "EXPLAIN "
    # 查询台 JSON 计划：CH 的 EXPLAIN 有 PLAN/PIPE/AST 等形态但非 JSON 行集，不开放
    explain_json_prefix = None
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
        from sqlalchemy.engine import URL

        from .. import engines


        readonly = role == "reader"
        stmt_timeout_s, _op_timeout_s = role_timeouts(cfg.policy, readonly)
        user, password_ref = resolve_account(cfg, role)
        password = resolve_secret(password_ref)

        # 会话设置走 URL query 参数（clickhouse-driver 在建连时应用）：
        # - max_execution_time 服务端语句超时（秒），与 readonly=1 可共存。
        query: dict[str, str] = {"max_execution_time": str(stmt_timeout_s)}
        if readonly:
            query["readonly"] = "1"
        url = URL.create(
            "clickhouse+native",
            username=user,
            password=password,
            host=host,
            port=port or 9000,
            database=schema or cfg.database or "default",
            query=query,
        )
        return engines.create_engine(
            url,
            pool_pre_ping=True,
            **engines._sa_pool_kwargs(pool_size),
            connect_args={
                "connect_timeout": 5,
                # 展示在 system.processes / system.query_log 的 client_name 中
                "client_name": DB_CLIENT_NAME,
            },
        )

    def search_tables(self, engine: SAEngine, q: str, limit: int = 50) -> list[dict]:
        like = f"%{q}%"
        sql = ("SELECT database, name FROM system.tables"
               " WHERE name LIKE :q AND database NOT IN"
               " ('system','information_schema','INFORMATION_SCHEMA')"
               " ORDER BY database, name LIMIT :n")
        with engine.connect() as conn:
            rows = conn.execute(text(sql), {"q": like, "n": limit}).fetchall()
        return [{"db": r[0] or "", "table": r[1]} for r in rows]

    def table_sizes(self, engine: SAEngine, schema: str | None = None) -> dict[str, int]:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT table, sum(bytes_on_disk) FROM system.parts"
                    " WHERE active AND database = coalesce(:s, currentDatabase())"
                    " GROUP BY table"), {"s": schema}).fetchall()
            return {str(r[0]): int(r[1] or 0) for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def estimate_row_count(self, engine: SAEngine, table: str,
                           schema: str | None = None) -> int | None:
        """system.tables.total_rows（CH 自己维护的精确值）。"""
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT total_rows FROM system.tables"
                         " WHERE database = coalesce(:s, currentDatabase()) AND name = :t"),
                    {"t": table, "s": schema},
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else None
        except Exception:  # noqa: BLE001
            return None

    def get_table_ddl(self, engine: SAEngine, table: str,
                      schema: str | None = None) -> str:
        """ClickHouse 有 SHOW CREATE TABLE，返回单列 DDL 原文（row[0]，MySQL 是 row[1]）。"""
        from sqlalchemy import inspect

        from ..engines import _ensure_table_exists

        insp = inspect(engine)
        _ensure_table_exists(insp, table, schema)
        preparer = engine.dialect.identifier_preparer
        q = (preparer.quote(schema) + "." if schema else "") + preparer.quote(table)
        with engine.connect() as conn:
            row = conn.execute(text(f"SHOW CREATE TABLE {q}")).fetchone()
        return str(row[0]) if row else ""

    def syntax_check_supported(self, stmt: str) -> bool:
        from .base import first_sql_keyword

        head = first_sql_keyword(stmt)
        return head in _CH_EXPLAINABLE_HEADS

    def check_statement(self, conn, stmt: str) -> SyntaxCheck:  # noqa: ANN001
        """EXPLAIN SYNTAX 只重写不执行，是 CH 的语法复核手段。"""
        from ..errors import classify_db_error, sanitize_db_message

        try:
            conn.execute(text(f"EXPLAIN SYNTAX {stmt}"))
        except Exception as e:  # noqa: BLE001
            if classify_db_error(e) == "sql_syntax_error":
                return SyntaxCheck(supported=True, ok=False,
                                   error=sanitize_db_message(str(e)))
            return SyntaxCheck(supported=True, ok=True)
        return SyntaxCheck(supported=True, ok=True)
