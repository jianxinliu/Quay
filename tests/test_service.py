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
    svc.data_dir = str(tmp_path / "data")
    yield svc
    svc.close()


class TestRuntimeSettings:
    """并发/连接池设置热生效：save_settings 同步引擎池大小、变更时回收旧引擎。"""

    def _svc(self, service):
        from dbmcp.settings import SettingsStore
        service.settings = SettingsStore(":memory:")
        return service

    def test_apply_sets_engine_pool_size(self, service):
        svc = self._svc(service)
        svc.save_settings({"engine_pool_size": "42"})
        assert svc.pool.engine_pool_size == 42

    def test_pool_size_change_disposes_pool(self, service, monkeypatch):
        svc = self._svc(service)
        calls = []
        monkeypatch.setattr(svc.pool, "dispose", lambda: calls.append(1))
        svc.save_settings({"engine_pool_size": "30"})  # 变了 → 回收
        assert calls == [1]
        svc.save_settings({"sql_page_size": "50"})     # 池大小没变 → 不回收
        assert calls == [1]

    def test_clamped_to_bounds(self, service):
        svc = self._svc(service)
        svc.save_settings({"engine_pool_size": "9999", "mcp_max_concurrency": "1"})
        s = svc.get_settings()
        assert s["engine_pool_size"] == 100    # 上界夹取
        assert s["mcp_max_concurrency"] == 10  # 下界夹取


class TestReconnect:
    """人工重连：绕过健康位强制重建连接（供查询台「重连数据库」用）。"""

    def test_reconnect_healthy_returns_ok(self, service):
        out = service.reconnect_connection("demo", "main", CALLER)
        assert out == {"ok": True, "engine": "sqlite"}

    def test_reconnect_clears_exhausted(self, service):
        # 模拟连接已被判定 exhausted（后台重连线程已放弃）
        from dbmcp.health import Health

        service.health._entries[("demo", "main")] = Health(
            state="exhausted", fail_count=5, last_error="lost connection"
        )
        # test_connection 走健康位，exhausted 时会被直接挡下、根本连不上
        with pytest.raises(Exception):
            service.test_connection("demo", "main", CALLER)
        # reconnect 绕过健康位强制重建，成功后健康位清空、连接恢复可用
        out = service.reconnect_connection("demo", "main", CALLER)
        assert out["ok"] is True
        assert service.health.get("demo", "main") is None
        service.test_connection("demo", "main", CALLER)  # 现在可用了

    def test_reconnect_probe_failure_reenters_backoff(self, service, monkeypatch):
        """探测仍失败：原样抛出，并重新进入后台退避重连（unavailable）。"""
        def boom(_p, _c):
            raise ConnectionRefusedError("Connection refused")

        monkeypatch.setattr(service, "_health_probe", boom)
        with pytest.raises(ConnectionRefusedError):
            service.reconnect_connection("demo", "main", CALLER)
        h = service.health.get("demo", "main")
        assert h is not None and h.state == "unavailable"
        assert "Connection refused" in h.last_error
        service.health.stop()  # 收尾后台重连线程

    def test_reconnect_unknown_connection_raises(self, service):
        with pytest.raises(KeyError):
            service.reconnect_connection("demo", "nope", CALLER)


class TestProbeFields:
    def test_redis_no_auth_not_blocked_by_password_guard(self, service):
        """无认证 Redis：不填密码不应被『请填写密码』挡住，而是真正去连。"""
        res = service.probe_connection_fields(
            {"engine": "redis", "host": "127.0.0.1", "port": 1, "environment": "local"}
        )
        # 连不上（端口 1）是预期的；关键是没被密码守卫短路
        assert res.ok is False
        assert "请填写密码" not in res.message
        assert "连接失败" in res.message

    def test_mysql_still_requires_password(self, service):
        res = service.probe_connection_fields(
            {"engine": "mysql", "host": "127.0.0.1", "port": 3306, "user": "root"}
        )
        assert res.ok is False
        assert "请填写密码" in res.message


