"""懒猫免密登录：_ingress_authed 的单元测试。

默认关闭；仅当 DBM_TRUSTED_PROXY_AUTH 开启且请求带 lzc-ingress 注入的身份 header
（X-Forwarded-By: lzc-ingress + X-HC-User-ID）时才视为已认证。
"""
from __future__ import annotations

from starlette.requests import Request

from dbmcp.admin import _ingress_authed


def _req(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/approvals",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


_INGRESS = {"x-forwarded-by": "lzc-ingress", "x-hc-user-id": "alice"}


def test_off_by_default(monkeypatch):
    """未设 DBM_TRUSTED_PROXY_AUTH 时一律不放行（不影响本地进程模式安全模型）。"""
    monkeypatch.delenv("DBM_TRUSTED_PROXY_AUTH", raising=False)
    assert _ingress_authed(_req(_INGRESS)) is False


def test_enabled_and_valid(monkeypatch):
    monkeypatch.setenv("DBM_TRUSTED_PROXY_AUTH", "1")
    assert _ingress_authed(_req(_INGRESS)) is True


def test_enabled_requires_both_headers(monkeypatch):
    monkeypatch.setenv("DBM_TRUSTED_PROXY_AUTH", "1")
    # 缺 X-HC-User-ID
    assert _ingress_authed(_req({"x-forwarded-by": "lzc-ingress"})) is False
    # 缺 X-Forwarded-By
    assert _ingress_authed(_req({"x-hc-user-id": "alice"})) is False
    # X-Forwarded-By 非 lzc-ingress（伪造防护）
    assert _ingress_authed(_req({"x-forwarded-by": "evil", "x-hc-user-id": "alice"})) is False
    # 有 header 名但值为空
    assert _ingress_authed(_req({"x-forwarded-by": "lzc-ingress", "x-hc-user-id": ""})) is False


def test_env_truthy_variants(monkeypatch):
    for val in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("DBM_TRUSTED_PROXY_AUTH", val)
        assert _ingress_authed(_req(_INGRESS)) is True
    for val in ("0", "false", "no", ""):
        monkeypatch.setenv("DBM_TRUSTED_PROXY_AUTH", val)
        assert _ingress_authed(_req(_INGRESS)) is False
