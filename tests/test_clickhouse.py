"""ClickHouse（只读分析）支持测试。

- 纯函数单测（无需服务器）：config 校验、SQL 分类、分页注入的 clickhouse 方言。
- 真实 e2e（需本地 ClickHouse，native 端口 19000、用户 default/chpw；不可用则整体 skip）：
  建库建表 → 反射 → 只读查询 → **数据库层 readonly=1 防线** → 行数/容量估算 → 建表语句 → 搜表。
  只读防线（readonly=1 走 URL query 才生效）只有真实 ClickHouse 才验得出，SQLite 单测发现不了。
"""

import pytest

from dbmcp.audit.classify import classify
from dbmcp.config import AppConfig, ConnectionConfig
from dbmcp import engines
from dbmcp.audit.log import AuditStore
from dbmcp.service import CallerInfo, DbmService, QueryRejected

CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-ch")
CH_HOST, CH_PORT, CH_USER, CH_PW = "127.0.0.1", 19000, "default", "chpw"
TEST_DB = "dbm_ch_test"


# ---------- 纯函数单测（始终运行）----------

class TestClickhousePureFunctions:
    def test_config_requires_host_and_user_password_optional(self):
        # host + user 必填，password 可空（default 用户常无密码）
        cfg = ConnectionConfig.model_validate(
            {"engine": "clickhouse", "host": "h", "user": "default"})
        assert cfg.engine == "clickhouse"
        assert cfg.password is None
        with pytest.raises(ValueError, match="clickhouse 连接缺少必填字段"):
            ConnectionConfig.model_validate({"engine": "clickhouse", "host": "h"})

    def test_classify_select_readonly(self):
        assert classify("SELECT * FROM t", "clickhouse").readonly is True

    def test_classify_insert_not_readonly(self):
        assert classify("INSERT INTO t VALUES (1)", "clickhouse").readonly is False

    def test_classify_alter_mutation_not_readonly(self):
        # ClickHouse 的删改是 ALTER ... DELETE/UPDATE（mutation），必须判为写
        assert classify("ALTER TABLE t DELETE WHERE id = 1", "clickhouse").readonly is False

    def test_paginate_injects_limit(self):
        out, paged, _ = engines.paginate_sql("SELECT * FROM t", "clickhouse", 100, 0)
        assert paged is True
        assert "LIMIT 100" in out.upper()

    def test_paginate_respects_user_limit(self):
        out, paged, _ = engines.paginate_sql("SELECT * FROM t LIMIT 5", "clickhouse", 100, 0)
        assert paged is False


# ---------- 真实 e2e ----------

def _clickhouse_available() -> bool:
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL
        url = URL.create("clickhouse+native", username=CH_USER, password=CH_PW,
                         host=CH_HOST, port=CH_PORT, database="default")
        eng = create_engine(url, connect_args={"connect_timeout": 2})
        with eng.connect() as c:
            c.exec_driver_sql("SELECT 1")
        eng.dispose()
        return True
    except Exception:
        return False


ch = pytest.mark.skipif(not _clickhouse_available(), reason="本地 ClickHouse(19000) 不可用")


def _admin_engine():
    """无 readonly 的管理连接，用于建库建表播数据。"""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    url = URL.create("clickhouse+native", username=CH_USER, password=CH_PW,
                     host=CH_HOST, port=CH_PORT, database="default")
    return create_engine(url, connect_args={"connect_timeout": 5})


@pytest.fixture
def service(tmp_path):
    admin = _admin_engine()
    with admin.begin() as c:
        c.exec_driver_sql(f"CREATE DATABASE IF NOT EXISTS {TEST_DB}")
        c.exec_driver_sql(f"DROP TABLE IF EXISTS {TEST_DB}.orders")
        c.exec_driver_sql(
            f"CREATE TABLE {TEST_DB}.orders (id UInt64, amount Decimal(10,2),"
            f" created DateTime, channel String) ENGINE = MergeTree ORDER BY id")
        c.exec_driver_sql(
            f"INSERT INTO {TEST_DB}.orders VALUES"
            f" (1, 9.90, now(), 'web'), (2, 5.00, now(), 'app'), (3, 12.5, now(), 'web')")
    admin.dispose()

    # reader 账号即 default（有写权限），但 reader 引擎带 ?readonly=1 → DB 层禁写
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"ch": {
        "engine": "clickhouse", "environment": "local",
        "host": CH_HOST, "port": CH_PORT, "user": CH_USER, "password": "env://CH_PW",
        "database": TEST_DB, "policy": {"max_rows": 2},
    }}}}})
    import os
    os.environ["CH_PW"] = CH_PW
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"))
    yield svc
    svc.close()


