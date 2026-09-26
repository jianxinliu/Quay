"""AdminContext：mount_admin 原闭包里被各路由组共享的状态
（service、guard、_shell、调用方信息、异步任务管理器…），拆成对象后由各路由模块的 mount(ctx) 取用。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ..service import CallerInfo, DbmService

from ..i18n import use_locale
from .common import _COOKIE_NAME, _authed, _local_request_ok, _page, _session_value, _wants_json


class AdminContext:
    """各路由模块共享的状态；属性名与原闭包变量名保持一致。"""

    __slots__ = ("mcp", "service", "admin_token", "no_auth", "expected_cookie",
                 "guard", "_shell", "_theme", "_caller", "_analysis_ws", "_resolve_conn",
                 "_db_param", "_jobmgr", "_solo_key")

    def __init__(self, **kw: object) -> None:
        for k in self.__slots__:
            setattr(self, k, kw[k])


def build_context(mcp: "FastMCP", service: "DbmService", admin_token: str,
                  *, no_auth: bool = False) -> AdminContext:
    """构造共享上下文（原 mount_admin 闭包头部的逐字搬运）。"""
    expected_cookie = _session_value(admin_token)

    def guard(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        """未认证访问受保护路由 → 重定向登录页；非本机来源（Host/Origin 不符）→ 403。

        no_auth 模式跳过认证检查（本机测试专用）；Host/Origin 校验仍保留。
        """
        @wraps(handler)
        async def _wrapped(req: Request) -> Response:
            if not _local_request_ok(req):
                if _wants_json(req):
                    return JSONResponse(
                        {"ok": False, "error": "请求被拒绝：管理后台只能从本机访问"},
                        status_code=403)
                return Response("forbidden: request must originate from localhost",
                                status_code=403)
            if not no_auth and not _authed(req, expected_cookie):
                # 前端 fetch 期望 JSON → 返回 401 JSON（前端据此提示重新登录）；
                # 浏览器页面导航 → 仍 303 到登录页。
                if _wants_json(req):
                    return JSONResponse(
                        {"ok": False, "error": "登录已过期，请刷新页面重新登录"},
                        status_code=401)
                return RedirectResponse(url="/admin/login", status_code=303)
            # 后台里这类共享文案（风险理由/体检报告/错误）按系统设置选语言，默认中文
            with use_locale(_text_locale()):
                return await handler(req)
        return _wrapped

    def _text_locale() -> str:
        try:
            return str(service.get_settings().get("text_language") or "zh")
        except Exception:
            return "zh"

    def _theme() -> str:
        """系统设置里的主题（dark / light），全站页面外壳共用；读不到按默认深色。"""
        try:
            return "light" if service.get_settings().get("theme") == "light" else "dark"
        except Exception:
            return "dark"

    def _shell(title: str, body: str, doc: bool = True,
               extra_head: str = "") -> HTMLResponse:
        """渲染登录后的页面，自动注入待审批数（侧栏角标 + 顶部横幅）。"""
        try:
            # 惰性过期：存储态还是 pending 但已过 TTL 的单不该继续闪红点
            pending = len([c for c in service.list_changes("pending")
                           if c.effective_status() == "pending"])
        except Exception:
            pending = 0
        fs = None
        if doc:
            try:
                fs = int(service.get_settings().get("ui_font_size") or 14)
            except Exception:
                fs = None
        return HTMLResponse(_page(title, body, pending=pending, doc=doc, font_size=fs,
                                  extra_head=extra_head, theme=_theme()))

    def _caller(req: Request) -> "CallerInfo":
        from ..service import CallerInfo
        return CallerInfo(agent="admin-ui", session_id=req.cookies.get(_COOKIE_NAME, "")[:12])

    # ---------- 查询台（DataGrip 风格：元信息浏览 + SQL 执行 + 导出）----------

    def _analysis_ws(raw: str) -> str | None:
        """conn 形如 "analysis/<workspace>" 时返回工作区名（查询台把工作区当连接用）。"""
        return raw.split("/", 1)[1] if raw.startswith("analysis/") else None

    def _resolve_conn(raw: str) -> tuple[str, str]:
        """解析 "project/connection"，校验存在，返回 (project, connection)。"""
        if not raw or "/" not in raw:
            raise KeyError("请选择连接")
        project, connection = raw.split("/", 1)
        service.config.get_connection(project, connection)  # 不存在会抛
        return project, connection

    # ---------- Redis 浏览 / 命令窗口（对标 Medis）----------

    def _db_param(raw: str | None) -> int | None:
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    # 异步查询任务：查询在服务端串行队列执行，页面切走/刷新不中断；前端凭 job_id
    # 轮询取结果（job_id 持久化在前端状态里，切回来自动续接）。结果保留 10 分钟。
    # 按连接串行（同一连接同时只跑一条 SQL，其余排队 FIFO；不同连接各自并行）——
    # queue_key=(project, connection)；workflow/画布这类多连接任务用独立 key（object()）
    # 各自并行、不参与串行。计时/排队位置/取消都由 JobManager 统一提供。
    from ..jobs import JobManager

    _jobmgr = JobManager(ttl_s=600)

    def _solo_key() -> object:
        """给不参与串行的任务（workflow/画布 DAG）一个唯一 key，使其立即并行执行。"""
        return object()

    return AdminContext(mcp=mcp, service=service, admin_token=admin_token, no_auth=no_auth, expected_cookie=expected_cookie, guard=guard, _shell=_shell, _theme=_theme, _caller=_caller, _analysis_ws=_analysis_ws, _resolve_conn=_resolve_conn, _db_param=_db_param, _jobmgr=_jobmgr, _solo_key=_solo_key)
