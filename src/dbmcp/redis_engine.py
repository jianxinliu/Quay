"""Redis 适配：连接池 + 命令执行，SSH 多跳隧道复用 tunnel 模块。

Redis 无 reader/writer 双账号概念（通常单 AUTH），写命令的防线是审批流本身；
配置了 writer 账号（Redis 6+ ACL）时写命令用 writer 建独立客户端。
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import redis

from .config import ConnectionConfig, SshIdentity
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
        # SSH 证书库活引用；service 构造时指向 AppConfig.ssh_identities
        self.identities: dict[str, SshIdentity] | None = None

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
            entry = _build_client(cfg, role, db=eff_db, identities=self.identities)
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


def build_probe_client(
    cfg: ConnectionConfig, role: Role = "reader",
    identities: dict[str, SshIdentity] | None = None,
) -> _PooledRedis:
    """临时建 Redis 客户端（含隧道），已 PING。调用方用完必须 .dispose()。"""
    return _build_client(cfg, role, identities=identities)


def _build_client(
    cfg: ConnectionConfig, role: Role, db: int | None = None,
    identities: dict[str, SshIdentity] | None = None,
) -> _PooledRedis:
    tunnel: SSHTunnel | None = None
    host, port = cfg.host or "127.0.0.1", cfg.port or 6379
    if cfg.jump_hosts:
        tunnel = open_tunnel(host, port, cfg.jump_hosts, cfg.ssh_options, identities)
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
    """执行已通过分类/审批的命令。调用方负责传入 parse_command 的结果。

    结果先脱敏（CONFIG GET 密码项 / ACL 口令哈希），再截断——密钥不落返回值（安全红线）。
    """
    start = dt.datetime.now()
    value = client.execute_command(*parts)
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    redacted = redact_command_result(parts, _jsonable(value))
    return RedisResult(value=_truncate(redacted, max_cell_chars), duration_ms=duration_ms)


# 密钥不落返回值：会回显凭证的命令，结果脱敏后再返回/展示 --------------------
_SECRET_CFG_HINT = ("pass", "auth", "secret", "token", "credential")
# ACL 输出里的凭证：#<sha256 哈希>、>明文口令、<待删口令、裸 64 位 hex 哈希
_ACL_SECRET_RE = re.compile(
    r"(#[0-9a-fA-F]{40,})|(>\S+)|(<\S+)|(\b[0-9a-fA-F]{64}\b)")
_MASK = "***已隐藏***"


def redact_command_result(parts: list[str], value: Any) -> Any:
    """对可能回显凭证的命令结果脱敏。value 须已是 _jsonable 后的结构。"""
    if not parts:
        return value
    cmd = parts[0].upper()
    sub = parts[1].upper() if len(parts) > 1 else ""
    if cmd == "CONFIG" and sub == "GET":
        return _redact_config_pairs(value)
    if cmd == "ACL" and sub in ("LIST", "GETUSER", "GENPASS", "WHOAMI"):
        return _redact_acl(value)
    return value


def redact_command_text(command: str, parts: list[str]) -> str:
    """对**命令原文**里的凭证脱敏后再落审计（H3：密码永不进审计记录）。

    覆盖会把明文口令写在命令里的命令：CONFIG SET requirepass/masterauth …、
    ACL SETUSER …（>明文 / #哈希 / <待删）、AUTH …、HELLO … AUTH user pass。
    非敏感命令原样返回（保留原始文本）。redact 后按 token 重组，仅用于审计展示，
    真正执行用的仍是未脱敏的 parts。
    """
    if not parts:
        return command
    cmd = parts[0].upper()
    sub = parts[1].upper() if len(parts) > 1 else ""
    red = list(parts)
    if cmd == "CONFIG" and sub == "SET":
        # CONFIG SET <param> <value> [<param> <value> ...]：敏感参数遮蔽其值
        for i in range(2, len(red) - 1, 2):
            if any(h in red[i].lower() for h in _SECRET_CFG_HINT):
                red[i + 1] = _MASK
    elif cmd == "ACL" and sub == "SETUSER":
        red = [_ACL_SECRET_RE.sub(_MASK, tok) for tok in red]
    elif cmd == "AUTH":
        if len(red) >= 2:  # AUTH <pass> 或 AUTH <user> <pass>：最后一个 token 是口令
            red[-1] = _MASK
    elif cmd == "HELLO":
        for i, tok in enumerate(red):  # HELLO [proto] AUTH <user> <pass>
            if tok.upper() == "AUTH" and i + 2 < len(red):
                red[i + 2] = _MASK
    else:
        return command  # 无敏感内容：保留原文
    return " ".join(red)


def _redact_config_pairs(value: Any) -> Any:
    """CONFIG GET 结果是 [key, val, key, val, ...]；键名含 pass/auth 等则遮蔽其值。"""
    if not isinstance(value, list):
        return value
    out = list(value)
    for i in range(0, len(out) - 1, 2):
        if any(h in str(out[i]).lower() for h in _SECRET_CFG_HINT):
            out[i + 1] = _MASK
    return out


def _redact_acl(value: Any) -> Any:
    """ACL LIST/GETUSER/GENPASS 里的口令哈希/明文一律遮蔽。"""
    if isinstance(value, str):
        return _ACL_SECRET_RE.sub(_MASK, value)
    if isinstance(value, list):
        return [_redact_acl(x) for x in value]
    return value


# ---------- Medis 式浏览（只读探测，不经审批）----------

def _database_count(client: redis.Redis, default: int = 16) -> int:
    """`CONFIG GET databases` 拿逻辑库数量；被禁用/不支持（云托管常禁 CONFIG）时回退 default。"""
    try:
        cfg = client.config_get("databases")
        n = int(cfg.get("databases", default))
        return n if n > 0 else default
    except Exception:  # noqa: BLE001  CONFIG 被禁 → 用默认库数
        return default


MIN_DBS_SHOWN = 16  # 至少展示这么多库（覆盖常见 0..15），即便实例配置更多且都为空


def keyspace_dbs(client: redis.Redis, min_dbs: int = MIN_DBS_SHOWN) -> list[dict]:
    """列出逻辑库（含空库）；有数据的带键数（`INFO keyspace` 只报有键的库）。

    返回 [{"db": 0, "keys": 12, "expires": 3}, {"db": 1, "keys": 0, "expires": 0}, ...]，
    按库号升序。空库 keys=0 也出现（前端据此显隐键数）。

    **上限**：有些实例配置很大的 databases（如 256），全列会刷屏。故只展示到
    max(MIN_DBS_SHOWN, 最大非空库号+1)，但**永远包含所有有数据的库**（即便库号很大）。
    """
    info = client.info("keyspace")  # {'db0': {'keys': 12, 'expires': 3, 'avg_ttl': 0}, ...}
    stats_by_db: dict[int, dict] = {}
    for name, stats in info.items():
        if not name.startswith("db"):
            continue
        try:
            idx = int(name[2:])
        except ValueError:
            continue
        keys = int(stats.get("keys", 0)) if isinstance(stats, dict) else 0
        expires = int(stats.get("expires", 0)) if isinstance(stats, dict) else 0
        stats_by_db[idx] = {"keys": keys, "expires": expires}
    total = _database_count(client)
    max_nonempty = max(stats_by_db) if stats_by_db else -1
    shown = min(total, max(min_dbs, max_nonempty + 1))
    # range(shown) 覆盖连续前缀；并集 stats_by_db 保证任何有数据的库（哪怕号很大）都在
    idxs = set(range(shown)) | set(stats_by_db)
    out = [{"db": i, **stats_by_db.get(i, {"keys": 0, "expires": 0})} for i in sorted(idxs)]
    return out


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


# msgpack 解码是全局展示偏好（系统设置 redis_msgpack_decode），由 service 在读值前设置。
# 用模块级开关而非逐层穿参：_decode_bytes 被 _jsonable 深层递归调用，穿参会散落到几十处；
# 且该值对所有请求一致，并发下即便竞态也只会写成同一个值，无害。
_MSGPACK_DECODE = True


def set_msgpack_decode(enabled: bool) -> None:
    """设置是否对非 UTF-8 值尝试 msgpack 解码（系统设置驱动）。"""
    global _MSGPACK_DECODE
    _MSGPACK_DECODE = bool(enabled)


def _decode_bytes(b: bytes) -> Any:
    """字节值智能解码：优先 UTF-8 文本；否则（若开启）试 msgpack（仅当解出 dict/list 结构，
    避免任意二进制被误判）→ 返回结构化对象（供 JSON 展示）；再否则回退 BINARY HEX。"""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if _MSGPACK_DECODE:
        try:
            import msgpack  # noqa: PLC0415  可选依赖，未装则跳过（回退 HEX）
            obj = msgpack.unpackb(b, raw=False, strict_map_key=False)
            if isinstance(obj, (dict, list)):   # 只认结构化，标量/误判一律按二进制
                return obj
        except Exception:  # noqa: BLE001  解码失败即非 msgpack，回退
            pass
    return _hexdump(b)


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, bytes):
        decoded = _decode_bytes(v)
        return _jsonable(decoded) if isinstance(decoded, (dict, list)) else decoded
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(val) for k, val in v.items()}
    if isinstance(v, set):
        return sorted(_jsonable(x) for x in v)
    return str(v)
