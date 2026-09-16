"""风险评估取元数据时带上 schema：`console.ev` 这类限定名要能查到行数与索引。

只按表名查会落到连接的默认 schema 上，带 schema 的表就查不到行数与索引，
风险报告缺这两项、每次确认还要重新反射一遍。

真实库部分需要本地容器，连不上则跳过：
    docker run -d --name dbm-meta-pg -e POSTGRES_PASSWORD=123456 -e POSTGRES_DB=testdb \\
        -p 15432:5432 postgres:17
    docker run -d --name dbm-meta-mysql -e MYSQL_ROOT_PASSWORD=123456 -e MYSQL_DATABASE=testdb \\
        -p 13306:3306 mysql:8.4
    PG：console.ev（5000 行，gaid 上有索引）与 public.ev（3 行），ANALYZE；
    MySQL：other.orders（2000 行，uid 上有索引），ANALYZE TABLE。
"""

import os
import sqlite3

import pytest
from sqlalchemy import create_engine, text

from dbmcp import engines
from dbmcp.audit.log import AuditStore
from dbmcp.audit.risk import assess
from dbmcp.config import AppConfig
from dbmcp.metadata import MetadataCache
from dbmcp.service import DbmService


class _Meta:
    def __init__(self, rows, indexed):
        self.row_estimate = rows
        self.indexed_columns = set(indexed)


def test_risk_asks_for_qualified_name():
    asked = []

    def provider(name):
        asked.append(name)
        return _Meta(5_000_000, {"id"}) if name == "console.ev" else None

    r = assess("UPDATE console.ev SET note = 'x' WHERE gaid = 'a'", "postgres", provider)
    assert "console.ev" in asked
    assert r.tables == ["ev"]  # 展示仍是表名
    assert r.row_estimate == 5_000_000
    assert r.uses_index is False  # 元数据真的被用上了：gaid 不在索引列里


def test_unqualified_table_uses_execution_schema(tmp_path):
    """查询台选了执行 schema 时，没写 schema 的表按那个 schema 找。"""
    cfg = AppConfig.model_validate({"projects": {"p": {"connections": {"c": {
        "engine": "sqlite", "database": str(tmp_path / "x.sqlite3")}}}}})
    svc = DbmService(cfg, AuditStore(":memory:"))
    asked = []
    svc.metadata = type("M", (), {"get": lambda self, p, c, cfg, t, database=None:
                                  asked.append(t),
                                  "close": lambda self: None})()
    try:
        provider = svc._meta_provider("p", "c", cfg, schema="console")
        provider("ev")
        provider("other.t")
        assert asked == ["console.ev", "other.t"]
    finally:
        svc.close()


def test_sqlite_attached_schema(tmp_path):
    main = tmp_path / "main.sqlite3"
    aux = tmp_path / "aux.sqlite3"
    with sqlite3.connect(aux) as conn:
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);"
                           "CREATE INDEX idx_v ON t (v);"
                           "INSERT INTO t (v) VALUES ('a'), ('b'), ('c');")
    sqlite3.connect(main).close()
    eng = create_engine(f"sqlite:///{main}")

    from sqlalchemy import event

    @event.listens_for(eng, "connect")
    def _attach(dbapi_conn, _):
        dbapi_conn.execute(f"ATTACH DATABASE '{aux}' AS aux")

    try:
        meta = engines.collect_table_meta(eng, "sqlite", "t", "aux")
        assert meta["row_estimate"] == 3
        assert any(i["columns"] == ["v"] for i in meta["indexes"])
    finally:
        eng.dispose()


# ---------------- 真实 PG / MySQL ----------------

PG_URL = os.environ.get("DBM_E2E_PG_URL", "postgresql+psycopg://postgres:123456@127.0.0.1:15432/testdb")
MY_URL = os.environ.get("DBM_E2E_MYSQL_URL", "mysql+pymysql://root:123456@127.0.0.1:13306/testdb")


def _ready(url, probe):
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            ok = conn.execute(text(probe)).scalar()
        eng.dispose()
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(
    not _ready(PG_URL, "SELECT to_regclass('console.ev') IS NOT NULL"),
    reason="需要本地 PG（console.ev）")
mysql = pytest.mark.skipif(
    not _ready(MY_URL, "SELECT COUNT(*) FROM information_schema.tables"
                       " WHERE table_schema='other' AND table_name='orders'"),
    reason="需要本地 MySQL（other.orders）")


def _cache(tmp_path, conn_cfg):
    cfg = AppConfig.model_validate({"projects": {"p": {"connections": {"c": conn_cfg}}}})
    svc = DbmService(cfg, AuditStore(":memory:"))
    svc.metadata = MetadataCache(tmp_path / "m.sqlite3", svc.pool)
    return svc, cfg.get_connection("p", "c")


@pg
def test_pg_schema_qualified_estimate(tmp_path):
    eng = create_engine(PG_URL)
    try:
        # 同名表在两个 schema 里：必须取到对的那张
        assert engines.estimate_row_count(eng, "postgres", "ev", "console") == 5000
        assert engines.estimate_row_count(eng, "postgres", "ev", "public") == 3
        assert engines.estimate_row_count(eng, "postgres", "ev") == 3  # 默认 schema
    finally:
        eng.dispose()

    svc, cfg = _cache(tmp_path, {
        "engine": "postgres", "host": "127.0.0.1", "port": 15432, "database": "testdb",
        "user": "postgres", "password": "plain://123456"})
    try:
        r = assess("UPDATE console.ev SET note = 'x' WHERE gaid = 'g1'", "postgres",
                   svc._meta_provider("p", "c", cfg))
        assert r.row_estimate == 5000
        assert r.uses_index is True
        # 查询台选了 console 作为执行 schema、SQL 不写 schema
        r = assess("DELETE FROM ev WHERE note = 'n'", "postgres",
                   svc._meta_provider("p", "c", cfg, schema="console"))
        assert r.row_estimate == 5000 and r.uses_index is False
        # 第二次命中缓存，不再反射
        seen = []
        orig = engines.collect_table_meta
        engines.collect_table_meta = lambda *a, **k: seen.append(a) or orig(*a, **k)
        try:
            assess("UPDATE console.ev SET note = 'y' WHERE id = 1", "postgres",
                   svc._meta_provider("p", "c", cfg))
        finally:
            engines.collect_table_meta = orig
        assert seen == []
    finally:
        svc.close()


@mysql
def test_mysql_other_database_estimate(tmp_path):
    svc, cfg = _cache(tmp_path, {
        "engine": "mysql", "host": "127.0.0.1", "port": 13306, "database": "testdb",
        "user": "root", "password": "plain://123456"})
    try:
        r = assess("UPDATE other.orders SET note = 'y' WHERE uid = 3", "mysql",
                   svc._meta_provider("p", "c", cfg))
        assert r.row_estimate == 2000
        assert r.uses_index is True
        r = assess("DELETE FROM orders WHERE note = 'x'", "mysql",
                   svc._meta_provider("p", "c", cfg, schema="other"))
        assert r.row_estimate == 2000 and r.uses_index is False
    finally:
        svc.close()
