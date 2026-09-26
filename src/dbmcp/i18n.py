"""同一份字符串两种读者：agent 恒英文，后台里的人按设置（默认中文）。

风险判定理由、审批单错误、「表不存在」、体检报告这类文案既进 agent 的工具返回值，
也显示在中文后台的审批页 / 体检浮层里。直接改英文会让中文界面夹英文，所以按**调用路径**
选语言：MCP 工具调用进来时中间件把当前语言设成 en，管理后台的 guard 按系统设置
`text_language` 设；库/服务层只管 `t("key", **kw)`，不关心是谁在读。

语言放在 contextvar 里：MCP 工具与后台路由都是 async 入口、再 `anyio.to_thread` 进线程，
contextvar 随 anyio 复制进线程，服务层不必逐层传参。

每个模块把自己的文案就近 `register()` 进目录：键唯一（模块前缀），值是 (zh, en)。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

Locale = Literal["zh", "en"]
LOCALES: tuple[str, ...] = ("zh", "en")
DEFAULT_LOCALE: Locale = "zh"

_LOCALE: ContextVar[str] = ContextVar("dbm_locale", default=DEFAULT_LOCALE)
_CATALOG: dict[str, tuple[str, str]] = {}


def register(entries: dict[str, tuple[str, str]]) -> None:
    """登记一批文案：{key: (zh, en)}。键重复直接报错——两处用同一个键会互相覆盖而不自知。"""
    for key, pair in entries.items():
        if key in _CATALOG and _CATALOG[key] != pair:
            raise ValueError(f"i18n key already registered with different text: {key}")
        if len(pair) != 2:
            raise ValueError(f"i18n entry must be (zh, en): {key}")
        _CATALOG[key] = (str(pair[0]), str(pair[1]))


def current_locale() -> str:
    return _LOCALE.get()


def normalize_locale(value: object) -> Locale:
    v = str(value or "").strip().lower()
    return "en" if v == "en" else "zh"


@contextmanager
def use_locale(locale: object) -> Iterator[None]:
    """在一段代码里切换语言（退出时恢复）。传入非法值按默认中文。"""
    token = _LOCALE.set(normalize_locale(locale))
    try:
        yield
    finally:
        _LOCALE.reset(token)


def t(key: str, /, **kw: object) -> str:
    """取当前语言的文案并格式化。未登记的键原样返回（宁可露出键名也不要抛异常把请求打挂）。"""
    pair = _CATALOG.get(key)
    if pair is None:
        return key
    text = pair[1] if _LOCALE.get() == "en" else pair[0]
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def has_key(key: str) -> bool:
    return key in _CATALOG
