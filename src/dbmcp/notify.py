"""通知抽象：需要人为介入的场景（新审批单、连接 exhausted）主动发一条通知。

遵循 CLAUDE.md 的"安静即正常"原则：只在有事时通知，成功/常规不通知。

设计：
- Notifier 抽象基类，具体渠道各自实现
- InboxNotifier（inbox.py）：管理后台内推（默认恒开，SSE 铃铛）
- WebhookNotifier：外部渠道（Bark / 企微机器人 / 飞书机器人），预定义 provider 模板
- MacOsNotifier：osascript display notification（本地进程模式可选，Docker 无用）
- CompositeNotifier：把多个 Notifier 组成一路，并发发送、单渠道失败不影响其他
- NotifierRouter：每次 send 前从 SettingsStore 读最新配置动态组装 Composite，
  设置改完即时生效不需要重启

发送线程模型：所有 send() 都必须非阻塞——由 Notifier 自己开 daemon 线程发；
调用方不用 async。异常吞掉不再抛（通知失败不该影响业务流程）。
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Callable

logger = logging.getLogger(__name__)

# Webhook 请求超时（秒）——短一点，通知不应拖住业务
_WEBHOOK_TIMEOUT_S = 5


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
        # osascript display notification 不支持点击跳转，把 URL 附到 body 尾部
        # 让用户能看到（复制粘贴到浏览器）——比完全丢弃有用
        deeplink = (meta or {}).get("deeplink") or ""
        full_body = f"{body}\n{deeplink}" if deeplink else body
        threading.Thread(
            target=self._send_sync, args=(title, full_body),
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


# ---------- Webhook notifiers（Bark / 企微 / 飞书）----------


def build_bark_payload(title: str, body: str, url: str | None = None,
                       group: str = "Quay") -> dict:
    """Bark（iOS 推送）payload。POST 到 {server}/{device_key}。

    url 若给出，Bark 点击通知会打开该 URL（Bark 官方 `url` 字段）。
    """
    p = {"title": title, "body": body, "group": group}
    if url:
        p["url"] = url
    return p


def build_wecom_payload(title: str, body: str, url: str | None = None) -> dict:
    """企业微信群机器人 payload。

    有 URL 时用 markdown 类型嵌入超链接；无则退回 text 类型（更兼容）。
    """
    if url:
        return {
            "msgtype": "markdown",
            "markdown": {"content": f"**{title}**\n\n{body}\n\n[前往处理]({url})"},
        }
    return {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}


def build_feishu_payload(title: str, body: str, url: str | None = None) -> dict:
    """飞书自定义机器人 payload。

    有 URL 时用 post 富文本类型嵌入超链接；无则退回 text 类型。
    """
    if url:
        return {
            "msg_type": "post",
            "content": {"post": {"zh_cn": {"title": title, "content": [[
                {"tag": "text", "text": body},
                {"tag": "text", "text": "\n"},
                {"tag": "a", "text": "前往处理", "href": url},
            ]]}}},
        }
    return {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}


def build_admin_deeplink(base_url: str, path: str) -> str:
    """把管理后台内部路径拼成外部可访问的绝对 URL。

    base_url 默认 http://127.0.0.1:8100（本机）；Docker/反代场景用户在设置里改。
    """
    base = (base_url or "http://127.0.0.1:8100").rstrip("/")
    path = "/" + path.lstrip("/")
    return f"{base}{path}"


def approval_deeplink(base_url: str, change_id: int) -> str:
    """审批详情页的直达 URL。"""
    return build_admin_deeplink(base_url, f"/admin/approvals/{int(change_id)}")


# provider 名 → (URL 构造函数, payload 构造函数)
# URL 构造函数签名：(config: dict) → str
# payload 构造函数签名：(title, body, url=None) → dict
def _bark_url(cfg: dict) -> str:
    """Bark 支持自建 server；官方 https://api.day.app。device key 为路径。"""
    server = str(cfg.get("bark_server") or "https://api.day.app").rstrip("/")
    key = str(cfg.get("bark_key") or "").strip()
    if not key:
        raise ValueError("Bark device key 未配置")
    return f"{server}/{key}"


def _wecom_url(cfg: dict) -> str:
    url = str(cfg.get("wecom_webhook") or "").strip()
    if not url:
        raise ValueError("企微机器人 webhook 未配置")
    return url


def _feishu_url(cfg: dict) -> str:
    url = str(cfg.get("feishu_webhook") or "").strip()
    if not url:
        raise ValueError("飞书机器人 webhook 未配置")
    return url


_WEBHOOK_PROVIDERS: dict[
    str, tuple[Callable[[dict], str], Callable[..., dict]]
] = {
    "bark": (_bark_url, build_bark_payload),
    "wecom": (_wecom_url, build_wecom_payload),
    "feishu": (_feishu_url, build_feishu_payload),
}


