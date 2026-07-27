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

    def test_decimal_strings_become_double(self, store):
        """MySQL DECIMAL 经 _jsonable 变数字字符串——必须推断为 DOUBLE 才能聚合。"""
        store.import_rows("ws1", "amt", ["ch", "amount"],
                          [["a", "12.50"], ["b", "3.00"], ["a", None]])
        info = store.describe_dataset("ws1", "amt")
        types = {c["name"]: c["type"] for c in info["columns"]}
        assert types["amount"] == "DOUBLE" and types["ch"] == "VARCHAR"
        out = store.run_sql("ws1", "SELECT sum(amount) FROM amt")
        assert out["rows"][0][0] == 15.5

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

    def test_drop_dataset_table_and_view(self, store):
        """drop_dataset 对表和视图都能删（DuckDB 的 DROP TABLE 遇 VIEW 会报类型错）。"""
        store.import_rows("ws1", "t", ["a"], [[1]])
        store.run_sql("ws1", "CREATE VIEW v AS SELECT * FROM t")
        store.drop_dataset("ws1", "v")   # 视图
        store.drop_dataset("ws1", "t")   # 表
        store.drop_dataset("ws1", "nope")  # 不存在 → 静默
        assert store.list_datasets("ws1") == []

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

    def test_compile_join_two_way_legacy_ports(self):
        """老图端口 left/right 兼容为 in_1/in_2；老 cfg 的 l./r. 别名也翻译为 a./b."""
        from dbmcp.workflows import compile_graph
        g = {"nodes": [
                _node("a", "source", "o", conn="p/c", sql="SELECT 1"),
                _node("b", "source", "u", conn="p/c", sql="SELECT 1"),
                _node("j", "join", "ou", kind="LEFT", on="l.uid = r.id")],
             "edges": [{"from": "a", "to": "j", "port": "left"},
                       {"from": "b", "to": "j", "port": "right"}]}
        sql = compile_graph(g)["steps"][0]["sql"]
        assert 'FROM "o" a LEFT JOIN "u" b ON a.uid = b.id' in sql

    def test_compile_join_two_way_new_ports(self):
        """新格式 in_1/in_2 端口 + a/b 别名。"""
        from dbmcp.workflows import compile_graph
        g = {"nodes": [
                _node("a", "source", "o", conn="p/c", sql="SELECT 1"),
                _node("b", "source", "u", conn="p/c", sql="SELECT 1"),
                _node("j", "join", "ou", kind="INNER", on="a.uid = b.id")],
             "edges": [{"from": "a", "to": "j", "port": "in_1"},
                       {"from": "b", "to": "j", "port": "in_2"}]}
        sql = compile_graph(g)["steps"][0]["sql"]
        assert 'FROM "o" a INNER JOIN "u" b ON a.uid = b.id' in sql

    def test_compile_join_three_way(self):
        """3 路 JOIN：别名 a/b/c，ON 用户手写整段条件，堆到最后一个 JOIN 后。"""
        from dbmcp.workflows import compile_graph
        g = {"nodes": [
                _node("s1", "source", "orders", conn="p/c", sql="SELECT 1"),
                _node("s2", "source", "users", conn="p/c", sql="SELECT 1"),
                _node("s3", "source", "goods", conn="p/c", sql="SELECT 1"),
                _node("j", "join", "j3", kind="INNER",
                      on="a.uid=b.id AND a.gid=c.id",
                      select="a.oid, b.name, c.title")],
             "edges": [{"from": "s1", "to": "j", "port": "in_1"},
                       {"from": "s2", "to": "j", "port": "in_2"},
                       {"from": "s3", "to": "j", "port": "in_3"}]}
        sql = compile_graph(g)["steps"][0]["sql"]
        assert ('SELECT a.oid, b.name, c.title FROM "orders" a INNER JOIN "users" b'
                ' INNER JOIN "goods" c ON a.uid=b.id AND a.gid=c.id') in sql

    def test_compile_join_port_order_matters(self):
        """端口序号决定 JOIN 顺序：in_2 早于 in_3 出现在 SQL 中，即使 edge 定义顺序反了。"""
        from dbmcp.workflows import compile_graph
        g = {"nodes": [
                _node("s1", "source", "t1", conn="p/c", sql="SELECT 1"),
                _node("s2", "source", "t2", conn="p/c", sql="SELECT 1"),
                _node("s3", "source", "t3", conn="p/c", sql="SELECT 1"),
                _node("j", "join", "jj", on="a.x=b.x AND b.y=c.y")],
             "edges": [{"from": "s3", "to": "j", "port": "in_3"},
                       {"from": "s1", "to": "j", "port": "in_1"},
                       {"from": "s2", "to": "j", "port": "in_2"}]}
        sql = compile_graph(g)["steps"][0]["sql"]
        # a=t1, b=t2, c=t3
        assert 'FROM "t1" a' in sql
        assert '"t2" b' in sql
        assert '"t3" c' in sql
        # b 出现在 c 之前
        assert sql.index('"t2" b') < sql.index('"t3" c')

    def test_compile_join_default_select_n_way(self):
        """N 路 JOIN 未指定 SELECT 时默认展开 a.*, b.*, c.*..."""
        from dbmcp.workflows import compile_graph
        g = {"nodes": [
                _node("s1", "source", "t1", conn="p/c", sql="SELECT 1"),
                _node("s2", "source", "t2", conn="p/c", sql="SELECT 1"),
                _node("s3", "source", "t3", conn="p/c", sql="SELECT 1"),
                _node("j", "join", "jj", on="a.x=b.x AND b.y=c.y")],
             "edges": [{"from": "s1", "to": "j", "port": "in_1"},
                       {"from": "s2", "to": "j", "port": "in_2"},
                       {"from": "s3", "to": "j", "port": "in_3"}]}
        sql = compile_graph(g)["steps"][0]["sql"]
        assert 'SELECT a.*, b.*, c.*' in sql

    def test_compile_errors(self):
        from dbmcp.workflows import WorkflowError, compile_graph
        with pytest.raises(WorkflowError, match="为空"):
            compile_graph({"nodes": [], "edges": []})
        with pytest.raises(WorkflowError, match="不合法"):
            compile_graph({"nodes": [_node("a", "filter", "1bad", where="x")], "edges": []})
        with pytest.raises(WorkflowError, match="缺少输入"):
            compile_graph({"nodes": [_node("a", "filter", "f", where="x")], "edges": []})
        with pytest.raises(WorkflowError, match="至少需要两个输入"):
            compile_graph({"nodes": [_node("a", "source", "s", conn="p/c", sql="SELECT 1"),
                                     _node("j", "join", "jj", on="a.x=b.x")],
                           "edges": [{"from": "a", "to": "j", "port": "in_1"}]})
        with pytest.raises(WorkflowError, match="缺少 ON"):
            compile_graph({"nodes": [_node("s1", "source", "t1", conn="p/c", sql="SELECT 1"),
                                     _node("s2", "source", "t2", conn="p/c", sql="SELECT 1"),
                                     _node("j", "join", "jj")],
                           "edges": [{"from": "s1", "to": "j", "port": "in_1"},
                                     {"from": "s2", "to": "j", "port": "in_2"}]})
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

    def test_preview_columns_lazy_and_refresh(self, service, tmp_path):
        """preview_columns：懒建模式复用已存 view；refresh=True 强制重建。"""
        g = {"nodes": [
                _node("a", "source", "u", conn="demo/main", sql="SELECT * FROM users"),
                _node("b", "filter", "adults", where="age >= 30"),
                _node("c", "aggregate", "stats", group="", aggs="count(*) AS n")],
             "edges": [{"from": "a", "to": "b", "port": "in"},
                       {"from": "b", "to": "c", "port": "in"}]}
        # 首次预览 b：懒建应从空工作区物化 source a + step b
        out = service.workflow_preview_columns("ws1", g, "b", CALLER)
        assert out["columns"] and {c["name"] for c in out["columns"]} == {"id", "name", "age"}
        assert "error" not in out
        # 第二次同节点：懒模式命中已建 view，直接 DESCRIBE
        out2 = service.workflow_preview_columns("ws1", g, "b", CALLER)
        assert out2 == out
        # 预览 c：会追加建 c，b 已存不再重跑
        outc = service.workflow_preview_columns("ws1", g, "c", CALLER)
        assert {c["name"] for c in outc["columns"]} == {"n"}
        # refresh=True 强制重建
        out_r = service.workflow_preview_columns("ws1", g, "b", CALLER, refresh=True)
        assert out_r == out

    def test_preview_columns_missing_node(self, service):
        g = {"nodes": [_node("a", "source", "u", conn="demo/main", sql="SELECT * FROM users")],
             "edges": []}
        out = service.workflow_preview_columns("ws1", g, "not-exist", CALLER)
        assert out["columns"] == [] and "不在流程中" in out["error"]

    def test_preview_columns_compile_error(self, service):
        """编译失败 → 返回 {columns:[], error}，不抛异常。"""
        g = {"nodes": [_node("a", "filter", "f", where="x")], "edges": []}
        out = service.workflow_preview_columns("ws1", g, "a", CALLER)
        assert out["columns"] == [] and "编译流程失败" in out["error"]

    def test_preview_columns_upstream_error(self, service):
        """上游连接不存在 → error 携带节点名，不抛。"""
        g = {"nodes": [
                _node("a", "source", "bad_src", conn="nope/nope", sql="SELECT 1"),
                _node("b", "filter", "flt", where="1=1")],
             "edges": [{"from": "a", "to": "b", "port": "in"}]}
        out = service.workflow_preview_columns("ws1", g, "b", CALLER)
        assert out["columns"] == [] and "bad_src" in out["error"]

    def test_preview_node_returns_rows(self, service):
        g = {"nodes": [
                _node("a", "source", "u", conn="demo/main", sql="SELECT * FROM users"),
                _node("b", "filter", "adults", where="age >= 30")],
             "edges": [{"from": "a", "to": "b", "port": "in"}]}
        out = service.workflow_preview_node("ws1", g, "b", CALLER, limit=10)
        assert set(out["columns"]) == {"id", "name", "age"}
        # alice(30) + carol(41) 两行
        assert out["row_count"] == 2

    def test_preview_node_limit_clamped(self, service):
        g = {"nodes": [_node("a", "source", "u", conn="demo/main", sql="SELECT * FROM users")],
             "edges": []}
        out = service.workflow_preview_node("ws1", g, "a", CALLER, limit=10000)
        # 上限 1000，clamp 不抛
        assert out["row_count"] <= 1000

    def test_agent_save_workflow_guard(self, service, tmp_path):
        """agent 侧保存（allow_replace_graph=False）：可建/覆盖脚本式，不可覆盖人画的 DAG。"""
        from dbmcp.workflows import WorkflowStore
        svc = service
        svc.workflows = WorkflowStore(tmp_path / "wf.sqlite3")
        svc.analysis_import("ws1", "u", "demo", "main", "SELECT * FROM users", CALLER)
        # agent 创建 + 迭代覆盖自己的脚本式 workflow
        svc.workflow_save("agent-wf", "ws1", "SELECT 1", CALLER, allow_replace_graph=False)
        wf = svc.workflow_save("agent-wf", "ws1", "SELECT 2", CALLER, allow_replace_graph=False)
        assert wf["script"] == "SELECT 2"
        # 人画的 DAG：agent 同名保存被拒
        g = {"nodes": [_node("a", "source", "u", conn="demo/main", sql="SELECT 1")], "edges": []}
        svc.workflow_save("human-dag", "ws1", "", CALLER, graph=g)
        with pytest.raises(ValueError, match="不允许覆盖"):
            svc.workflow_save("human-dag", "ws1", "SELECT 1", CALLER, allow_replace_graph=False)
        # 后台侧（默认 allow_replace_graph=True）不受限
        svc.workflow_save("human-dag", "ws1", "", CALLER, graph=g)

    def test_seed_examples(self, tmp_path):
        """首次启动播种内置示例；已有 workflow 或已播种过则不动。"""
        from dbmcp.examples import EXAMPLE_NAME, seed_examples
        from dbmcp.workflows import WorkflowStore
        store = WorkflowStore(tmp_path / "wf.sqlite3")
        assert seed_examples(store, tmp_path) is True
        wf = store.get(EXAMPLE_NAME)
        assert wf.graph and len(wf.graph["nodes"]) == 8 and wf.chart["type"] == "bar"
        assert (tmp_path / "demo" / "channel_cost.csv").exists()
        assert seed_examples(store, tmp_path) is False  # 幂等
        store.delete(EXAMPLE_NAME)
        store.save("mine", "ws", "SELECT 1", [])
        assert seed_examples(store, tmp_path) is False  # 删了示例但有别的 → 不复活

    def test_run_graph_unsaved_and_compile_error(self, service, tmp_path):
        from dbmcp.workflows import WorkflowStore
        service.workflows = WorkflowStore(tmp_path / "wf.sqlite3")
        g = {"nodes": [_node("a", "source", "u", conn="demo/main", sql="SELECT * FROM users")],
             "edges": []}
        out = service.workflow_run_graph("ws1", g, CALLER)
        assert out["ok"] is True and out["output"]["row_count"] == 3
        bad = service.workflow_run_graph("ws1", {"nodes": [], "edges": []}, CALLER)
        assert bad["ok"] is False and "为空" in bad["steps"][0]["error"]

