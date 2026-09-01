"""DB 错误翻译层：分类正确 + 敏感信息不外发。

这层是「错误控制不外扩到 agent」的关键：驱动异常里带着 DSN（含密码）、绑定参数、
SQLAlchemy 背景链接，直接冒泡给 agent 既泄密又没法行动。
"""

from dbmcp.errors import (
    DbErrorInfo,
    classify_db_error,
    sanitize_db_message,
    translate_db_error,
)


def _exc(msg: str) -> Exception:
    return RuntimeError(msg)


class TestSanitize:
    def test_strips_sql_and_parameters_echo(self):
        raw = ('(pymysql.err.ProgrammingError) (1064, "bad syntax")\n'
               '[SQL: INSERT INTO users (pw) VALUES (%(pw)s)]\n'
               '[parameters: {\'pw\': \'hunter2\'}]')
        out = sanitize_db_message(raw)
        assert "hunter2" not in out
        assert "[SQL:" not in out and "[parameters:" not in out
        assert "bad syntax" in out

    def test_strips_sqlalchemy_background_link(self):
        raw = 'boom (Background on this error at: https://sqlalche.me/e/20/f405)'
        assert "sqlalche.me" not in sanitize_db_message(raw)

    def test_redacts_dsn_credentials(self):
        raw = "Can't connect to mysql+pymysql://root:sUp3rS3cret@10.0.0.9:3306/app"
        out = sanitize_db_message(raw)
        assert "sUp3rS3cret" not in out
        assert "***@10.0.0.9" in out

    def test_redacts_credentials_in_redis_url(self):
        out = sanitize_db_message("redis://admin:p%40ss@cache:6379/0 refused")
        assert "p%40ss" not in out
        assert "***@cache:6379" in out

    def test_collapses_multiline_and_caps_length(self):
        out = sanitize_db_message("line1\n   line2\n\nline3")
        assert "\n" not in out and "line1 line2 line3" == out
        assert len(sanitize_db_message("x" * 5000)) <= 601

    def test_unwraps_code_tuple(self):
        assert sanitize_db_message('(1146, "Table \'a.b\' doesn\'t exist")').startswith("1146: ")


class TestClassify:
    def test_mysql_syntax_error_by_code(self):
        assert classify_db_error(_exc('(pymysql.err.ProgrammingError) (1064, "you have an '
                                      'error in your SQL syntax")')) == "sql_syntax_error"

    def test_postgres_syntax_error_by_message(self):
        assert classify_db_error(_exc('syntax error at or near "selct"')) == "sql_syntax_error"

    def test_sqlite_syntax_error_by_message(self):
        assert classify_db_error(_exc('near "selct": syntax error')) == "sql_syntax_error"

    def test_table_not_found(self):
        assert classify_db_error(_exc('(1146, "Table \'a.b\' doesn\'t exist")')) == "table_not_found"
        assert classify_db_error(_exc('relation "orders" does not exist')) == "table_not_found"
        assert classify_db_error(_exc("no such table: orders")) == "table_not_found"

    def test_column_not_found(self):
        assert classify_db_error(_exc('(1054, "Unknown column \'x\' in field list")')) \
            == "column_not_found"

    def test_permission_denied(self):
        assert classify_db_error(
            _exc('(1142, "DELETE command denied to user \'agent_read\'")')) == "permission_denied"

    def test_readonly_violation(self):
        assert classify_db_error(_exc('(1792, "Cannot execute statement in a READ ONLY '
                                      'transaction")')) == "readonly_violation"
        assert classify_db_error(
            _exc("Code: 164. DB::Exception: Cannot execute query in readonly mode")) \
            == "readonly_violation"

    def test_query_timeout(self):
        assert classify_db_error(_exc('(3024, "Query execution was interrupted, maximum '
                                      'statement execution time exceeded")')) == "query_timeout"
        assert classify_db_error(
            _exc("canceling statement due to statement timeout")) == "query_timeout"

    def test_deadlock_and_lock_timeout(self):
        assert classify_db_error(_exc('(1213, "Deadlock found")')) == "deadlock"
        assert classify_db_error(_exc('(1205, "Lock wait timeout exceeded")')) == "lock_timeout"

    def test_duplicate_key(self):
        assert classify_db_error(_exc('(1062, "Duplicate entry \'1\' for key")')) == "duplicate_key"

    def test_unknown_falls_back_to_db_error(self):
        assert classify_db_error(_exc("something nobody predicted")) == "db_error"

    def test_reads_code_from_chained_cause(self):
        try:
            try:
                raise ValueError('(1064, "syntax")')
            except ValueError as inner:
                raise RuntimeError("wrapper") from inner
        except RuntimeError as e:
            assert classify_db_error(e) == "sql_syntax_error"


