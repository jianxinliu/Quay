"""连接健康监控：状态位、退避、重连、is_connection_error 分类。"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from dbmcp.health import (
    ConnectionUnavailable,
    Health,
    HealthMonitor,
    is_connection_error,
)


class TestIsConnectionError:
    def test_socket_timeout_is_conn_error(self):
        assert is_connection_error(socket.timeout("timed out"))

    def test_connection_error_is_conn_error(self):
        assert is_connection_error(ConnectionRefusedError("Connection refused"))

    def test_gaierror_is_conn_error(self):
        assert is_connection_error(socket.gaierror("Name or service not known"))

    def test_named_operational_error(self):
        class OperationalError(Exception):
            pass

        assert is_connection_error(OperationalError("(2013, 'Lost connection to MySQL server')"))

    def test_message_gone_away(self):
        assert is_connection_error(RuntimeError("MySQL server has gone away"))

    def test_tunnel_error_by_name(self):
        class TunnelError(Exception):
            pass

        assert is_connection_error(TunnelError("隧道就绪超时 30s"))

    def test_syntax_error_is_not_conn_error(self):
        # SQL 语法/权限错不能触发重连——它们无法通过重连解决
        assert not is_connection_error(ValueError("SQL 语法错误"))

    def test_bare_exception_is_not_conn_error(self):
        assert not is_connection_error(Exception("something else"))


class TestHealthMonitor:
    def test_ok_state_by_default(self):
        m = HealthMonitor(probe=lambda p, c: None)
        # 无记录时 check 直接放行
        m.check("p", "c")
        assert m.get("p", "c") is None

    def test_mark_failed_then_check_raises_unavailable(self):
        m = HealthMonitor(probe=lambda p, c: (_ for _ in ()).throw(RuntimeError("still down")))
        m.mark_failed("p", "c", "OperationalError: lost connection")
        with pytest.raises(ConnectionUnavailable) as ei:
            m.check("p", "c")
        assert ei.value.state == "unavailable"
        assert ei.value.retry_after_s >= 5
        # 记录里保留了错误摘要
        h = m.get("p", "c")
        assert h.state == "unavailable"
        assert "lost connection" in h.last_error
        m.stop()

    def test_probe_success_transitions_to_ok(self):
        # probe 立即成功；缩短第一档退避让测试快
        called = threading.Event()

        def probe(p, c):
            called.set()
            return None

        m = HealthMonitor(probe=probe)
        m.mark_failed("p", "c", "boom")
        # 把 next_retry_at 提前到现在，让后台线程立即探测
        h = m._entries[("p", "c")]
        h.next_retry_at = time.monotonic()
        assert called.wait(timeout=2)
        # 给它一点时间把状态刷成 ok
        for _ in range(20):
            if m.get("p", "c") and m.get("p", "c").state == "ok":
                break
            time.sleep(0.05)
        assert m.get("p", "c").state == "ok"
        m.stop()

    def test_probe_failure_advances_backoff_then_exhausts(self, monkeypatch):
        exhausted = []
        attempts = []

        def probe(p, c):
            attempts.append(1)
            raise ConnectionRefusedError("nope")

        def on_ex(p, c, err):
            exhausted.append((p, c, err))

        # 用极短退避跑完整个流程，避免真的睡数分钟
        monkeypatch.setattr("dbmcp.health.BACKOFF_STEPS_S", (0.01, 0.01, 0.01))
        m = HealthMonitor(probe=probe, on_exhausted=on_ex)
        m.mark_failed("p", "c", "boom")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            h = m._entries.get(("p", "c"))
            if h is None or h.state == "exhausted":
                break
            time.sleep(0.02)
        h = m.get("p", "c")
        assert h.state == "exhausted", f"state={h.state} fail_count={h.fail_count}"
        assert h.fail_count >= 3
        # exhausted 回调只调一次
        assert len(exhausted) == 1
        assert exhausted[0][:2] == ("p", "c")
        m.stop()

    def test_exhausted_check_gives_actionable_message(self):
        m = HealthMonitor(probe=lambda p, c: (_ for _ in ()).throw(RuntimeError("down")))
        # 直接推到 exhausted，绕过后台线程；退避窗口内（下次重试还没到点）
        h = Health(state="exhausted", fail_count=5, last_error="boom",
                   next_retry_at=time.monotonic() + 60)
        m._entries[("p", "c")] = h
        with pytest.raises(ConnectionUnavailable) as ei:
            m.check("p", "c")
        assert ei.value.state == "exhausted"
        # 不再是"永久放弃"：仍给出下次重试时间，且文案说明在自动重试
        assert ei.value.retry_after_s > 0
        assert "自动重试" in str(ei.value)
        m.stop()


class TestCircuitBreakerHalfOpen:
    """断路器半开：退避到点后放行一个真实请求去探路，成功即自愈、不需人工点重连。"""

    def _down(self, state="unavailable", fail_count=1, due_in=60.0):
        m = HealthMonitor(probe=lambda p, c: (_ for _ in ()).throw(RuntimeError("down")))
        m._entries[("p", "c")] = Health(
            state=state, fail_count=fail_count, last_error="boom",
            next_retry_at=time.monotonic() + due_in)
        return m

    def test_within_backoff_window_fails_fast(self):
        m = self._down(due_in=60)
        with pytest.raises(ConnectionUnavailable):
            m.check("p", "c")
        m.stop()

    def test_due_lets_exactly_one_request_through(self):
        m = self._down(due_in=-1)          # 已到点
        m.check("p", "c")                  # 第一个请求被放行去探路
        assert m.get("p", "c").probing is True
        with pytest.raises(ConnectionUnavailable):
            m.check("p", "c")              # 并发的第二个仍快速失败，不重复打 DB
        m.stop()

    def test_exhausted_also_half_opens(self):
        """exhausted 不再是死路：到点同样放行探路，DB 恢复后下一次调用即自愈。"""
        m = self._down(state="exhausted", fail_count=5, due_in=-1)
        m.check("p", "c")
        m.mark_ok("p", "c")
        assert m.get("p", "c").state == "ok"
        m.check("p", "c")                  # 之后正常放行
        m.stop()

    def test_half_open_failure_reschedules_and_advances(self):
        m = self._down(due_in=-1)
        before = m.get("p", "c").fail_count
        m.check("p", "c")
        m.mark_failed("p", "c", "OperationalError: lost connection")
        h = m.get("p", "c")
        assert h.probing is False
        assert h.fail_count == before + 1
        assert h.next_retry_at > time.monotonic()   # 重新排了退避
        with pytest.raises(ConnectionUnavailable):
            m.check("p", "c")
        m.stop()

    def test_stale_probe_lease_expires(self, monkeypatch):
        """探路者没回报成败（调用方异常退出）时不能把连接永久锁死。"""
        monkeypatch.setattr("dbmcp.health.PROBE_LEASE_S", 0.05)
        m = self._down(due_in=-1)
        m.check("p", "c")
        assert m.get("p", "c").probing is True
        time.sleep(0.08)
        m.check("p", "c")                  # 租约过期 → 允许新的探路
        m.stop()

    def test_never_gives_up_after_exhausted(self, monkeypatch):
        """退避阶梯用完后仍持续重试——DB 恢复后无人访问也能自动转 ok。"""
        attempts = []
        recovered = threading.Event()

        def probe(p, c):
            attempts.append(1)
            if len(attempts) < 6:          # 前 5 次失败足以走完退避阶梯进 exhausted
                raise ConnectionRefusedError("nope")
            recovered.set()

        monkeypatch.setattr("dbmcp.health.BACKOFF_STEPS_S", (0.01, 0.01, 0.01))
        monkeypatch.setattr("dbmcp.health._TICK_S", 0.01)
        m = HealthMonitor(probe=probe)
        m.mark_failed("p", "c", "boom")
        assert recovered.wait(timeout=5), f"探测在 exhausted 后停了，attempts={len(attempts)}"
        for _ in range(50):
            if m.get("p", "c") and m.get("p", "c").state == "ok":
                break
            time.sleep(0.02)
        assert m.get("p", "c").state == "ok"
        m.stop()

    def test_force_clear(self):
        m = HealthMonitor(probe=lambda p, c: None)
        m.mark_failed("p", "c", "boom")
        assert m.get("p", "c").state == "unavailable"
        m.force_clear("p", "c")
        assert m.get("p", "c") is None
        m.stop()

    def test_mark_ok_from_unavailable(self):
        m = HealthMonitor(probe=lambda p, c: None)
        m.mark_failed("p", "c", "boom")
        m.mark_ok("p", "c")
        assert m.get("p", "c").state == "ok"
        # 之后 check 放行
        m.check("p", "c")
        m.stop()
