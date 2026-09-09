"""建表语句相关的两个新工具：

- `table_ddl`：看一张（或几张）表的建表语句；
- `sync_table_ddl`：批量把表结构同步到另一条连接（在本地照着线上重建一套空表）。

外加表同步的**体积闸门**（sync_max_bytes）——行数上限管不住行很宽的表。
"""

import sqlite3

import pytest

from dbmcp import engines
from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService
from dbmcp.settings import SettingsStore

CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-ddl")


@pytest.fixture
def service(tmp_path):
    """源库 prod（users + orders）+ 目标库 local（空）。"""
    src = tmp_path / "src.sqlite3"
    conn = sqlite3.connect(src)
    conn.executescript(
        """CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
           CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL);
           INSERT INTO users (name) VALUES ('alice'), ('bob');"""
    )
    conn.commit()
    conn.close()
    dst = tmp_path / "dst.sqlite3"
    sqlite3.connect(dst).close()

    cfg = AppConfig.model_validate({"projects": {"demo": {"connections": {
        "prod": {"engine": "sqlite", "database": str(src), "environment": "prod"},
        "local": {"engine": "sqlite", "database": str(dst), "environment": "local"},
        "staging": {"engine": "sqlite", "database": str(dst), "environment": "staging"},
    }}}})
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     ApprovalStore(tmp_path / "a.sqlite3"))
    svc.settings = SettingsStore(":memory:")
    yield svc
    svc.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------- 查建表语句 ----------------

class TestGetTableDdls:
    def test_returns_in_requested_order(self, service):
        got = service.get_table_ddls("demo", "prod", ["orders", "users"], CALLER)
        assert [g["table"] for g in got] == ["orders", "users"]
        assert "CREATE TABLE" in got[0]["ddl"].upper()

    def test_one_bad_table_does_not_abort_the_batch(self, service):
        """整批因为一个错表名全废是最难用的失败方式。"""
        got = service.get_table_ddls("demo", "prod", ["users", "nope"], CALLER)
        assert "ddl" in got[0]
        assert "error" in got[1] and "nope" in got[1]["error"]

    def test_blank_names_dropped(self, service):
        assert len(service.get_table_ddls("demo", "prod", ["users", "  ", ""], CALLER)) == 1

    def test_empty_rejected(self, service):
        with pytest.raises(ValueError, match="至少要给一个表名"):
            service.get_table_ddls("demo", "prod", [], CALLER)

    def test_too_many_rejected_with_hint(self, service):
        with pytest.raises(ValueError, match="list_tables"):
            service.get_table_ddls("demo", "prod", [f"t{i}" for i in range(30)], CALLER)

    def test_audited_per_table(self, service):
        service.get_table_ddls("demo", "prod", ["users", "orders"], CALLER)
        tools = [r["tool"] for r in service.store.recent(10)]
        assert tools.count("table_ddl") == 2


@pytest.mark.anyio
async def test_table_ddl_tool(service):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        assert "table_ddl" in {t.name for t in await c.list_tools()}

        one = (await c.call_tool("table_ddl", {
            "project": "demo", "connection": "prod", "table": "users"})).data
        assert "CREATE TABLE" in one.upper()
        assert not one.startswith("-- users")     # 单表不加表名分隔头

        many = (await c.call_tool("table_ddl", {
            "project": "demo", "connection": "prod", "table": "users, orders"})).data
        assert "-- users" in many and "-- orders" in many

        # 单表取不到就该原样报错，而不是回一段「取失败」的注释让 agent 以为成功了
        with pytest.raises(ToolError):
            await c.call_tool("table_ddl", {
                "project": "demo", "connection": "prod", "table": "nope"})

        # 批量里的坏表名如实标出来，不静默跳过
        mixed = (await c.call_tool("table_ddl", {
            "project": "demo", "connection": "prod", "table": "users,nope"})).data
        assert "取建表语句失败" in mixed


# ---------------- 结构同步 ----------------

