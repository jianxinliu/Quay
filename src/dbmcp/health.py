"""连接健康状态位 + 后台重连。

每个 (project, connection) 一份 Health，与引擎池并行维护，覆盖 SQL 引擎与 Redis：
- ok:              可用；工具直接执行
- unavailable:     捕到连接级错误，后台正在重连；工具入口直接抛 ConnectionUnavailable
- exhausted:       重连超过最大次数，需人为在管理后台"测试连接"或改配置

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

# 退避阶梯（秒），第 N 次失败等第 N 项时间再试；索引超出后转 exhausted
BACKOFF_STEPS_S = (5, 15, 45, 120, 300)


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
        """工具入口调用：若当前不可用抛 ConnectionUnavailable，否则放行。"""
        with self._lock:
            h = self._entries.get((project, connection))
            if h is None or h.state == "ok":
                return
            if h.state == "exhausted":
                raise ConnectionUnavailable(
                    f"连接 {project}/{connection} 已确认不可用（连续 {h.fail_count} 次重连失败），"
                    f"请联系管理员在后台检查。最近错误：{h.last_error or '未知'}",
                    retry_after_s=0, state="exhausted",
                )
            # unavailable：告诉 agent 大约多久后可以重试
            wait = max(0, int(h.next_retry_at - time.monotonic()))
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
                          last_change_at=h.last_change_at)

    def mark_ok(self, project: str, connection: str) -> None:
        """执行成功后调：清失败计数并转 ok。"""
        with self._lock:
            h = self._entries.get((project, connection))
            if h is None or h.state == "ok":
                return
            h.state = "ok"
            h.fail_count = 0
            h.last_error = ""
            h.next_retry_at = 0.0
            h.last_change_at = time.monotonic()

    def mark_failed(self, project: str, connection: str, error: str) -> None:
        """捕到连接级异常时调：进入 unavailable、启一次重连（若还没启）。"""
        with self._lock:
            if self._closed:
                return
            h = self._entries.get((project, connection))
            if h is None:
                h = Health()
                self._entries[(project, connection)] = h
            if h.state == "exhausted":
                # 已确认不可用，保留状态，不再退化
                return
            first = h.state == "ok"
            h.state = "unavailable"
            h.last_error = _short(error)
            h.last_change_at = time.monotonic()
            if first:
                # 首次失败：从第一档退避开始探测
                h.next_retry_at = time.monotonic() + BACKOFF_STEPS_S[0]
                h.fail_count = 0  # 探测线程会在实际失败时 +1
                self._start_reconnect(project, connection, h)

    def force_clear(self, project: str, connection: str) -> None:
        """后台"测试连接"/连接更新等场景：无条件清健康状态，让下一次访问重建。"""
        with self._lock:
            self._entries.pop((project, connection), None)

    def stop(self) -> None:
        """服务关闭时调：停止所有后台重连线程（daemon 也会随进程退出，此为主动配合）。"""
        with self._lock:
            self._closed = True
            self._entries.clear()

    # ---------- 内部 ----------

    def _start_reconnect(self, project: str, connection: str, h: Health) -> None:
        """在锁内调用；启一个 daemon 线程做退避探测。"""
        if h._thread is not None and h._thread.is_alive():
            return

        def _loop() -> None:
            while True:
                with self._lock:
                    if self._closed:
                        return
                    cur = self._entries.get((project, connection))
                    if cur is None or cur.state == "ok":
                        return
                    wait = max(0.0, cur.next_retry_at - time.monotonic())
                if wait > 0:
                    time.sleep(wait)
                # 探测（不占用锁——外部 probe 可能耗时/阻塞）
                try:
                    self._probe(project, connection)
                    with self._lock:
                        cur = self._entries.get((project, connection))
                        if cur is not None:
                            cur.state = "ok"
                            cur.fail_count = 0
                            cur.last_error = ""
                            cur.next_retry_at = 0.0
                            cur.last_change_at = time.monotonic()
                    logger.info("connection %s/%s reconnected", project, connection)
                    return
                except Exception as e:  # noqa: BLE001
                    with self._lock:
                        cur = self._entries.get((project, connection))
                        if cur is None or self._closed:
                            return
                        cur.fail_count += 1
                        cur.last_error = _short(str(e))
                        idx = min(cur.fail_count, len(BACKOFF_STEPS_S) - 1)
                        if cur.fail_count >= len(BACKOFF_STEPS_S):
                            cur.state = "exhausted"
                            cur.next_retry_at = 0.0
                            cur.last_change_at = time.monotonic()
                            err_snapshot = cur.last_error
                    if cur.state == "exhausted":
                        logger.warning("connection %s/%s exhausted after %d retries: %s",
                                       project, connection, cur.fail_count, err_snapshot)
                        try:
                            self._on_exhausted(project, connection, err_snapshot)
                        except Exception:  # noqa: BLE001
                            logger.exception("on_exhausted callback failed")
                        return
                    with self._lock:
                        cur = self._entries.get((project, connection))
                        if cur is not None:
                            cur.next_retry_at = time.monotonic() + BACKOFF_STEPS_S[idx]

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