class TestTranslate:
    def test_returns_kind_message_hint(self):
        info = translate_db_error(_exc('(1146, "Table \'a.b\' doesn\'t exist")'))
        assert isinstance(info, DbErrorInfo)
        assert info.kind == "table_not_found"
        assert info.hint  # 每一类都要给出「下一步怎么办」
        assert info.as_text().startswith("[table_not_found] ")

    def test_unknown_error_still_sanitized_not_swallowed(self):
        info = translate_db_error(_exc("weird failure at mysql://u:pw123@h/db"))
        assert info.kind == "db_error"
        assert "pw123" not in info.as_text()
        assert "weird failure" in info.message

    def test_empty_message_falls_back_to_type_name(self):
        assert translate_db_error(RuntimeError("")).message == "RuntimeError"


class _PgExc(Exception):
    """模拟 psycopg 异常：类名不以 Error 结尾 + 带 .sqlstate（真机形态）。"""

    def __init__(self, msg: str, sqlstate: str = "") -> None:
        super().__init__(msg)
        self.sqlstate = sqlstate


class TestPostgres:
    """PG 专项回归。真机（PostgreSQL 17 + psycopg3）实测得到的错误形态。

    两个根因：① psycopg3 的异常类名多数不以 Error 结尾（UndefinedTable /
    InsufficientPrivilege / AdminShutdown），旧的驱动前缀正则匹配不到，驱动类名会
    原样外发；② PG 的「列不存在」与「表不存在」消息都含 "does not exist"，
    只按消息片段判会把列错误误判成表错误——必须用 SQLSTATE。
    """

    def test_strips_dotted_driver_class_without_error_suffix(self):
        raw = "(psycopg.errors.InsufficientPrivilege) permission denied for table crawl_job"
        assert sanitize_db_message(raw) == "permission denied for table crawl_job"

    def test_still_strips_classic_error_suffix_prefix(self):
        assert sanitize_db_message("(OperationalError) boom") == "boom"

    def test_does_not_strip_leading_parenthesised_sql(self):
        # 真的以括号开头的内容不能被当成驱动前缀剥掉
        assert sanitize_db_message("(SELECT 1) is not valid here").startswith("(SELECT 1)")

    def test_drops_caret_position_line(self):
        raw = 'syntax error at or near "SELCT"\nLINE 1: SELCT * FROM t1\n        ^'
        out = sanitize_db_message(raw)
        assert "^" not in out
        assert out == 'syntax error at or near "SELCT" LINE 1: SELCT * FROM t1'

    def test_column_not_found_by_sqlstate_not_table_not_found(self):
        exc = _PgExc('column "nope" does not exist', "42703")
        assert classify_db_error(exc) == "column_not_found"

    def test_table_not_found_by_sqlstate(self):
        assert classify_db_error(_PgExc('relation "nope" does not exist', "42P01")) == "table_not_found"

    def test_permission_denied_by_sqlstate(self):
        exc = _PgExc("permission denied for table crawl_job", "42501")
        assert classify_db_error(exc) == "permission_denied"

    def test_readonly_violation_by_sqlstate(self):
        exc = _PgExc("cannot execute UPDATE in a read-only transaction", "25006")
        assert classify_db_error(exc) == "readonly_violation"

    def test_statement_timeout_and_user_cancel_share_sqlstate(self):
        # 57014 有意不入 SQLSTATE 表：超时与人工取消同码，靠文案区分
        assert classify_db_error(
            _PgExc("canceling statement due to statement timeout", "57014")) == "query_timeout"
        assert classify_db_error(
            _PgExc("canceling statement due to user request", "57014")) == "query_canceled"

    def test_sqlstate_beats_message_fragment(self):
        # 消息里同时含 "does not exist"（表规则）但 SQLSTATE 说是列——以 SQLSTATE 为准
        exc = _PgExc('column "x" does not exist', "42703")
        assert translate_db_error(exc).kind == "column_not_found"

    def test_translated_message_has_no_driver_internals(self):
        exc = _PgExc("(psycopg.errors.UndefinedTable) relation \"nope\" does not exist\n"
                     "LINE 1: SELECT * FROM nope\n                      ^\n"
                     "[SQL: SELECT * FROM nope]", "42P01")
        info = translate_db_error(exc)
        assert "psycopg" not in info.message
        assert "[SQL:" not in info.message and "^" not in info.message