class TestRedisHiddenFromMcp:
    def _svc(self, tmp_path):
        cfg = AppConfig.model_validate({"projects": {
            "p": {"connections": {
                "sql": {"engine": "sqlite", "database": ":memory:"},
                "cache": {"engine": "redis", "host": "127.0.0.1"},
            }},
            "redisonly": {"connections": {
                "r": {"engine": "redis", "host": "127.0.0.1"},
            }},
        }})
        return DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"))

    def test_list_connections_excludes_redis(self, tmp_path):
        svc = self._svc(tmp_path)
        names = [c["connection"] for c in svc.list_connections("p")]
        assert names == ["sql"]  # redis 连接 cache 被过滤
        svc.close()

    def test_list_projects_drops_redis_only_project(self, tmp_path):
        svc = self._svc(tmp_path)
        projs = {p["project"]: p["connections"] for p in svc.list_projects()}
        assert projs == {"p": ["sql"]}  # redisonly 只有 redis 连接 → 整个项目不出现
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

    def test_bigint_serialized_as_string_with_number_type(self, service):
        # 雪花 ID 超 2^53：值以字符串返回保精度，列类型仍标 number（前端图标显 #、编辑 WHERE 精确）
        out = service.query("demo", "main", "SELECT 1726946581640544256 AS id", CALLER)
        assert out["rows"][0][0] == "1726946581640544256"
        assert out["column_types"][0] == "number"

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

    def test_describe_table_qualified_name_splits_schema(self, service):
        # 「库.表」限定名应拆出 schema，不整体当表名（sqlite 下 schema 无效但不应崩）
        info = service.describe_table("demo", "main", "main.users", CALLER)
        assert [c["name"] for c in info["columns"]] == ["id", "name", "age"]

    def test_sample_rows_table_name_not_injectable(self, service):
        with pytest.raises(ValueError, match="不存在"):
            service.sample_rows("demo", "main", "users; DROP TABLE users", 5, CALLER)

    def test_sample_rows(self, service):
        result = service.sample_rows("demo", "main", "users", 5, CALLER)
        # limit 受连接策略 max_rows=2 约束
        assert result["row_count"] == 2

    def test_export_table_selects_fields_limit_and_format(self, service):
        summary = service.export_table(
            "demo", "main", "users", ["name", "age"], 2, "csv", CALLER
        )
        assert summary["media_type"] == "text/csv; charset=utf-8"
        assert summary["filename"].endswith(".csv")
        assert summary["fields"] == ["name", "age"]
        assert summary["row_count"] == 2
        assert summary["requested_limit"] == 2
        token, filename = summary["download_url"].rsplit("/", 2)[-2:]
        path = service.resolve_mcp_export(token, filename)
        assert path is not None
        assert path.read_bytes().startswith(b"\xef\xbb\xbfname,age")

    def test_export_table_rejects_unknown_field(self, service):
        with pytest.raises(ValueError, match="字段不存在"):
            service.export_table(
                "demo", "main", "users", ["name", "password"], 2, "json", CALLER
            )

    @pytest.mark.parametrize("fmt", ["json", "xlsx"])
    def test_export_table_structured_preview(self, service, fmt):
        summary = service.export_table(
            "demo", "main", "users", ["id", "name"], 2, fmt, CALLER
        )
        preview = service.preview_mcp_export(summary["token"])
        assert preview is not None
        assert preview["columns"] == ["id", "name"]
        assert len(preview["rows"]) == 2

    def test_export_table_markdown_text_preview(self, service):
        summary = service.export_table(
            "demo", "main", "users", ["id", "name"], 2, "markdown", CALLER
        )
        preview = service.preview_mcp_export(summary["token"])
        assert preview is not None
        assert "| id | name |" in preview["raw"]

    def test_export_table_respects_connection_row_limit(self, service):
        with pytest.raises(ValueError, match="连接策略上限"):
            service.export_table(
                "demo", "main", "users", None, 3, "json", CALLER
            )

    def test_export_table_name_not_injectable(self, service):
        with pytest.raises(ValueError, match="不存在"):
            service.export_table(
                "demo", "main", "users; DROP TABLE users", None, 1, "csv", CALLER
            )

    def test_export_download_rejects_bad_token_and_traversal(self, service):
        assert service.resolve_mcp_export("not-a-token", "x.csv") is None
        assert service.resolve_mcp_export("a" * 48, "../audit.sqlite3") is None


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

    def test_syntax_error_returns_error_not_write_confirm(self, service):
        """语法错误 SQL 应报'语法错误'，而非被当写操作弹'确认执行'卡片。"""
        out = service.admin_run_sql("demo", "main", "SELECT 'unterminated", CALLER)
        assert out["kind"] == "error"
        assert "语法" in out["error"]
        # 不是 confirm/write
        assert out["kind"] not in ("confirm", "write")

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

    def test_multi_statement_write_splits_and_executes(self, service):
        # 多语句批量：run_write 按分号拆开逐条执行（单条 execute 不支持多语句）
        out = service.admin_run_sql(
            "demo", "main",
            "INSERT INTO users (name, age) VALUES ('dave', 40); "
            "UPDATE users SET age = 41 WHERE name = 'dave'",
            CALLER, confirm=True,
        )
        assert out["kind"] == "write"
        assert out["affected_rows"] == 2  # 1 插入 + 1 更新，累加
        res = service.query("demo", "main", "SELECT age FROM users WHERE name='dave'", CALLER)
        assert res["rows"][0][0] == 41

    def test_multi_statement_without_confirm_returns_batch_risk(self, service):
        out = service.admin_run_sql(
            "demo", "main",
            "UPDATE users SET age=1 WHERE name='alice'; DELETE FROM users WHERE name='bob'",
            CALLER,
        )
        assert out["kind"] == "confirm"
        assert out["risk"]["statement_kind"] == "MultiStatement"
        # 未执行：alice 年龄未变、bob 还在
        res = service.query("demo", "main", "SELECT count(*) AS c FROM users", CALLER)
        assert res["rows"][0][0] == 3

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


