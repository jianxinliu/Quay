"""表同步（sync_table）：纯函数 + 服务层审批闭环。

服务层用两个 SQLite 连接当源/目标（sqlite 的 writer 复用同一文件、无账号概念，
正好把「计划 → 审批 → 核销执行」这条链路完整跑通）。跨引擎 DDL 转写只能靠纯函数
+ 真机 e2e 验证，SQLite 单测测不出方言差异——同 CLAUDE.md 反复出现的那条教训。
"""

import sqlite3

import pytest

from dbmcp.approvals import KIND_SYNC, ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.service import CallerInfo, DbmService, QueryRejected
from dbmcp.settings import SettingsStore
from dbmcp.sync import (
    DATA_NONE,
    DATA_REPLACE,
    DDL_RECREATE,
    DDL_SKIP,
    SyncError,
    SyncSpec,
    assess_plan,
    build_select_sql,
    rewrite_ddl,
    spec_fingerprint,
    validate_spec,
)

CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-sync")

MYSQL_DDL = """CREATE TABLE `orders` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint NOT NULL DEFAULT '0',
  `amount` decimal(10,2) NOT NULL DEFAULT '0.00',
  `channel` varchar(32) COLLATE utf8mb4_general_ci DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_created` (`user_id`,`created_at`),
  KEY `idx_channel` (`channel`)
) ENGINE=InnoDB AUTO_INCREMENT=2001 DEFAULT CHARSET=utf8mb4 COMMENT='订单表'"""


def _spec(**over) -> SyncSpec:
    base = {
        "source_project": "demo", "source_connection": "prod", "source_table": "users",
        "target_project": "demo", "target_connection": "local", "target_table": "users",
    }
    base.update(over)
    return SyncSpec(**base)


class TestValidateSpec:
    def test_accepts_normal_spec(self):
        validate_spec(_spec())  # 不抛即通过

    @pytest.mark.parametrize("over", [
        {"ddl": "wipe"},
        {"data": "merge"},
        {"ddl": DDL_SKIP, "data": DATA_NONE},
        {"limit": 0},
        {"limit": 10_000_000},
    ])
    def test_rejects_bad_modes(self, over):
        with pytest.raises(SyncError):
            validate_spec(_spec(**over))

    @pytest.mark.parametrize("table", ['users"; DROP TABLE x --', "a.b", "has space", ""])
    def test_rejects_non_identifier_table(self, table):
        with pytest.raises(SyncError, match="不是合法标识符"):
            validate_spec(_spec(source_table=table))

    def test_rejects_self_sync(self):
        with pytest.raises(SyncError, match="同一张表"):
            validate_spec(_spec(target_connection="prod"))

    def test_same_table_name_on_other_connection_is_fine(self):
        validate_spec(_spec(target_connection="local"))


class TestFingerprint:
    def test_stable_for_same_spec(self):
        assert spec_fingerprint(_spec()) == spec_fingerprint(_spec())

    @pytest.mark.parametrize("over", [
        {"limit": 500}, {"where": "id > 1"}, {"data": DATA_REPLACE},
        {"target_table": "users_copy"}, {"order_by": "id DESC"},
    ])
    def test_changes_when_any_field_changes(self, over):
        assert spec_fingerprint(_spec()) != spec_fingerprint(_spec(**over))


class TestBuildSelect:
    def test_quotes_identifiers_per_dialect(self):
        sql = build_select_sql("mysql", "orders", ["id", "channel"], 10, database="shop")
        assert sql == "SELECT `id`, `channel` FROM `shop`.`orders` LIMIT 10"
        sql = build_select_sql("postgres", "orders", ["id"], 10)
        assert sql == 'SELECT "id" FROM "orders" LIMIT 10'

    def test_where_and_order_by(self):
        sql = build_select_sql("sqlite", "t", ["a"], 5, where=" a > 1 ", order_by=" a DESC ")
        assert sql == 'SELECT "a" FROM "t" WHERE a > 1 ORDER BY a DESC LIMIT 5'

    def test_no_columns_means_star(self):
        assert build_select_sql("sqlite", "t", [], 5) == 'SELECT * FROM "t" LIMIT 5'


