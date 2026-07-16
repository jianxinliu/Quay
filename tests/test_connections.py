"""连接管理测试：配置写回 + keyring 引用 + SSH key 校验 + 热加载/池回收。"""

import sys
import types

import pytest

from dbmcp.config import load_config
from dbmcp.connections import ConnectionAdminError, ConnectionManager, validate_ssh_key_path


@pytest.fixture
def fake_keyring(monkeypatch):
    """内存版 keyring，避免污染真实钥匙串。"""
    store = {}
    mod = types.ModuleType("keyring")
    errmod = types.ModuleType("keyring.errors")

    class PasswordDeleteError(Exception):
        pass

    errmod.PasswordDeleteError = PasswordDeleteError
    mod.errors = errmod
    mod.set_password = lambda s, a, v: store.__setitem__((s, a), v)
    mod.get_password = lambda s, a: store.get((s, a))

    def _del(s, a):
        if (s, a) not in store:
            raise PasswordDeleteError()
        del store[(s, a)]

    mod.delete_password = _del
    monkeypatch.setitem(sys.modules, "keyring", mod)
    monkeypatch.setitem(sys.modules, "keyring.errors", errmod)
    return store


@pytest.fixture
def empty_config(tmp_path):
    path = tmp_path / "conn.yaml"
    path.write_text("projects: {}\n", encoding="utf-8")
    return load_config(path), path


class TestUpsert:
    def test_create_writes_yaml_and_keyring(self, empty_config, fake_keyring):
        cfg, path = empty_config
        mgr = ConnectionManager(cfg, path)
        mgr.upsert(
            "local", "db1", engine="mysql", environment="dev",
            host="127.0.0.1", port=3306, database="app", user="root",
            password="s3cret", writer_user=None, writer_password=None,
            jump_hosts=[], ssh_options_extra=[],
            max_rows=500, mask_columns=["email"], force_privileged=True,
        )
        # 密码写进了 keyring，不落明文
        assert "s3cret" in fake_keyring.values()
        reloaded = load_config(path)
        conn = reloaded.get_connection("local", "db1")
        assert conn.host == "127.0.0.1"
        assert conn.password.startswith("keyring://")
        assert "s3cret" not in path.read_text(encoding="utf-8")  # 文件里没有明文
        assert conn.policy.mask_columns == ["email"]

    def test_edit_keep_password_when_blank(self, empty_config, fake_keyring):
        cfg, path = empty_config
        mgr = ConnectionManager(cfg, path)
        common = dict(engine="mysql", environment="dev", host="h", port=3306,
                      database="d", user="u", writer_user=None, writer_password=None,
                      jump_hosts=[], ssh_options_extra=[],
                      max_rows=100, mask_columns=[], force_privileged=True)
        mgr.upsert("local", "db1", password="orig", **common)
        ref1 = cfg.get_connection("local", "db1").password
        # 编辑时密码留空 → 沿用旧引用
        mgr.upsert("local", "db1", password=None, **{**common, "max_rows": 200})
        conn = cfg.get_connection("local", "db1")
        assert conn.password == ref1
        assert conn.policy.max_rows == 200

    def test_writer_account(self, empty_config, fake_keyring):
        cfg, path = empty_config
        mgr = ConnectionManager(cfg, path)
        mgr.upsert("local", "db1", engine="mysql", environment="dev", host="h",
                   port=3306, database="d", user="u", password="p",
                   writer_user="writer", writer_password="wp",
                   jump_hosts=[], ssh_options_extra=[],
                   max_rows=500, mask_columns=[], force_privileged=True)
        conn = cfg.get_connection("local", "db1")
        assert conn.writer.user == "writer"
        assert conn.writer.password.startswith("keyring://")

    def test_writer_user_without_password_fails(self, empty_config, fake_keyring):
        cfg, path = empty_config
        mgr = ConnectionManager(cfg, path)
        with pytest.raises(ConnectionAdminError, match="writer 密码"):
            mgr.upsert("local", "db1", engine="mysql", environment="dev", host="h",
                       port=3306, database="d", user="u", password="p",
                       writer_user="writer", writer_password=None,
                       jump_hosts=[], ssh_options_extra=[],
                       max_rows=500, mask_columns=[], force_privileged=True)

    def test_invalid_engine_config_rejected(self, empty_config, fake_keyring):
        cfg, path = empty_config
        mgr = ConnectionManager(cfg, path)
        # mysql 缺 host → ConnectionConfig 校验失败
        with pytest.raises(ConnectionAdminError, match="连接配置无效|缺少必填"):
            mgr.upsert("local", "db1", engine="mysql", environment="dev", host=None,
                       port=None, database="d", user="u", password="p",
                       writer_user=None, writer_password=None,
                       jump_hosts=[], ssh_options_extra=[],
                       max_rows=500, mask_columns=[])


