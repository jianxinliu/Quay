"""PostgreSQL 跨库：agent 用 pg_database 在同一连接的不同 database 里查询 / 写入 / 同步。

PG 一条连接只能绑一个 database（服务端不支持跨库引用），所以「换库」= 换一条连接。
这里验证的不变量：
- pg_database 先校验再建连接（不存在的库不能把整条连接的健康位打坏）；
- 写操作的执行库随审批单存下，核销时在**审批时的那个库**执行，重提改了库就拒绝；
- 表同步可分别指定源/目标 PG 库，且老审批单的计划指纹不受新字段影响。

前半部分不连库；后半部分需要真实 PG（默认 127.0.0.1:15432，postgres/123456），
库里要有 testdb / shop / billing 三个库，连不上则跳过：

    docker run -d --name dbm-pgdb-e2e -e POSTGRES_PASSWORD=123456 -e POSTGRES_DB=testdb \\
        -p 15432:5432 postgres:17
    然后在容器里：
    CREATE DATABASE shop; CREATE DATABASE billing;
    CREATE DATABASE noconn; ALTER DATABASE noconn ALLOW_CONNECTIONS false;
    -- testdb: orders(id int pk, note text)，1 行
    -- shop:   sales.orders(id int pk, amount numeric)，2 行；public.items(id, name)，1 行 'shop-item'
    -- billing: invoices(id int pk, status int)，2 行
"""

import os
import sqlite3

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine, text

from dbmcp import sync
from dbmcp.approvals import ApprovalError, ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.health import is_connection_error
from dbmcp.metadata import MetadataCache
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="pgdb")


# ---------------- 不连库的部分 ----------------

class TestHealthClassification:
    def test_missing_database_is_not_connection_error(self):
        """连一个不存在的库，原文带 `connection to server`，但重连治不好，不能判为断连。"""
        exc = Exception('connection to server at "127.0.0.1", port 5432 failed: '
                        'FATAL:  database "nope" does not exist')
        assert is_connection_error(exc) is False

    def test_real_connection_failure_still_detected(self):
        exc = Exception('connection to server at "127.0.0.1", port 5432 failed: '
                        'Connection refused')
        assert is_connection_error(exc) is True


class TestApprovalDatabase:
    def _create(self, store, database=None):
        return store.create(
            project="p", connection="c", environment="dev", engine="postgres",
            sql="DELETE FROM t", fingerprint="fp", reason="", risk_level="low",
            risk_report={}, agent="a", session_id="s", database=database,
        )

    def test_database_roundtrip(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        assert self._create(store, "shop").database == "shop"
        assert self._create(store).database == ""

    def test_old_db_gets_column(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        ApprovalStore(path)
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE change_request DROP COLUMN database")
        store = ApprovalStore(path)  # 迁移补列
        assert self._create(store, "billing").database == "billing"

    def test_consume_rejects_other_database(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        c = self._create(store, "shop")
        store.approve(c.id, "human")
        with pytest.raises(ApprovalError, match="拒绝执行"):
            store.consume(c.id, "fp", ("p", "c"), database="billing")
        # 声明成默认库也算对不上
        with pytest.raises(ApprovalError, match="拒绝执行"):
            store.consume(c.id, "fp", ("p", "c"), database="")
        assert store.get(c.id).status == "approved"  # 被拒不核销

    def test_consume_without_declared_database_uses_stored(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.sqlite3")
        c = self._create(store, "shop")
        store.approve(c.id, "human")
        assert store.consume(c.id, "fp", ("p", "c")).database == "shop"


class TestSyncSpecPgDatabase:
    def _spec(self, **kw):
        base = dict(source_project="p", source_connection="a", source_table="t",
                    target_project="p", target_connection="a", target_table="t")
        base.update(kw)
        return sync.SyncSpec(**base)

    def test_fingerprint_unchanged_when_unset(self):
        """升级前批准的审批单（字典里没有新键）升级后重提仍算出同一指纹。"""
        spec = self._spec(target_connection="b")
        old_dict = {k: v for k, v in spec.to_dict().items()}
        assert "source_pg_database" not in old_dict
        assert sync.spec_fingerprint(sync.SyncSpec.from_dict(old_dict)) == \
            sync.spec_fingerprint(spec)

    def test_fingerprint_changes_with_database(self):
        a = self._spec(target_connection="b")
        b = self._spec(target_connection="b", target_pg_database="shop")
        assert sync.spec_fingerprint(a) != sync.spec_fingerprint(b)
        assert sync.SyncSpec.from_dict(b.to_dict()).target_pg_database == "shop"

    def test_same_table_in_other_database_is_not_self_sync(self):
        sync.validate_spec(self._spec(source_pg_database="shop", target_pg_database="billing"))
        with pytest.raises(sync.SyncError, match="same table"):
            sync.validate_spec(self._spec())

    def test_bad_identifier_rejected(self):
        with pytest.raises(sync.SyncError, match="PG database"):
            sync.validate_spec(self._spec(target_connection="b",
                                          target_pg_database="x; DROP"))

    def test_plan_shows_database(self):
        spec = self._spec(target_connection="b", source_pg_database="shop",
                          source_database="sales")
        text_ = sync.render_plan(spec, "dev", "postgres", "local", "postgres",
                                 ["id"], "", [], True, None)
        assert "p/a.shop.sales.t" in text_


def test_pg_database_rejected_on_non_pg_connection(tmp_path):
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(tmp_path / "x.sqlite3")}}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"))
    try:
        assert svc.resolve_pg_database("demo", "main", None) is None
        assert svc.resolve_pg_database("demo", "main", "  ") is None
        with pytest.raises(ValueError, match="only applies to PostgreSQL"):
            svc.query("demo", "main", "SELECT 1", CALLER, database="shop")
    finally:
        svc.close()


def test_metadata_cache_keys_by_database(tmp_path):
    cache = MetadataCache(tmp_path / "m.sqlite3", pool=None)  # type: ignore[arg-type]
    payload = {"columns": [], "indexes": [], "primary_key": [], "row_estimate": 7}
    cache._write("p", "c", cache._key("orders", "shop"), payload, 1.0)
    assert cache._read("p", "c", "orders", "shop").row_estimate == 7
    assert cache._read("p", "c", "orders") is None
    assert cache._read("p", "c", "orders", "billing") is None


@pytest.mark.anyio
async def test_tools_expose_pg_database(tmp_path):
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": "sqlite", "database": str(tmp_path / "x.sqlite3")}}}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"))
    try:
        async with Client(build_mcp(svc)) as c:
            tools = {t.name: t for t in await c.list_tools()}
        assert "list_server_databases" in tools
        for name in ("query", "execute", "sample_rows", "list_databases", "list_tables",
                     "describe_table", "table_ddl", "export_table", "analysis_import"):
            assert "pg_database" in tools[name].inputSchema["properties"], name
        for name in ("sync_table", "sync_table_ddl"):
            props = tools[name].inputSchema["properties"]
            assert {"source_pg_database", "target_pg_database"} <= set(props), name
    finally:
        svc.close()


