"""SQL 分类器测试：只读放行 + 默认拒绝的各种绕过路径。"""

import pytest

from dbmcp.audit.classify import classify, fingerprint


class TestReadonlyAllowed:
    @pytest.mark.parametrize(
        ("sql", "engine"),
        [
            ("SELECT * FROM users WHERE id = 1", "mysql"),
            ("SELECT * FROM users", "postgres"),
            ("SELECT 1", "sqlite"),
            ("SHOW TABLES", "mysql"),
            ("SHOW server_version", "postgres"),
            ("DESCRIBE users", "mysql"),
            ("EXPLAIN SELECT * FROM users", "mysql"),
            ("EXPLAIN SELECT * FROM users", "postgres"),
            ("EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM users", "postgres"),
            ("EXPLAIN ANALYZE SELECT * FROM users", "postgres"),
            ("WITH t AS (SELECT 1 AS a) SELECT * FROM t", "postgres"),
            ("select id, name from users order by id limit 10", "mysql"),
        ],
    )
    def test_allowed(self, sql, engine):
        verdict = classify(sql, engine)
        assert verdict.readonly, f"{sql!r} 应放行，实际拒绝: {verdict.reason}"


class TestWritesRejected:
    @pytest.mark.parametrize(
        ("sql", "engine"),
        [
            ("INSERT INTO t VALUES (1)", "mysql"),
            ("UPDATE t SET a = 1", "postgres"),
            ("DELETE FROM t", "mysql"),
            ("DROP TABLE t", "postgres"),
            ("TRUNCATE TABLE t", "mysql"),
            ("CREATE TABLE t (a int)", "postgres"),
            ("ALTER TABLE t ADD COLUMN b int", "mysql"),
            ("GRANT ALL ON t TO someone", "postgres"),
        ],
    )
    def test_plain_writes(self, sql, engine):
        assert not classify(sql, engine).readonly


class TestBypassAttemptsRejected:
    """默认拒绝原则下必须挡住的绕过路径。"""

    def test_multi_statement(self):
        assert not classify("SELECT 1; DROP TABLE t", "mysql").readonly

    def test_cte_hiding_dml(self):
        # 顶层是 Select，但 CTE 里藏了 INSERT（PG 合法语法）
        sql = "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x"
        verdict = classify(sql, "postgres")
        assert not verdict.readonly
        assert "写操作" in verdict.reason

    def test_explain_wrapping_dml(self):
        # PG 的 EXPLAIN ANALYZE 会真实执行内层语句
        assert not classify("EXPLAIN ANALYZE DELETE FROM t", "postgres").readonly
        assert not classify("EXPLAIN UPDATE t SET a = 1", "postgres").readonly
        assert not classify("EXPLAIN DELETE FROM t", "mysql").readonly

    def test_select_for_update(self):
        assert not classify("SELECT * FROM t FOR UPDATE", "mysql").readonly

    def test_parse_error_rejected(self):
        verdict = classify("not valid sql at all ???", "mysql")
        assert not verdict.readonly
        assert "解析失败" in verdict.reason

    def test_empty_rejected(self):
        assert not classify("", "mysql").readonly
        assert not classify("   ", "postgres").readonly

    def test_unknown_engine_rejected(self):
        assert not classify("SELECT 1", "redis").readonly


class TestFingerprint:
    def test_whitespace_and_case_insensitive(self):
        a = fingerprint("SELECT  *  FROM users WHERE id=1", "mysql")
        b = fingerprint("select * from users where id = 1", "mysql")
        assert a == b

    def test_different_sql_different_fingerprint(self):
        a = fingerprint("SELECT * FROM users", "mysql")
        b = fingerprint("SELECT * FROM orders", "mysql")
        assert a != b

    def test_unparseable_still_fingerprints(self):
        assert fingerprint("garbage ??? sql", "mysql")
