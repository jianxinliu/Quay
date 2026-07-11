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


class TestWorkflow:
    @pytest.fixture
    def svc_wf(self, service, tmp_path):
        from dbmcp.workflows import WorkflowStore
        service.workflows = WorkflowStore(tmp_path / "wf.sqlite3")
        return service

    def test_split_statements(self):
        from dbmcp.workflows import split_statements
        out = split_statements("SELECT 1; -- c;\nSELECT 'a;b'; CREATE VIEW v AS SELECT 2")
        assert len(out) == 3 and out[1].endswith("SELECT 'a;b'")  # 语句携带前导注释

    def test_save_run_rerun(self, svc_wf):
        svc = svc_wf
        # 导入(自动记 provenance)→ 保存 workflow(脚本两步)
        svc.analysis_import("ws1", "u", "demo", "main", "SELECT * FROM users", CALLER, limit=10)
        wf = svc.workflow_save("adults", "ws1",
                               "CREATE OR REPLACE VIEW grown AS SELECT * FROM u WHERE age >= 30;"
                               "SELECT count(*) AS n FROM grown", CALLER)
        assert wf["sources"][0]["dataset"] == "u" and wf["sources"][0]["kind"] == "connection"
        # 重跑:重拉 + 逐步执行,输出为最后的 SELECT
        out = svc.workflow_run("adults", CALLER)
        assert out["ok"] is True
        assert out["steps"][0]["step"].startswith("导入 u") and out["steps"][0]["rows"] == 3
        assert out["output"]["rows"][0][0] == 2
        # 源数据变化后重跑结果随之更新(模拟:直接改工作区数据不行——改源库)
        import sqlite3
        db = svc.config.get_connection("demo", "main").database
        c = sqlite3.connect(db); c.execute("INSERT INTO users (name, age) VALUES ('dave', 50)"); c.commit(); c.close()
        out2 = svc.workflow_run("adults", CALLER)
        assert out2["output"]["rows"][0][0] == 3  # 重拉后 3 个成年人

    def test_run_stops_on_failed_step(self, svc_wf):
        svc = svc_wf
        svc.analysis_import("ws1", "u", "demo", "main", "SELECT * FROM users", CALLER)
        svc.workflow_save("bad", "ws1", "SELECT * FROM not_exist; SELECT 1", CALLER)
        out = svc.workflow_run("bad", CALLER)
        assert out["ok"] is False
        failed = [s for s in out["steps"] if not s["ok"]]
        assert failed and "not_exist" in failed[0]["step"]

    def test_chart_config_roundtrip(self, svc_wf):
        """图表配置随 workflow 保存/读取（P3 可视化）；不传则为 None；老库自动加列。"""
        svc = svc_wf
        svc.analysis_import("ws1", "u", "demo", "main", "SELECT * FROM users", CALLER)
        chart = {"type": "bar", "x": "name", "y": "age", "agg": "sum", "view": "chart"}
        wf = svc.workflow_save("viz", "ws1", "SELECT name, age FROM u", CALLER, chart=chart)
        assert wf["chart"] == chart
        assert svc.workflows.get("viz").chart == chart
        # 覆盖保存可清掉图表配置
        wf2 = svc.workflow_save("viz", "ws1", "SELECT name, age FROM u", CALLER)
        assert wf2["chart"] is None

    def test_chart_column_migration(self, tmp_path):
        """老版本建的表（无 chart 列）打开时自动 ALTER 补列。"""
        import sqlite3 as s3
        db = tmp_path / "old.sqlite3"
        conn = s3.connect(db)
        conn.execute("CREATE TABLE analysis_workflow (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     " name TEXT NOT NULL UNIQUE, workspace TEXT NOT NULL, script TEXT NOT NULL,"
                     " sources TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,"
                     " updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO analysis_workflow (name, workspace, script, sources,"
                     " created_at, updated_at) VALUES ('old', 'ws', 'SELECT 1', '[]', 't', 't')")
        conn.commit()
        conn.close()
        from dbmcp.workflows import WorkflowStore
        store = WorkflowStore(db)
        assert store.get("old").chart is None
        assert store.save("old", "ws", "SELECT 1", [], {"type": "pie"}).chart == {"type": "pie"}

    def test_list_delete(self, svc_wf):
        svc = svc_wf
        svc.analysis_import("ws1", "u", "demo", "main", "SELECT 1 AS x", CALLER)
        svc.workflow_save("w1", "ws1", "SELECT 1", CALLER)
        assert any(w["name"] == "w1" for w in svc.workflow_list())
        svc.workflow_delete("w1")
        assert not any(w["name"] == "w1" for w in svc.workflow_list())


def _node(nid, typ, name, **cfg):
    return {"id": nid, "type": typ, "name": name, "x": 0, "y": 0, "cfg": cfg}


