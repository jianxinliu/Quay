"""后台查询台的异步任务管理：**按 queue_key 忙时直接拒绝** + 计时 + 取消。

需求演进：一开始做了排队，但排队位置对使用者不够清晰。改为——**同一 key（通常=连接）
一次只允许一条 SQL 在跑；此时再派发新的直接拒绝（raise Busy）**，让调用方给出明确反馈
（「该连接有查询正在执行，请等待或取消后重试」），而不是悄悄排队。不同 key（不同连接、
workflow/画布用唯一 key）互不影响、各自并行。

- 每个被接受的任务起一个 daemon 线程执行（单管理员场景无需池化）。
- 取消：对正在跑的任务调用其注册的取消函数（engines.make_canceller，DB 层 KILL/中断）；
  任务函数抛错后按「取消」而非「失败」归类。
- 纯逻辑（线程 + 可调用对象），可用简单函数单测。

任务函数签名 JobFn：接收一个 register(canceller) 回调，返回结果（任意，通常是 dict）。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

Register = Callable[[Callable[[], None]], None]
JobFn = Callable[[Register], Any]

_TERMINAL = ("done", "error", "canceled")


class Busy(Exception):
    """该 key 已有任务在执行，拒绝新任务。"""


class _Job:
    __slots__ = ("id", "key", "fn", "status", "submitted_ts", "started_ts",
                 "finished_ts", "result", "error", "canceller", "canceling")

    def __init__(self, job_id: str, key: Any, fn: JobFn) -> None:
        self.id = job_id
        self.key = key
        self.fn = fn
        self.status = "running"        # running -> done/error/canceled（无排队态）
        self.submitted_ts = time.time()
        self.started_ts: float | None = None
        self.finished_ts: float | None = None
        self.result: Any = None
        self.error: str | None = None
        self.canceller: Callable[[], None] | None = None
        self.canceling = False


class JobManager:
    """按 key 忙时拒绝的任务管理器。线程安全。"""

    def __init__(self, ttl_s: int = 600) -> None:
        self.ttl_s = ttl_s
        self._jobs: dict[str, _Job] = {}
        self._active: dict[Any, str] = {}   # key -> 正在跑的 job_id
        self._lock = threading.Lock()

    # ---------- 提交 / 查询 / 取消 ----------

    def submit(self, key: Any, fn: JobFn) -> str:
        """提交任务；若 key 已有任务在跑则 raise Busy。立即返回 job_id（不阻塞）。"""
        self._gc()
        with self._lock:
            if key in self._active:
                raise Busy(key)
            job_id = uuid.uuid4().hex[:12]
            job = _Job(job_id, key, fn)
            job.started_ts = time.time()
            self._jobs[job_id] = job
            self._active[key] = job_id
        threading.Thread(target=self._run, args=(job,), name="dbm-jobq", daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        """任务快照：状态 + 执行耗时 + 结果/错误。"""
        now = time.time()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            started = job.started_ts or job.submitted_ts
            elapsed_ms = int(((job.finished_ts or now) - started) * 1000)
            return {
                "status": job.status,
                "elapsed_ms": max(elapsed_ms, 0),
                "result": job.result if job.status == "done" else None,
                "error": job.error if job.status in ("error", "canceled") else None,
            }

    def cancel(self, job_id: str) -> bool:
        """取消正在跑的任务：触发 DB 层取消。返回是否受理（未知/已结束返回 False）。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in _TERMINAL:
                return False
            job.canceling = True
            canceller = job.canceller
        if canceller is not None:
            try:
                canceller()
            except Exception:  # noqa: BLE001
                pass
        return True

    # ---------- 内部 ----------

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
        finally:
            with self._lock:
                if self._active.get(job.key) == job.id:
                    self._active.pop(job.key, None)

    def _gc(self) -> None:
        now = time.time()
        with self._lock:
            dead = [jid for jid, j in self._jobs.items()
                    if j.finished_ts is not None and now - j.finished_ts > self.ttl_s]
            for jid in dead:
                self._jobs.pop(jid, None)
