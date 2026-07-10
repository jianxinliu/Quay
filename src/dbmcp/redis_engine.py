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
        self._entries: dict[tuple[str, str, Role], _PooledRedis] = {}
        self._lock = threading.Lock()
        self._idle_reclaim_s = idle_reclaim_s

    def get(
        self, project: str, connection: str, cfg: ConnectionConfig, role: Role = "reader"
    ) -> redis.Redis:
        key = (project, connection, role)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry.tunnel is None or entry.tunnel.is_alive()):
                entry.last_used = time.monotonic()
                return entry.client
            if entry is not None:
                entry.dispose()
                del self._entries[key]
            entry = _build_client(cfg, role)
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


def _build_client(cfg: ConnectionConfig, role: Role) -> _PooledRedis:
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
        password = resolve_secret(cfg.password) if cfg.password else None

    timeout = cfg.policy.statement_timeout_s
    try:
        client = redis.Redis(
            host=host,
            port=port,
            db=int(cfg.database or 0),
            username=user,
            password=password,
            decode_responses=True,
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


def _truncate(v: Any, max_chars: int) -> Any:
    if isinstance(v, str) and len(v) > max_chars:
        return v[:max_chars] + f"…[已截断，原 {len(v)} 字符]"
    if isinstance(v, list):
        return [_truncate(x, max_chars) for x in v]
    if isinstance(v, dict):
        return {k: _truncate(val, max_chars) for k, val in v.items()}
    return v


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return repr(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(val) for k, val in v.items()}
    if isinstance(v, set):
        return sorted(_jsonable(x) for x in v)
    return str(v)
