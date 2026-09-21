"""MySQL 驱动：pymysql + 会话级只读防线与语句超时。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine as SAEngine

from ..config import ConnectionConfig
from ..secrets import resolve_secret
from .base import (
    DB_CLIENT_NAME,
    DbDriver,
    Role,
    SyntaxCheck,
    _SYNTAX_CHECK_STMT_NAME,
    register,
    resolve_account,
    role_timeouts,
)

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


@register
class MysqlDriver(DbDriver):
    name = "mysql"
    dialect = "mysql"
    sa_dialect_names = ("mysql",)
    default_port = 3306
    # MySQL 9 起 explain_format 默认 TREE，而 TREE 解释不了 DML（只回一句
    # "not executable by iterator executor"）；显式要传统表格式才有 type/key/rows 可看。
    icon = "mysql"
    client_lib = ("pymysql", "pymysql")
    explain_prefix = "EXPLAIN FORMAT=TRADITIONAL "
    explain_json_prefix = "EXPLAIN FORMAT=JSON "
    explain_format = "json"

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
        stmt_timeout_s, op_timeout_s = role_timeouts(cfg.policy, readonly)
        user, password_ref = resolve_account(cfg, role)
        password = resolve_secret(password_ref)
        # writer 引擎不施加数据库层只读约束（写操作已经过审批）

        url = URL.create(
            "mysql+pymysql",
            username=user,
            password=password,
            host=host,
            port=port or 3306,
            database=schema or cfg.database,
        )
        engine = engines.create_engine(
            url,
            pool_pre_ping=True,
            **engines._sa_pool_kwargs(pool_size),
            connect_args={
                "connect_timeout": 5,
                # 展示在 MySQL performance_schema.session_connect_attrs 中
                "program_name": DB_CLIENT_NAME,
                # reader 的 read_timeout 加宽限，让服务端 max_execution_time 先于 socket 超时
                # （否则二者相等时长 SELECT 被打成 2013 Lost connection，而非干净的超时错误）
                "read_timeout": mysql_read_timeout(stmt_timeout_s, op_timeout_s, readonly),
                "write_timeout": op_timeout_s,
            },
        )

        # max_execution_time 只对 SELECT 生效，用语句超时；socket 层已按角色区分
        statements = mysql_session_statements(stmt_timeout_s, readonly)

        @engines.event.listens_for(engine, "connect")
        def _mysql_session_setup(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            try:
                for stmt in statements:
                    cursor.execute(stmt)
            finally:
                cursor.close()

        return engine

    def make_canceller(self, engine: SAEngine, sa_conn) -> Callable[[], None]:  # noqa: ANN001
        """MySQL：新开一条连接执行 KILL QUERY <连接id>（同账号可杀自己的查询）。"""
        try:
            raw = sa_conn.connection.dbapi_connection
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None:
            return lambda: None
        try:
            cid = int(raw.thread_id())
        except Exception:  # noqa: BLE001
            return lambda: None

        def _cancel() -> None:
            with engine.connect() as c:
                c.exec_driver_sql(f"KILL QUERY {cid}")

        return _cancel

    def search_tables(self, engine: SAEngine, q: str, limit: int = 50) -> list[dict]:
        like = f"%{q}%"
        sql = ("SELECT table_schema, table_name FROM information_schema.tables"
               " WHERE table_name LIKE :q AND table_schema NOT IN"
               " ('mysql','information_schema','performance_schema','sys')"
               " ORDER BY table_schema, table_name LIMIT :n")
        with engine.connect() as conn:
            rows = conn.execute(text(sql), {"q": like, "n": limit}).fetchall()
        return [{"db": r[0] or "", "table": r[1]} for r in rows]

    def table_sizes(self, engine: SAEngine, schema: str | None = None) -> dict[str, int]:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT table_name, COALESCE(data_length,0)+COALESCE(index_length,0)"
                    " FROM information_schema.tables"
                    " WHERE table_schema = COALESCE(:s, DATABASE())"), {"s": schema}).fetchall()
            return {str(r[0]): int(r[1] or 0) for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def estimate_row_count(self, engine: SAEngine, table: str,
                           schema: str | None = None) -> int | None:
        """information_schema.tables.table_rows（引擎维护的近似值）。"""
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT table_rows FROM information_schema.tables"
                        " WHERE table_schema = COALESCE(:s, DATABASE()) AND table_name = :t"
                    ),
                    {"t": table, "s": schema},
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else None
        except Exception:  # noqa: BLE001
            return None

    def get_table_ddl(self, engine: SAEngine, table: str,
                      schema: str | None = None) -> str:
        """MySQL 有 SHOW CREATE TABLE，返回两列（第二列是 DDL 原文）。"""
        from ..engines import _ensure_table_exists

        insp = inspect(engine)
        _ensure_table_exists(insp, table, schema)
        preparer = engine.dialect.identifier_preparer
        q = (preparer.quote(schema) + "." if schema else "") + preparer.quote(table)
        with engine.connect() as conn:
            row = conn.execute(text(f"SHOW CREATE TABLE {q}")).fetchone()
        return str(row[1]) if row and len(row) > 1 else ""

    def syntax_check_supported(self, stmt: str) -> bool:
        return True

    def check_statement(self, conn, stmt: str) -> SyntaxCheck:  # noqa: ANN001
        """PREPARE 复核：用会话变量传 SQL 文本（参数化，SQL 本身不拼进语句）。"""
        from ..errors import classify_db_error, sanitize_db_message

        try:
            conn.execute(text("SET @dbm_syntax_sql = :s"), {"s": stmt})
            conn.execute(text(f"PREPARE {_SYNTAX_CHECK_STMT_NAME} FROM @dbm_syntax_sql"))
        except Exception as e:  # noqa: BLE001
            if classify_db_error(e) == "sql_syntax_error":
                return SyntaxCheck(supported=True, ok=False,
                                   error=sanitize_db_message(str(e)))
            return SyntaxCheck(supported=True, ok=True)
        finally:
            try:
                conn.execute(text(f"DEALLOCATE PREPARE {_SYNTAX_CHECK_STMT_NAME}"))
            except Exception:  # noqa: BLE001
                pass  # PREPARE 本身失败时没东西可释放，忽略
        return SyntaxCheck(supported=True, ok=True)
