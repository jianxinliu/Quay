"""agent 侧 SQL 语法预检：sqlglot 初筛 + 目标 DB「只解析不执行」复核。

核心不变量：
1. 真语法错 → 精确报错，且 execute **不生成审批单**（不浪费人工审批）；
2. sqlglot 认不出但 DB 认的方言写法（如 MySQL 无括号 DROP PARTITION）→ 不被误拒，
   仍走原有「默认拒绝 → 审批流」兜底；
3. 复核过程绝不执行语句。
"""

import sqlite3

import pytest
from sqlalchemy import create_engine

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.engines import (
    SyntaxCheck,
    _syntax_check_supported,
    dry_run_syntax_check,
    first_sql_keyword,
)
from dbmcp.service import CallerInfo, DbmService, QueryRejected, SqlSyntaxError

CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-syntax")


@pytest.fixture
def sa_engine(tmp_path):
    db = tmp_path / "chk.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE t (a INTEGER, b TEXT);"
                       "INSERT INTO t VALUES (1, 'x');")
    conn.commit()
    conn.close()
    e = create_engine(f"sqlite:///{db}")
    yield e
    e.dispose()


class TestFirstKeyword:
    def test_plain(self):
        assert first_sql_keyword("  SELECT 1") == "select"

    def test_skips_line_comment(self):
        assert first_sql_keyword("-- 取数\nSELECT 1") == "select"

    def test_skips_block_comment(self):
        assert first_sql_keyword("/* c */ UPDATE t SET a=1") == "update"

    def test_empty(self):
        assert first_sql_keyword("   ") == ""


class TestDryRunSyntaxCheck:
    def test_bad_syntax_reported(self, sa_engine):
        res = dry_run_syntax_check(sa_engine, "selct 1", "sqlite")
        assert res.supported and not res.ok
        assert "syntax error" in res.error.lower()

    def test_good_sql_passes(self, sa_engine):
        assert dry_run_syntax_check(sa_engine, "SELECT a FROM t", "sqlite") == \
            SyntaxCheck(supported=True, ok=True)

    def test_missing_table_is_not_a_syntax_error(self, sa_engine):
        """表不存在不该被当成语法错——那是执行期的真实原因，交给正常路径去报。"""
        res = dry_run_syntax_check(sa_engine, "SELECT * FROM nope", "sqlite")
        assert res.supported and res.ok

    def test_multi_statement_reports_offending_index(self, sa_engine):
        res = dry_run_syntax_check(sa_engine, "SELECT 1; selct 2", "sqlite")
        assert not res.ok and res.stmt_index == 2

    def test_does_not_execute_the_statement(self, sa_engine):
        """复核 DELETE 不能真的删数据。"""
        res = dry_run_syntax_check(sa_engine, "DELETE FROM t", "sqlite")
        assert res.supported and res.ok
        with sa_engine.connect() as c:
            assert c.execute(__import__("sqlalchemy").text("SELECT count(*) FROM t")).scalar() == 1

    def test_unsupported_engine_reports_unsupported(self, sa_engine):
        res = dry_run_syntax_check(sa_engine, "SELECT 1", "mongodb")
        assert res.supported is False and res.ok is True

    def test_prepare_unsupported_statement_is_not_a_syntax_error(self):
        """MySQL 对 PREPARE 不支持的合法语句报 1295（不是 1064）——必须放行，否则误拒。

        真机验证过的场景：`HANDLER orders OPEN` 是合法 MySQL、sqlglot 解析不了、
        PREPARE 协议也不支持。只认「语法错」这一类才判死，是零假阳性的关键。
        """
        from dbmcp.engines import _check_one_statement

        class _Conn:
            def execute(self, *_a, **_kw):
                raise RuntimeError(
                    "(1295, 'This command is not supported in the prepared statement protocol yet')")

        res = _check_one_statement(_Conn(), "HANDLER orders OPEN", "mysql")
        assert res.supported is True and res.ok is True

    def test_postgres_ddl_is_not_checked(self, sa_engine):
        """PG 的 PREPARE 不接受 DDL（会报语法错），这类组合必须标为「无法复核」。"""
        res = dry_run_syntax_check(sa_engine, "ALTER TABLE t DROP PARTITION p1, p2", "postgres")
        assert res.supported is False


@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
                       "INSERT INTO users (name) VALUES ('alice');")
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
            "writer": {"user": "x", "password": "plain://unused"},
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"),
                     ApprovalStore(tmp_path / "audit.sqlite3"))
    svc.data_dir = str(tmp_path / "data")
    yield svc
    svc.close()


