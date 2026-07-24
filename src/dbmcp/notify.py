"""通知抽象：需要人为介入的场景（新审批单、连接 exhausted）主动发一条通知。

遵循 CLAUDE.md 的"安静即正常"原则：只在有事时通知，成功/常规不通知。

设计：
- Notifier 抽象基类，具体渠道各自实现
- MacOsNotifier：osascript display notification（本地进程模式默认，无依赖）
- NoopNotifier：非 macOS 或明确禁用时使用
- 后续可扩：企微/电报 webhook、邮件 SMTP 等

发送线程模型：所有 send() 都必须非阻塞——由 Notifier 自己开 daemon 线程发；
调用方不用 async。异常吞掉不再抛（通知失败不该影响业务流程）。
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str, meta: dict | None = None) -> None:
        """异步发送通知，永远不抛异常。meta 供未来路由/富文本用，本 MVP 忽略。"""


class NoopNotifier(Notifier):
    def send(self, title: str, body: str, meta: dict | None = None) -> None:
        logger.debug("notify (noop): %s / %s", title, body)


class MacOsNotifier(Notifier):
    """macOS 本地通知（osascript display notification）。

    不用 terminal-notifier 之类的三方依赖——osascript 是系统自带。
    通知在系统通知中心弹出；用户可在"系统设置 - 通知"里控制显示样式。
    """

    def send(self, title: str, body: str, meta: dict | None = None) -> None:
        threading.Thread(
            target=self._send_sync, args=(title, body),
            daemon=True, name="dbm-notify",
        ).start()

    @staticmethod
    def _send_sync(title: str, body: str) -> None:
        try:
            # AppleScript 里字符串用双引号；用户内容里的双引号/反斜杠必须转义
            safe_title = _escape_applescript(title)
            safe_body = _escape_applescript(body)
            script = (
                f'display notification "{safe_body}" '
                f'with title "Quay" subtitle "{safe_title}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                check=False, capture_output=True, timeout=5,
            )
        except Exception:  # noqa: BLE001
            logger.exception("macOS notification failed")


def _escape_applescript(s: str) -> str:
    """转义 AppleScript 字符串里的双引号与反斜杠，防止脚本注入/断行。"""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def build_default_notifier() -> Notifier:
    """按当前环境选择合适的 Notifier。

    - macOS + 有 osascript：MacOsNotifier
    - 其他：NoopNotifier（不报错、不发通知——非 macOS 环境暂无本地渠道）
    """
    if platform.system() == "Darwin" and shutil.which("osascript"):
        return MacOsNotifier()
    return NoopNotifier()