class WebhookNotifier(Notifier):
    """外部 webhook 渠道通用实现。

    provider ∈ {'bark', 'wecom', 'feishu'}；config 里放该 provider 需要的字段
    （见 _WEBHOOK_PROVIDERS 的 URL 构造函数）。发送异步、失败仅打日志。
    """

    def __init__(self, provider: str, config: dict):
        if provider not in _WEBHOOK_PROVIDERS:
            raise ValueError(f"未知通知渠道: {provider}")
        self._provider = provider
        self._config = dict(config or {})

    def send(self, title: str, body: str, meta: dict | None = None) -> None:
        deeplink = (meta or {}).get("deeplink") or None
        threading.Thread(
            target=self._send_sync, args=(title, body, deeplink),
            daemon=True, name=f"dbm-notify-{self._provider}",
        ).start()

    def _send_sync(self, title: str, body: str, deeplink: str | None = None) -> None:
        url_fn, payload_fn = _WEBHOOK_PROVIDERS[self._provider]
        try:
            url = url_fn(self._config)
        except ValueError as e:
            logger.warning("%s webhook 未生效: %s", self._provider, e)
            return
        payload = payload_fn(title, body, url=deeplink) if deeplink else payload_fn(title, body)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_S) as resp:
                if resp.status >= 400:
                    logger.warning("%s webhook 返回 %s: %s",
                                   self._provider, resp.status, resp.read()[:200])
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("%s webhook 发送失败: %s", self._provider, e)
        except Exception:  # noqa: BLE001
            logger.exception("%s webhook 未预期异常", self._provider)


# ---------- 组合 & 路由 ----------


class CompositeNotifier(Notifier):
    """并发调用多个下游 Notifier；单渠道失败被吞（不阻断其他）。"""

    def __init__(self, notifiers: list[Notifier]):
        self._notifiers = [n for n in notifiers if n is not None]

    def send(self, title: str, body: str, meta: dict | None = None) -> None:
        for n in self._notifiers:
            try:
                n.send(title, body, meta)
            except Exception:  # noqa: BLE001
                logger.exception("notifier %s send failed", type(n).__name__)


class NotifierRouter(Notifier):
    """每次 send 前调 factory() 得到 Notifier 组合——设置变更即时生效不需重启。

    factory 返回值应该是 Notifier 或 None（None 视作 NoopNotifier）。
    """

    def __init__(self, factory: Callable[[], Notifier | None]):
        self._factory = factory

    def send(self, title: str, body: str, meta: dict | None = None) -> None:
        try:
            n = self._factory()
        except Exception:  # noqa: BLE001
            logger.exception("notifier factory failed")
            return
        if n is None:
            return
        try:
            n.send(title, body, meta)
        except Exception:  # noqa: BLE001
            logger.exception("routed notifier failed")


def is_macos() -> bool:
    return platform.system() == "Darwin" and bool(shutil.which("osascript"))


def build_from_settings(settings: dict, inbox: Notifier | None) -> Notifier:
    """按设置组装 Notifier：Inbox 恒开 + 主外部渠道 + 可选 macOS。

    settings 键：
    - notify_primary: 'none' | 'bark' | 'wecom' | 'feishu'
    - notify_bark_server / notify_bark_key
    - notify_wecom_webhook
    - notify_feishu_webhook
    - notify_macos_enabled: bool（仅 macOS 有效）
    inbox 传 None 时不加内推（供无 admin 存储的极简 CLI 场景，理论不该发生）
    """
    channels: list[Notifier] = []
    if inbox is not None:
        channels.append(inbox)

    primary = str(settings.get("notify_primary") or "none").lower()
    # 组装时用 URL 构造函数预校验一次——避免加进一个"发送时才发现没配置"的空壳 Notifier
    provider_config: dict[str, dict] = {
        "bark": {"bark_server": settings.get("notify_bark_server"),
                 "bark_key": settings.get("notify_bark_key")},
        "wecom": {"wecom_webhook": settings.get("notify_wecom_webhook")},
        "feishu": {"feishu_webhook": settings.get("notify_feishu_webhook")},
    }
    if primary in _WEBHOOK_PROVIDERS:
        cfg = provider_config[primary]
        url_fn, _ = _WEBHOOK_PROVIDERS[primary]
        try:
            url_fn(cfg)  # 预校验
        except ValueError:
            logger.info("primary channel %s selected but not configured; skipping", primary)
        else:
            channels.append(WebhookNotifier(primary, cfg))

    if bool(settings.get("notify_macos_enabled")) and is_macos():
        channels.append(MacOsNotifier())

    if not channels:
        return NoopNotifier()
    if len(channels) == 1:
        return channels[0]
    return CompositeNotifier(channels)


def build_default_notifier() -> Notifier:
    """无 settings 场景的兜底：macOS 上给系统通知，其他 Noop。

    正式 serve 路径请用 NotifierRouter(factory=lambda: build_from_settings(...))
    以支持"改设置即时生效"。
    """
    return MacOsNotifier() if is_macos() else NoopNotifier()