class TestConsoleUnmasked:
    """已认证的后台查询台/导出要看真实数据，不脱敏；agent 路径仍脱敏（密码不落工具返回值）。"""

    @pytest.fixture
    def svc(self, tmp_path):
        db_file = tmp_path / "biz.sqlite3"
        conn = sqlite3.connect(db_file)
        conn.executescript(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, password TEXT);"
            "INSERT INTO accounts (name, password) VALUES ('alice', 's3cr3t');"
        )
        conn.commit()
        conn.close()
        cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "local"}}}}})
        s = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"))
        s.data_dir = str(tmp_path / "data")
        yield s
        s.close()

    def test_console_read_not_masked(self, svc):
        out = svc.admin_run_sql("demo", "main", "SELECT name, password FROM accounts", CALLER)
        assert out["rows"][0] == ["alice", "s3cr3t"]        # 真实密码，未脱敏
        assert "masked_columns" not in out

    def test_console_export_not_masked(self, svc):
        data, _, _ = svc.admin_export("demo", "main", "SELECT name, password FROM accounts", "csv", CALLER)
        assert b"s3cr3t" in data

    def test_agent_table_export_is_masked(self, svc):
        summary = svc.export_table(
            "demo", "main", "accounts", ["name", "password"], 10, "csv", CALLER
        )
        token, filename = summary["download_url"].rsplit("/", 2)[-2:]
        data = svc.resolve_mcp_export(token, filename).read_bytes()
        assert b"s3cr3t" not in data
        assert b"***MASKED***" in data
        assert summary["masked_columns"] == ["password"]

    def test_agent_query_still_masked(self, svc):
        out = svc.query("demo", "main", "SELECT name, password FROM accounts", CALLER)
        assert out["rows"][0][1] == "***MASKED***"          # agent 仍脱敏
        assert "password" in out.get("masked_columns", [])


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


