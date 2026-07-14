import pytest

from dbmcp.config import AppConfig, JumpHost, load_config, parse_hostspec, save_config

VALID_YAML = """
projects:
  demo:
    connections:
      main:
        engine: mysql
        host: 127.0.0.1
        database: demo
        environment: dev
        user: reader
        password: env://DEMO_PW
      cache-file:
        engine: sqlite
        database: ./demo.sqlite3
"""


def test_load_valid(tmp_path):
    path = tmp_path / "conn.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    cfg = load_config(path)
    conn = cfg.get_connection("demo", "main")
    assert conn.engine == "mysql"
    assert conn.policy.max_rows == 1000  # 默认策略
    assert cfg.get_connection("demo", "cache-file").engine == "sqlite"


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/conn.yaml")


def test_mysql_requires_credentials():
    with pytest.raises(ValueError, match="缺少必填字段"):
        AppConfig.model_validate(
            {"projects": {"p": {"connections": {"c": {"engine": "mysql", "host": "x"}}}}}
        )


def test_sqlite_requires_database():
    with pytest.raises(ValueError, match="sqlite"):
        AppConfig.model_validate(
            {"projects": {"p": {"connections": {"c": {"engine": "sqlite"}}}}}
        )


def test_unknown_project_and_connection():
    cfg = AppConfig.model_validate(
        {"projects": {"p": {"connections": {"c": {"engine": "sqlite", "database": ":memory:"}}}}}
    )
    with pytest.raises(KeyError, match="不存在"):
        cfg.get_connection("nope", "c")
    with pytest.raises(KeyError, match="不存在"):
        cfg.get_connection("p", "nope")


class TestJumpHost:
    def test_parse_hostspec(self):
        assert parse_hostspec("bob@host:2222") == {"host": "host", "user": "bob", "port": 2222}
        assert parse_hostspec("host") == {"host": "host"}
        assert parse_hostspec("host:22") == {"host": "host", "port": 22}
        with pytest.raises(ValueError):
            parse_hostspec("  ")

    def test_bare_string_coerced(self):
        jh = JumpHost.model_validate("alice@bastion:22")
        assert jh.host == "bastion" and jh.user == "alice" and jh.port == 22
        assert jh.label() == "alice@bastion:22"

    def test_legacy_jump_hosts_strings_load(self):
        cfg = AppConfig.model_validate({"projects": {"p": {"connections": {"c": {
            "engine": "mysql", "host": "h", "user": "u", "password": "env://X",
            "jump_hosts": ["b1", "alice@b2:2222"]}}}}})
        hops = cfg.get_connection("p", "c").jump_hosts
        assert [h.label() for h in hops] == ["b1", "alice@b2:2222"]

    def test_ssh_identities_roundtrip(self, tmp_path):
        cfg = AppConfig.model_validate({
            "ssh_identities": {"id1": {"key_path": "/k1", "known_hosts_path": "/kh1"}},
            "projects": {"p": {"connections": {"c": {
                "engine": "mysql", "host": "h", "user": "u", "password": "env://X",
                "jump_hosts": [{"host": "b1", "user": "a", "identity": "id1"}]}}}}})
        path = tmp_path / "c.yaml"
        save_config(cfg, path)
        reloaded = load_config(path)
        assert reloaded.ssh_identities["id1"].key_path == "/k1"
        assert reloaded.ssh_identities["id1"].known_hosts_path == "/kh1"
        hop = reloaded.get_connection("p", "c").jump_hosts[0]
        assert hop.host == "b1" and hop.user == "a" and hop.identity == "id1"
