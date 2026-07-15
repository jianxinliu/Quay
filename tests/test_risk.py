"""风险评估引擎测试。用假的 meta_provider，不触库。"""

from dataclasses import dataclass

from dbmcp.audit.risk import assess


@dataclass
class FakeMeta:
    row_estimate: int | None
    indexed_columns: set[str]


def provider(mapping):
    return lambda table: mapping.get(table)


class TestCritical:
    def test_drop(self):
        r = assess("DROP TABLE users", "mysql", provider({}))
        assert r.level == "CRITICAL"
        assert r.statement_kind == "Drop"

    def test_truncate(self):
        r = assess("TRUNCATE TABLE users", "mysql", provider({}))
        assert r.level == "CRITICAL"

    def test_delete_without_where(self):
        r = assess("DELETE FROM users", "mysql", provider({"users": FakeMeta(5000, {"id"})}))
        assert r.level == "CRITICAL"
        assert r.has_where is False
        assert r.affected_estimate == 5000

    def test_update_without_where(self):
        r = assess("UPDATE users SET active = 0", "postgres", provider({}))
        assert r.level == "CRITICAL"
        assert r.has_where is False

    def test_unparseable(self):
        r = assess("garbage ??? sql", "mysql", provider({}))
        assert r.level == "CRITICAL"

    def test_tokenize_error_is_critical_not_raised(self):
        # 引号不闭合 → TokenizeError（非 ParseError）：应降级为 CRITICAL 而非抛异常
        r = assess("UPDATE t SET x='unterminated WHERE id=1", "mysql", provider({}))
        assert r.level == "CRITICAL" and r.statement_kind == "Unparseable"


class TestMultiStatement:
    """多语句批量：逐条评估、等级取最高、逐条列出理由。"""

    def test_batch_takes_max_level(self):
        # INSERT(MEDIUM) + DROP(CRITICAL) → 整批 CRITICAL
        r = assess("INSERT INTO t (a) VALUES (1); DROP TABLE u", "mysql", provider({}))
        assert r.level == "CRITICAL"
        assert r.statement_kind == "MultiStatement"
        assert set(r.tables) == {"t", "u"}
        # 概要 + 每条一行
        assert any("多语句批量" in x for x in r.reasons)
        assert sum(1 for x in r.reasons if x.startswith("[")) == 2

    def test_batch_all_medium(self):
        r = assess("INSERT INTO t (a) VALUES (1); INSERT INTO t (a) VALUES (2)",
                   "mysql", provider({}))
        assert r.level == "MEDIUM"
        assert r.statement_kind == "MultiStatement"


class TestHigh:
    def test_delete_where_unindexed_column(self):
        # WHERE 用了未索引的列 → 可能全表扫描 → HIGH
        meta = {"users": FakeMeta(100_000, {"id", "email"})}
        r = assess("DELETE FROM users WHERE nickname = 'x'", "mysql", provider(meta))
        assert r.level == "HIGH"
        assert r.uses_index is False

    def test_alter_large_table(self):
        meta = {"orders": FakeMeta(5_000_000, {"id"})}
        r = assess("ALTER TABLE orders ADD COLUMN note text", "postgres", provider(meta))
        assert r.level == "HIGH"
        assert any("锁表" in w for w in r.warnings)


class TestMedium:
    def test_update_where_indexed(self):
        meta = {"users": FakeMeta(100_000, {"id", "email"})}
        r = assess("UPDATE users SET active = 0 WHERE id = 42", "mysql", provider(meta))
        assert r.level == "MEDIUM"
        assert r.uses_index is True
        assert r.has_where is True

    def test_insert(self):
        r = assess("INSERT INTO users (name) VALUES ('x')", "mysql", provider({}))
        assert r.level == "MEDIUM"
        assert r.statement_kind == "Insert"

    def test_alter_small_table(self):
        meta = {"cfg": FakeMeta(10, {"id"})}
        r = assess("ALTER TABLE cfg ADD COLUMN note text", "postgres", provider(meta))
        assert r.level == "MEDIUM"


class TestReport:
    def test_to_dict_and_requires_approval(self):
        r = assess("DELETE FROM users", "mysql", provider({}))
        d = r.to_dict()
        assert d["level"] == "CRITICAL"
        assert d["tables"] == ["users"]
        assert r.requires_approval is True

    def test_row_estimate_takes_max_across_tables(self):
        meta = {"a": FakeMeta(10, set()), "b": FakeMeta(999, set())}
        r = assess("UPDATE a SET x = (SELECT max(y) FROM b) WHERE a.id = 1", "mysql", provider(meta))
        assert r.row_estimate == 999