class TestPrivilegeGate:
    """保存时的账号权限校验门（monkeypatch 探测，不真连库）。"""

    def _upsert(self, mgr, **over):
        base = dict(engine="mysql", environment="dev", host="db", port=3306,
                    database="app", user="reader", password="p", writer_user=None,
                    writer_password=None, jump_hosts=[],
                    ssh_options_extra=[], max_rows=500, mask_columns=[])
        base.update(over)
        mgr.upsert("local", "c", **base)

    def test_readonly_account_saves(self, empty_config, fake_keyring, monkeypatch):
        from dbmcp.probe import ProbeResult
        monkeypatch.setattr("dbmcp.probe.probe_connection",
                            lambda cfg, pw, ident=None: ProbeResult(ok=True, has_write=False, is_superuser=False))
        cfg, path = empty_config
        self._upsert(ConnectionManager(cfg, path))
        assert cfg.get_connection("local", "c").host == "db"

    def test_write_account_blocked(self, empty_config, fake_keyring, monkeypatch):
        from dbmcp.probe import ProbeResult
        monkeypatch.setattr("dbmcp.probe.probe_connection",
                            lambda cfg, pw, ident=None: ProbeResult(ok=True, has_write=True, is_superuser=False))
        cfg, path = empty_config
        with pytest.raises(ConnectionAdminError, match="写权限"):
            self._upsert(ConnectionManager(cfg, path))

    def test_superuser_blocked(self, empty_config, fake_keyring, monkeypatch):
        from dbmcp.probe import ProbeResult
        monkeypatch.setattr("dbmcp.probe.probe_connection",
                            lambda cfg, pw, ident=None: ProbeResult(ok=True, has_write=True, is_superuser=True))
        cfg, path = empty_config
        with pytest.raises(ConnectionAdminError, match="超级用户"):
            self._upsert(ConnectionManager(cfg, path))

    def test_force_bypasses_gate(self, empty_config, fake_keyring, monkeypatch):
        from dbmcp.probe import ProbeResult
        called = []
        monkeypatch.setattr("dbmcp.probe.probe_connection",
                            lambda cfg, pw, ident=None: called.append(1) or ProbeResult(ok=True, has_write=True))
        cfg, path = empty_config
        self._upsert(ConnectionManager(cfg, path), force_privileged=True)
        assert called == []  # 强制时根本不探测
        assert cfg.get_connection("local", "c").host == "db"

    def test_unreachable_blocked(self, empty_config, fake_keyring, monkeypatch):
        from dbmcp.probe import ProbeResult
        monkeypatch.setattr("dbmcp.probe.probe_connection",
                            lambda cfg, pw, ident=None: ProbeResult(ok=False, message="连接超时"))
        cfg, path = empty_config
        with pytest.raises(ConnectionAdminError, match="无法连接"):
            self._upsert(ConnectionManager(cfg, path))

    def test_sqlite_skips_gate(self, empty_config, fake_keyring, tmp_path):
        cfg, path = empty_config
        db = tmp_path / "x.sqlite3"
        import sqlite3
        sqlite3.connect(db).close()
        # sqlite 无账号模型，不触发探测（若触发会因 monkeypatch 缺失而真连，这里不 patch 也应通过）
        ConnectionManager(cfg, path).upsert(
            "local", "s", engine="sqlite", environment="local", host=None, port=None,
            database=str(db), user=None, password=None, writer_user=None, writer_password=None,
            jump_hosts=[], ssh_options_extra=[], max_rows=10, mask_columns=[])
        assert cfg.get_connection("local", "s").engine == "sqlite"


