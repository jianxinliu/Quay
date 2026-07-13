"""Redis 适配：连接池 + 命令执行，SSH 多跳隧道复用 tunnel 模块。

Redis 无 reader/writer 双账号概念（通常单 AUTH），写命令的防线是审批流本身；
配置了 writer 账号（Redis 6+ ACL）时写命令用 writer 建独立客户端。
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass
from typing import Any

import redis

from .config import ConnectionConfig
from .engines import DEFAULT_IDLE_RECLAIM_S, Role
from .secrets import resolve_secret
from .tunnel import SSHTunnel, open_tunnel


@dataclass
class RedisResult:
    value: Any
    duration_ms: int


@dataclass
class _PooledRedis:
    client: redis.Redis
    tunnel: SSHTunnel | None
    last_used: float

    def dispose(self) -> None:
        try:
            self.client.close()
        finally:
            if self.tunnel is not None:
                self.tunnel.close()


class RedisPool:
    def __init__(self, idle_reclaim_s: int = DEFAULT_IDLE_RECLAIM_S):
        # key 含 db 维度：Medis 式切库浏览时每个 (conn, role, db) 一个独立 client，
        # 避免在共享 client 上 SELECT 造成并发竞态（db=None 表示用 cfg.database）。
        self._entries: dict[tuple[str, str, Role, int], _PooledRedis] = {}
        self._lock = threading.Lock()
        self._idle_reclaim_s = idle_reclaim_s

    def get(
        self, project: str, connection: str, cfg: ConnectionConfig,
        role: Role = "reader", db: int | None = None,
    ) -> redis.Redis:
        eff_db = int(cfg.database or 0) if db is None else db
        key = (project, connection, role, eff_db)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry.tunnel is None or entry.tunnel.is_alive()):
                entry.last_used = time.monotonic()
                return entry.client
            if entry is not None:
                entry.dispose()
                del self._entries[key]
            entry = _build_client(cfg, role, db=eff_db)
            self._entries[key] = entry
            return entry.client

    def reap_idle(self) -> int:
        now = time.monotonic()
        reaped = 0
        with self._lock:
            for key in list(self._entries):
                entry = self._entries[key]
                if now - entry.last_used >= self._idle_reclaim_s:
                    entry.dispose()
                    del self._entries[key]
                    reaped += 1
        return reaped

    def dispose_connection(self, project: str, connection: str) -> None:
        with self._lock:
            for key in [k for k in self._entries if k[0] == project and k[1] == connection]:
                self._entries.pop(key).dispose()

    def dispose(self) -> None:
        with self._lock:
            for entry in self._entries.values():
                entry.dispose()
            self._entries.clear()


def build_probe_client(cfg: ConnectionConfig, role: Role = "reader") -> _PooledRedis:
    """临时建 Redis 客户端（含隧道），已 PING。调用方用完必须 .dispose()。"""
    return _build_client(cfg, role)


def _build_client(cfg: ConnectionConfig, role: Role, db: int | None = None) -> _PooledRedis:
    tunnel: SSHTunnel | None = None
    host, port = cfg.host or "127.0.0.1", cfg.port or 6379
    if cfg.jump_hosts:
        tunnel = open_tunnel(host, port, cfg.jump_hosts, cfg.ssh_options)
        host, port = "127.0.0.1", tunnel.local_port

    user, password = None, None
    if role == "writer" and cfg.writer is not None:
        user, password = cfg.writer.user, resolve_secret(cfg.writer.password)
    else:
        user = cfg.user
        # 无认证实例：password 引用为空 → None（redis-py 不发 AUTH）
        password = resolve_secret(cfg.password) if cfg.password else None

    eff_db = int(cfg.database or 0) if db is None else db
    timeout = cfg.policy.statement_timeout_s
    try:
        client = redis.Redis(
            host=host,
            port=port,
            db=eff_db,
            username=user,
            password=password or None,
            # 取原始 bytes 自行解码：值可能是二进制（序列化/protobuf 等），
            # decode_responses=True 会在 redis-py 内部 UTF-8 解码时对二进制抛
            # UnicodeDecodeError。改由 _jsonable 逐值处理：能 UTF-8 就文本，否则 BINARY HEX。
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=timeout,
        )
        client.ping()
    except Exception:
        if tunnel is not None:
            tunnel.close()
        raise
    return _PooledRedis(client=client, tunnel=tunnel, last_used=time.monotonic())


def run_command(
    client: redis.Redis, parts: list[str], max_cell_chars: int = 4096
) -> RedisResult:
    """执行已通过分类/审批的命令。调用方负责传入 parse_command 的结果。"""
    start = dt.datetime.now()
    value = client.execute_command(*parts)
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return RedisResult(value=_truncate(_jsonable(value), max_cell_chars), duration_ms=duration_ms)


# ---------- Medis 式浏览（只读探测，不经审批）----------

def keyspace_dbs(client: redis.Redis) -> list[dict]:
    """`INFO keyspace` 解析出有数据的库（redis 只在有键时才列 dbN）。

    返回 [{"db": 0, "keys": 12, "expires": 3}, ...]，按库号升序。空库不出现。
    """
    info = client.info("keyspace")  # {'db0': {'keys': 12, 'expires': 3, 'avg_ttl': 0}, ...}
    out = []
    for name, stats in info.items():
        if not name.startswith("db"):
            continue
        try:
            idx = int(name[2:])
        except ValueError:
            continue
        keys = int(stats.get("keys", 0)) if isinstance(stats, dict) else 0
        if keys <= 0:
            continue
        out.append({"db": idx, "keys": keys,
                    "expires": int(stats.get("expires", 0)) if isinstance(stats, dict) else 0})
    return sorted(out, key=lambda d: d["db"])


def scan_keys(
    client: redis.Redis, pattern: str = "*", max_keys: int = 2000, scan_count: int = 500,
) -> dict:
    """SCAN 出至多 max_keys 个键并逐键取类型（pipeline）。

    返回 {"keys": [{"key": k, "type": t}, ...], "truncated": bool}。
    用 SCAN 而非 KEYS：不阻塞 redis、可增量；超过上限即截断并标记。
    """
    names: list[str] = []
    cursor = 0
    truncated = False
    while True:
        cursor, batch = client.scan(cursor=cursor, match=pattern or "*", count=scan_count)
        names.extend(batch)
        if len(names) >= max_keys:
            names = names[:max_keys]
            truncated = truncated or cursor != 0
            break
        if cursor == 0:
            break
    if not names:
        return {"keys": [], "truncated": truncated}
    pipe = client.pipeline(transaction=False)
    for k in names:
        pipe.type(k)
    types = pipe.execute()
    keys = [{"key": _jsonable(k), "type": _jsonable(t)}
            for k, t in zip(names, types, strict=False)]
    keys.sort(key=lambda d: d["key"])
    return {"keys": keys, "truncated": truncated}


def read_value(client: redis.Redis, key: str, max_cell_chars: int = 4096,
               max_items: int = 1000) -> dict:
    """按类型取键值 + 元信息（TTL / 内存 / 编码），对标 Medis 键详情面板。

    string → {"value": s}；hash → {"fields": {f: v}}；list → {"items": [...]}；
    set → {"members": [...]}；zset → {"members": [{"member","score"}]}。
    """
    ktype = _jsonable(client.type(key))  # bytes → str（raw 客户端）
    if ktype == "none":
        raise KeyError(f"键 {key!r} 不存在")

    ttl = client.ttl(key)  # -1 永久、-2 不存在
    try:
        encoding = _jsonable(client.object("encoding", key))
    except Exception:
        encoding = None
    try:
        mem = client.memory_usage(key)
    except Exception:
        mem = None

    detail: dict = {"key": key, "type": ktype, "ttl": ttl, "encoding": encoding,
                    "memory_bytes": mem}
    if ktype == "string":
        detail["value"] = _truncate(_jsonable(client.get(key)), max_cell_chars)
    elif ktype == "hash":
        h = client.hgetall(key)
        # 字段名多为文本、值可能二进制 → 都过 _jsonable（二进制转 BINARY HEX）
        detail["fields"] = {_jsonable(k): _truncate(_jsonable(v), max_cell_chars)
                            for k, v in h.items()}
        detail["length"] = client.hlen(key)
    elif ktype == "list":
        detail["items"] = [_truncate(_jsonable(v), max_cell_chars)
                           for v in client.lrange(key, 0, max_items - 1)]
        detail["length"] = client.llen(key)
    elif ktype == "set":
        members = list(client.sscan_iter(key, count=max_items))[:max_items]
        detail["members"] = [_truncate(_jsonable(v), max_cell_chars) for v in members]
        detail["length"] = client.scard(key)
    elif ktype == "zset":
        pairs = client.zrange(key, 0, max_items - 1, withscores=True)
        detail["members"] = [{"member": _truncate(_jsonable(m), max_cell_chars), "score": s}
                             for m, s in pairs]
        detail["length"] = client.zcard(key)
    else:
        detail["value"] = f"（暂不支持的类型：{ktype}）"
    return detail


def _truncate(v: Any, max_chars: int) -> Any:
    if isinstance(v, str) and len(v) > max_chars:
        return v[:max_chars] + f"…[已截断，原 {len(v)} 字符]"
    if isinstance(v, list):
        return [_truncate(x, max_chars) for x in v]
    if isinstance(v, dict):
        return {k: _truncate(val, max_chars) for k, val in v.items()}
    return v


def _hexdump(b: bytes) -> str:
    """二进制值 → `BINARY HEX 88 A7 4F …`（对标 Medis 的二进制展示）。"""
    h = b.hex()
    return "BINARY HEX " + " ".join(h[i:i + 2].upper() for i in range(0, len(h), 2))


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return _hexdump(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(val) for k, val in v.items()}
    if isinstance(v, set):
        return sorted(_jsonable(x) for x in v)
    return str(v)
