"""体检记录：存储层 + service 落库 + 后台路由。

体检是一次性快照，历史留痕只在「存得下、取得回、删得掉」时才可信——
这三条就是本文件的核心断言。
"""

import sqlite3

import pytest
from starlette.testclient import TestClient

from dbmcp.admin import mount_admin
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.checkup_records import CheckupRecordStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import DbmService

TOKEN = "test-admin-token"


def _report(overall="warn", scope="shop", engine="mysql"):
    # 报告 dict 刻意不含 instance_scope：真实的 CheckupReport.to_dict() 也没有这个顶层
    # 字段，标志只能由调用方传（fixture 里带上会让「从 report 里取」的写法假通过）
    return {
        "engine": engine, "version": "8.0", "scope": scope, "started_at": "2026-11-13T02:00:00Z",
        "elapsed_ms": 42, "overall": overall, "summary": "2 项需关注",
        "checks": [
            {"name": "deadlocks", "title": "死锁", "status": "critical", "dimension": "locks"},
            {"name": "cache", "title": "缓存命中率", "status": "warn", "dimension": "cache"},
            {"name": "server", "title": "连接", "status": "ok", "dimension": "connectivity"},
        ],
        "dimensions": [{"name": "locks", "title": "锁"}, {"name": "cache", "title": "缓存"},
                       {"name": "connectivity", "title": "连接"}],
        "privileges": [],
    }


class TestStore:
    def test_save_list_get_roundtrip(self, tmp_path):
        store = CheckupRecordStore(tmp_path / "dbm.sqlite3")
        rid = store.save("demo", "demo/main", _report())
        assert rid > 0

        rows = store.list("demo", "demo/main")
        assert len(rows) == 1
        assert rows[0]["overall"] == "warn"
        assert rows[0]["scope"] == "shop"
        # 摘要里要能直接看出有问题的项数（列表页不拉全文）
        assert rows[0]["problems"] == 2
        assert "report" not in rows[0]
        # 实例级标志由调用方给（报告里没有这个字段），默认普通体检
        assert rows[0]["instance_scope"] is False

        full = store.get(rid)
        assert full is not None and full["report"]["engine"] == "mysql"
        assert len(full["report"]["checks"]) == 3
        assert store.get(99999) is None

    def test_list_is_scoped_per_connection_newest_first(self, tmp_path):
        store = CheckupRecordStore(tmp_path / "dbm.sqlite3")
        a = store.save("demo", "demo/a", _report(scope="a"))
        b = store.save("demo", "demo/a", _report(scope="b", overall="ok"))
        store.save("demo", "demo/b", _report(scope="c"))
        rows = store.list("demo", "demo/a")
        assert [r["id"] for r in rows] == [b, a]
        assert len(store.list("demo", "demo/b")) == 1

    def test_list_is_scoped_per_project(self, tmp_path):
        """两个项目都有叫 main 的连接时，历史不能串到一起。"""
        store = CheckupRecordStore(tmp_path / "dbm.sqlite3")
        store.save("demo", "main", _report(scope="demo_db"))
        store.save("prod", "main", _report(scope="prod_db"))
        assert [r["scope"] for r in store.list("demo", "main")] == ["demo_db"]
        assert [r["scope"] for r in store.list("prod", "main")] == ["prod_db"]

    def test_instance_scope_flag_is_stored(self, tmp_path):
        store = CheckupRecordStore(tmp_path / "dbm.sqlite3")
        store.save("demo", "demo/main", _report(), instance_scope=True)
        assert store.list("demo", "demo/main")[0]["instance_scope"] is True

    def test_delete_and_purge(self, tmp_path):
        store = CheckupRecordStore(tmp_path / "dbm.sqlite3")
        rid = store.save("demo", "demo/main", _report())
        assert store.delete(rid) is True
        assert store.delete(rid) is False
        assert store.list("demo", "demo/main") == []

        store.save("demo", "demo/main", _report())
        # 保留期按 created_at 算：刚存的不该被清，改成 2020 年后再清
        assert store.purge_older_than(1) == 0
        con = sqlite3.connect(tmp_path / "dbm.sqlite3")
        con.execute("UPDATE checkup_report SET created_at = '2020-01-01T00:00:00+00:00'")
        con.commit()
        con.close()
        assert store.purge_older_than(1) == 1
        assert store.list("demo", "demo/main") == []

    def test_old_db_gets_table_created(self, tmp_path):
        """老库（没有 checkup_report 表）打开时自动建表，不报错。"""
        db = tmp_path / "old.sqlite3"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE unrelated (x INTEGER)")
        con.commit()
        con.close()
        store = CheckupRecordStore(db)
        assert store.save("demo", "demo/main", _report()) > 0
        assert len(store.list("demo", "demo/main")) == 1


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    sqlite3.connect(db_file).close()
    # 两个项目各有一条同名连接 main：记录必须按「项目 + 连接」隔离
    cfg = AppConfig.model_validate({"projects": {
        "demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "local"}}},
        "other": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "local"}}},
    }})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     ApprovalStore(tmp_path / "a.sqlite3"))
    svc.checkup_records = CheckupRecordStore(tmp_path / "dbm.sqlite3")
    mcp = build_mcp(svc)
    mount_admin(mcp, svc, admin_token=TOKEN)
    with TestClient(mcp.http_app()) as tc:
        tc.post("/admin/login", data={"token": TOKEN})
        yield tc, svc
    svc.close()