def _tables_in(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


class TestSyncTableDdls:
    def test_creates_structure_without_data(self, service, tmp_path):
        out = service.sync_table_ddls("demo", "prod", "demo", "local",
                                      ["users", "orders"], CALLER)
        assert out["requested"] == 2 and out["succeeded"] == 2
        dst = tmp_path / "dst.sqlite3"
        assert {"users", "orders"} <= _tables_in(dst)
        conn = sqlite3.connect(dst)
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        conn.close()

    def test_dry_run_creates_nothing(self, service, tmp_path):
        out = service.sync_table_ddls("demo", "prod", "demo", "local",
                                      ["users"], CALLER, dry_run=True)
        assert out["tables"][0]["status"] == "planned"
        assert "users" not in _tables_in(tmp_path / "dst.sqlite3")

    def test_bad_table_does_not_stop_the_rest(self, service, tmp_path):
        out = service.sync_table_ddls("demo", "prod", "demo", "local",
                                      ["nope", "users"], CALLER)
        assert out["tables"][0]["status"] == "failed" and "error" in out["tables"][0]
        assert out["succeeded"] == 1
        assert "users" in _tables_in(tmp_path / "dst.sqlite3")

    def test_ddl_mode_restricted_to_structure_modes(self, service):
        """skip 意味着「不建表」，在只同步结构的工具里就是个空操作。"""
        with pytest.raises(ValueError, match="create_if_missing"):
            service.sync_table_ddls("demo", "prod", "demo", "local",
                                    ["users"], CALLER, ddl="skip")

    def test_too_many_tables_rejected(self, service):
        with pytest.raises(ValueError, match="分批"):
            service.sync_table_ddls("demo", "prod", "demo", "local",
                                    [f"t{i}" for i in range(60)], CALLER)

    def test_empty_rejected(self, service):
        with pytest.raises(ValueError, match="至少要给一个表名"):
            service.sync_table_ddls("demo", "prod", "demo", "local", [], CALLER)

    def test_prod_target_refused(self, service):
        """红线沿用 sync_table 那套：不往生产建表。"""
        out = service.sync_table_ddls("demo", "local", "demo", "prod", ["users"], CALLER)
        assert out["tables"][0]["status"] == "failed"
        assert "prod" in out["tables"][0]["error"]

    def test_staging_target_goes_through_approval(self, service, tmp_path):
        out = service.sync_table_ddls("demo", "prod", "demo", "staging",
                                      ["users"], CALLER)
        assert out["tables"][0]["status"] == "approval_required"
        assert "users" not in _tables_in(tmp_path / "dst.sqlite3")

    def test_audited_as_sync_write(self, service):
        service.sync_table_ddls("demo", "prod", "demo", "local", ["users"], CALLER)
        assert "sync_write" in {r["tool"] for r in service.store.recent(20)}


@pytest.mark.anyio
async def test_sync_table_ddl_tool(service, tmp_path):
    from fastmcp import Client

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        assert "sync_table_ddl" in {t.name for t in await c.list_tools()}
        out = (await c.call_tool("sync_table_ddl", {
            "source_project": "demo", "source_connection": "prod",
            "target_project": "demo", "target_connection": "local",
            "tables": "users, orders",
        })).data
    assert out["succeeded"] == 2
    assert {"users", "orders"} <= _tables_in(tmp_path / "dst.sqlite3")


# ---------------- 同步体积闸门 ----------------

class TestSyncByteBudget:
    @pytest.fixture
    def wide(self, tmp_path):
        """一张「行很宽」的表：3 行，每行 ~1KB。"""
        path = tmp_path / "wide.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, blob TEXT)")
        conn.executemany("INSERT INTO big (blob) VALUES (?)", [("x" * 1000,)] * 3)
        conn.commit()
        conn.close()
        return engines.create_engine(f"sqlite:///{path}")

    def test_no_budget_returns_everything(self, wide):
        _, rows, truncated = engines.fetch_rows_for_copy(wide, "SELECT * FROM big", 10)
        assert len(rows) == 3 and truncated is False

    def test_budget_stops_early_and_flags_truncation(self, wide):
        _, rows, truncated = engines.fetch_rows_for_copy(
            wide, "SELECT * FROM big", 10, max_bytes=2500)
        assert len(rows) == 2 and truncated is True

    def test_first_row_always_kept(self, wide):
        """一行就超预算说明这张表本身就宽；返回空会让调用方误以为源表是空的。"""
        _, rows, truncated = engines.fetch_rows_for_copy(
            wide, "SELECT * FROM big", 10, max_bytes=1)
        assert len(rows) == 1 and truncated is True

    def test_row_limit_still_applies(self, wide):
        _, rows, truncated = engines.fetch_rows_for_copy(
            wide, "SELECT * FROM big", 2, max_bytes=10_000_000)
        assert len(rows) == 2 and truncated is True

    def test_service_reads_limit_from_settings(self, service):
        service.save_settings({"sync_max_bytes": 4 * 1024 * 1024})
        assert service.sync_max_bytes() == 4 * 1024 * 1024

    def test_settings_floor_keeps_it_sane(self, service):
        """设置项有下界（1MB）：把上限调到几百字节等于让同步永远只搬一行。"""
        service.save_settings({"sync_max_bytes": 10})
        assert service.sync_max_bytes() == 1024 * 1024

    def test_sync_truncated_by_bytes_says_so(self, service, monkeypatch):
        """撞体积上限和撞行数上限的解法不同，提示必须分得清。

        直接压低 sync_max_bytes 而不走设置项：设置项有 1MB 下界（合理），
        造一张真的超过 1MB 的表只为测一句文案不划算。
        """
        from dbmcp import sync

        monkeypatch.setattr(service, "sync_max_bytes", lambda: 15)
        spec = sync.SyncSpec(
            source_project="demo", source_connection="prod", source_table="users",
            target_project="demo", target_connection="local", target_table="users",
            ddl=sync.DDL_CREATE_IF_MISSING, data=sync.DATA_APPEND, limit=1000,
        )
        out = service.sync_table(spec, CALLER)
        assert out["status"] == "executed"
        assert out["source_truncated"] is True
        assert "体积上限" in out["note"] and "sync_max_bytes" in out["note"]