class TestGraph:
    """DAG 画布：编译器 + 图 workflow 运行。"""

    def test_compile_linear(self):
        from dbmcp.workflows import compile_graph
        g = {"nodes": [
                _node("a", "source", "orders", conn="demo/main", sql="SELECT * FROM users"),
                _node("b", "filter", "adults", where="age >= 30"),
                _node("c", "aggregate", "stats", group="name", aggs="count(*) AS n"),
                _node("d", "output", "out", order_by="n DESC", limit=100)],
             "edges": [{"from": "a", "to": "b", "port": "in"},
                       {"from": "b", "to": "c", "port": "in"},
                       {"from": "c", "to": "d", "port": "in"}]}
        plan = compile_graph(g)
        assert plan["sources"][0]["dataset"] == "orders" and plan["sources"][0]["node"] == "a"
        assert 'VIEW "adults" AS SELECT * FROM "orders" WHERE age >= 30' in plan["steps"][0]["sql"]
        assert "GROUP BY name" in plan["steps"][1]["sql"]
        assert plan["steps"][2]["sql"] == 'SELECT * FROM "stats" ORDER BY n DESC LIMIT 100'

    def test_compile_join_ports(self):
        from dbmcp.workflows import compile_graph
        g = {"nodes": [
                _node("a", "source", "o", conn="p/c", sql="SELECT 1"),
                _node("b", "source", "u", conn="p/c", sql="SELECT 1"),
                _node("j", "join", "ou", kind="LEFT", on="l.uid = r.id")],
             "edges": [{"from": "a", "to": "j", "port": "left"},
                       {"from": "b", "to": "j", "port": "right"}]}
        sql = compile_graph(g)["steps"][0]["sql"]
        assert 'FROM "o" l LEFT JOIN "u" r ON l.uid = r.id' in sql

    def test_compile_errors(self):
        from dbmcp.workflows import WorkflowError, compile_graph
        with pytest.raises(WorkflowError, match="为空"):
            compile_graph({"nodes": [], "edges": []})
        with pytest.raises(WorkflowError, match="不合法"):
            compile_graph({"nodes": [_node("a", "filter", "1bad", where="x")], "edges": []})
        with pytest.raises(WorkflowError, match="缺少输入"):
            compile_graph({"nodes": [_node("a", "filter", "f", where="x")], "edges": []})
        with pytest.raises(WorkflowError, match="接满左右"):
            compile_graph({"nodes": [_node("a", "source", "s", conn="p/c", sql="SELECT 1"),
                                     _node("j", "join", "jj", on="l.a=r.b")],
                           "edges": [{"from": "a", "to": "j", "port": "left"}]})
        with pytest.raises(WorkflowError, match="存在环"):
            compile_graph({"nodes": [_node("a", "filter", "f1", where="x"),
                                     _node("b", "filter", "f2", where="y")],
                           "edges": [{"from": "a", "to": "b", "port": "in"},
                                     {"from": "b", "to": "a", "port": "in"}]})

    def test_graph_workflow_end_to_end(self, service, tmp_path):
        from dbmcp.workflows import WorkflowStore
        service.workflows = WorkflowStore(tmp_path / "wf.sqlite3")
        g = {"nodes": [
                _node("a", "source", "u", conn="demo/main", sql="SELECT * FROM users"),
                _node("b", "filter", "adults", where="age >= 30"),
                _node("c", "aggregate", "stats", group="", aggs="count(*) AS n"),
                _node("d", "output", "out")],
             "edges": [{"from": "a", "to": "b", "port": "in"},
                       {"from": "b", "to": "c", "port": "in"},
                       {"from": "c", "to": "d", "port": "in"}]}
        wf = service.workflow_save("dag", "ws1", "", CALLER, graph=g)
        assert wf["graph"]["nodes"][0]["name"] == "u"
        assert wf["sources"][0]["dataset"] == "u"  # 配方来自图的 source 节点
        out = service.workflow_run("dag", CALLER)
        assert out["ok"] is True
        assert out["output"]["rows"][0][0] == 2  # alice/carol ≥ 30
        by_node = {s.get("node"): s for s in out["steps"]}
        assert by_node["a"]["ok"] and by_node["b"]["ok"] and by_node["d"]["ok"]
        # 中间节点是工作区里的视图，可单独预览
        prev = service.analysis_sql("ws1", "SELECT count(*) FROM adults", CALLER)
        assert prev["rows"][0][0] == 2

    def test_run_graph_unsaved_and_compile_error(self, service, tmp_path):
        from dbmcp.workflows import WorkflowStore
        service.workflows = WorkflowStore(tmp_path / "wf.sqlite3")
        g = {"nodes": [_node("a", "source", "u", conn="demo/main", sql="SELECT * FROM users")],
             "edges": []}
        out = service.workflow_run_graph("ws1", g, CALLER)
        assert out["ok"] is True and out["output"]["row_count"] == 3
        bad = service.workflow_run_graph("ws1", {"nodes": [], "edges": []}, CALLER)
        assert bad["ok"] is False and "为空" in bad["steps"][0]["error"]