class TestQueryPrecheck:
    def test_syntax_error_gets_precise_message(self, service):
        with pytest.raises(SqlSyntaxError) as ei:
            service.query("demo", "main", "selct 1", CALLER)
        msg = str(ei.value)
        assert "[sql_syntax_error]" in msg
        assert "语法" in msg
        # 不再是误导性的「仅允许只读语句」
        assert "仅允许只读语句" not in msg

    def test_unparseable_but_db_accepts_still_rejected_honestly(self, service, monkeypatch):
        """DB 认这条语法但解析器不认：判定不了只读性，仍按默认拒绝红线拒——但说实话。"""
        monkeypatch.setattr("dbmcp.engines.dry_run_syntax_check",
                            lambda *_a, **_kw: SyntaxCheck(supported=True, ok=True))
        with pytest.raises(QueryRejected) as ei:
            service.query("demo", "main", "selct 1", CALLER)
        assert not isinstance(ei.value, SqlSyntaxError)
        assert "无法判定它是否只读" in str(ei.value)

    def test_syntax_error_is_audited_as_rejected(self, service):
        with pytest.raises(SqlSyntaxError):
            service.query("demo", "main", "selct 1", CALLER)
        rows = service.store.recent(limit=5)
        assert any(r["status"] == "rejected" and "语法错误" in (r["detail"] or "") for r in rows)


class TestExecutePrecheck:
    def test_syntax_error_does_not_create_approval(self, service):
        with pytest.raises(SqlSyntaxError):
            service.execute("demo", "main", "UPDTE users SET name='x'", CALLER)
        assert service.approvals.list_by_status() == []

    def test_dialect_gap_still_falls_back_to_approval(self, service, monkeypatch):
        """sqlglot 认不出、DB 说语法没问题 → 保留原有审批兜底，不误拒。

        对应真实场景：MySQL 合法的 `ALTER TABLE t DROP PARTITION p1, p2`（无括号）。
        """
        monkeypatch.setattr("dbmcp.engines.dry_run_syntax_check",
                            lambda *_a, **_kw: SyntaxCheck(supported=True, ok=True))
        out = service.execute("demo", "main", "ALTER TABLE users DROP PARTITION p1, p2", CALLER)
        assert out["status"] == "approval_required"
        assert out["change_id"]

    def test_unsupported_check_falls_back_to_approval(self, service, monkeypatch):
        monkeypatch.setattr("dbmcp.engines.dry_run_syntax_check",
                            lambda *_a, **_kw: SyntaxCheck(supported=False, ok=True))
        out = service.execute("demo", "main", "selct nonsense", CALLER)
        assert out["status"] == "approval_required"

    def test_valid_write_unaffected(self, service):
        """能被解析的正常写操作完全不走预检，行为不变。"""
        out = service.execute("demo", "main", "UPDATE users SET name='bob' WHERE id=1", CALLER)
        assert out["status"] == "approval_required"


class TestPostgresSupportGate:
    """PG 的预检门禁：DML 与「打错的首关键字」都要送 DB 复核，只跳过 PREPARE 不支持的合法语句。

    修复前门禁是「首词 ∈ 可 PREPARE 白名单」，于是**最常见的错误——首关键字打错
    （SELCT / UPDTE）——恰好永远落在白名单外被整条跳过**，PG 上预检形同虚设
    （真机 `SELCT * FROM crawl_job` 实测 supported=False）。现在的规则是：
    首词不是任何合法 PG 语句关键字 → 不存在以它开头的合法语句 → 交给 PREPARE 报真实语法错
    （仍由 DB 判死，不自己下结论）。
    """

    def test_dml_heads_supported(self):
        for sql in ("SELECT 1", "INSERT INTO t VALUES (1)", "UPDATE t SET a=1",
                    "DELETE FROM t", "WITH x AS (SELECT 1) SELECT * FROM x",
                    "VALUES (1)", "TABLE t"):
            assert _syntax_check_supported("postgres", sql), sql

    def test_typo_head_is_checked_not_skipped(self):
        for sql in ("SELCT * FROM t", "UPDTE t SET a=1", "FROM t SELECT 1", "xyzzy 1"):
            assert _syntax_check_supported("postgres", sql), sql

    def test_valid_but_unpreparable_statements_are_skipped(self):
        # PG 的 PREPARE 只吃 DML，这些合法语句喂进去会被误报语法错 → 必须跳过
        for sql in ("CREATE TABLE t(id int)", "ALTER TABLE t ADD c int", "DROP TABLE t",
                    "VACUUM t", "GRANT SELECT ON t TO ro", "REVOKE SELECT ON t FROM ro",
                    "CREATE ROLE ro LOGIN", "REINDEX TABLE t", "SET search_path=public",
                    "EXPLAIN SELECT 1", "TRUNCATE t", "COPY t FROM STDIN", "ANALYZE t"):
            assert not _syntax_check_supported("postgres", sql), sql

    def test_leading_comment_does_not_break_gate(self):
        assert _syntax_check_supported("postgres", "-- c\nSELECT 1")
        assert not _syntax_check_supported("postgres", "/* c */ CREATE TABLE t(id int)")
