"""JobManager 单测：按 key 串行、排队位置、计时、取消（排队中/运行中/竞态）、gc。"""

import threading
import time

import pytest

from dbmcp.jobs import JobManager


def _wait_until(pred, timeout=3.0, interval=0.005):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_same_key_runs_serially():
    """同一 key：第二条必须等第一条结束才开始。"""
    mgr = JobManager()
    gate = threading.Event()
    started = []

    def first(_register):
        started.append("a")
        gate.wait(2)
        return {"v": "a"}

    def second(_register):
        started.append("b")
        return {"v": "b"}

    ja = mgr.submit("conn1", first)
    jb = mgr.submit("conn1", second)

    assert _wait_until(lambda: mgr.get(ja)["status"] == "running")
    # 第一条在跑、第二条应仍排队，且未开始执行
    assert mgr.get(jb)["status"] == "queued"
    assert mgr.get(jb)["queue_position"] == 1
    assert started == ["a"]

    gate.set()
    assert _wait_until(lambda: mgr.get(jb)["status"] == "done")
    assert started == ["a", "b"]
    assert mgr.get(ja)["result"] == {"v": "a"}


def test_different_keys_run_in_parallel():
    """不同 key：并行执行，互不阻塞。"""
    mgr = JobManager()
    both_running = threading.Barrier(2, timeout=3)

    def work(_register):
        both_running.wait()  # 只有两条同时在跑才能通过
        return "ok"

    ja = mgr.submit("connA", work)
    jb = mgr.submit("connB", work)
    assert _wait_until(lambda: mgr.get(ja)["status"] == "done")
    assert _wait_until(lambda: mgr.get(jb)["status"] == "done")


def test_queue_position_counts_ahead():
    """排队位置 = 前面仍会执行的任务数（含正在跑的）。"""
    mgr = JobManager()
    gate = threading.Event()

    def blocking(_register):
        gate.wait(2)

    def quick(_register):
        return 1

    running = mgr.submit("c", blocking)
    q1 = mgr.submit("c", quick)
    q2 = mgr.submit("c", quick)
    assert _wait_until(lambda: mgr.get(running)["status"] == "running")
    assert mgr.get(q1)["queue_position"] == 1  # 前面 1 条在跑
    assert mgr.get(q2)["queue_position"] == 2  # 前面 1 跑 + 1 排队
    gate.set()
    assert _wait_until(lambda: mgr.get(q2)["status"] == "done")


def test_cancel_queued_never_runs():
    """取消排队中的任务：状态 canceled，且永不执行。"""
    mgr = JobManager()
    gate = threading.Event()
    ran = []

    def blocking(_register):
        gate.wait(2)

    def should_not_run(_register):
        ran.append(1)

    r = mgr.submit("c", blocking)
    q = mgr.submit("c", should_not_run)
    assert _wait_until(lambda: mgr.get(r)["status"] == "running")
    assert mgr.cancel(q) is True
    assert mgr.get(q)["status"] == "canceled"
    gate.set()
    assert _wait_until(lambda: mgr.get(r)["status"] == "done")
    time.sleep(0.05)
    assert ran == []  # 被取消的排队任务没跑


def test_cancel_running_invokes_canceller():
    """取消运行中的任务：触发注册的取消函数，任务抛错后归类为 canceled。"""
    mgr = JobManager()
    canceled = threading.Event()
    proceed = threading.Event()

    def work(register):
        def canceller():
            canceled.set()
            proceed.set()  # 模拟 DB 层中断，让查询立即抛错返回
        register(canceller)
        proceed.wait(2)
        if canceled.is_set():
            raise RuntimeError("interrupted")  # 模拟 KILL QUERY 触发的 OperationalError
        return "done"

    j = mgr.submit("c", work)
    assert _wait_until(lambda: mgr.get(j)["status"] == "running")
    assert mgr.cancel(j) is True
    assert canceled.wait(2)
    assert _wait_until(lambda: mgr.get(j)["status"] == "canceled")


def test_cancel_before_register_still_fires():
    """竞态：取消先于 register 到达时，register 时补发取消函数。"""
    mgr = JobManager()
    entered = threading.Event()
    release = threading.Event()
    fired = threading.Event()

    def work(register):
        entered.set()
        release.wait(2)  # 卡住，等 cancel 先到

        def canceller():
            fired.set()
        register(canceller)  # register 时应立即补发（因为已 canceling）
        return "x"

    j = mgr.submit("c", work)
    assert entered.wait(2)
    assert mgr.cancel(j) is True   # 运行中但尚未 register
    release.set()
    assert fired.wait(2)


def test_timing_fields():
    """计时字段：等待耗时与执行耗时非负、可读。"""
    mgr = JobManager()

    def work(_register):
        time.sleep(0.05)
        return "ok"

    j = mgr.submit("c", work)
    assert _wait_until(lambda: mgr.get(j)["status"] == "done")
    snap = mgr.get(j)
    assert snap["elapsed_ms"] >= 40
    assert snap["wait_ms"] >= 0


def test_worker_recreated_after_drain():
    """队列排空后 worker 退出；再次 submit 能重建 worker 正常执行。"""
    mgr = JobManager()

    def work(_register):
        return "ok"

    j1 = mgr.submit("c", work)
    assert _wait_until(lambda: mgr.get(j1)["status"] == "done")
    # 等 worker 注销
    assert _wait_until(lambda: "c" not in mgr._workers)
    j2 = mgr.submit("c", work)
    assert _wait_until(lambda: mgr.get(j2)["status"] == "done")


def test_gc_removes_expired():
    """gc 清理超过 TTL 的已结束任务。"""
    mgr = JobManager(ttl_s=0)

    def work(_register):
        return "ok"

    j = mgr.submit("c", work)
    assert _wait_until(lambda: mgr.get(j)["status"] == "done")
    time.sleep(0.02)
    mgr.submit("c2", work)  # 触发 gc
    assert _wait_until(lambda: mgr.get(j) is None)


def test_error_job_reports_error():
    """任务抛错（非取消）→ status error，带错误文本。"""
    mgr = JobManager()

    def boom(_register):
        raise ValueError("boom")

    j = mgr.submit("c", boom)
    assert _wait_until(lambda: mgr.get(j)["status"] == "error")
    assert "boom" in mgr.get(j)["error"]


def test_cancel_unknown_or_finished_returns_false():
    mgr = JobManager()
    assert mgr.cancel("nope") is False

    def work(_register):
        return "ok"

    j = mgr.submit("c", work)
    assert _wait_until(lambda: mgr.get(j)["status"] == "done")
    assert mgr.cancel(j) is False
