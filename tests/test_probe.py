"""连接探测测试：MySQL 授权解析（纯函数）+ SQLite 连通 + SSH 失败。

只读/读写账号判定用 analyze_mysql_grants 单测覆盖——不依赖能否在真实实例建受限账号。
"""

import sqlite3


from dbmcp.config import ConnectionConfig
from dbmcp.probe import analyze_mysql_grants, probe_connection, probe_ssh


class TestMysqlGrantAnalysis:
    def test_readonly_user(self):
        grants = ["GRANT SELECT ON `dbm_e2e`.* TO `dbm_ro`@`%`"]
        has_write, is_super = analyze_mysql_grants(grants, "dbm_ro")
        assert has_write is False
        assert is_super is False

    def test_readwrite_user(self):
        grants = ["GRANT SELECT, INSERT, UPDATE, DELETE ON `dbm_e2e`.* TO `dbm_rw`@`%`"]
        has_write, _ = analyze_mysql_grants(grants, "dbm_rw")
        assert has_write is True

    def test_grant_all(self):
        grants = ["GRANT ALL PRIVILEGES ON `app`.* TO `x`@`%`"]
        has_write, is_super = analyze_mysql_grants(grants, "x")
        assert has_write is True
        assert is_super is False  # 仅库级 ALL，非全局

    def test_root_superuser(self):
        grants = ["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost` WITH GRANT OPTION"]
        has_write, is_super = analyze_mysql_grants(grants, "root")
        assert has_write is True
        assert is_super is True

    def test_username_root_is_super(self):
        # 即使 grants 看不出，用户名 root 直接判超级用户
        has_write, is_super = analyze_mysql_grants(["GRANT USAGE ON *.* TO `root`@`%`"], "root")
        assert is_super is True

    def test_usage_only_no_write(self):
        has_write, is_super = analyze_mysql_grants(["GRANT USAGE ON *.* TO `guest`@`%`"], "guest")
        assert has_write is False
        assert is_super is False

    def test_global_grant_option_is_super(self):
        grants = ["GRANT SELECT ON *.* TO `admin`@`%` WITH GRANT OPTION"]
        _, is_super = analyze_mysql_grants(grants, "admin")
        assert is_super is True


class TestSqliteProbe:
    def test_sqlite_connectivity(self, tmp_path):
        db = tmp_path / "x.sqlite3"
        sqlite3.connect(db).close()
        cfg = ConnectionConfig(engine="sqlite", database=str(db), environment="local")
        r = probe_connection(cfg, None)
        assert r.ok
        assert r.has_write is None  # sqlite 无账号权限模型
        assert r.privileged is False

    def test_sqlite_missing_file(self, tmp_path):
        cfg = ConnectionConfig(engine="sqlite", database=str(tmp_path / "nope.sqlite3"),
                               environment="local")
        # sqlite 会自动建文件，所以仍连得上；这里只确认不崩
        assert probe_connection(cfg, None).ok


class TestSSHProbe:
    def test_no_jump_hosts(self):
        cfg = ConnectionConfig(engine="mysql", host="127.0.0.1", port=3306,
                               environment="dev", user="u", password="plain://x")
        r = probe_ssh(cfg)
        assert not r.ok
        assert "未配置 SSH 跳板" in r.message

    def test_unreachable_jump_fails(self, monkeypatch):
        # 缩短隧道就绪等待：ssh 对无效主机名的失败速度依赖 DNS/网络环境（曾把全量
        # 拖到 15s+ 被误判为挂死），压到 2s 让用例确定性快速失败
        monkeypatch.setattr("dbmcp.tunnel._READY_TIMEOUT_S", 2.0)
        cfg = ConnectionConfig(engine="mysql", host="10.0.0.1", port=3306,
                               environment="prod", user="u", password="plain://x",
                               jump_hosts=["127.0.0.1:1"])
        r = probe_ssh(cfg)
        assert not r.ok  # 不可达跳板 → 快速失败


class TestPrivileged:
    def test_privileged_property(self):
        from dbmcp.probe import ProbeResult
        assert ProbeResult(ok=True, has_write=True, is_superuser=False).privileged is True
        assert ProbeResult(ok=True, has_write=False, is_superuser=True).privileged is True
        assert ProbeResult(ok=True, has_write=False, is_superuser=False).privileged is False
        assert ProbeResult(ok=True, has_write=None, is_superuser=None).privileged is False