class TestAiGenerateSql:
    """AI 生成 SQL：门禁、首轮收集 DDL、追问续接、审计（用假 ai.generate_sql，不真调 CLI）。"""

    def _enable_ai(self, service, **over):
        from dbmcp.settings import SettingsStore
        service.settings = SettingsStore(":memory:")
        service.save_settings({"ai_enabled": "true", **over})

    def test_disabled_rejects(self, service):
        self._enable_ai(service, ai_enabled="false")  # 默认已改为开，显式关来测门禁
        with pytest.raises(QueryRejected, match="未开启"):
            service.ai_generate_sql("demo", "main", "统计用户数", CALLER)

    def test_empty_question_rejects(self, service):
        self._enable_ai(service)
        with pytest.raises(QueryRejected, match="想查什么"):
            service.ai_generate_sql("demo", "main", "  ", CALLER)

    def test_first_turn_gathers_ddl_and_audits(self, service, monkeypatch):
        from dbmcp import ai
        self._enable_ai(service)
        captured = {}

        def fake_gen(**kw):
            captured.update(kw)
            return ai.AIResult(sql="SELECT count(*) FROM users", explanation="走全表",
                               session_id="sid-1")
        monkeypatch.setattr(ai, "generate_sql", fake_gen)
        out = service.ai_generate_sql("demo", "main", "统计用户数", CALLER,
                                      tables=["users"], explain=True)
        # 首轮把 users 的建表语句发给了 AI
        ddl_names = [n for n, _ in captured["ddls"]]
        assert "users" in ddl_names
        assert "CREATE TABLE" in captured["ddls"][0][1]
        assert captured["session_id"] is None
        assert out == {"sql": "SELECT count(*) FROM users",
                       "explanation": "走全表", "session_id": "sid-1"}
        # 落审计
        recs = service.store.recent(filters={"tool": "ai_generate_sql"})
        assert recs and recs[0]["status"] == "ok"

    def test_followup_skips_ddl_and_passes_session(self, service, monkeypatch):
        from dbmcp import ai
        self._enable_ai(service)
        captured = {}

        def fake_gen(**kw):
            captured.update(kw)
            return ai.AIResult(sql="SELECT count(*) FROM users WHERE age>20",
                               explanation="", session_id="sid-1")
        monkeypatch.setattr(ai, "generate_sql", fake_gen)
        service.ai_generate_sql("demo", "main", "只看成年人", CALLER, session_id="sid-1")
        assert captured["ddls"] == []          # 追问未新增表时不重发表结构
        assert captured["session_id"] == "sid-1"
        recs = service.store.recent(filters={"tool": "ai_followup_sql"})
        assert recs and recs[0]["status"] == "ok"

    def test_followup_with_new_tables_sends_their_ddl(self, service, monkeypatch):
        from dbmcp import ai
        self._enable_ai(service)
        captured = {}

        def fake_gen(**kw):
            captured.update(kw)
            return ai.AIResult(sql="SELECT 1", explanation="", session_id="sid-1")
        monkeypatch.setattr(ai, "generate_sql", fake_gen)
        # 追问时新增了 users 表：只补发这张新表的建表语句
        service.ai_generate_sql("demo", "main", "关联用户表", CALLER,
                                tables=["users"], session_id="sid-1")
        ddl_names = [n for n, _ in captured["ddls"]]
        assert ddl_names == ["users"]
        assert "CREATE TABLE" in captured["ddls"][0][1]
        assert captured["session_id"] == "sid-1"

    def test_too_many_tables_rejects_before_ddl(self, service, monkeypatch):
        from dbmcp import ai
        self._enable_ai(service, ai_max_tables="1")
        monkeypatch.setattr(ai, "generate_sql",
                            lambda **kw: pytest.fail("不该调到 AI"))
        with pytest.raises(QueryRejected, match="超过上限"):
            service.ai_generate_sql("demo", "main", "q", CALLER,
                                    tables=["users", "another"])

    def test_ai_error_becomes_query_rejected(self, service, monkeypatch):
        from dbmcp import ai
        self._enable_ai(service)

        def boom(**kw):
            raise ai.AIError("claude 返回错误：403")
        monkeypatch.setattr(ai, "generate_sql", boom)
        with pytest.raises(QueryRejected, match="403"):
            service.ai_generate_sql("demo", "main", "q", CALLER, tables=["users"])


