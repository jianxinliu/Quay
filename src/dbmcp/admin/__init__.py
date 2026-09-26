"""管理后台：审计查询 + 审批中心 + 连接管理。

挂在 MCP 应用同一 ASGI 服务下（custom_route），默认只随 daemon 监听 127.0.0.1。
服务端渲染，无外部依赖/资源，避免引入前端框架与 CSP 问题。

认证：所有 /admin/* 路由需登录（/admin/login 除外）。token 由 DBM_ADMIN_TOKEN 注入，
登录后下发签名 cookie（hmac(token)，不暴露 token 原文），httponly + samesite=lax。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import (
    approvals,
    audit,
    auth,
    connections,
    console,
    dashboard,
    exports,
    notifications,
    privileges,
    redis,
    settings,
    static,
    workflows,
)
from .approvals import _explain_html
from .auth import _login_page
from .common import _allowed_hosts, _hostname_of, _local_request_ok, _page, error_payload
from .connections import _connectable_engines, _connection_form, _engine_icon_file
from .context import build_context

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ..service import DbmService

__all__ = [
    "mount_admin",
    "error_payload",
    "_explain_html",
    "_allowed_hosts",
    "_hostname_of",
    "_local_request_ok",
    "_connectable_engines",
    "_connection_form",
    "_engine_icon_file",
    "_page",
    "_login_page",
]


def mount_admin(mcp: "FastMCP", service: "DbmService", admin_token: str,
                *, no_auth: bool = False) -> None:
    """挂载管理后台。no_auth=True 时跳过认证——仅供本机测试脚手架，绝不用于生产。"""
    ctx = build_context(mcp, service, admin_token, no_auth=no_auth)
    # 注册顺序与拆分前一致（路径互不重叠，顺序只为可读）
    auth.mount(ctx)
    dashboard.mount(ctx)
    exports.mount(ctx)
    approvals.mount(ctx)
    audit.mount(ctx)
    connections.mount(ctx)
    static.mount(ctx)
    console.mount(ctx)
    redis.mount(ctx)
    privileges.mount(ctx)
    settings.mount(ctx)
    notifications.mount(ctx)
    workflows.mount(ctx)
