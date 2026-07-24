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
        # 直接推到 exhausted，绕过后台线程
        h = Health(state="exhausted", fail_count=5, last_error="boom")
        m._entries[("p", "c")] = h
        with pytest.raises(ConnectionUnavailable) as ei:
            m.check("p", "c")
        assert ei.value.state == "exhausted"
        assert ei.value.retry_after_s == 0
        assert "管理员" in str(ei.value)
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