class TestSSHKey:
    def test_inline_key_per_hop(self, empty_config, fake_keyring, tmp_path):
        key = tmp_path / "id_key"
        key.write_text("KEY")
        key.chmod(0o600)
        cfg, path = empty_config
        ConnectionManager(cfg, path).upsert(
            "local", "db1", engine="mysql", environment="prod", host="h", port=3306,
            database="d", user="u", password="p", writer_user=None, writer_password=None,
            jump_hosts=[{"host": "bastion", "user": "alice", "key_path": str(key)}],
            ssh_options_extra=["-o", "X=1"],
            max_rows=500, mask_columns=[], force_privileged=True,
        )
        conn = cfg.get_connection("local", "db1")
        assert conn.ssh_options == ["-o", "X=1"]  # 不再注入 -i（每跳带自己的 key）
        assert len(conn.jump_hosts) == 1
        hop = conn.jump_hosts[0]
        assert hop.host == "bastion" and hop.user == "alice" and hop.key_path == str(key)

    def test_multi_hop_saved(self, empty_config, fake_keyring):
        cfg, path = empty_config
        ConnectionManager(cfg, path).upsert(
            "local", "db1", engine="mysql", environment="dev", host="h", port=3306,
            database="d", user="u", password="p", writer_user=None, writer_password=None,
            jump_hosts=[{"host": "b1", "user": "a"}, {"host": "b2", "port": 2222}],
            ssh_options_extra=[], max_rows=500, mask_columns=[], force_privileged=True,
        )
        hops = load_config(path).get_connection("local", "db1").jump_hosts
        assert [h.label() for h in hops] == ["a@b1", "b2:2222"]

    def test_inline_key_missing_rejected(self, empty_config, fake_keyring):
        cfg, path = empty_config
        with pytest.raises(ConnectionAdminError, match="不存在"):
            ConnectionManager(cfg, path).upsert(
                "local", "db1", engine="mysql", environment="dev", host="h", port=3306,
                database="d", user="u", password="p", writer_user=None, writer_password=None,
                jump_hosts=[{"host": "b", "key_path": "/no/such/key"}],
                ssh_options_extra=[], max_rows=500, mask_columns=[], force_privileged=True,
            )

    def test_missing_key_rejected(self):
        with pytest.raises(ConnectionAdminError, match="不存在"):
            validate_ssh_key_path("/no/such/key")

    def test_overly_open_key_rejected(self, tmp_path):
        key = tmp_path / "bad_key"
        key.write_text("K")
        key.chmod(0o644)
        with pytest.raises(ConnectionAdminError, match="权限过宽"):
            validate_ssh_key_path(str(key))