class TestRoutes:
    def test_checkup_saves_record_and_routes_serve_it(self, client):
        tc, svc = client
        r = tc.get("/admin/sql/checkup", params={"conn": "demo/main"})
        assert r.status_code == 200 and r.json()["ok"] is True

        listed = tc.get("/admin/sql/checkup/records", params={"conn": "demo/main"})
        body = listed.json()
        assert body["ok"] is True
        assert len(body["records"]) == 1
        rec = body["records"][0]
        assert rec["engine"] == "sqlite"

        one = tc.get(f"/admin/sql/checkup/records/{rec['id']}", params={"conn": "demo/main"})
        assert one.json()["record"]["report"]["checks"]

        gone = tc.post(f"/admin/sql/checkup/records/{rec['id']}/delete",
                       data={"conn": "demo/main"})
        assert gone.json()["ok"] is True
        assert tc.get("/admin/sql/checkup/records",
                      params={"conn": "demo/main"}).json()["records"] == []
        # 再删一次 → 404
        assert tc.post(f"/admin/sql/checkup/records/{rec['id']}/delete",
                       data={"conn": "demo/main"}).status_code == 404

    def test_record_belongs_to_its_connection(self, client):
        """id 是全局自增的：不带 conn / 带别的项目的同名连接，都不能读或删这条记录。"""
        tc, _ = client
        tc.get("/admin/sql/checkup", params={"conn": "demo/main"})
        rec = tc.get("/admin/sql/checkup/records",
                     params={"conn": "demo/main"}).json()["records"][0]
        rid = rec["id"]

        # 不带 conn：拒绝（否则任何人凭 id 就能读）
        assert tc.get(f"/admin/sql/checkup/records/{rid}").status_code == 400
        # 别的项目的同名连接：看不到、也删不掉
        assert tc.get(f"/admin/sql/checkup/records/{rid}",
                      params={"conn": "other/main"}).status_code == 404
        assert tc.post(f"/admin/sql/checkup/records/{rid}/delete",
                       data={"conn": "other/main"}).status_code == 404
        # 记录还在
        assert len(tc.get("/admin/sql/checkup/records",
                          params={"conn": "demo/main"}).json()["records"]) == 1

    def test_missing_record_is_404(self, client):
        tc, _ = client
        assert tc.get("/admin/sql/checkup/records/424242",
                      params={"conn": "demo/main"}).status_code == 404

    def test_store_absent_does_not_break_checkup(self, client):
        """未注入存储（单测/库内直用）时体检照常返回，只是不落历史。"""
        tc, svc = client
        svc.checkup_records = None
        assert tc.get("/admin/sql/checkup", params={"conn": "demo/main"}).json()["ok"] is True
        assert tc.get("/admin/sql/checkup/records",
                      params={"conn": "demo/main"}).json()["records"] == []