class TestAiGenerateWorkflow:
    """AI 生成 workflow：门禁、compile 校验、编译失败重修一次、审计、拓扑布局。"""

    def _enable_ai(self, service, **over):
        from dbmcp.settings import SettingsStore
        service.settings = SettingsStore(":memory:")
        service.save_settings({"ai_enabled": "true", **over})

    _GOOD = {"nodes": [
        {"id": "a", "type": "source", "name": "users_src",
         "cfg": {"conn": "demo/main", "sql": "SELECT id, name FROM users"}},
        {"id": "b", "type": "output", "name": "result", "cfg": {"limit": 10}}],
        "edges": [{"from": "a", "to": "b", "port": "in"}]}
    _BAD = {"nodes": [{"id": "a", "type": "source", "name": "bad name",
                       "cfg": {"conn": "demo/main", "sql": "SELECT 1"}}], "edges": []}

    def test_disabled_rejects(self, service):
        self._enable_ai(service, ai_enabled="false")  # 默认已改为开，显式关来测门禁
        with pytest.raises(QueryRejected, match="未开启"):
            service.ai_generate_workflow("demo", "main", "聚合分析", CALLER)

    def test_happy_path_compiles_and_layouts(self, service, monkeypatch):
        from dbmcp import ai
        self._enable_ai(service)
        monkeypatch.setattr(ai, "generate_workflow",
                            lambda **kw: (dict(self._GOOD), "sid-1"))
        out = service.ai_generate_workflow("demo", "main", "把用户输出前10", CALLER,
                                           tables=["users"])
        g = out["graph"]
        assert [n["name"] for n in g["nodes"]] == ["users_src", "result"]
        # 拓扑布局赋了坐标：source 在第 0 层、output 在第 1 层
        xs = {n["name"]: n["x"] for n in g["nodes"]}
        assert xs["users_src"] < xs["result"]
        recs = service.store.recent(filters={"tool": "ai_generate_workflow"})
        assert recs and recs[0]["status"] == "ok"

    def test_repair_once_on_compile_error(self, service, monkeypatch):
        from dbmcp import ai
        self._enable_ai(service)
        calls = {"n": 0}

        def fake(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                assert kw.get("repair_error") is None
                return dict(self._BAD), "sid-1"      # 首轮：非法节点名 → compile 失败
            assert kw.get("repair_error") and kw.get("session_id") == "sid-1"
            return dict(self._GOOD), "sid-1"         # 重修：合法
        monkeypatch.setattr(ai, "generate_workflow", fake)
        out = service.ai_generate_workflow("demo", "main", "q", CALLER, tables=["users"])
        assert calls["n"] == 2                        # 恰好重修一次
        assert any(n["name"] == "result" for n in out["graph"]["nodes"])

    def test_repair_still_invalid_rejects(self, service, monkeypatch):
        from dbmcp import ai
        self._enable_ai(service)
        monkeypatch.setattr(ai, "generate_workflow",
                            lambda **kw: (dict(self._BAD), "sid-1"))  # 两次都非法
        with pytest.raises(QueryRejected, match="仍不合法"):
            service.ai_generate_workflow("demo", "main", "q", CALLER, tables=["users"])

    def test_schema_passed_to_ai_and_patched_when_missing(self, service, monkeypatch):
        """用户传了 schema：应传给 ai.generate_workflow；AI 漏写 cfg.schema 时兜底填。"""
        from dbmcp import ai, engines
        self._enable_ai(service)
        # sqlite fixture 里没有 "mydb" 库，跳过 DDL 获取（只是为了拼 prompt 用）
        monkeypatch.setattr(engines, "get_table_ddl",
                            lambda *a, **k: "CREATE TABLE users(id INT)")
        captured = {}
        # AI 输出的 source 节点故意不带 schema → 兜底应补上
        graph_missing_schema = {"nodes": [
            {"id": "a", "type": "source", "name": "users_src",
             "cfg": {"conn": "demo/main", "sql": "SELECT id FROM users"}},
            {"id": "b", "type": "output", "name": "result", "cfg": {"limit": 10}}],
            "edges": [{"from": "a", "to": "b", "port": "in"}]}

        def fake(**kw):
            captured.update(kw)
            return dict(graph_missing_schema), "sid-1"
        monkeypatch.setattr(ai, "generate_workflow", fake)
        out = service.ai_generate_workflow("demo", "main", "q", CALLER,
                                            schema="mydb", tables=["users"])
        # schema 已传给 ai
        assert captured.get("schema") == "mydb"
        assert captured.get("default_conn") == "demo/main"
        # 兜底：source 节点 cfg.schema 被补成 mydb
        src = next(n for n in out["graph"]["nodes"] if n["type"] == "source")
        assert src["cfg"]["schema"] == "mydb"

    def test_current_graph_passed_to_ai(self, service, monkeypatch):
        """修改模式：current_graph 应传给 ai.generate_workflow 走修改分支。"""
        from dbmcp import ai, engines
        self._enable_ai(service)
        monkeypatch.setattr(engines, "get_table_ddl",
                            lambda *a, **k: "CREATE TABLE users(id INT)")
        cur = {"nodes": [
            {"id": "a", "type": "source", "name": "raw",
             "cfg": {"conn": "demo/main", "sql": "SELECT * FROM users"}}],
            "edges": []}
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return dict(self._GOOD), "sid-1"
        monkeypatch.setattr(ai, "generate_workflow", fake)
        service.ai_generate_workflow("demo", "main", "改一下", CALLER,
                                     tables=["users"], current_graph=cur)
        assert captured.get("current_graph") == cur

    def test_schema_patch_respects_ai_choice(self, service, monkeypatch):
        """AI 主动写了 cfg.schema 时兜底不应覆盖。"""
        from dbmcp import ai, engines
        self._enable_ai(service)
        monkeypatch.setattr(engines, "get_table_ddl",
                            lambda *a, **k: "CREATE TABLE users(id INT)")
        graph_with_schema = {"nodes": [
            {"id": "a", "type": "source", "name": "users_src",
             "cfg": {"conn": "demo/main", "schema": "ai_chose",
                     "sql": "SELECT id FROM users"}},
            {"id": "b", "type": "output", "name": "result", "cfg": {"limit": 10}}],
            "edges": [{"from": "a", "to": "b", "port": "in"}]}
        monkeypatch.setattr(ai, "generate_workflow",
                            lambda **kw: (dict(graph_with_schema), "sid-1"))
        out = service.ai_generate_workflow("demo", "main", "q", CALLER,
                                            schema="user_chose", tables=["users"])
        src = next(n for n in out["graph"]["nodes"] if n["type"] == "source")
        assert src["cfg"]["schema"] == "ai_chose"   # AI 的选择保留


class _RecordingNotifier:
    """内存 notifier：把 send 参数记录下来，供断言。"""

    def __init__(self):
        self.events: list[dict] = []

    def send(self, title, body, meta=None):
        self.events.append({"title": title, "body": body, "meta": meta or {}})


def _make_service_with_notifier(tmp_path, *, engine="sqlite", db_file=None):
    """构造带 RecordingNotifier 的 service。"""
    if db_file is None:
        db_file = tmp_path / "biz.sqlite3"
        conn = sqlite3.connect(db_file)
        conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
                           "INSERT INTO users (name) VALUES ('alice'), ('bob');")
        conn.commit()
        conn.close()
    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {"main": {
        "engine": engine, "database": str(db_file), "environment": "dev",
        "writer": {"user": "x", "password": "plain://unused"},
    }}}}})
    from dbmcp.approvals import ApprovalStore
    notifier = _RecordingNotifier()
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"),
                     ApprovalStore(tmp_path / "audit.sqlite3"), notifier=notifier)
    return svc, notifier


