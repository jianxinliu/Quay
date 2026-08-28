"""连接健康状态位 + 后台重连（断路器：closed / half-open / open）。

每个 (project, connection) 一份 Health，与引擎池并行维护，覆盖 SQL 引擎与 Redis：
- ok:              可用；工具直接执行
- unavailable:     捕到连接级错误，后台正在退避重连；退避窗口内的请求快速失败
- exhausted:       连续失败超过退避阶梯长度——**仍在以最长间隔持续重试**，只是标记
                   「大概率需要人看一眼」，供后台/前端红色告警与通知使用

**自愈保证**：无论处于哪个状态，都不会永久放弃——
1. 后台重连线程一直在跑（退避封顶 BACKOFF_STEPS_S[-1]），DB 恢复后无人访问也能自动转 ok；
2. 退避到点后，`check()` 会**放行一个真实请求**去探路（half-open）。成功即恢复、失败即
   重新计时。这样 DB 恢复后的第一次调用就能自愈，不必等下一次定时探测，也不需要人工点重连。

只当"连接级"异常触发标记（网络断/socket 超时/tunnel 死/连不上），SQL 语法错、
权限拒、审批拒等业务错不打标——它们无法通过重连解决。

线程安全：内部一把锁，check/set/clear 都短临界区；不做长阻塞。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

State = Literal["ok", "unavailable", "exhausted"]

# 退避阶梯（秒），第 N 次失败等第 N 项时间再试；用完后**保持最后一档持续重试**（不放弃）。
# 封顶取 60s 而非更大值：既然不再有「终态放弃」，封顶就直接决定「DB 恢复后最坏多久自动可用」
# ——1 分钟内自愈，人基本不需要去点重连；代价只是对一台长期挂掉的 DB 每分钟一次轻量探测。
BACKOFF_STEPS_S = (5, 15, 30, 45, 60)

# 半开探路的租约：放行的那个请求若这么久还没回报成败（调用方异常退出等），
# 视作租约过期、允许下一个请求再探路，避免连接被一个"卡住的探路者"永久锁死。
PROBE_LEASE_S = 120.0

# 后台重连线程的分段睡眠粒度：退避可能长达 300s，分段睡才能对 stop()/next_retry_at
# 的变化及时响应（而不是死睡到点）。
_TICK_S = 1.0


class ConnectionUnavailable(Exception):
    """连接暂时/彻底不可用。message 面向 agent；retry_after_s 为建议重试秒数（0 表示不再重试）。"""

    def __init__(self, message: str, retry_after_s: int = 0, state: State = "unavailable"):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.state = state


@dataclass
class Health:
    state: State = "ok"
    fail_count: int = 0
    next_retry_at: float = 0.0      # time.monotonic() 单调时钟
    last_error: str = ""
    last_change_at: float = 0.0     # 供通知去重用
    probing: bool = False           # 半开：已放行一个探路请求（或后台线程正在探测）
    probe_started_at: float = 0.0   # 探路开始时刻，用于租约过期判定
    _thread: threading.Thread | None = field(default=None, repr=False)


class HealthMonitor:
    """健康状态位 + 后台重连线程调度。

    probe(project, connection) 由外部注入：拿到连接后执行 SELECT 1 之类的轻量探测，
    成功返回 True，失败 raise。这样 SQL 引擎与 Redis 都能复用同一套调度器。
    """

    def __init__(
        self,
        probe: Callable[[str, str], None],
        on_exhausted: Callable[[str, str, str], None] | None = None,
    ):
        self._entries: dict[tuple[str, str], Health] = {}
        self._lock = threading.Lock()
        self._probe = probe
        self._on_exhausted = on_exhausted or (lambda *_a, **_kw: None)
        self._closed = False

    def check(self, project: str, connection: str) -> None:
        """工具入口调用：健康就放行；不健康时退避窗口内快速失败、到点则放行一个探路请求。"""
        with self._lock:
            h = self._entries.get((project, connection))
            if h is None or h.state == "ok":
                return
            now = time.monotonic()
            # 探路租约过期（探路者没回报成败）：解锁，允许重新探路
            if h.probing and now - h.probe_started_at > PROBE_LEASE_S:
                h.probing = False
            if not h.probing and now >= h.next_retry_at:
                # 半开：放行这一个请求去真实尝试，成败由 mark_ok / mark_failed 回收
                h.probing = True
                h.probe_started_at = now
                return
            wait = max(0, int(h.next_retry_at - now))
            if h.state == "exhausted":
                raise ConnectionUnavailable(
                    f"连接 {project}/{connection} 持续不可用（已连续 {h.fail_count} 次重连失败，"
                    f"仍在每 {BACKOFF_STEPS_S[-1]} 秒自动重试，约 {wait} 秒后再试）。"
                    f"长时间不恢复通常需要人工检查网络/账号/隧道配置。"
                    f"最近错误：{h.last_error or '未知'}",
                    retry_after_s=max(wait, 5), state="exhausted",
                )
            # unavailable：告诉 agent 大约多久后可以重试
            raise ConnectionUnavailable(
                f"连接 {project}/{connection} 暂时不可用，后台正在自动重连"
                f"（约 {wait} 秒后重试）。请稍后再试。最近错误：{h.last_error or '未知'}",
                retry_after_s=max(wait, 5), state="unavailable",
            )

    def get(self, project: str, connection: str) -> Health | None:
        with self._lock:
            h = self._entries.get((project, connection))
            if h is None:
                return None
            # 拷贝一份返回（避免调用方看到线程内变化）
            return Health(state=h.state, fail_count=h.fail_count,
                          next_retry_at=h.next_retry_at, last_error=h.last_error,
                          last_change_at=h.last_change_at, probing=h.probing,
                          probe_started_at=h.probe_started_at)

    def snapshot(self) -> dict[tuple[str, str], Health]:
        """全量健康快照（供管理后台/查询台渲染状态灯）。只含非 ok 的记录才有意义，但全给。"""
        with self._lock:
            return {
                k: Health(state=h.state, fail_count=h.fail_count,
                          next_retry_at=h.next_retry_at, last_error=h.last_error,
                          last_change_at=h.last_change_at, probing=h.probing)
                for k, h in self._entries.items()
            }

    def mark_ok(self, project: str, connection: str) -> None:
        """执行成功后调：清失败计数并转 ok。"""
        with self._lock:
            h = self._entries.get((project, connection))
            if h is None or (h.state == "ok" and not h.probing):
                return
            h.state = "ok"
            h.fail_count = 0
            h.last_error = ""
            h.next_retry_at = 0.0
            h.probing = False
            h.last_change_at = time.monotonic()

    def mark_failed(self, project: str, connection: str, error: str) -> None:
        """捕到连接级异常时调：进入 unavailable / 推进退避，并确保后台重连线程在跑。"""
        notify_err = ""
        with self._lock:
            if self._closed:
                return
            h = self._entries.get((project, connection))
            if h is None:
                h = Health()
                self._entries[(project, connection)] = h
            h.last_error = _short(error)
            now = time.monotonic()
            if h.state == "ok":
                # 首次失败：从第一档退避开始探测
                h.state = "unavailable"
                h.fail_count = 0  # 探测失败时才 +1
                h.probing = False
                h.next_retry_at = now + BACKOFF_STEPS_S[0]
                h.last_change_at = now
            elif h.probing:
                # 半开探路失败：计一次失败并推进退避
                h.probing = False
                if self._advance_backoff_locked(h, now):
                    notify_err = h.last_error
            # 非探路的重复失败（退避窗口内并发请求各自撞上）不重排退避，
            # 否则高频请求会把退避一直往后推、永远等不到探测。
            self._start_reconnect(project, connection, h)
        if notify_err:
            self._fire_exhausted(project, connection, notify_err)

    def force_clear(self, project: str, connection: str) -> None:
        """后台「测试连接」/「重连」/连接配置更新等场景：无条件清健康状态，让下一次访问重建。"""
        with self._lock:
            self._entries.pop((project, connection), None)

    def stop(self) -> None:
        """服务关闭时调：停止所有后台重连线程（daemon 也会随进程退出，此为主动配合）。"""
        with self._lock:
            self._closed = True
            self._entries.clear()

    # ---------- 内部 ----------

    def _advance_backoff_locked(self, h: Health, now: float) -> bool:
        """在锁内调用：失败计数 +1、排下一次退避。返回 True 表示刚转入 exhausted（需通知一次）。

        退避阶梯用完后**保持最后一档持续重试**，不再有"终态放弃"——DB 恢复即自愈。
        """
        h.fail_count += 1
        idx = min(h.fail_count, len(BACKOFF_STEPS_S) - 1)
        h.next_retry_at = now + BACKOFF_STEPS_S[idx]
        if h.fail_count >= len(BACKOFF_STEPS_S) and h.state != "exhausted":
            h.state = "exhausted"
            h.last_change_at = now
            return True
        return False

    def _fire_exhausted(self, project: str, connection: str, error: str) -> None:
        logger.warning("connection %s/%s exhausted (仍在持续重试): %s", project, connection, error)
        try:
            self._on_exhausted(project, connection, error)
        except Exception:  # noqa: BLE001
            logger.exception("on_exhausted callback failed")

    def _start_reconnect(self, project: str, connection: str, h: Health) -> None:
        """在锁内调用；启一个 daemon 线程做退避探测（线程已在跑则不重复启）。"""
        if h._thread is not None and h._thread.is_alive():
            return

        key = (project, connection)

        def _loop() -> None:
            while True:
                # 1) 等到退避到点（分段睡，便于 stop / next_retry_at 变化及时生效）
                with self._lock:
                    if self._closed:
                        return
                    cur = self._entries.get(key)
                    if cur is None or cur.state == "ok":
                        return
                    wait = cur.next_retry_at - time.monotonic()
                    busy = cur.probing and (
                        time.monotonic() - cur.probe_started_at <= PROBE_LEASE_S)
                if wait > 0 or busy:
                    # busy：已有真实请求在半开探路，等它的结果，别重复打 DB
                    time.sleep(min(max(wait, 0.0), _TICK_S) if wait > 0 else _TICK_S)
                    continue

                # 2) 占住探路名额后再探测（与 check() 的半开互斥）
                with self._lock:
                    if self._closed:
                        return
                    cur = self._entries.get(key)
                    if cur is None or cur.state == "ok":
                        return
                    if cur.probing:
                        continue
                    cur.probing = True
                    cur.probe_started_at = time.monotonic()

                try:
                    self._probe(project, connection)
                except Exception as e:  # noqa: BLE001
                    notify_err = ""
                    with self._lock:
                        cur = self._entries.get(key)
                        if cur is None or self._closed:
                            return
                        cur.probing = False
                        cur.last_error = _short(str(e))
                        if self._advance_backoff_locked(cur, time.monotonic()):
                            notify_err = cur.last_error
                    if notify_err:
                        self._fire_exhausted(project, connection, notify_err)
                    continue  # 关键：不再有"放弃"分支，继续按最长间隔重试

                with self._lock:
                    cur = self._entries.get(key)
                    if cur is not None:
                        cur.state = "ok"
                        cur.fail_count = 0
                        cur.last_error = ""
                        cur.next_retry_at = 0.0
                        cur.probing = False
                        cur.last_change_at = time.monotonic()
                logger.info("connection %s/%s reconnected", project, connection)
                return

        t = threading.Thread(target=_loop, name=f"dbm-reconnect-{project}-{connection}",
                             daemon=True)
        h._thread = t
        t.start()

# ---------- 连接级异常识别 ----------

# 名字与"业务错误"重叠、只在**消息命中连接级片段时**才算连接级：
# - sqlalchemy 的 OperationalError 同时覆盖 pymysql 2013（连接断）与 sqlite 的
#   "no such table"（业务错误）。仅按类名判定会把 SQL 语法/表不存在也当连接错处理。
_AMBIGUOUS_EXC_NAMES = frozenset({
    "OperationalError",     # sqlalchemy/pymysql/psycopg/sqlite 都用它，含义各不同
    "DBAPIError",           # 多数驱动异常最终基类
})

# 严格属于"连接级"的类名（业务错误从不用这些名字）
_CONNECTION_LEVEL_EXC_NAMES = frozenset({
    "InterfaceError",       # 驱动层连接接口异常
    "DisconnectionError",   # sqlalchemy 池检测断连
    "TunnelError",          # 我们自己的 SSH 隧道异常
    "ConnectionError",      # redis-py / 网络通用
    "TimeoutError",         # redis-py TimeoutError
    "BusyLoadingError",     # redis-py 启动中
})

# 错误消息里含这些片段的也算连接级（跨驱动的兜底）
_CONNECTION_LEVEL_MSG_PARTS = (
    "lost connection",              # pymysql 2013
    "server has gone away",         # pymysql 2006
    "gone away",
    "connection refused",           # ECONNREFUSED
    "connection reset",              # ECONNRESET
    "broken pipe",
    "connection closed",
    "can't connect",
    "cannot connect",
    "no route to host",
    "network is unreachable",
    "temporary failure in name resolution",
    "name or service not known",
    "隧道就绪超时",                   # 我们自己的 SSH 隧道错误
    "隧道启动失败",
    "ssh: connect",
)


def is_connection_error(exc: BaseException) -> bool:
    """判断异常是否属于"连接级"——可以通过重连解决的那类。

    SQL 语法错、权限拒、审批拒返回 False（重连也没用）。
    """
    if isinstance(exc, (socket.timeout, ConnectionError, socket.gaierror, OSError)):
        # OSError 覆盖 ECONNREFUSED / ECONNRESET / EHOSTUNREACH 等
        # 但 OSError 太宽，须叠加消息片段判定
        msg = str(exc).lower()
        if isinstance(exc, (socket.timeout, ConnectionError, socket.gaierror)):
            return True
        # 纯 OSError：靠 errno / 消息片段
        errno = getattr(exc, "errno", None)
        if errno in (61, 104, 110, 111, 113):  # macOS/Linux 常见连接错误码
            return True
        return any(p in msg for p in _CONNECTION_LEVEL_MSG_PARTS)

    # 遍历异常继承链：
    # - 严格连接级类名 → True
    # - 名字重叠类（OperationalError 等）→ 必须叠加消息片段才算
    msg = str(exc).lower()
    msg_hit = any(p in msg for p in _CONNECTION_LEVEL_MSG_PARTS)
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 8:
        cls_name = type(cur).__name__
        if cls_name in _CONNECTION_LEVEL_EXC_NAMES:
            return True
        if cls_name in _AMBIGUOUS_EXC_NAMES and msg_hit:
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1

    return msg_hit


def _short(text: str, limit: int = 200) -> str:
    text = text.strip().splitlines()[0] if text else ""
    return text[:limit]