class TestRewriteDdl:
    def test_same_engine_same_name_is_verbatim(self):
        ddl, warns = rewrite_ddl(MYSQL_DDL, "mysql", "mysql", "orders", "orders")
        assert ddl == MYSQL_DDL.strip()
        assert warns == []

    def test_same_engine_rename_keeps_dialect_properties(self):
        ddl, warns = rewrite_ddl(MYSQL_DDL, "mysql", "mysql", "orders", "orders_copy")
        assert "`orders_copy`" in ddl
        assert "ENGINE=InnoDB" in ddl          # 同引擎不剥表属性
        assert "INDEX `idx_channel`" in ddl    # 同引擎保留二级索引
        assert warns == []

    def test_cross_engine_strips_dialect_specifics(self):
        ddl, warns = rewrite_ddl(MYSQL_DDL, "mysql", "postgres", "orders", "orders")
        assert "ENGINE=InnoDB" not in ddl
        assert "AUTO_INCREMENT" not in ddl
        assert "COLLATE" not in ddl
        assert "ON UPDATE" not in ddl
        assert "UBIGINT" not in ddl            # 无符号类型降级成 BIGINT
        assert "BIGINT" in ddl
        assert 'PRIMARY KEY ("id")' in ddl
        assert "idx_channel" not in ddl        # 二级索引不跨引擎同步
        assert any("近似 DDL" in w for w in warns)
        assert any("二级索引" in w for w in warns)

    def test_cross_engine_result_is_executable_on_sqlite(self):
        """转写不是「看着像」就行——MySQL 的 `UNIQUE KEY 名字 (列)` 直译过来 SQLite 不认。"""
        ddl, _ = rewrite_ddl(MYSQL_DDL, "mysql", "sqlite", "orders", "orders_local")
        conn = sqlite3.connect(":memory:")
        conn.execute(ddl)   # 语法错会在这里抛
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders_local)")]
        assert cols == ["id", "user_id", "amount", "channel", "created_at"]

    def test_multi_statement_source_ddl_keeps_create_table(self):
        """SQLite 的建表原文里跟着 CREATE INDEX（get_table_ddl 拼的），不能让它把解析搞挂。"""
        src = 'CREATE TABLE "t" (a INTEGER);\n\nCREATE INDEX i ON "t" (a)'
        ddl, warns = rewrite_ddl(src, "sqlite", "sqlite", "t", "t2")
        assert ddl.startswith("CREATE TABLE")
        assert "t2" in ddl
        assert any("其它语句" in w for w in warns)

    def test_rejects_non_create_table(self):
        with pytest.raises(SyncError):
            rewrite_ddl("SELECT 1", "mysql", "postgres", "a", "b")

    def test_rejects_empty(self):
        with pytest.raises(SyncError):
            rewrite_ddl("   ", "mysql", "mysql", "a", "b")


class TestAssessPlan:
    def test_plain_copy_is_medium(self):
        assert assess_plan(_spec(), "local", "dev")["level"] == "MEDIUM"

    @pytest.mark.parametrize("over", [{"ddl": DDL_RECREATE}, {"data": DATA_REPLACE}])
    def test_destructive_modes_are_high(self, over):
        assert assess_plan(_spec(**over), "local", "dev")["level"] == "HIGH"

    def test_non_local_target_is_high(self):
        assert assess_plan(_spec(), "staging", "dev")["level"] == "HIGH"

    def test_prod_source_warns_about_data_copy(self):
        report = assess_plan(_spec(), "local", "prod")
        assert any("生产数据副本" in w for w in report["warnings"])


# ---------------------------------------------------------------- 服务层

