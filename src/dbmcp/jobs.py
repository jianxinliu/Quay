"""后台查询台的异步任务管理：按 queue_key 串行执行 + 排队位置 + 计时 + 取消。

背景：后台查询台的每条 SQL 原本 fire-and-forget 起一个 daemon 线程，多条同时跑。
需求：**同一连接同一时刻只跑一条 SQL**，其余按 FIFO 排队；不同连接（不同 key）各自并行。
再叠加：排队位置展示、执行计时、以及对排队中/运行中任务的取消。

设计要点：
- 每个 queue_key 一个 worker 线程，惰性创建；队列排空时 worker 在锁内注销并退出，
  避免每连接常驻一个空转线程（历史教训：非 daemon / 常驻辅助线程会拖挂进程退出）。
- 取消：排队中 → 直接标记 canceled，worker 取到时跳过；运行中 → 调用任务注册的取消函数
  （engines.make_canceller，在 DB 层 KILL/中断），任务函数抛错后按「取消」而非「失败」归类。
- 纯逻辑（线程 + 可调用对象），可用简单函数单测。

任务函数签名 JobFn：接收一个 register(canceller) 回调，返回结果（任意，通常是 dict）。
任务在真正开始跑 SQL、拿到底层连接时调用 register，把取消函数交回给管理器。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

# register 回调：把「取消函数」交回管理器
Register = Callable[[Callable[[], None]], None]
# 任务函数：拿到 register 后执行，返回结果
JobFn = Callable[[Register], Any]

_TERMINAL = ("done", "error", "canceled")


class _Job:
    __slots__ = ("id", "key", "fn", "status", "submitted_ts", "started_ts",
                 "finished_ts", "result", "error", "canceller", "canceling")

    def __init__(self, job_id: str, key: Any, fn: JobFn) -> None:
        self.id = job_id
        self.key = key
        self.fn = fn
        self.status = "queued"          # queued -> running -> done/error/canceled
        self.submitted_ts = time.time()
        self.started_ts: float | None = None
        self.finished_ts: float | None = None
        self.result: Any = None
        self.error: str | None = None
        self.canceller: Callable[[], None] | None = None
        self.canceling = False


class JobManager:
    """按 key 串行的任务管理器。线程安全。"""

    def __init__(self, ttl_s: int = 600) -> None:
        self.ttl_s = ttl_s
        self._jobs: dict[str, _Job] = {}
        self._pending: dict[Any, deque[str]] = {}   # key -> 排队中的 job_id（FIFO）
        self._active: dict[Any, str] = {}            # key -> 正在跑的 job_id
        self._workers: set[Any] = set()              # 有存活 worker 的 key
        self._lock = threading.Lock()

    # ---------- 提交 / 查询 / 取消 ----------

    def submit(self, key: Any, fn: JobFn) -> str:
        """提交一个任务到 key 队列，立即返回 job_id（不阻塞）。"""
        self._gc()
        job_id = uuid.uuid4().hex[:12]
        job = _Job(job_id, key, fn)
        with self._lock:
            self._jobs[job_id] = job
            self._pending.setdefault(key, deque()).append(job_id)
            start_worker = key not in self._workers
            if start_worker:
                self._workers.add(key)
        if start_worker:
            threading.Thread(target=self._worker, args=(key,),
                             name="dbm-jobq", daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        """任务快照：状态 + 排队位置(前面还有几条) + 等待/执行耗时 + 结果/错误。"""
        now = time.time()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            queue_position = None
            if job.status == "queued":
                queue_position = self._ahead_locked(job)
            wait_ms = int(((job.started_ts or now) - job.submitted_ts) * 1000)
            if job.started_ts is not None:
                elapsed_ms = int(((job.finished_ts or now) - job.started_ts) * 1000)
            else:
                elapsed_ms = 0
            return {
                "status": job.status,
                "queue_position": queue_position,
                "wait_ms": max(wait_ms, 0),
                "elapsed_ms": max(elapsed_ms, 0),
                "result": job.result if job.status == "done" else None,
                "error": job.error if job.status in ("error", "canceled") else None,
            }

    def cancel(self, job_id: str) -> bool:
        """取消任务。排队中 → 标记 canceled（worker 跳过）；运行中 → 触发 DB 层取消。

        返回是否受理（未知/已结束返回 False）。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in _TERMINAL:
                return False
            if job.status == "queued":
                job.status = "canceled"
                job.error = "已取消（排队中）"
                job.finished_ts = time.time()
                return True
            # running
            job.canceling = True
            canceller = job.canceller
        if canceller is not None:
            try:
                canceller()
            except Exception:  # noqa: BLE001
                pass
        return True

    # ---------- 内部 ----------

    def _ahead_locked(self, job: _Job) -> int:
        """调用方须持锁。排在该排队任务前面、仍会执行的任务数（含正在跑的一条）。"""
        ahead = 1 if self._active.get(job.key) else 0
        for jid in self._pending.get(job.key, ()):  # noqa: B007
            if jid == job.id:
                break
            other = self._jobs.get(jid)
            if other is not None and other.status == "queued":
                ahead += 1
        return ahead

    def _worker(self, key: Any) -> None:
        while True:
            with self._lock:
                dq = self._pending.get(key)
                job = None
                while dq:
                    jid = dq.popleft()
                    candidate = self._jobs.get(jid)
                    if candidate is None or candidate.status != "queued":
                        continue  # 已取消/丢失，跳过
                    job = candidate
                    break
                if job is None:
                    # 队列排空：在同一把锁内注销 worker 并退出，避免与 submit 竞态
                    self._workers.discard(key)
                    self._pending.pop(key, None)
                    return
                job.status = "running"
                job.started_ts = time.time()
                self._active[key] = job.id

            self._run(job)

            with self._lock:
                if self._active.get(key) == job.id:
                    self._active.pop(key, None)

    def _run(self, job: _Job) -> None:
        def register(canceller: Callable[[], None]) -> None:
            fire = None
            with self._lock:
                job.canceller = canceller
                if job.canceling:          # 取消在 register 之前到达 → 立即补发
                    fire = canceller
            if fire is not None:
                try:
                    fire()
                except Exception:  # noqa: BLE001
                    pass

        try:
            result = job.fn(register)
            with self._lock:
                if job.canceling:
                    job.status = "canceled"
                    job.error = "已取消"
                else:
                    job.status = "done"
                    job.result = result
                job.finished_ts = time.time()
        except Exception as e:  # noqa: BLE001
            with self._lock:
                if job.canceling:
                    job.status = "canceled"
                    job.error = "已取消"
                else:
                    job.status = "error"
                    job.error = str(e) or f"{type(e).__name__}"
                job.finished_ts = time.time()

    def _gc(self) -> None:
        now = time.time()
        with self._lock:
            dead = [jid for jid, j in self._jobs.items()
                    if j.finished_ts is not None and now - j.finished_ts > self.ttl_s]
            for jid in dead:
                self._jobs.pop(jid, None)