class TestHealthIntegration:
    """连接级异常自动打标 + 后续入口直接抛 ConnectionUnavailable。"""

    def test_conn_error_marks_unavailable_and_next_call_rejected(self, tmp_path, monkeypatch):
        svc, _ = _make_service_with_notifier(tmp_path)
        # 正常一次 → 健康位应为 ok（或未记录）
        svc.query("demo", "main", "SELECT 1 AS x", CALLER)

        # 让底层 run_query 抛出「连接级」错误 → 应打标 unavailable
        from dbmcp import engines

        def _boom(engine, sql, max_rows, max_cell_chars=4096, on_start=None):
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr(engines, "run_query", _boom)
        with pytest.raises(ConnectionRefusedError):
            svc.query("demo", "main", "SELECT 1", CALLER)

        # 再问一次：入口就应抛 ConnectionUnavailable，压根不进 DB
        from dbmcp.health import ConnectionUnavailable
        with pytest.raises(ConnectionUnavailable) as ei:
            svc.query("demo", "main", "SELECT 1", CALLER)
        assert ei.value.state == "unavailable"
        assert ei.value.retry_after_s >= 0
        svc.close()

    def test_syntax_error_does_not_mark_unavailable(self, tmp_path):
        svc, _ = _make_service_with_notifier(tmp_path)
        # 语法错误 → OperationalError 之外的通用 exc，不打标
        with pytest.raises(Exception):
            svc.query("demo", "main", "SELECT * FROM no_such_table", CALLER)
        # 下一次不应被健康位挡住
        result = svc.query("demo", "main", "SELECT 1 AS x", CALLER)
        assert result["rows"][0][0] == 1
        svc.close()

    def test_connection_config_change_clears_health(self, tmp_path, monkeypatch):
        svc, _ = _make_service_with_notifier(tmp_path)
        from dbmcp import engines
        monkeypatch.setattr(engines, "run_query",
                            lambda *a, **kw: (_ for _ in ()).throw(ConnectionRefusedError("no")))
        with pytest.raises(ConnectionRefusedError):
            svc.query("demo", "main", "SELECT 1", CALLER)
        # 模拟"连接配置更新"路径清健康位
        svc.health.force_clear("demo", "main")
        monkeypatch.undo()
        # 再查应成功
        svc.query("demo", "main", "SELECT 1 AS x", CALLER)
        svc.close()


