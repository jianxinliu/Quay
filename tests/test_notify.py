"""通知抽象：接口、macOS 实现（mock subprocess）、平台自动选择。"""

from __future__ import annotations

import platform
import time
from unittest.mock import patch

from dbmcp.notify import (
    CompositeNotifier,
    MacOsNotifier,
    NoopNotifier,
    Notifier,
    NotifierRouter,
    WebhookNotifier,
    _escape_applescript,
    build_bark_payload,
    build_default_notifier,
    build_feishu_payload,
    build_from_settings,
    build_wecom_payload,
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


class TestWebhookPayloads:
    def test_bark_payload(self):
        p = build_bark_payload("hello", "world")
        assert p == {"title": "hello", "body": "world", "group": "Quay"}

    def test_wecom_payload_shape(self):
        p = build_wecom_payload("hello", "world")
        assert p == {"msgtype": "text", "text": {"content": "hello\nworld"}}

    def test_feishu_payload_shape(self):
        p = build_feishu_payload("hello", "world")
        assert p == {"msg_type": "text", "content": {"text": "hello\nworld"}}


class TestWebhookNotifier:
    def test_bark_missing_key_no_send(self):
        # 未填 key → URL 构造抛 ValueError → 日志警告、不 POST
        n = WebhookNotifier("bark", {"bark_key": ""})
        with patch("dbmcp.notify.urllib.request.urlopen") as m:
            n._send_sync("t", "b")
            assert not m.called

    def test_bark_sends_to_configured_server(self):
        n = WebhookNotifier("bark", {"bark_server": "https://x/", "bark_key": "abc"})
        with patch("dbmcp.notify.urllib.request.urlopen") as m:
            m.return_value.__enter__ = lambda self_: type("R", (), {"status": 200, "read": lambda: b""})()
            m.return_value.__exit__ = lambda *a: None
            n._send_sync("hello", "world")
            req = m.call_args.args[0]
            assert req.full_url == "https://x/abc"
            assert req.get_method() == "POST"

    def test_wecom_sends_to_webhook_url(self):
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
        n = WebhookNotifier("wecom", {"wecom_webhook": url})
        with patch("dbmcp.notify.urllib.request.urlopen") as m:
            m.return_value.__enter__ = lambda self_: type("R", (), {"status": 200, "read": lambda: b""})()
            m.return_value.__exit__ = lambda *a: None
            n._send_sync("t", "b")
            assert m.call_args.args[0].full_url == url

    def test_send_never_raises_on_network_error(self):
        import urllib.error
        n = WebhookNotifier("bark", {"bark_key": "k"})
        with patch("dbmcp.notify.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("nope")):
            n._send_sync("t", "b")  # 不应抛

    def test_unknown_provider_rejected(self):
        import pytest
        with pytest.raises(ValueError, match="未知通知渠道"):
            WebhookNotifier("email", {})


class TestComposite:
    def test_delivers_to_all(self):
        calls: list[tuple] = []

        class Rec(Notifier):
            def __init__(self, tag):
                self.tag = tag
            def send(self, t, b, meta=None):
                calls.append((self.tag, t, b))

        c = CompositeNotifier([Rec("a"), Rec("b")])
        c.send("T", "B")
        assert sorted(x[0] for x in calls) == ["a", "b"]

    def test_one_failure_doesnt_block_others(self):
        got: list[str] = []

        class Boom(Notifier):
            def send(self, t, b, meta=None):
                raise RuntimeError("boom")

        class Ok(Notifier):
            def send(self, t, b, meta=None):
                got.append(t)

        c = CompositeNotifier([Boom(), Ok()])
        c.send("hi", "there")
        assert got == ["hi"]


class TestRouter:
    def test_reads_factory_each_call(self):
        counter = {"n": 0}
        got: list[str] = []

        class Rec(Notifier):
            def send(self, t, b, meta=None):
                got.append(t)

        def factory():
            counter["n"] += 1
            return Rec()

        r = NotifierRouter(factory)
        r.send("a", "1")
        r.send("b", "2")
        assert counter["n"] == 2
        assert got == ["a", "b"]

    def test_none_factory_result_is_noop(self):
        r = NotifierRouter(lambda: None)
        r.send("t", "b")  # 不应抛


class TestBuildFromSettings:
    def test_no_channels_returns_noop(self):
        n = build_from_settings({"notify_primary": "none"}, inbox=None)
        assert isinstance(n, NoopNotifier)

    def test_inbox_only(self):
        rec: list[tuple] = []

        class Rec(Notifier):
            def send(self, t, b, meta=None):
                rec.append((t, b))

        n = build_from_settings({"notify_primary": "none"}, inbox=Rec())
        n.send("x", "y")
        assert rec == [("x", "y")]

    def test_wecom_selected_composes_inbox_and_wecom(self):
        n = build_from_settings(
            {"notify_primary": "wecom", "notify_wecom_webhook": "https://x/wh"},
            inbox=NoopNotifier(),
        )
        assert isinstance(n, CompositeNotifier)

    def test_bark_missing_key_falls_back_to_inbox_only(self):
        # 选了 Bark 但 key 空 → WebhookNotifier 构造失败被吞 → 只剩 inbox
        n = build_from_settings(
            {"notify_primary": "bark", "notify_bark_key": ""},
            inbox=NoopNotifier(),
        )
        # 只有一个渠道时直接返回该 Notifier（Inbox）
        assert isinstance(n, NoopNotifier)
