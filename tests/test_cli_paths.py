"""命令行的「零文件首跑」：默认路径按运行形态选、配置缺了从模板生成、token 存 env 文件。

uvx / pipx 装的命令行没有仓库目录，config/ data/ 这类相对路径会随 cwd 漂移；
这些用例锁住两种形态各自的默认值与首次启动的生成行为。
"""

from __future__ import annotations

import os
import stat

import pytest

from dbmcp import __main__ as cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DBM_HOME", str(tmp_path / "home"))
    for k in ("DBM_CONFIG", "DBM_DATA_DIR", "DBM_ENV_FILE", "DBM_ADMIN_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path / "home"


class TestDefaultPaths:
    def test_installed_cli_uses_user_dir(self, home):
        assert cli.default_config_path() == str(home / "connections.yaml")
        assert cli.default_data_dir() == str(home / "data")
        assert cli.env_file_path() == home / "env"

    def test_repo_layout_keeps_relative_paths(self, home, tmp_path):
        (tmp_path / "config").mkdir()
        assert cli.default_config_path() == "config/connections.yaml"
        assert cli.default_data_dir() == "data"

    def test_env_overrides_win(self, home, monkeypatch):
        monkeypatch.setenv("DBM_CONFIG", "/x/c.yaml")
        monkeypatch.setenv("DBM_DATA_DIR", "/x/d")
        monkeypatch.setenv("DBM_ENV_FILE", "/x/env")
        assert cli.default_config_path() == "/x/c.yaml"
        assert cli.default_data_dir() == "/x/d"
        assert str(cli.env_file_path()) == "/x/env"


class TestEnsureConfig:
    def test_creates_template_with_absolute_demo_db(self, home):
        cfg = home / "connections.yaml"
        assert cli.ensure_config(cfg, home / "data") is True
        text = cfg.read_text(encoding="utf-8")
        assert cli._TEMPLATE_DEMO_DB not in text
        assert (home / "data" / "demo" / "shop.sqlite3").as_posix() in text
        # 生成的配置必须能被 load_config 读进去，且只有示例库这一条真实连接
        from dbmcp.config import load_config
        conf = load_config(cfg)
        assert list(conf.projects["demo"].connections) == ["shop"]

    def test_existing_config_untouched(self, home):
        cfg = home / "connections.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("projects: {}\n", encoding="utf-8")
        assert cli.ensure_config(cfg, home / "data") is False
        assert cfg.read_text(encoding="utf-8") == "projects: {}\n"


class TestEnvFile:
    def test_load_env_file_sets_missing_only(self, home, monkeypatch):
        env = home / "env"
        env.parent.mkdir(parents=True)
        env.write_text('# 注释\nDBM_ADMIN_TOKEN="abc"\nexport DEMO_PW=x=y\nALREADY=new\n',
                       encoding="utf-8")
        monkeypatch.setenv("ALREADY", "old")
        monkeypatch.delenv("DEMO_PW", raising=False)
        assert cli.load_env_file() == 2
        assert os.environ["DBM_ADMIN_TOKEN"] == "abc"
        assert os.environ["DEMO_PW"] == "x=y"       # 值里的 = 保留
        assert os.environ["ALREADY"] == "old"       # 已设置的不覆盖
        monkeypatch.delenv("DBM_ADMIN_TOKEN")
        monkeypatch.delenv("DEMO_PW")

    def test_missing_env_file_is_fine(self, home):
        assert cli.load_env_file() == 0

    def test_persist_token_creates_600_file_and_replaces_in_place(self, home):
        env = home / "env"
        assert cli.persist_admin_token("t1") is True
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        env.write_text(env.read_text(encoding="utf-8") + "OTHER=1", encoding="utf-8")  # 无尾换行
        assert cli.persist_admin_token("t2") is True
        lines = env.read_text(encoding="utf-8").splitlines()
        assert lines == ["DBM_ADMIN_TOKEN=t2", "OTHER=1"]   # 原地替换，不留两行同名值
        assert cli.load_env_file() == 2 and os.environ["DBM_ADMIN_TOKEN"] == "t2"
