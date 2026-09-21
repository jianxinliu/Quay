"""驱动可插拔架构的回归保护。

核心约定（用户明确要的能力）：**后续加 DB 支持 = 加一个驱动文件并注册**，不用动
config 的类型、admin 的表单/端口/图标清单、sync 的源目标清单、编辑器 lint 的方言
白名单。本文件用一个临时注册的「假引擎」端到端验证这条链路，并钉住内置引擎的能力默认值。
"""

import pytest

from dbmcp import sync
from dbmcp.admin import _connectable_engines, _connection_form, _engine_icon_file
from dbmcp.drivers import (
    DRIVERS,
    DbDriver,
    connectable_engines,
    driver_for_engine,
    engine_default_port,
    engine_dialect,
    get_driver,
    register,
    supported_engines,
)
from dbmcp.engines import make_canceller


class _FakeDriver(DbDriver):
    """一个只在测试里存在的引擎，用来证明「加驱动就够」。"""

    name = "vertica"
    dialect = "vertica"
    sa_dialect_names = ("vertica",)
    default_port = 5433
    icon = "vertica"


@pytest.fixture
def registry_snapshot():
    """注册表是全局的：测试结束必须还原，免得污染其它用例。"""
    saved = dict(DRIVERS)
    yield DRIVERS
    DRIVERS.clear()
    DRIVERS.update(saved)


def test_builtin_drivers_registered():
    assert set(supported_engines()) >= {"mysql", "postgres", "sqlite", "clickhouse"}
    for e in ("mysql", "postgres", "sqlite", "clickhouse"):
        assert get_driver(e).dialect is not None


def test_builtin_capability_defaults():
    """能力默认值钉死，避免哪天手滑改错（如让 sqlite 走「选库」分支会处处崩）。"""
    assert get_driver("sqlite").has_schema_layer is False
    assert get_driver("postgres").needs_database_layer is True
    assert get_driver("mysql").needs_database_layer is False
    # ClickHouse 本项目只读：不做同步目标、AI 生成也关着
    ch = get_driver("clickhouse")
    assert ch.sync_target is False and ch.ai_sql is False
    # 可连接的内置引擎都该有方言（lint/美化/同步转写要用）和图标
    for e in connectable_engines():
        d = get_driver(e)
        assert d.dialect, f"{e} 缺 sqlglot 方言"
        assert d.icon, f"{e} 缺品牌图标"


def test_registering_a_driver_flows_through_every_engine_list(registry_snapshot):
    """加一个驱动：表单下拉、同步源/目标、方言、端口、图标全部自动跟上。"""
    register(_FakeDriver)

    assert "vertica" in supported_engines()
    assert "vertica" in connectable_engines()
    assert "vertica" in _connectable_engines()          # 连接表单下拉
    assert "vertica" in sync.source_engines()
    assert "vertica" in sync.target_engines()           # sync_target 默认 True
    assert engine_dialect("vertica") == "vertica"
    assert engine_default_port("vertica") == 5433
    assert _engine_icon_file("vertica") == "vertica"

    # 表单真的把它渲染进下拉，默认端口也联动上了
    html = _connection_form("demo", "new", None, [])
    assert "value='vertica'" in html
    assert "vertica:'5433'" in html


def test_non_connectable_driver_stays_out_of_connection_form(registry_snapshot):
    """进程内引擎（如 DuckDB）可以注册进注册表贡献方言/图标，但不能出现在连接表单。"""

    class _Inproc(DbDriver):
        name = "inproc"
        dialect = "inproc"
        connectable = False
        icon = "inproc"

    register(_Inproc)
    assert "inproc" in supported_engines()
    assert "inproc" not in connectable_engines()
    assert "inproc" not in _connectable_engines()
    assert "inproc" not in sync.source_engines()
    assert _connection_form("demo", "new", None, []).count("inproc") == 0


def test_sync_target_opt_in_per_driver(registry_snapshot):
    class _ReadOnly(_FakeDriver):
        name = "vertica"
        sync_target = False

    register(_ReadOnly)
    assert "vertica" in sync.source_engines()
    assert "vertica" not in sync.target_engines()


def test_unknown_engine_degrades_instead_of_raising():
    """未注册引擎不该抛异常把调用方打挂：能力查询给 None，留给调用方降级或拒绝。"""
    assert engine_dialect("mongodb") is None
    assert engine_default_port("mongodb") is None
    with pytest.raises(Exception):  # noqa: PT011
        get_driver("mongodb")


def test_driver_for_engine_reads_sa_dialect_name():
    """运行期只知道已建好的引擎时（取消、执行计划）能反查回驱动。"""
    class _FakeEngine:
        class dialect:
            name = "postgresql"

    assert driver_for_engine(_FakeEngine()) is get_driver("postgres")


def test_make_canceller_uses_driver_dispatch():
    """取消器按 engine.dialect.name 路由到驱动（driver_for_engine），不再 if 分支。"""
    class _FakeEngine:
        class dialect:
            name = "sqlite"

        def connect(self):  # noqa: ANN001, ANN201
            raise AssertionError("取消器不应为了拿标识而去连库")

    # sqlite 分支从原始连接取 interrupt 句柄；这里给不出 raw，应当安全降级为空操作
    assert make_canceller(_FakeEngine(), object())() is None
