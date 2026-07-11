"""服务层集成测试：用 SQLite 连接跑通 查询 / 拒绝 / schema / 审计 全链路。"""

import sqlite3

import pytest

from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.service import CallerInfo, DbmService, QueryRejected

CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-1")


@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER);
        CREATE INDEX idx_users_name ON users (name);
        INSERT INTO users (name, age) VALUES ('alice', 30), ('bob', 25), ('carol', NULL);
        """
    )
    conn.commit()
    conn.close()

    cfg = AppConfig.model_validate(
        {
            "projects": {
                "demo": {
                    "connections": {
                        "main": {
                            "engine": "sqlite",
                            "database": str(db_file),
                            "environment": "local",
                            "policy": {"max_rows": 2},
                        }
                    }
                }
            }
        }
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"))
    yield svc
    svc.close()


class TestQuery:
    def test_select_ok_and_audited(self, service):
        result = service.query("demo", "main", "SELECT id, name FROM users ORDER BY id", CALLER)
        assert result["columns"] == ["id", "name"]
        assert result["row_count"] == 2  # max_rows=2 截断
        assert result["truncated"] is True

        logs = service.store.recent()
        assert logs[0]["status"] == "ok"
        assert logs[0]["agent"] == "pytest/1.0"
        assert logs[0]["row_count"] == 2
        assert logs[0]["fingerprint"]

    def test_write_rejected_and_audited(self, service):
        with pytest.raises(QueryRejected, match="已拒绝"):
            service.query("demo", "main", "DELETE FROM users", CALLER)

        logs = service.store.recent()
        assert logs[0]["status"] == "rejected"
        assert logs[0]["sql"] == "DELETE FROM users"
        # 数据未被删
        result = service.query("demo", "main", "SELECT count(*) AS c FROM users", CALLER)
        assert result["rows"][0][0] == 3

    def test_readonly_enforced_at_db_layer(self, service):
        """即使绕过分类器直接执行写 SQL，数据库层 PRAGMA query_only 也会拒绝。"""
        from dbmcp import engines

        cfg = service.config.get_connection("demo", "main")
        engine = service.pool.get("demo", "main", cfg)
        with pytest.raises(Exception, match="query_only|readonly|attempt to write"):
            engines.run_query(engine, "DELETE FROM users", max_rows=10)

    def test_query_error_audited(self, service):
        with pytest.raises(Exception):
            service.query("demo", "main", "SELECT * FROM no_such_table", CALLER)
        assert service.store.recent()[0]["status"] == "error"


class TestSchemaTools:
    def test_list_tables(self, service):
        assert service.list_tables("demo", "main", CALLER) == ["users"]

    def test_describe_table(self, service):
        info = service.describe_table("demo", "main", "users", CALLER)
        names = [c["name"] for c in info["columns"]]
        assert names == ["id", "name", "age"]
        assert info["primary_key"] == ["id"] or info["primary_key"] == []  # sqlite 方言差异
        assert any(i["name"] == "idx_users_name" for i in info["indexes"])

    def test_describe_missing_table(self, service):
        with pytest.raises(ValueError, match="不存在"):
            service.describe_table("demo", "main", "nope", CALLER)

    def test_sample_rows_table_name_not_injectable(self, service):
        with pytest.raises(ValueError, match="不存在"):
            service.sample_rows("demo", "main", "users; DROP TABLE users", 5, CALLER)

    def test_sample_rows(self, service):
        result = service.sample_rows("demo", "main", "users", 5, CALLER)
        # limit 受连接策略 max_rows=2 约束
        assert result["row_count"] == 2


class TestMeta:
    def test_list_projects_and_connections(self, service):
        assert service.list_projects() == [{"project": "demo", "connections": ["main"]}]
        conns = service.list_connections("demo")
        assert conns[0]["connection"] == "main"
        assert "password" not in conns[0] and "user" not in conns[0]

    def test_test_connection(self, service):
        assert service.test_connection("demo", "main", CALLER)["ok"] is True


class TestAdminConsole:
    """后台查询台入口：读直跑 / 写二次确认 / 确认后执行 / 导出。"""

    def test_read_returns_rows(self, service):
        out = service.admin_run_sql("demo", "main", "SELECT id, name FROM users ORDER BY id", CALLER)
        assert out["kind"] == "read"
        assert out["columns"] == ["id", "name"]

    def test_write_without_confirm_returns_risk_not_executed(self, service):
        out = service.admin_run_sql("demo", "main", "DELETE FROM users WHERE name='bob'", CALLER)
        assert out["kind"] == "confirm"
        assert "risk" in out and "level" in out["risk"]
        # 未执行：bob 还在
        res = service.query("demo", "main", "SELECT count(*) AS c FROM users", CALLER)
        assert res["rows"][0][0] == 3

    def test_write_with_confirm_executes(self, service):
        out = service.admin_run_sql(
            "demo", "main", "DELETE FROM users WHERE name='bob'", CALLER, confirm=True
        )
        assert out["kind"] == "write"
        assert out["affected_rows"] == 1
        # 落审计：admin_execute（写操作后立即检查，最新一条即是）
        assert service.store.recent()[0]["tool"] == "admin_execute"
        res = service.query("demo", "main", "SELECT count(*) AS c FROM users", CALLER)
        assert res["rows"][0][0] == 2

    def test_export_read_result_as_csv(self, service):
        data, media_type, ext = service.admin_export(
            "demo", "main", "SELECT id, name FROM users ORDER BY id", "csv", CALLER
        )
        assert ext == "csv" and data.startswith(b"\xef\xbb\xbf")
        assert b"id,name" in data

    def test_export_rejects_write_sql(self, service):
        with pytest.raises(QueryRejected, match="只读"):
            service.admin_export("demo", "main", "DELETE FROM users", "csv", CALLER)

    def test_pagination_auto_limit_and_next(self, service):
        # 连接 max_rows=2，users 有 3 行 → 每页 2 行、有下一页
        out = service.admin_run_sql("demo", "main", "SELECT id FROM users ORDER BY id", CALLER)
        assert out["paginated"] is True and out["page"] == 0 and out["page_size"] == 2
        assert out["has_next"] is True and len(out["rows"]) == 2
        # 第 2 页：剩 1 行、无下一页
        out2 = service.admin_run_sql("demo", "main", "SELECT id FROM users ORDER BY id",
                                     CALLER, page=1)
        assert out2["page"] == 1 and out2["has_next"] is False and len(out2["rows"]) == 1

    def test_user_limit_respected_not_paginated(self, service):
        out = service.admin_run_sql("demo", "main", "SELECT id FROM users LIMIT 5", CALLER)
        assert out["paginated"] is False


class TestNoDatabaseSchema:
    """未绑定默认库的连接：不带 schema 列表要给清晰报错（而非 SQLAlchemy 反射崩溃）。"""

    def test_list_tables_no_database_mysql_raises_clean(self, tmp_path):
        from dbmcp.audit.log import AuditStore
        from dbmcp.config import AppConfig
        cfg = AppConfig.model_validate({"projects": {"p": {"connections": {
            "nodb": {"engine": "mysql", "host": "h", "user": "u", "password": "plain://x",
                     "environment": "dev"}}}}})
        svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"))
        # 守卫在建连之前触发，无需真实连接
        with pytest.raises(ValueError, match="未绑定默认库"):
            svc.list_tables("p", "nodb", CALLER)
        svc.close()

    def test_list_databases_sqlite_returns_empty(self, service):
        assert service.list_databases("demo", "main", CALLER) == []


class TestNoDatabaseHint:
    def test_no_database_error_detection(self):
        from dbmcp.service import _is_no_database_error
        assert _is_no_database_error(Exception("(1046, 'No database selected')"))
        assert _is_no_database_error(Exception("no schema has been selected to create in"))
        assert not _is_no_database_error(Exception("table not found"))

    def test_list_connections_note_when_no_database(self, tmp_path):
        from dbmcp.audit.log import AuditStore
        from dbmcp.config import AppConfig
        cfg = AppConfig.model_validate({"projects": {"p": {"connections": {
            "nodb": {"engine": "mysql", "host": "h", "user": "u", "password": "plain://x",
                     "environment": "dev"},
            "withdb": {"engine": "mysql", "host": "h", "database": "app", "user": "u",
                       "password": "plain://x", "environment": "dev"},
        }}}})
        svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"))
        conns = {c["connection"]: c for c in svc.list_connections("p")}
        assert "note" in conns["nodb"] and "全限定" in conns["nodb"]["note"]
        assert "note" not in conns["withdb"]
        svc.close()
