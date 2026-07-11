"""分析工作台测试：DuckDB 工作区 CRUD、快照导入、跨源 JOIN、审计留痕。"""

import sqlite3

import pytest

pytest.importorskip("duckdb")

from dbmcp.analysis import AnalysisError, AnalysisStore  # noqa: E402
from dbmcp.audit.log import AuditStore  # noqa: E402
from dbmcp.config import AppConfig  # noqa: E402
from dbmcp.service import CallerInfo, DbmService, QueryRejected  # noqa: E402

CALLER = CallerInfo(agent="pytest/1.0", session_id="s1")


@pytest.fixture
def store(tmp_path):
    return AnalysisStore(tmp_path / "analysis")


class TestStore:
    def test_import_rows_and_query(self, store):
        n = store.import_rows("ws1", "users",
                              ["id", "name", "score"],
                              [[1, "alice", 9.5], [2, "bob", None], [3, "carol", 7.0]])
        assert n == 3
        out = store.run_sql("ws1", "SELECT name FROM users WHERE score > 8")
        assert out["rows"] == [["alice"]]
        # 类型推断：id BIGINT、score DOUBLE
        info = store.describe_dataset("ws1", "users")
        types = {c["name"]: c["type"] for c in info["columns"]}
        assert types["id"] == "BIGINT" and types["score"] == "DOUBLE"

    def test_replace_dataset(self, store):
        store.import_rows("ws1", "t", ["a"], [[1]])
        store.import_rows("ws1", "t", ["b"], [[2], [3]])  # 同名替换
        out = store.run_sql("ws1", "SELECT * FROM t")
        assert out["columns"] == ["b"] and len(out["rows"]) == 2

    def test_cross_source_join_and_view(self, store):
        store.import_rows("ws1", "orders", ["uid", "amt"], [[1, 10], [1, 20], [2, 5]])
        store.import_rows("ws1", "users", ["id", "city"], [[1, "SH"], [2, "BJ"]])
        # 沙箱内自由建 VIEW（虚拟表）
        store.run_sql("ws1", "CREATE VIEW city_amt AS "
                             "SELECT u.city, sum(o.amt) AS total FROM orders o "
                             "JOIN users u ON u.id = o.uid GROUP BY u.city")
        out = store.run_sql("ws1", "SELECT * FROM city_amt ORDER BY total DESC")
        assert out["rows"] == [["SH", 30], ["BJ", 5]]
        ds = {d["name"]: d for d in store.list_datasets("ws1")}
        assert ds["city_amt"]["type"] == "view" and ds["orders"]["rows"] == 3

    def test_import_csv_file(self, store, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("id,name\n1,foo\n2,bar\n")
        n = store.import_file("ws1", "csvdata", str(f))
        assert n == 2
        out = store.run_sql("ws1", "SELECT name FROM csvdata WHERE id = 2")
        assert out["rows"] == [["bar"]]

    def test_bad_names_rejected(self, store):
        with pytest.raises(AnalysisError, match="工作区名"):
            store.create_workspace("../evil")
        with pytest.raises(AnalysisError, match="数据集名"):
            store.import_rows("ws1", "a;drop", ["x"], [[1]])

    def test_missing_workspace(self, store):
        with pytest.raises(AnalysisError, match="不存在"):
            store.run_sql("nope", "SELECT 1")

    def test_workspace_lifecycle(self, store):
        store.create_workspace("tmp")
        assert any(w["workspace"] == "tmp" for w in store.list_workspaces())
        store.drop_workspace("tmp")
        assert not any(w["workspace"] == "tmp" for w in store.list_workspaces())

    def test_get_ddl(self, store):
        store.import_rows("ws1", "t", ["a"], [[1]])
        assert "CREATE TABLE" in store.get_ddl("ws1", "t")


@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        "INSERT INTO users (name, age) VALUES ('alice', 30), ('bob', 25), ('carol', 41);"
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(db_file), "environment": "local",
        "policy": {"max_rows": 100},
    }}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"))
    svc.analysis = AnalysisStore(tmp_path / "analysis")
    yield svc
    svc.close()


class TestService:
    def test_import_from_connection_audited(self, service):
        out = service.analysis_import("ws1", "u", "demo", "main",
                                      "SELECT * FROM users", CALLER, limit=10)
        assert out["rows"] == 3
        # 沙箱内分析
        res = service.analysis_sql("ws1", "SELECT count(*) FROM u WHERE age > 28", CALLER)
        assert res["rows"][0][0] == 2
        # 审计：取数(query) + 导入(analysis_import) + 分析(analysis_sql)
        tools = [r["tool"] for r in service.store.recent()]
        assert "analysis_import" in tools and "analysis_sql" in tools and "query" in tools
        rec = [r for r in service.store.recent() if r["tool"] == "analysis_sql"][0]
        assert rec["project"] == "analysis" and rec["connection"] == "ws1"

    def test_import_rejects_write_sql(self, service):
        with pytest.raises(QueryRejected, match="只读"):
            service.analysis_import("ws1", "x", "demo", "main",
                                    "DELETE FROM users", CALLER)

    def test_import_respects_limit(self, service):
        out = service.analysis_import("ws1", "u2", "demo", "main",
                                      "SELECT * FROM users", CALLER, limit=2)
        assert out["rows"] == 2 and out["truncated_to_limit"] is True

    def test_overview(self, service):
        service.analysis_import("ws1", "u", "demo", "main", "SELECT * FROM users", CALLER)
        ov = service.analysis_overview()
        ws = [w for w in ov if w["workspace"] == "ws1"][0]
        assert any(d["name"] == "u" for d in ws["datasets"])

    def test_sandbox_write_allowed_no_approval(self, service):
        """沙箱边界：工作区内 DDL/DML 自由（不需要审批），这是设计要求。"""
        service.analysis_import("ws1", "u", "demo", "main", "SELECT * FROM users", CALLER)
        service.analysis_sql("ws1", "CREATE VIEW adults AS SELECT * FROM u WHERE age >= 30", CALLER)
        service.analysis_sql("ws1", "DELETE FROM u WHERE age < 28", CALLER)  # 沙箱内 DELETE 直接执行
        assert service.analysis_sql("ws1", "SELECT count(*) FROM u", CALLER)["rows"][0][0] == 2
