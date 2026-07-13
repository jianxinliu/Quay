"""Redis 命令文档数据集（命令窗口右侧文档面板）测试。纯数据，无需 redis。"""

from dbmcp.redis_docs import REDIS_COMMANDS, lookup


def test_lookup_basic():
    d = lookup("GET")
    assert d is not None
    assert "获取" in d["summary"] or "值" in d["summary"]
    assert d["url"].startswith("https://redis.io/docs/latest/commands/")


def test_lookup_case_insensitive_and_first_token():
    assert lookup("del") == lookup("DEL")
    # 只取首个 token（命令 + 参数）
    assert lookup("ZADD key 1 a") == lookup("zadd")


def test_lookup_empty_and_unknown_return_none():
    assert lookup("") is None
    assert lookup("   ") is None
    assert lookup("NOTACOMMAND") is None


def test_dataset_well_formed():
    assert len(REDIS_COMMANDS) >= 150
    for name, entry in REDIS_COMMANDS.items():
        assert name == name.upper(), f"命令名应大写: {name}"
        for field in ("summary", "syntax", "group", "url"):
            assert entry.get(field), f"{name} 缺字段 {field}"
        assert entry["url"].startswith("https://redis.io/docs/latest/commands/"), name