@ch
class TestClickhouseE2E:
    def test_list_tables(self, service):
        assert "orders" in service.list_tables("demo", "ch", CALLER)

    def test_describe_table(self, service):
        info = service.describe_table("demo", "ch", "orders", CALLER)
        names = [c["name"] for c in info["columns"]]
        assert names == ["id", "amount", "created", "channel"]
        # ClickHouse 的 ORDER BY key 反射为主键
        assert "id" in info["primary_key"]

    def test_select_ok_and_paginated(self, service):
        res = service.query("demo", "ch", "SELECT id, channel FROM orders ORDER BY id", CALLER)
        assert res["columns"] == ["id", "channel"]
        assert res["row_count"] == 2          # max_rows=2 截断
        assert res["truncated"] is True

    def test_aggregate(self, service):
        res = service.query("demo", "ch", "SELECT count() AS c FROM orders", CALLER)
        assert int(res["rows"][0][0]) == 3

    def test_write_rejected_by_classifier(self, service):
        with pytest.raises(QueryRejected):
            service.query("demo", "ch", "INSERT INTO orders VALUES (9,1.0,now(),'x')", CALLER)

    def test_readonly_enforced_at_db_layer(self, service):
        """绕过分类器，直接在 reader 引擎上跑写 → ClickHouse readonly=1 报 Code 164。"""
        cfg = service.config.get_connection("demo", "ch")
        engine = service.pool.get("demo", "ch", cfg)
        with pytest.raises(Exception, match="164|readonly|Cannot execute"):
            engines.run_query(engine, "INSERT INTO orders VALUES (9,1.0,now(),'x')", max_rows=10)
        # 确认没写进去
        res = service.query("demo", "ch", "SELECT count() AS c FROM orders", CALLER)
        assert int(res["rows"][0][0]) == 3

    def test_estimate_row_count(self, service):
        cfg = service.config.get_connection("demo", "ch")
        engine = service.pool.get("demo", "ch", cfg)
        assert engines.estimate_row_count(engine, "clickhouse", "orders") == 3

    def test_table_sizes(self, service):
        cfg = service.config.get_connection("demo", "ch")
        engine = service.pool.get("demo", "ch", cfg)
        sizes = engines.table_sizes(engine, "clickhouse", TEST_DB)
        assert sizes.get("orders", 0) > 0

    def test_get_table_ddl(self, service):
        cfg = service.config.get_connection("demo", "ch")
        engine = service.pool.get("demo", "ch", cfg)
        ddl = engines.get_table_ddl(engine, "clickhouse", "orders")
        assert "CREATE TABLE" in ddl and "MergeTree" in ddl

    def test_search_tables(self, service):
        cfg = service.config.get_connection("demo", "ch")
        engine = service.pool.get("demo", "ch", cfg)
        hits = engines.search_tables(engine, "clickhouse", "orders")
        assert any(h["table"] == "orders" for h in hits)

    def test_explain_does_not_execute(self, service):
        res = service.query("demo", "ch", "EXPLAIN SELECT * FROM orders", CALLER)
        assert res["row_count"] >= 1

    def test_no_database_lists_databases_then_tables(self, service, tmp_path):
        """未绑定 database 时应能列出库（库→表→列三级树），选库后再列表。

        `service` fixture 负责播种 dbm_ch_test.orders；这里另建一个**无 database** 的连接，
        验证 list_databases 不再返回空、选库后能列表、不选库则明确引导报错。
        （回归：service.list_databases 曾对 clickhouse 直接 return []）
        """
        import os
        os.environ["CH_PW"] = CH_PW
        cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"chnodb": {
            "engine": "clickhouse", "environment": "dev",
            "host": CH_HOST, "port": CH_PORT, "user": CH_USER, "password": "env://CH_PW",
        }}}}})  # 无 database
        svc = DbmService(cfg, AuditStore(tmp_path / "nodb.sqlite3"))
        try:
            dbs = svc.list_databases("demo", "chnodb", CALLER)
            assert TEST_DB in dbs, f"list_databases 应含 {TEST_DB}，实际 {dbs}"
            assert "orders" in svc.list_tables("demo", "chnodb", CALLER, schema=TEST_DB)
            with pytest.raises(ValueError, match="未绑定默认库"):
                svc.list_tables("demo", "chnodb", CALLER)
        finally:
            svc.close()
