"""Redis 命令分类测试。"""

import pytest

from dbmcp.audit.redis_rules import classify_command, command_fingerprint


class TestRead:
    @pytest.mark.parametrize("cmd", [
        "GET user:1", "get user:1", "MGET a b c", "HGETALL h", "SCAN 0",
        "TTL k", "DBSIZE", "PING", "INFO", "ZRANGE z 0 -1",
    ])
    def test_read_commands_pass(self, cmd):
        v = classify_command(cmd)
        assert v.readonly, f"{cmd} 应放行: {v.reason}"
        assert v.level == "LOW"


class TestWrite:
    @pytest.mark.parametrize("cmd", ["SET k v", "DEL k", "HSET h f v", "LPUSH l a", "EXPIRE k 60", "INCR counter"])
    def test_write_medium(self, cmd):
        v = classify_command(cmd)
        assert not v.readonly
        assert v.level == "MEDIUM"

    @pytest.mark.parametrize("cmd", ["RENAME a b", "SINTERSTORE dst a b", "COPY a b"])
    def test_write_high(self, cmd):
        v = classify_command(cmd)
        assert not v.readonly
        assert v.level == "HIGH"


class TestCritical:
    @pytest.mark.parametrize("cmd", [
        "FLUSHDB", "FLUSHALL", "KEYS *", "CONFIG SET maxmemory 0",
        "EVAL 'return 1' 0", "SHUTDOWN", "REPLICAOF host 6379",
    ])
    def test_critical(self, cmd):
        v = classify_command(cmd)
        assert not v.readonly
        assert v.level == "CRITICAL"

    def test_unknown_command_critical(self):
        v = classify_command("SOMENEWCMD arg")
        assert not v.readonly
        assert v.level == "CRITICAL"
        assert "未知命令" in v.reason

    def test_empty_rejected(self):
        v = classify_command("   ")
        assert not v.readonly


class TestParsing:
    def test_quoted_value_with_spaces(self):
        v = classify_command('SET greeting "hello world"')
        assert v.command == "SET"
        assert v.args == ["greeting", "hello world"]

    def test_fingerprint_case_insensitive_command(self):
        assert command_fingerprint("set k v") == command_fingerprint("SET k v")
        # 参数大小写敏感（key 是大小写敏感的）
        assert command_fingerprint("SET K v") != command_fingerprint("SET k v")