class TestApprovalNotify:
    def test_approval_created_fires_notification(self, tmp_path):
        svc, notifier = _make_service_with_notifier(tmp_path)
        r = svc.execute("demo", "main", "DELETE FROM users WHERE id=1", CALLER)
        assert r["status"] == "approval_required"
        # 有且仅有一条通知，内容含审批单号 + 项目/连接
        approval_events = [e for e in notifier.events
                           if e["meta"].get("kind") == "approval_created"]
        assert len(approval_events) == 1
        e = approval_events[0]
        assert f"#{r['change_id']}" in e["title"]
        assert "demo/main" in e["title"]
        assert e["meta"]["change_id"] == r["change_id"]
        assert e["meta"]["risk_level"]
        svc.close()


class TestExhaustedNotify:
    def test_exhausted_does_not_notify(self, tmp_path, caplog):
        """连接 exhausted 只落 warn 日志，不发通知——agent 侧已经收到明确的
        [connection_exhausted] ToolError 会告诉用户，再推通知反而是噪音（尤其
        网络抖动导致反复 exhausted 时会连发）。"""
        import logging
        svc, notifier = _make_service_with_notifier(tmp_path)
        with caplog.at_level(logging.WARNING, logger="dbmcp.service"):
            svc._on_connection_exhausted("demo", "main", "OperationalError: down")
        # 没往 notifier 发
        assert notifier.events == []
        # 但落了 warn 日志（方便运维排查）
        assert any("exhausted" in rec.getMessage() for rec in caplog.records)
        svc.close()
