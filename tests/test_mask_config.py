"""敏感列脱敏的可配置性：全局设置 + 连接级覆盖，且只作用于 agent 路径。

契约：
1. 默认仍然脱敏（安全默认没变）；
2. 全局关掉后 agent 能拿到真实值；
3. 连接级 Policy 覆盖全局（两个方向都要能覆盖）；
4. 手动点名的 mask_columns 不受这个开关影响；
5. 后台查询台/导出（mask=False）从头到尾都是真实值，不受任何开关影响。
"""

import sqlite3

import pytest

from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.masking import MASK
from dbmcp.service import CallerInfo, DbmService
from dbmcp.settings import SettingsStore

CALLER = CallerInfo(agent="pytest/1.0", session_id="s-mask")


@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, password TEXT, salary INTEGER);"
        "INSERT INTO accounts (name, password, salary) VALUES ('alice', 'p@ss', 100);"
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"))
    svc.settings = SettingsStore(":memory:")
    svc.data_dir = str(tmp_path / "data")
    yield svc
    svc.close()


def _pw(out: dict) -> object:
    return out["rows"][0][out["columns"].index("password")]


class TestGlobalSwitch:
    def test_masked_by_default(self, service):
        """默认仍然打码——把开关做成可配置，不等于把默认放开。"""
        out = service.query("demo", "main", "SELECT id, password FROM accounts", CALLER)
        assert _pw(out) == MASK
        assert out["masked_columns"] == ["password"]

    def test_global_off_returns_real_value(self, service):
        service.save_settings({"mask_sensitive_columns": "false"})
        out = service.query("demo", "main", "SELECT id, password FROM accounts", CALLER)
        assert _pw(out) == "p@ss"
        assert "masked_columns" not in out


class TestConnectionOverride:
    def _cfg(self, service):
        return service.config.get_connection("demo", "main")

    def test_connection_off_overrides_global_on(self, service):
        self._cfg(service).policy.mask_default_patterns = False
        assert service.mask_default_patterns(self._cfg(service)) is False
        out = service.query("demo", "main", "SELECT id, password FROM accounts", CALLER)
        assert _pw(out) == "p@ss"

    def test_connection_on_overrides_global_off(self, service):
        service.save_settings({"mask_sensitive_columns": "false"})
        self._cfg(service).policy.mask_default_patterns = True
        assert service.mask_default_patterns(self._cfg(service)) is True
        out = service.query("demo", "main", "SELECT id, password FROM accounts", CALLER)
        assert _pw(out) == MASK

    def test_none_follows_global(self, service):
        cfg = self._cfg(service)
        assert cfg.policy.mask_default_patterns is None      # 配置里不写 = 跟随全局
        assert service.mask_default_patterns(cfg) is True
        service.save_settings({"mask_sensitive_columns": "false"})
        assert service.mask_default_patterns(cfg) is False


class TestExplicitColumnsUnaffected:
    def test_named_columns_still_masked_when_switch_off(self, service):
        """开关只管「按词猜」；点名要脱敏的列任何时候都脱敏。"""
        service.save_settings({"mask_sensitive_columns": "false"})
        service.config.get_connection("demo", "main").policy.mask_columns = ["salary"]
        out = service.query("demo", "main", "SELECT password, salary FROM accounts", CALLER)
        assert out["rows"][0] == ["p@ss", MASK]
        assert out["masked_columns"] == ["salary"]


class TestConsoleAlwaysReal:
    def test_admin_console_unaffected_by_switch(self, service):
        """后台查询台走 mask=False，无论开关怎么设都返回真实值。"""
        for value in ("true", "false"):
            service.save_settings({"mask_sensitive_columns": value})
            out = service.admin_run_sql("demo", "main", "SELECT password FROM accounts", CALLER)
            assert out["rows"][0][0] == "p@ss"
