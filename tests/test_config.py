import pytest

from dbmcp.config import AppConfig, load_config

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
