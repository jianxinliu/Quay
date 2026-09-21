"""PostgreSQL 驱动：psycopg3，库/schema 两层，只读防线走服务端 options。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine as SAEngine

from ..config import ConnectionConfig
from ..secrets import resolve_secret
from .base import DB_CLIENT_NAME, DbDriver, Role, SyntaxCheck, register, resolve_account, role_timeouts

# PG 的 PREPARE 只接受这些开头的语句；其余（DDL 等）交给它会报**语法错**，是假阳性来源
_PG_PREPARABLE_HEADS = ("select", "insert", "update", "delete", "values", "with", "table")
# PG 的全部语句起始关键字（PostgreSQL SQL Commands 目录）。用途只有一个：判断
# 「这个词到底是不是一条合法语句的开头」。
# 背景：PG 的 PREPARE 只吃 DML，DDL 会被报成语法错，所以原来只对 _PG_PREPARABLE_HEADS
# 做复核。但这样一来**最常见的错误——把首关键字打错（SELCT）——恰好永远落在白名单外、
# 被整条跳过**，预检对 PG 等于形同虚设（真机 `SELCT * FROM crawl_job` 实测 supported=False）。
# 修法：首词若压根不是任何 PG 语句关键字，说明不存在以它开头的合法语句，交给 PREPARE 去报
# 真实语法错（仍由 DB 判死，不自己下结论——见 CLAUDE.md「静态解析器和目标 DB 是两套语法」）。
_PG_STATEMENT_HEADS = frozenset({
    "abort", "alter", "analyze", "analyse", "begin", "call", "checkpoint", "close", "cluster",
    "comment", "commit", "copy", "create", "deallocate", "declare", "delete", "discard", "do",
    "drop", "end", "execute", "explain", "fetch", "grant", "import", "insert", "listen", "load",
    "lock", "merge", "move", "notify", "prepare", "reassign", "refresh", "reindex", "release",
    "reset", "revoke", "rollback", "savepoint", "security", "select", "set", "show", "start",
    "table", "truncate", "unlisten", "update", "vacuum", "values", "with",
})

_SYNTAX_CHECK_STMT_NAME = "dbm_syntax_chk"


def pg_database_name(cfg: ConnectionConfig) -> str:
    """PG 连接实际要连的库名。未在配置里绑定 database 时**显式回退到 reader 账号名**。

    为什么不能就这么留空交给驱动：libpq 在 dbname 缺省时会用**连接账号自己的名字**当库名。
    于是同一个连接的 reader 与 writer 会落到**两个不同的库**——reader 连 `drama`、
    writer 连 `root`（真机 drama-u 就是这样，报 `FATAL: database "root" does not exist`）。
    库不存在时是直接连不上（吵，但至少看得见）；真正危险的是**库恰好存在**的情况：
    审批通过的写会安静地打进另一个库。

    所以这里把那个隐式默认变成显式的、且**对所有角色一致**的选择：跟 reader 走。
    配置里绑了 database 就用绑定的，不受影响。
    """
    return cfg.database or cfg.user or ""


@register
class PostgresDriver(DbDriver):
    name = "postgres"
    dialect = "postgres"
    # engine.dialect.name 在 psycopg3 驱动下是 "postgresql"（旧 psycopg2 也可能是 "postgres"）
    sa_dialect_names = ("postgresql", "postgres")
    default_port = 5432
    icon = "postgresql"
    explain_prefix = "EXPLAIN "
    explain_json_prefix = "EXPLAIN (FORMAT JSON) "
    explain_format = "json"
    # PG 的 database 与 schema 是两层：一条连接只绑一个库，浏览别的库要另建连接
    needs_database_layer = True
    client_lib = ("psycopg", "psycopg[binary]")

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

        url = URL.create(
            "postgresql+psycopg",
            username=user,
            password=password,
            host=host,
            port=port or 5432,
            # 显式选定的库优先（查询台切库）；否则用配置里的，未绑库时按 reader 账号名
            # 回退，保证 reader/writer 连的是同一个库
            database=(database or pg_database_name(cfg)) or None,
        )
        # PG 的 statement_timeout 对所有语句生效（含写）；writer 用写超时，reader 用语句超时
        options = f"-c statement_timeout={op_timeout_s * 1000}"
        if readonly:
            options += " -c default_transaction_read_only=on"
        if schema:
            options += f" -c search_path={schema}"
        return engines.create_engine(
            url,
            pool_pre_ping=True,
            **engines._sa_pool_kwargs(pool_size),
            connect_args={
                "connect_timeout": 5,
                # 展示在 pg_stat_activity.application_name 中
                "application_name": DB_CLIENT_NAME,
                "options": options,
            },
        )

    def make_canceller(self, engine: SAEngine, sa_conn) -> Callable[[], None]:  # noqa: ANN001
        """Postgres：SELECT pg_cancel_backend(<pid>)。"""
        try:
            raw = sa_conn.connection.dbapi_connection
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None:
            return lambda: None

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

        def _cancel() -> None:
            with engine.connect() as c:
                c.exec_driver_sql(f"SELECT pg_cancel_backend({int(pid)})")

        return _cancel

    def search_tables(self, engine: SAEngine, q: str, limit: int = 50) -> list[dict]:
        like = f"%{q}%"
        sql = ("SELECT schemaname, tablename FROM pg_catalog.pg_tables"
               " WHERE tablename LIKE :q AND schemaname NOT IN ('pg_catalog','information_schema')"
               " ORDER BY schemaname, tablename LIMIT :n")
        with engine.connect() as conn:
            rows = conn.execute(text(sql), {"q": like, "n": limit}).fetchall()
        return [{"db": r[0] or "", "table": r[1]} for r in rows]

    def list_server_databases(self, engine: SAEngine) -> list[str]:
        """PG：inspect().get_schema_names() 只给 schema，看不到同服务器其它 database。

        要浏览别的库必须查 pg_database，再对选中的库另建一条连接（EnginePool.get(database=…)）。
        """
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT datname FROM pg_database "
                "WHERE datallowconn AND NOT datistemplate ORDER BY datname"
            )).fetchall()
        return [r[0] for r in rows]

    def table_sizes(self, engine: SAEngine, schema: str | None = None) -> dict[str, int]:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT c.relname, pg_total_relation_size(c.oid)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE c.relkind IN ('r','p')"
                    " AND n.nspname = COALESCE(:s, current_schema())"), {"s": schema}).fetchall()
            return {str(r[0]): int(r[1] or 0) for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def estimate_row_count(self, engine: SAEngine, table: str,
                           schema: str | None = None) -> int | None:
        """pg_class.reltuples（analyze 后的近似值，-1 表示未统计）。

        必须限定 schema：不同 schema 下的同名表都叫这个 relname。
        """
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT c.reltuples::bigint FROM pg_class c"
                         " JOIN pg_namespace n ON n.oid = c.relnamespace"
                         " WHERE c.relname = :t AND n.nspname = COALESCE(:s, current_schema())"),
                    {"t": table, "s": schema},
                ).fetchone()
                if not row or row[0] is None or row[0] < 0:
                    return None
                return int(row[0])
        except Exception:  # noqa: BLE001
            return None

    def syntax_check_supported(self, stmt: str) -> bool:
        from .base import first_sql_keyword

        head = first_sql_keyword(stmt)
        if not head:
            return False
        # 可 PREPARE 的 DML 照旧复核；非语句关键字（多半是打错的首词）也送去复核，
        # 只有「合法但 PREPARE 不支持」的 DDL/工具类语句才跳过。
        return head in _PG_PREPARABLE_HEADS or head not in _PG_STATEMENT_HEADS

    def check_statement(self, conn, stmt: str) -> SyntaxCheck:  # noqa: ANN001
        """PG 的 PREPARE 不接受参数化的语句文本，只能内嵌；这条 SQL 本就是调用方要执行的，
        不构成注入面放大。整个复核不提交，PREPARE 随事务回滚。"""
        from ..errors import classify_db_error, sanitize_db_message

        try:
            conn.execute(text(f"PREPARE {_SYNTAX_CHECK_STMT_NAME} AS {stmt}"))
        except Exception as e:  # noqa: BLE001
            if classify_db_error(e) == "sql_syntax_error":
                msg = sanitize_db_message(str(e))
                # PG 把出错行原样回显（`LINE 1: PREPARE dbm_syntax_chk AS SELCT …`），
                # 这个 PREPARE 包装是我们加的、用户没写过，回显里要抹掉免得误导。
                msg = msg.replace(f"PREPARE {_SYNTAX_CHECK_STMT_NAME} AS ", "")
                return SyntaxCheck(supported=True, ok=False, error=msg)
            # 权限不足 / 表不存在 / 其它——语法本身 DB 是认的，不该按语法错拒
            return SyntaxCheck(supported=True, ok=True)
        finally:
            try:
                conn.execute(text(f"DEALLOCATE PREPARE {_SYNTAX_CHECK_STMT_NAME}"))
            except Exception:  # noqa: BLE001
                pass  # PREPARE 本身失败时没东西可释放，忽略
        return SyntaxCheck(supported=True, ok=True)