@pytest.fixture
def service(tmp_path):
    """源库 prod（3 行 users）+ 目标库 local（空）；两者都是 sqlite。"""
    src_file = tmp_path / "src.sqlite3"
    conn = sqlite3.connect(src_file)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER);
        CREATE INDEX idx_users_name ON users (name);
        INSERT INTO users (id, name, age) VALUES (1,'alice',30),(2,'bob',25),(3,'carol',NULL);
        """
    )
    conn.commit()
    conn.close()

    dst_file = tmp_path / "dst.sqlite3"
    sqlite3.connect(dst_file).close()   # 空库

    cfg = AppConfig.model_validate({
        "projects": {"demo": {"connections": {
            "prod": {"engine": "sqlite", "database": str(src_file), "environment": "prod"},
            "local": {"engine": "sqlite", "database": str(dst_file), "environment": "local"},
            # 同一个目标文件再挂一条 staging 连接：local 目标免审批，审批闭环用它来测
            "staging": {"engine": "sqlite", "database": str(dst_file),
                        "environment": "staging"},
        }}}
    })
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"),
                     approvals=ApprovalStore(tmp_path / "approvals.sqlite3"))
    svc.settings = SettingsStore(":memory:")
    svc.data_dir = str(tmp_path / "data")
    yield svc
    svc.close()


def _dst_rows(service, table="users"):
    cfg = service.config.get_connection("demo", "local")
    conn = sqlite3.connect(cfg.database)
    try:
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    finally:
        conn.close()


def _staging_spec(**over) -> SyncSpec:
    """目标指向 staging 连接：非 local/dev，走完整审批闭环。"""
    over.setdefault("target_connection", "staging")
    return _spec(**over)


def _approve_and_run(service, submitted, spec):
    """模拟人在后台点「仅批准」，随后 agent 带 change_id 重提。"""
    cid = submitted["change_id"]
    service.approve_change(cid, decided_by="tester")
    return service.sync_table(spec, CALLER, change_id=cid)


class TestSyncHappyPath:
    def test_dry_run_returns_plan_without_approval(self, service):
        spec = _spec()
        out = service.sync_table(spec, CALLER, dry_run=True)
        assert out["status"] == "planned"
        assert out["target_exists"] is False
        assert out["columns"] == ["id", "name", "age"]
        assert "CREATE TABLE" in out["plan"]
        assert service.list_changes() == []   # 没占审批单

    def test_creates_table_and_copies_rows(self, service):
        # 目标是 local 连接 → 不需要审批，一次调用就落地
        result = service.sync_table(_spec(), CALLER, reason="本地复现 bug")
        assert result["status"] == "executed"
        assert result["auto_approved"] is True
        assert result["affected_rows"] == 3
        assert _dst_rows(service) == [(1, "alice", 30), (2, "bob", 25), (3, "carol", None)]
        assert [s["step"] for s in result["steps"]] == ["create", "copy"]

    def test_where_and_limit_narrow_the_copy(self, service):
        spec = _spec(where="age >= 25", order_by="id DESC", limit=1)
        result = service.sync_table(spec, CALLER)
        assert result["affected_rows"] == 1
        assert _dst_rows(service) == [(2, "bob", 25)]
        assert result["source_truncated"] is True     # 符合条件的还有更多
        assert "note" in result

    def test_target_table_can_be_renamed(self, service):
        result = service.sync_table(_spec(target_table="users_snapshot"), CALLER)
        assert result["affected_rows"] == 3
        assert len(_dst_rows(service, "users_snapshot")) == 3

    def test_ddl_only_creates_empty_table(self, service):
        result = service.sync_table(_spec(data=DATA_NONE), CALLER)
        assert result["affected_rows"] == 0
        assert _dst_rows(service) == []

    def test_replace_clears_target_before_writing(self, service):
        service.sync_table(_spec(), CALLER)
        # 目标表已有 3 行；再同步一次 append 会变 6 行（主键冲突这里不涉及，先删掉一行制造差异）
        result = service.sync_table(
            _spec(ddl=DDL_SKIP, data=DATA_REPLACE, where="id = 1"), CALLER)
        assert result["affected_rows"] == 1
        assert _dst_rows(service) == [(1, "alice", 30)]   # 其余两行被清掉

    def test_recreate_drops_and_rebuilds(self, service):
        service.sync_table(_spec(), CALLER)
        result = service.sync_table(_spec(ddl=DDL_RECREATE, where="id = 3"), CALLER)
        assert [s["step"] for s in result["steps"]] == ["drop", "create", "copy"]
        assert _dst_rows(service) == [(3, "carol", None)]


class TestSyncGuards:
    def test_rejects_prod_target(self, service):
        spec = _spec(source_connection="local", source_table="users",
                     target_connection="prod", target_table="users_copy")
        with pytest.raises(QueryRejected, match="拒绝向生产环境"):
            service.sync_table(spec, CALLER, dry_run=True)

    def test_ddl_skip_needs_existing_target(self, service):
        with pytest.raises(QueryRejected, match="不存在"):
            service.sync_table(_spec(ddl=DDL_SKIP), CALLER, dry_run=True)

    def test_unknown_connection_is_key_error(self, service):
        with pytest.raises(KeyError):
            service.sync_table(_spec(target_connection="nope"), CALLER, dry_run=True)

    def test_where_cannot_smuggle_a_write(self, service):
        """WHERE 是自由文本：拼出来的整条 SQL 必须仍被 classify 判为只读。"""
        with pytest.raises(QueryRejected, match="非只读"):
            service.sync_table(_spec(where="1=1; DROP TABLE users"), CALLER)
        assert _dst_rows(service) == []   # 目标表已建但没被写坏

    def test_limit_clamped_to_setting(self, service):
        service.save_settings({"sync_max_rows": "2"})
        result = service.sync_table(_spec(limit=1000), CALLER)
        assert result["affected_rows"] == 2

    def test_missing_columns_on_target_are_reported(self, service):
        cfg = service.config.get_connection("demo", "local")
        conn = sqlite3.connect(cfg.database)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        conn.close()
        out = service.sync_table(_spec(ddl=DDL_SKIP), CALLER, dry_run=True)
        assert out["columns"] == ["id", "name"]
        assert any("age" in w for w in out["warnings"])

    def test_no_common_columns_is_rejected(self, service):
        cfg = service.config.get_connection("demo", "local")
        conn = sqlite3.connect(cfg.database)
        conn.execute("CREATE TABLE users (other INTEGER)")
        conn.commit()
        conn.close()
        with pytest.raises(QueryRejected, match="没有同名列"):
            service.sync_table(_spec(ddl=DDL_SKIP), CALLER, dry_run=True)


class TestApprovalScope:
    """审批只拦「改线上数据」：local/dev 目标直接执行，staging 及以上仍要人批。"""

    def test_local_target_skips_approval(self, service):
        out = service.sync_table(_spec(), CALLER)
        assert out["status"] == "executed"
        assert out["auto_approved"] is True
        # 免审批不等于免留痕：审批单照建，自动批准后原子核销
        change = service.get_change(out["change_id"])
        assert change.status == "consumed"
        assert change.decided_by == "auto"
        assert change.kind == KIND_SYNC

    def test_dev_target_skips_approval(self, service):
        service.config.get_connection("demo", "staging").environment = "dev"
        assert service.sync_table(_staging_spec(), CALLER)["status"] == "executed"

    def test_staging_target_still_requires_approval(self, service):
        out = service.sync_table(_staging_spec(), CALLER)
        assert out["status"] == "approval_required"
        assert "approval_url" in out
        assert service.list_tables("demo", "staging", CALLER) == []  # 没批准就一步不动


class TestSyncApprovalContract:
    def test_change_is_marked_as_sync_kind(self, service):
        submitted = service.sync_table(_staging_spec(), CALLER)
        change = service.get_change(submitted["change_id"])
        assert change.kind == KIND_SYNC
        assert change.payload["spec"]["source_table"] == "users"
        assert change.project == "demo" and change.connection == "staging"  # 挂在目标连接下

    def test_unapproved_change_id_is_rejected(self, service):
        spec = _staging_spec()
        submitted = service.sync_table(spec, CALLER)
        out = service.sync_table(spec, CALLER, change_id=submitted["change_id"])
        assert out["status"] == "rejected"

    def test_changed_params_fail_fingerprint(self, service):
        submitted = service.sync_table(_staging_spec(), CALLER)
        service.approve_change(submitted["change_id"], decided_by="tester")
        out = service.sync_table(_staging_spec(limit=5), CALLER,
                                 change_id=submitted["change_id"])
        assert out["status"] == "rejected"
        assert "不一致" in out["reason"]
        # 指纹不符时一步都不执行：目标表连建都没建
        assert service.list_tables("demo", "staging", CALLER) == []

    def test_change_is_single_use(self, service):
        spec = _staging_spec()
        submitted = service.sync_table(spec, CALLER)
        _approve_and_run(service, submitted, spec)
        out = service.sync_table(spec, CALLER, change_id=submitted["change_id"])
        assert out["status"] == "rejected"
        assert "已被使用过" in out["reason"]

    def test_executes_plan_stored_in_the_change(self, service):
        """审批后源表又多了一行：执行的是「计划」，所以会带上新数据（而不是提交时的快照）。"""
        spec = _staging_spec()
        submitted = service.sync_table(spec, CALLER)
        src = service.config.get_connection("demo", "prod")
        conn = sqlite3.connect(src.database)
        conn.execute("INSERT INTO users (id, name, age) VALUES (4, 'dave', 40)")
        conn.commit()
        conn.close()
        result = _approve_and_run(service, submitted, spec)
        assert result["affected_rows"] == 4

    def test_sync_change_cannot_be_run_through_execute(self, service):
        """同步审批单存的是计划文本，被 execute 当 SQL 发给 DB 就成了语法错甚至误执行。"""
        submitted = service.sync_table(_staging_spec(), CALLER)
        service.approve_change(submitted["change_id"], decided_by="tester")
        out = service.execute("demo", "staging", "SELECT 1", CALLER,
                              change_id=submitted["change_id"])
        assert out["status"] == "rejected"
        assert "表同步计划" in out["reason"]

    def test_admin_approve_and_execute_runs_the_sync(self, service):
        """后台「批准并立即执行」对同步单同样走计划执行路径。"""
        submitted = service.sync_table(_staging_spec(), CALLER)
        out = service.approve_and_execute_change(submitted["change_id"], decided_by="admin")
        assert out["status"] == "executed"
        assert out["affected_rows"] == 3
        assert service.get_change(submitted["change_id"]).exec_result["affected_rows"] == 3


class TestSyncAudit:
    def test_read_and_write_are_both_audited(self, service):
        service.sync_table(_spec(), CALLER)
        tools = [r["tool"] for r in service.store.recent(limit=50)]
        assert "sync_read" in tools
        assert "sync_write" in tools

    def test_sync_write_counts_as_a_write_in_audit_filter(self, service):
        service.sync_table(_spec(), CALLER)
        writes = service.store.recent(limit=50, filters={"rw": "write"})
        assert {r["tool"] for r in writes} == {"sync_write"}


class TestSyncAdminPage:
    """审批页要能正确渲染同步型审批单（它的 sql 字段是计划文本而不是 SQL）。"""

    @pytest.fixture
    def client(self, tmp_path):
        from starlette.testclient import TestClient

        from dbmcp.admin import mount_admin
        from dbmcp.server import build_mcp

        src_file = tmp_path / "src.sqlite3"
        conn = sqlite3.connect(src_file)
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
            "INSERT INTO users (name) VALUES ('alice'), ('bob');"
        )
        conn.commit()
        conn.close()
        dst_file = tmp_path / "dst.sqlite3"
        sqlite3.connect(dst_file).close()

        cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {
            "prod": {"engine": "sqlite", "database": str(src_file), "environment": "prod"},
            "staging": {"engine": "sqlite", "database": str(dst_file),
                        "environment": "staging"},
        }}}})
        svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                         ApprovalStore(tmp_path / "a.sqlite3"))
        svc.data_dir = str(tmp_path / "data")
        mcp = build_mcp(svc)
        mount_admin(mcp, svc, admin_token="tok")
        with TestClient(mcp.http_app()) as tc:
            tc.post("/admin/login", data={"token": "tok"})
            yield tc, svc
        svc.close()

    def test_detail_page_shows_plan_and_can_execute(self, client):
        tc, svc = client
        submitted = svc.sync_table(_staging_spec(), CALLER, reason="本地复现")
        cid = submitted["change_id"]

        page = tc.get(f"/admin/approvals/{cid}")
        assert page.status_code == 200
        assert "同步计划" in page.text
        assert "表同步计划" in page.text          # 计划正文渲染进了 <pre>

        # 「批准并立即执行」对同步单同样当场落地
        done = tc.post(f"/admin/approvals/{cid}/approve",
                       data={"by": "admin", "exec": "1"}, follow_redirects=False)
        assert done.status_code == 303
        assert svc.get_change(cid).exec_result["affected_rows"] == 2
