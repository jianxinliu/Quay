import sqlite3
import time

import pytest

from dbmcp.config import AppConfig
from dbmcp.engines import EnginePool
from dbmcp.metadata import MetadataCache


@pytest.fixture
def setup(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
        CREATE INDEX idx_users_email ON users (email);
        INSERT INTO users (name, email) VALUES ('a', 'a@x'), ('b', 'b@x'), ('c', 'c@x');
        """
    )
    conn.commit()
    conn.close()

    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "local"}}}}}
    )
    pool = EnginePool()
    cache = MetadataCache(tmp_path / "meta.sqlite3", pool)
    yield cfg, cache
    cache.close()
    pool.dispose()


def test_collect_and_cache(setup):
    cfg, cache = setup
    conn_cfg = cfg.get_connection("demo", "main")
    meta = cache.get("demo", "main", conn_cfg, "users")
    assert meta.row_estimate == 3
    assert {c["name"] for c in meta.columns} == {"id", "name", "email"}
    assert "email" in meta.indexed_columns
    assert "id" in meta.indexed_columns  # 主键


def test_served_from_cache(setup):
    cfg, cache = setup
    conn_cfg = cfg.get_connection("demo", "main")
    first = cache.get("demo", "main", conn_cfg, "users")
    # 第二次命中缓存（fetched_at 不变）
    second = cache.get("demo", "main", conn_cfg, "users")
    assert second.fetched_at == first.fetched_at


def test_ttl_expiry_refreshes(tmp_path, setup):
    cfg, cache = setup
    cache._ttl_s = 0  # 立即过期
    conn_cfg = cfg.get_connection("demo", "main")
    first = cache.get("demo", "main", conn_cfg, "users")
    time.sleep(0.01)
    second = cache.get("demo", "main", conn_cfg, "users")
    assert second.fetched_at > first.fetched_at


def test_indexed_columns_helper(setup):
    cfg, cache = setup
    conn_cfg = cfg.get_connection("demo", "main")
    meta = cache.get("demo", "main", conn_cfg, "users")
    # name 无索引
    assert "name" not in meta.indexed_columns
