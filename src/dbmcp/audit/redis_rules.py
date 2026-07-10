"""Redis 命令分类：读直通 / 写审批 / 高危命令（DESIGN.md 第五节）。

与 SQL 一致的默认拒绝原则：不认识的命令一律按 CRITICAL 写操作处理。
KEYS 虽是"读"，但会阻塞实例，按 CRITICAL 管控。
"""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass

# 只读命令：直接放行
READ_COMMANDS = {
    "GET", "MGET", "STRLEN", "EXISTS", "TYPE", "TTL", "PTTL",
    "HGET", "HMGET", "HGETALL", "HKEYS", "HVALS", "HLEN", "HEXISTS", "HSTRLEN",
    "LRANGE", "LLEN", "LINDEX", "LPOS",
    "SMEMBERS", "SISMEMBER", "SMISMEMBER", "SCARD", "SRANDMEMBER",
    "ZRANGE", "ZRANGEBYSCORE", "ZREVRANGE", "ZSCORE", "ZMSCORE", "ZCARD", "ZCOUNT", "ZRANK", "ZREVRANK",
    "SCAN", "HSCAN", "SSCAN", "ZSCAN",
    "DBSIZE", "PING", "ECHO", "INFO", "TIME", "MEMORY", "OBJECT", "RANDOMKEY",
    "GETRANGE", "BITCOUNT", "GETBIT", "DUMP", "XLEN", "XRANGE", "XREAD",
}

# 普通写命令：走审批，MEDIUM
WRITE_COMMANDS = {
    "SET", "SETEX", "PSETEX", "SETNX", "MSET", "MSETNX", "APPEND", "GETSET", "GETDEL", "SETRANGE",
    "DEL", "UNLINK", "EXPIRE", "PEXPIRE", "EXPIREAT", "PEXPIREAT", "PERSIST", "TOUCH",
    "INCR", "DECR", "INCRBY", "DECRBY", "INCRBYFLOAT", "SETBIT",
    "HSET", "HMSET", "HSETNX", "HDEL", "HINCRBY", "HINCRBYFLOAT",
    "LPUSH", "RPUSH", "LPUSHX", "RPUSHX", "LPOP", "RPOP", "LSET", "LREM", "LTRIM", "LINSERT", "RPOPLPUSH", "LMOVE",
    "SADD", "SREM", "SPOP", "SMOVE",
    "ZADD", "ZREM", "ZINCRBY", "ZPOPMIN", "ZPOPMAX", "ZREMRANGEBYRANK", "ZREMRANGEBYSCORE",
    "XADD", "XDEL", "XTRIM",
}

# 影响面大的写命令：走审批，HIGH
HIGH_RISK_COMMANDS = {
    "RENAME", "RENAMENX",       # 会覆盖目标 key
    "SDIFFSTORE", "SINTERSTORE", "SUNIONSTORE", "ZRANGESTORE",  # 批量写目标 key
    "COPY", "RESTORE", "SORT",  # SORT 带 STORE 时写；统一按 HIGH
    "SETEX",
}

# 危险/运维命令：走审批，CRITICAL
CRITICAL_COMMANDS = {
    "FLUSHDB", "FLUSHALL",      # 清库
    "KEYS",                     # 阻塞实例
    "CONFIG", "SHUTDOWN", "DEBUG", "RESET", "FAILOVER",
    "SCRIPT", "EVAL", "EVALSHA", "FUNCTION", "FCALL",  # 任意脚本
    "SLAVEOF", "REPLICAOF", "CLUSTER", "MIGRATE", "SWAPDB", "MOVE",
    "SAVE", "BGSAVE", "BGREWRITEAOF", "ACL", "CLIENT",
}


@dataclass
class RedisVerdict:
    readonly: bool
    level: str  # LOW / MEDIUM / HIGH / CRITICAL
    reason: str
    command: str
    args: list[str]


def parse_command(raw: str) -> list[str]:
    """按 shell 规则切分命令（支持引号包含空格的 value）。"""
    parts = shlex.split(raw)
    if not parts:
        raise ValueError("空命令")
    return parts


def classify_command(raw: str) -> RedisVerdict:
    try:
        parts = parse_command(raw)
    except ValueError as e:
        return RedisVerdict(False, "CRITICAL", f"命令解析失败: {e}", "", [])

    cmd = parts[0].upper()
    args = parts[1:]

    if cmd in READ_COMMANDS:
        return RedisVerdict(True, "LOW", "只读命令", cmd, args)
    if cmd in CRITICAL_COMMANDS:
        return RedisVerdict(False, "CRITICAL", f"{cmd} 属于危险/运维命令（清库、阻塞、脚本或拓扑变更）", cmd, args)
    if cmd in HIGH_RISK_COMMANDS:
        return RedisVerdict(False, "HIGH", f"{cmd} 影响面大（覆盖/批量写入目标 key）", cmd, args)
    if cmd in WRITE_COMMANDS:
        return RedisVerdict(False, "MEDIUM", f"{cmd} 属于写命令", cmd, args)
    return RedisVerdict(False, "CRITICAL", f"未知命令 {cmd}，按最高风险处理（默认拒绝）", cmd, args)


def command_fingerprint(raw: str) -> str:
    """命令规范化指纹：命令名大写 + 参数原样。"""
    try:
        parts = parse_command(raw)
        normalized = " ".join([parts[0].upper(), *parts[1:]])
    except ValueError:
        normalized = " ".join(raw.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