# ---------------- 真实 PostgreSQL ----------------

PG_HOST = os.environ.get("DBM_E2E_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("DBM_E2E_PG_PORT", "15432"))
PG_URL = f"postgresql+psycopg://postgres:123456@{PG_HOST}:{PG_PORT}"


def _pg_ready() -> bool:
    try:
        eng = create_engine(f"{PG_URL}/postgres", connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            names = {r[0] for r in conn.execute(text("SELECT datname FROM pg_database"))}
        eng.dispose()
        return {"testdb", "shop", "billing"} <= names
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(not _pg_ready(), reason="需要本地 PG（testdb/shop/billing 三个库）")


def _exec(db: str, sql: str):
    eng = create_engine(f"{PG_URL}/{db}")
    try:
        with eng.begin() as conn:
            res = conn.execute(text(sql))
            return res.fetchall() if res.returns_rows else None
    finally:
        eng.dispose()


def _pg_service(tmp_path, environment="dev") -> DbmService:
    conn = {"engine": "postgres", "host": PG_HOST, "port": PG_PORT, "database": "testdb",
            "environment": environment, "user": "postgres", "password": "plain://123456",
            "writer": {"user": "postgres", "password": "plain://123456"}}
    cfg = AppConfig.model_validate({"projects": {"pg": {"connections": {
        "main": conn, "local": {**conn, "environment": "local"}}}}})
    db_path = tmp_path / "a.sqlite3"
    svc = DbmService(cfg, AuditStore(db_path), ApprovalStore(db_path),
                     metadata=None)
    svc.data_dir = str(tmp_path / "data")
    svc.base_url = "http://127.0.0.1:8100"
    return svc


@pg
@pytest.mark.anyio
async def test_pg_read_tools_across_databases(tmp_path):
    svc = _pg_service(tmp_path)
    try:
        async with Client(build_mcp(svc)) as c:
            dbs = (await c.call_tool("list_server_databases",
                                     {"project": "pg", "connection": "main"})).data
            assert {"testdb", "shop", "billing"} <= set(dbs)
            assert "noconn" not in dbs  # datallowconn=false 的库不给

            schemas = (await c.call_tool("list_databases", {
                "project": "pg", "connection": "main", "pg_database": "shop"})).data
            assert "sales" in schemas

            tables = (await c.call_tool("list_tables", {
                "project": "pg", "connection": "main", "pg_database": "shop",
                "database": "sales"})).data
            assert tables == ["orders"]

            desc = (await c.call_tool("describe_table", {
                "project": "pg", "connection": "main", "pg_database": "shop",
                "database": "sales", "table": "orders"})).data
            assert [col["name"] for col in desc["columns"]] == ["id", "amount"]

            ddl = (await c.call_tool("table_ddl", {
                "project": "pg", "connection": "main", "pg_database": "billing",
                "table": "invoices"})).data
            assert "invoices" in ddl

            # 同一条 SQL：默认库里是 testdb 的 orders，shop 里是 sales.orders
            r = (await c.call_tool("query", {
                "project": "pg", "connection": "main",
                "sql": "SELECT current_database(), count(*) FROM orders"})).data
            assert "testdb\t1" in r
            r = (await c.call_tool("query", {
                "project": "pg", "connection": "main", "pg_database": "shop",
                "sql": "SELECT current_database(), count(*) FROM sales.orders"})).data
            assert "shop\t2" in r

            r = (await c.call_tool("sample_rows", {
                "project": "pg", "connection": "main", "pg_database": "shop",
                "table": "items"})).data
            assert "shop-item" in r

            # 指定的就是默认库 → 等同不指定
            r = (await c.call_tool("query", {
                "project": "pg", "connection": "main", "pg_database": "testdb",
                "sql": "SELECT current_database()"})).data
            assert "testdb" in r

            with pytest.raises(ToolError, match="can connect to"):
                await c.call_tool("query", {
                    "project": "pg", "connection": "main", "pg_database": "nope",
                    "sql": "SELECT 1"})
        # 库名写错不能连累整条连接
        h = svc.health.snapshot().get(("pg", "main"))
        assert h is None or h.state == "ok"
        audit = svc.store.recent()
        assert any("db=shop" in (a["detail"] or "") for a in audit)
    finally:
        svc.close()


@pg
@pytest.mark.anyio
async def test_pg_execute_runs_in_approved_database(tmp_path):
    _exec("billing", "UPDATE invoices SET status = 0")
    svc = _pg_service(tmp_path, environment="prod")
    sql = "UPDATE invoices SET status = 1 WHERE id = 1"
    try:
        async with Client(build_mcp(svc)) as c:
            r = (await c.call_tool("execute", {
                "project": "pg", "connection": "main", "pg_database": "billing",
                "sql": sql, "wait_seconds": 0})).data
            assert r["status"] == "approval_required"
            assert r["pg_database"] == "billing"
            cid = r["change_id"]
            assert svc.get_change(cid).database == "billing"
            # 风险评估/执行计划是在 billing 里取的（默认库 testdb 没有这张表）
            assert r["risk"].get("explain")
            svc.approve_change(cid, decided_by="human")

            # 重提时声明了别的库 → 拒绝，不核销
            bad = (await c.call_tool("execute", {
                "project": "pg", "connection": "main", "pg_database": "shop",
                "sql": sql, "change_id": cid})).data
            assert bad["status"] == "rejected"
            assert svc.get_change(cid).status == "approved"

            # 不声明库 → 按审批单记的 billing 执行
            ok = (await c.call_tool("execute", {
                "project": "pg", "connection": "main", "sql": sql,
                "change_id": cid})).data
            assert ok["status"] == "executed" and ok["affected_rows"] == 1
        assert _exec("billing", "SELECT status FROM invoices WHERE id = 1") == [(1,)]
    finally:
        svc.close()


@pg
def test_pg_backend_approve_and_execute_uses_stored_database(tmp_path):
    _exec("billing", "UPDATE invoices SET status = 0")
    svc = _pg_service(tmp_path, environment="prod")
    try:
        r = svc.execute("pg", "main", "UPDATE invoices SET status = 2 WHERE id = 2",
                        CALLER, database="billing")
        out = svc.approve_and_execute_change(r["change_id"], decided_by="human")
        assert out["affected_rows"] == 1
        assert _exec("billing", "SELECT status FROM invoices WHERE id = 2") == [(2,)]
    finally:
        svc.close()


@pg
def test_pg_sync_between_databases(tmp_path):
    _exec("billing", "DROP TABLE IF EXISTS items_copy")
    svc = _pg_service(tmp_path)
    try:
        spec = sync.SyncSpec(
            source_project="pg", source_connection="main", source_table="items",
            target_project="pg", target_connection="local", target_table="items_copy",
            source_pg_database="shop", target_pg_database="billing",
        )
        out = svc.sync_table(spec, CALLER)
        assert out["status"] == "executed" and out["affected_rows"] == 1
        assert _exec("billing", "SELECT name FROM items_copy") == [("shop-item",)]
        # 源库清单里查无此库 → 在建连接前就拒
        bad = sync.SyncSpec(**{**spec.to_dict(), "source_pg_database": "nope"})
        with pytest.raises(ValueError, match="can connect to"):
            svc.sync_table(bad, CALLER)
    finally:
        _exec("billing", "DROP TABLE IF EXISTS items_copy")
        svc.close()


@pg
def test_pg_analysis_import_records_database(tmp_path):
    from dbmcp.analysis import AnalysisStore

    svc = _pg_service(tmp_path)
    svc.analysis = AnalysisStore(tmp_path / "analysis")
    try:
        svc.analysis_import("ws", "o", "pg", "main", "SELECT * FROM sales.orders", CALLER,
                            database="shop")
        prov = svc.analysis.get_provenance("ws")
        assert prov[0]["database"] == "shop"
    finally:
        svc.close()
