"""Redis 控制台服务层测试（对标 Medis）：库列表只列有数据的、键扫描/取值、
命令窗口读直通 vs 写确认旁路。需本地 redis（127.0.0.1:6379，无认证）；不可用则整体 skip。

测试用独立 db（14），跑前后 flush，绝不碰其他库。
"""

import pytest

from dbmcp.audit.log import AuditStore
from dbmcp.config import AppConfig
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-redis")
TEST_DB = 14


def _redis_available() -> bool:
    try:
        import redis
        c = redis.Redis(host="127.0.0.1", port=6379, socket_connect_timeout=1)
        c.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="本地 redis 不可用")


@pytest.fixture
def service(tmp_path):
    import redis
    raw = redis.Redis(host="127.0.0.1", port=6379, db=TEST_DB, decode_responses=True)
    raw.flushdb()
    raw.set("offer:a:1", "x")
    raw.hset("cfg", mapping={"k1": "v1", "k2": "v2"})
    cfg = AppConfig.model_validate({
        "projects": {"local": {"connections": {"r": {
            "engine": "redis", "environment": "local",
            "host": "127.0.0.1", "port": 6379, "database": str(TEST_DB),
        }}}}
    })
    svc = DbmService(cfg, AuditStore(tmp_path / "audit.sqlite3"))
    yield svc
    svc.close()
    raw.flushdb()


class TestBrowse:
    def test_databases_only_lists_with_data(self, service):
        dbs = service.redis_databases("local", "r", CALLER)
        idxs = [d["db"] for d in dbs]
        assert TEST_DB in idxs
        # 每个列出的库都必须有键（keys > 0）——空库不出现
        assert all(d["keys"] > 0 for d in dbs)

    def test_keys_and_value(self, service):
        out = service.redis_keys("local", "r", CALLER, db=TEST_DB, pattern="*")
        names = {k["key"]: k["type"] for k in out["keys"]}
        assert names.get("offer:a:1") == "string"
        assert names.get("cfg") == "hash"

        val = service.redis_value("local", "r", "cfg", CALLER, db=TEST_DB)
        assert val["type"] == "hash"
        assert val["fields"] == {"k1": "v1", "k2": "v2"}
        assert val["ttl"] == -1  # 永久

    def test_non_msgpack_binary_shown_as_hex(self, service):
        """非结构化二进制（既非 UTF-8 也非 msgpack 容器）→ BINARY HEX，不崩。"""
        import redis
        raw = redis.Redis(host="127.0.0.1", port=6379, db=TEST_DB)
        raw.hset("bink", "f", b"\xff\xfe\xfd\xfc")  # 任意二进制
        val = service.redis_value("local", "r", "bink", CALLER, db=TEST_DB)
        assert val["fields"]["f"].startswith("BINARY HEX ")

    def test_msgpack_value_decoded_to_object(self, service):
        """msgpack 编码的值应解码成对象（对标 Medis 的 JSON 展示）。"""
        import msgpack
        import redis
        raw = redis.Redis(host="127.0.0.1", port=6379, db=TEST_DB)
        packed = msgpack.packb({"OfferId": "admaven", "Cap": 2700}, use_bin_type=True)
        raw.hset("mp", "admaven", packed)
        val = service.redis_value("local", "r", "mp", CALLER, db=TEST_DB)
        assert val["fields"]["admaven"] == {"OfferId": "admaven", "Cap": 2700}


class TestCommandWindow:
    def test_read_command_direct(self, service):
        out = service.admin_redis_run("local", "r", "GET offer:a:1", CALLER, db=TEST_DB)
        assert out["kind"] == "read"
        assert out["value"] == "x"

    def test_write_without_confirm_returns_risk(self, service):
        out = service.admin_redis_run("local", "r", "SET newk 1", CALLER, db=TEST_DB)
        assert out["kind"] == "confirm"
        assert out["risk"]["level"]  # 有风险等级
        # 未确认 → 不应真正写入
        import redis
        raw = redis.Redis(host="127.0.0.1", port=6379, db=TEST_DB, decode_responses=True)
        assert raw.exists("newk") == 0

    def test_write_with_confirm_executes(self, service):
        out = service.admin_redis_run("local", "r", "SET newk 42", CALLER, db=TEST_DB, confirm=True)
        assert out["kind"] == "write"
        import redis
        raw = redis.Redis(host="127.0.0.1", port=6379, db=TEST_DB, decode_responses=True)
        assert raw.get("newk") == "42"
        # 审计：写命令记为 admin_execute（后台旁路）
        logs = service.store.recent()
        assert any(rec["tool"] == "admin_execute" for rec in logs)
