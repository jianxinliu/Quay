"""Redis 引擎纯函数/近纯函数测试（无需真实 redis）：
库列表上限、密钥回显脱敏、生产环境写命令二次输入闸门。
"""

import pytest

from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.redis_engine import keyspace_dbs, redact_command_result
from dbmcp.service import CallerInfo, DbmService, QueryRejected


class FakeRedis:
    """最小假客户端：只实现 keyspace_dbs 用到的 info / config_get。"""
    def __init__(self, keyspace: dict, databases=16, config_raises=False):
        self._keyspace = keyspace
        self._databases = databases
        self._config_raises = config_raises

    def info(self, section):  # noqa: ARG002
        return self._keyspace

    def config_get(self, key):  # noqa: ARG002
        if self._config_raises:
            raise RuntimeError("CONFIG disabled")
        return {"databases": str(self._databases)}


class TestKeyspaceDbsCap:
    def test_lists_all_16_with_counts_only_on_nonempty(self):
        c = FakeRedis({"db10": {"keys": 5, "expires": 0}}, databases=16)
        dbs = keyspace_dbs(c)
        assert [d["db"] for d in dbs] == list(range(16))
        by = {d["db"]: d["keys"] for d in dbs}
        assert by[10] == 5
        assert by[0] == 0

    def test_caps_large_database_count(self):
        # 实例配 256 库，只有 db0/db4 有数据 → 只展示到 max(16, 4+1)=16
        c = FakeRedis({"db0": {"keys": 100}, "db4": {"keys": 3}}, databases=256)
        dbs = keyspace_dbs(c)
        assert [d["db"] for d in dbs] == list(range(16))  # 不是 256
        assert {d["db"] for d in dbs} >= {0, 4}

    def test_nonempty_db_beyond_cap_still_included(self):
        # 有数据的库号很大（200）→ 必须包含它，展示到它为止
        c = FakeRedis({"db200": {"keys": 7}}, databases=256)
        dbs = keyspace_dbs(c)
        idxs = {d["db"] for d in dbs}
        assert 200 in idxs
        assert 0 in idxs

    def test_config_disabled_falls_back_to_16(self):
        c = FakeRedis({"db0": {"keys": 1}}, config_raises=True)
        dbs = keyspace_dbs(c)
        assert [d["db"] for d in dbs] == list(range(16))


class TestRedaction:
    def test_config_get_password_redacted(self):
        out = redact_command_result(["CONFIG", "GET", "requirepass"],
                                    ["requirepass", "s3cr3t-pw"])
        assert out == ["requirepass", "***已隐藏***"]

    def test_config_get_masterauth_redacted(self):
        out = redact_command_result(["CONFIG", "GET", "*"],
                                    ["maxmemory", "0", "masterauth", "hunter2"])
        assert out == ["maxmemory", "0", "masterauth", "***已隐藏***"]

    def test_config_get_nonsecret_untouched(self):
        out = redact_command_result(["config", "get", "maxmemory"], ["maxmemory", "0"])
        assert out == ["maxmemory", "0"]

    def test_acl_list_hash_and_plain_redacted(self):
        line = "user default on #" + "a" * 64 + " >plainpw ~* +@all"
        out = redact_command_result(["ACL", "LIST"], [line])
        assert "a" * 64 not in out[0]
        assert "plainpw" not in out[0]
        assert "***已隐藏***" in out[0]

    def test_acl_getuser_bare_hash_redacted(self):
        out = redact_command_result(["ACL", "GETUSER", "default"],
                                    ["passwords", ["b" * 64], "flags", ["on"]])
        assert "b" * 64 not in str(out)

    def test_unrelated_command_untouched(self):
        assert redact_command_result(["GET", "k"], "value") == "value"


class TestProdWriteGate:
    def _service(self, tmp_path, environment):
        cfg = AppConfig.model_validate({"projects": {"sdk": {"connections": {"rp": {
            "engine": "redis", "environment": environment,
            "host": "127.0.0.1", "port": 6379,
        }}}}})
        return DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"))

    def test_prod_write_without_text_rejected(self, tmp_path):
        svc = self._service(tmp_path, "prod")
        caller = CallerInfo(agent="t", session_id="s")
        # 写命令 + confirm=True 但没输连接名 → 在连接 redis 之前就被拒
        with pytest.raises(QueryRejected):
            svc.admin_redis_run("sdk", "rp", "SET k 1", caller, confirm=True, confirm_text="wrong")
        svc.close()

    def test_prod_confirm_payload_carries_expect_text(self, tmp_path):
        svc = self._service(tmp_path, "prod")
        caller = CallerInfo(agent="t", session_id="s")
        out = svc.admin_redis_run("sdk", "rp", "SET k 1", caller, confirm=False)
        assert out["kind"] == "confirm"
        assert out["prod"] is True
        assert out["expect_text"] == "rp"
        svc.close()

    def test_nonprod_confirm_payload_no_gate(self, tmp_path):
        svc = self._service(tmp_path, "dev")
        caller = CallerInfo(agent="t", session_id="s")
        out = svc.admin_redis_run("sdk", "rp", "SET k 1", caller, confirm=False)
        assert out["kind"] == "confirm"
        assert out["prod"] is False
        assert out["expect_text"] is None
        svc.close()
