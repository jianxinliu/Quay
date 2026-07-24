"""通知抽象：接口、macOS 实现（mock subprocess）、平台自动选择。"""

from __future__ import annotations

import platform
import time
from unittest.mock import patch

from dbmcp.notify import (
    MacOsNotifier,
    NoopNotifier,
    Notifier,
    _escape_applescript,
    build_default_notifier,
)


class TestNoopNotifier:
    def test_noop_send_returns_none_and_doesnt_raise(self):
        n = NoopNotifier()
        assert isinstance(n, Notifier)
        assert n.send("t", "b") is None
        assert n.send("t", "b", meta={"kind": "test"}) is None


class TestMacOsNotifier:
    def test_send_invokes_osascript_async(self):
        n = MacOsNotifier()
        with patch("dbmcp.notify.subprocess.run") as mock_run:
            n.send("hello", "world")
            # 异步：给线程一点时间跑完
            for _ in range(30):
                if mock_run.called:
                    break
                time.sleep(0.02)
            assert mock_run.called
            args, kwargs = mock_run.call_args
            assert args[0][0] == "osascript"
            assert args[0][1] == "-e"
            assert 'display notification "world"' in args[0][2]
            assert 'subtitle "hello"' in args[0][2]

    def test_send_never_raises_on_subprocess_error(self):
        n = MacOsNotifier()
        with patch("dbmcp.notify.subprocess.run", side_effect=RuntimeError("bad")):
            # 不应抛（异常在后台线程被吞）
            n.send("t", "b")
            time.sleep(0.05)


class TestEscape:
    def test_escapes_quote_and_backslash(self):
        assert _escape_applescript('say "hi"') == 'say \\"hi\\"'
        assert _escape_applescript("path\\to\\thing") == "path\\\\to\\\\thing"

    def test_escapes_newline_to_space(self):
        assert _escape_applescript("line1\nline2") == "line1 line2"


class TestBuildDefault:
    def test_returns_macos_notifier_on_darwin_when_osascript_available(self):
        with patch("dbmcp.notify.platform.system", return_value="Darwin"), \
             patch("dbmcp.notify.shutil.which", return_value="/usr/bin/osascript"):
            assert isinstance(build_default_notifier(), MacOsNotifier)

    def test_returns_noop_on_linux(self):
        with patch("dbmcp.notify.platform.system", return_value="Linux"):
            assert isinstance(build_default_notifier(), NoopNotifier)

    def test_returns_noop_on_darwin_without_osascript(self):
        with patch("dbmcp.notify.platform.system", return_value="Darwin"), \
             patch("dbmcp.notify.shutil.which", return_value=None):
            assert isinstance(build_default_notifier(), NoopNotifier)

    def test_current_platform_returns_a_notifier(self):
        """当前平台调用不出错、返回 Notifier 子类。"""
        n = build_default_notifier()
        assert isinstance(n, Notifier)
        # macOS 上应是 MacOsNotifier（除非 osascript 不可用，通常都有）
        if platform.system() == "Darwin":
            assert isinstance(n, MacOsNotifier)
