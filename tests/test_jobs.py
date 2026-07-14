"""JobManager 单测：按 key 忙时拒绝（Busy）、异 key 并行、计时、取消（含竞态）、gc。"""

import threading
import time

import pytest

from dbmcp.jobs import Busy, JobManager


def _wait_until(pred, timeout=3.0, interval=0.005):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_same_key_busy_rejects_second():
    """同一 key：有任务在跑时，再提交直接 raise Busy（不排队）。"""
    mgr = JobManager()
    gate = threading.Event()
    ran = []

    def first(_register):
        ran.append("a")
        gate.wait(2)
        return {"v": "a"}

    def second(_register):
        ran.append("b")
        return {"v": "b"}

    ja = mgr.submit("conn1", first)
    assert _wait_until(lambda: mgr.get(ja)["status"] == "running")
    with pytest.raises(Busy):
        mgr.submit("conn1", second)      # 忙 → 拒绝
    assert ran == ["a"]                    # 第二条根本没跑

    gate.set()
    assert _wait_until(lambda: mgr.get(ja)["status"] == "done")
    # 前一条结束后，同 key 可以再提交
    jb = mgr.submit("conn1", second)
    assert _wait_until(lambda: mgr.get(jb)["status"] == "done")
    assert ran == ["a", "b"]


def test_different_keys_run_in_parallel():
    """不同 key：并行执行，互不阻塞、互不拒绝。"""
    mgr = JobManager()
    both_running = threading.Barrier(2, timeout=3)

    def work(_register):
        both_running.wait()  # 只有两条同时在跑才能通过
        return "ok"

    ja = mgr.submit("connA", work)
    jb = mgr.submit("connB", work)
    assert _wait_until(lambda: mgr.get(ja)["status"] == "done")
    assert _wait_until(lambda: mgr.get(jb)["status"] == "done")


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


def test_cancel_frees_key_for_next_submit():
    """取消/结束后 key 释放，可再次提交（不会一直 Busy）。"""
    mgr = JobManager()
    proceed = threading.Event()

    def work(register):
        register(lambda: proceed.set())
        proceed.wait(2)

    j1 = mgr.submit("c", work)
    assert _wait_until(lambda: mgr.get(j1)["status"] == "running")
    mgr.cancel(j1)
    assert _wait_until(lambda: mgr.get(j1)["status"] in ("canceled", "done"))
    # key 已释放
    j2 = mgr.submit("c", lambda _r: "ok")
    assert _wait_until(lambda: mgr.get(j2)["status"] == "done")


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


def test_timing_field():
    """执行耗时非负、大致合理。"""
    mgr = JobManager()

    def work(_register):
        time.sleep(0.05)
        return "ok"

    j = mgr.submit("c", work)
    assert _wait_until(lambda: mgr.get(j)["status"] == "done")
    assert mgr.get(j)["elapsed_ms"] >= 40


def test_gc_removes_expired():
    """gc 清理超过 TTL 的已结束任务。"""
    mgr = JobManager(ttl_s=0)
    j = mgr.submit("c", lambda _r: "ok")
    assert _wait_until(lambda: mgr.get(j)["status"] == "done")
    time.sleep(0.02)
    mgr.submit("c2", lambda _r: "ok")  # 触发 gc
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
    j = mgr.submit("c", lambda _r: "ok")
    assert _wait_until(lambda: mgr.get(j)["status"] == "done")
    assert mgr.cancel(j) is False