class TestSshIdentities:
    def _key(self, tmp_path, name="k"):
        key = tmp_path / name
        key.write_text("KEY")
        key.chmod(0o600)
        return key

    def test_upsert_and_reference(self, empty_config, fake_keyring, tmp_path):
        cfg, path = empty_config
        key = self._key(tmp_path)
        mgr = ConnectionManager(cfg, path)
        mgr.upsert_identity("prod-bastion", str(key))
        assert "prod-bastion" in load_config(path).ssh_identities
        # 连接引用该证书
        mgr.upsert(
            "local", "db1", engine="mysql", environment="dev", host="h", port=3306,
            database="d", user="u", password="p", writer_user=None, writer_password=None,
            jump_hosts=[{"host": "b", "identity": "prod-bastion"}],
            ssh_options_extra=[], max_rows=500, mask_columns=[], force_privileged=True,
        )
        assert cfg.get_connection("local", "db1").jump_hosts[0].identity == "prod-bastion"

    def test_reference_unknown_identity_rejected(self, empty_config, fake_keyring):
        cfg, path = empty_config
        with pytest.raises(ConnectionAdminError, match="不存在"):
            ConnectionManager(cfg, path).upsert(
                "local", "db1", engine="mysql", environment="dev", host="h", port=3306,
                database="d", user="u", password="p", writer_user=None, writer_password=None,
                jump_hosts=[{"host": "b", "identity": "ghost"}],
                ssh_options_extra=[], max_rows=500, mask_columns=[], force_privileged=True,
            )

    def test_delete_blocked_when_referenced(self, empty_config, fake_keyring, tmp_path):
        cfg, path = empty_config
        key = self._key(tmp_path)
        mgr = ConnectionManager(cfg, path)
        mgr.upsert_identity("id1", str(key))
        mgr.upsert(
            "local", "db1", engine="mysql", environment="dev", host="h", port=3306,
            database="d", user="u", password="p", writer_user=None, writer_password=None,
            jump_hosts=[{"host": "b", "identity": "id1"}],
            ssh_options_extra=[], max_rows=500, mask_columns=[], force_privileged=True,
        )
        with pytest.raises(ConnectionAdminError, match="引用"):
            mgr.delete_identity("id1")
        assert mgr.identity_referers("id1") == ["local/db1"]

    def test_delete_ok_when_unreferenced(self, empty_config, fake_keyring, tmp_path):
        cfg, path = empty_config
        mgr = ConnectionManager(cfg, path)
        mgr.upsert_identity("id1", str(self._key(tmp_path)))
        mgr.delete_identity("id1")
        assert "id1" not in load_config(path).ssh_identities

    def test_identity_bad_key_rejected(self, empty_config, fake_keyring):
        cfg, path = empty_config
        with pytest.raises(ConnectionAdminError, match="不存在"):
            ConnectionManager(cfg, path).upsert_identity("id1", "/no/such/key")


class TestDelete:
    def test_delete_removes_and_purges_keyring(self, empty_config, fake_keyring):
        cfg, path = empty_config
        mgr = ConnectionManager(cfg, path)
        mgr.upsert("local", "db1", engine="mysql", environment="dev", host="h", port=3306,
                   database="d", user="u", password="p", writer_user=None, writer_password=None,
                   jump_hosts=[], ssh_options_extra=[], max_rows=500,
                   mask_columns=[], force_privileged=True)
        assert len(fake_keyring) == 1
        mgr.delete("local", "db1")
        assert len(fake_keyring) == 0  # keyring 清理
        assert "local" not in load_config(path).projects  # 空项目一并移除

    def test_delete_missing(self, empty_config):
        cfg, path = empty_config
        with pytest.raises(ConnectionAdminError, match="不存在"):
            ConnectionManager(cfg, path).delete("x", "y")


class TestServiceHotReload:
    def test_upsert_evicts_pool(self, empty_config, fake_keyring, tmp_path):
        from dbmcp.audit.log import AuditStore
        from dbmcp.service import CallerInfo, DbmService
        cfg, path = empty_config
        # 预置一个 sqlite 连接并用一次，占住引擎池
        import sqlite3
        db = tmp_path / "x.sqlite3"
        sqlite3.connect(db).close()
        svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"), config_path=str(path))
        svc.upsert_connection("local", "s", CallerInfo(agent="t"),
                              engine="sqlite", environment="local", host=None, port=None,
                              database=str(db), user=None, password=None, writer_user=None,
                              writer_password=None, jump_hosts=[],
                              ssh_options_extra=[], max_rows=10, mask_columns=[])
        svc.query("local", "s", "SELECT 1", CallerInfo(agent="t"))
        assert ("local", "s", "reader", "") in svc.pool._entries
        # 改配置 → 池应被回收
        svc.upsert_connection("local", "s", CallerInfo(agent="t"),
                              engine="sqlite", environment="local", host=None, port=None,
                              database=str(db), user=None, password=None, writer_user=None,
                              writer_password=None, jump_hosts=[],
                              ssh_options_extra=[], max_rows=99, mask_columns=[])
        assert ("local", "s", "reader", "") not in svc.pool._entries
        assert svc.config.get_connection("local", "s").policy.max_rows == 99
        # 审计留痕
        assert any(r["tool"] == "upsert_connection" for r in svc.store.recent())
        svc.close()