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
            # 集合运算顶层是 SetOperation（非 Select），但只读——不应被误判为写（回归）
            ("SELECT 1 UNION SELECT 2", "mysql"),
            ("SELECT a FROM t UNION ALL SELECT b FROM u ORDER BY 1", "mysql"),
            ("SELECT 1 INTERSECT SELECT 2", "postgres"),
            ("SELECT 1 EXCEPT SELECT 2", "postgres"),
        ],
    )
    def test_allowed(self, sql, engine):
        verdict = classify(sql, engine)
        assert verdict.readonly, f"{sql!r} 应放行，实际拒绝: {verdict.reason}"


class TestSetOperationGuards:
    """UNION/INTERSECT/EXCEPT 放行，但分支里藏写操作/危险函数/加锁仍须判写。"""

    def test_union_of_selects_readonly(self):
        assert classify("SELECT 1 UNION ALL SELECT 2", "mysql").readonly

    def test_union_with_unsafe_function_rejected(self):
        assert not classify("SELECT SLEEP(1) UNION SELECT 2", "mysql").readonly

    def test_union_with_cte_write_rejected(self):
        sql = "WITH x AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM x UNION SELECT 1"
        assert not classify(sql, "postgres").readonly

    def test_union_with_for_update_rejected(self):
        assert not classify("SELECT 1 UNION SELECT id FROM t FOR UPDATE", "mysql").readonly


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

    def test_multi_statement_classified_as_batch_write(self):
        # 多语句放行进审批流：非只读、标为 MultiStatement、表名跨语句聚合
        v = classify(
            "ALTER TABLE cfg ADD COLUMN c INT; UPDATE cfg SET c=1 WHERE k='a'", "mysql"
        )
        assert v.readonly is False
        assert v.statement_kind == "MultiStatement"
        assert "cfg" in v.tables
        assert "多语句" in v.reason

    def test_unsafe_functions_not_readonly(self):
        """H2：有副作用/危险函数不能按只读放行（否则只读账号即可 DoS/读写文件）。"""
        for sql, eng in [
            ("SELECT SLEEP(10)", "mysql"),
            ("SELECT BENCHMARK(1000000, MD5(1))", "mysql"),
            ("SELECT LOAD_FILE('/etc/passwd')", "mysql"),
            ("SELECT pg_sleep(10)", "postgres"),
            ("SELECT pg_read_file('/etc/passwd')", "postgres"),
            ("SELECT lo_export(1, '/tmp/x')", "postgres"),
        ]:
            v = classify(sql, eng)
            assert v.readonly is False, sql
            assert "危险函数" in v.reason, sql

    def test_explain_write_not_readonly(self):
        """H4：EXPLAIN 内层写语句/危险函数一律不放行（PG EXPLAIN ANALYZE 会真执行内层）。"""
        assert classify("EXPLAIN ANALYZE DELETE FROM t", "postgres").readonly is False
        assert classify("EXPLAIN ANALYZE DELETE FROM t", "mysql").readonly is False
        assert classify("EXPLAIN ANALYZE SELECT pg_read_file('/x')", "postgres").readonly is False
        # 正常 EXPLAIN 只读语句仍放行
        assert classify("EXPLAIN SELECT 1", "postgres").readonly is True

    def test_parse_error_marked(self):
        """语法错误须标记 statement_kind=ParseError（供后台区分'语法错'与'真写操作'）。"""
        v = classify("SELECT 'unterminated", "mysql")
        assert v.readonly is False
        assert v.statement_kind == "ParseError"

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

    def test_tokenize_error_rejected_not_raised(self):
        # 引号不闭合走 sqlglot 分词阶段（TokenizeError，非 ParseError）：
        # 必须默认拒绝而非抛异常冒泡（否则 agent/后台会 500）
        verdict = classify("update t set x='unterminated where id=1", "mysql")
        assert not verdict.readonly
        assert "解析失败" in verdict.reason

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
