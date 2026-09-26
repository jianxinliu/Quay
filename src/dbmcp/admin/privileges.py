"""用户与权限管理接口。"""

from __future__ import annotations


import anyio.to_thread
from starlette.requests import Request
from starlette.responses import JSONResponse

from .common import error_payload
from .context import AdminContext


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _caller = ctx._caller
    _resolve_conn = ctx._resolve_conn

    # ---------- 用户与权限管理（查询台弹窗的数据接口）----------
    #
    # 没有独立页面：入口是查询台左树「库/连接」节点的右键菜单，账号与授权跟着你正在看的
    # 那个连接走。这里只提供 JSON 接口，UI 在 static/privileges.js（注册到 console.js 的
    # priv-panel 组件）。
    # 只挂在 @guard 后面（已认证的人），**不暴露为 MCP 工具**——红线 5 说连接与密钥
    # 管理 agent 碰不到，账号权限管理同理。写路径由服务端按动作名构造语句
    # （privileges.build），页面永远传不进任意 SQL。

    @mcp.custom_route("/admin/privileges/connections", methods=["GET"])
    @guard
    async def _priv_connections(_req: Request) -> JSONResponse:
        """只列支持权限管理的连接（PG / MySQL），并标出有没有 writer（管理）账号。"""
        from ..privileges import SUPPORTED_ENGINES
        conns = []
        for pname, proj in sorted(service.config.projects.items()):
            for cname, c in sorted(proj.connections.items()):
                if c.engine not in SUPPORTED_ENGINES:
                    continue
                conns.append({
                    "value": f"{pname}/{cname}", "project": pname, "connection": cname,
                    "engine": c.engine, "environment": c.environment or "",
                    "database": c.database or "", "has_writer": c.writer is not None,
                    "admin_user": (c.writer.user if c.writer else c.user),
                })
        return JSONResponse({"ok": True, "connections": conns})

    @mcp.custom_route("/admin/privileges/users", methods=["GET"])
    @guard
    async def _priv_users(req: Request) -> JSONResponse:
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            out = await anyio.to_thread.run_sync(
                service.admin_list_db_users, project, connection, _caller(req),
                req.query_params.get("db") or None)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/privileges/grants", methods=["GET"])
    @guard
    async def _priv_grants(req: Request) -> JSONResponse:
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            out = await anyio.to_thread.run_sync(
                service.admin_db_user_grants, project, connection,
                req.query_params.get("user", ""), _caller(req),
                req.query_params.get("host", "%"), req.query_params.get("db") or None)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/privileges/matrix", methods=["GET"])
    @guard
    async def _priv_matrix(req: Request) -> JSONResponse:
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            out = await anyio.to_thread.run_sync(
                service.admin_privilege_matrix, project, connection,
                req.query_params.get("schema", ""), _caller(req),
                req.query_params.get("db") or None)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/privileges/schemas", methods=["GET"])
    @guard
    async def _priv_schemas(req: Request) -> JSONResponse:
        """权限矩阵的 schema / 库下拉。复用查询台那套列库逻辑。"""
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            dbs = await anyio.to_thread.run_sync(
                service.list_databases, project, connection, _caller(req),
                req.query_params.get("db") or None)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, "databases": dbs})

    @mcp.custom_route("/admin/privileges/run", methods=["POST"])
    @guard
    async def _priv_run(req: Request) -> JSONResponse:
        """权限变更。第一次调用只回确认卡片，带 confirm=1 才真执行。"""
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"})
        try:
            project, connection = _resolve_conn(str(body.get("conn") or ""))
            confirm = str(body.get("confirm") or "") in ("1", "on", "true", "True")
            out = await anyio.to_thread.run_sync(
                lambda: service.admin_run_dcl(
                    project, connection, str(body.get("action") or ""),
                    body.get("params") or {}, _caller(req), confirm=confirm,
                    confirm_text=body.get("confirm_text"),
                    expect_fingerprint=body.get("expect_fingerprint"),
                    database=body.get("db") or None))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, **out})
