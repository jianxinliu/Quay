"""Redis 控制台接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .context import AdminContext


def _redis_body() -> str:
    """Redis 控制台页面（对标 Medis）：复用 console.css 外壳 + redis.css 布局，逻辑在 redis.js。"""
    return (
        '<link rel="stylesheet" href="/admin/static/console.css">'
        '<link rel="stylesheet" href="/admin/static/redis.css">'
        '<div id="dbm-redis"></div>'
        '<script src="/admin/static/vue.global.prod.js"></script>'
        '<script src="/admin/static/monaco/vs/loader.js"></script>'
        '<script src="/admin/static/dg-select.js"></script>'
        '<script src="/admin/static/redis.js"></script>'
    )


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _shell = ctx._shell
    _caller = ctx._caller
    _resolve_conn = ctx._resolve_conn
    _db_param = ctx._db_param

    @mcp.custom_route("/admin/redis/databases", methods=["GET"])
    @guard
    async def _redis_databases(req: Request) -> JSONResponse:
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            dbs = await anyio.to_thread.run_sync(
                service.redis_databases, project, connection, _caller(req))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "databases": dbs})

    @mcp.custom_route("/admin/redis/keys", methods=["GET"])
    @guard
    async def _redis_keys(req: Request) -> JSONResponse:
        db = _db_param(req.query_params.get("db"))
        pattern = req.query_params.get("pattern") or "*"
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            out = await anyio.to_thread.run_sync(
                service.redis_keys, project, connection, _caller(req), db, pattern)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/redis/value", methods=["GET"])
    @guard
    async def _redis_value(req: Request) -> JSONResponse:
        db = _db_param(req.query_params.get("db"))
        key = req.query_params.get("key", "")
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            out = await anyio.to_thread.run_sync(
                service.redis_value, project, connection, key, _caller(req), db)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/redis/run", methods=["POST"])
    @guard
    async def _redis_run(req: Request) -> JSONResponse:
        f = await req.form()
        db = _db_param(str(f.get("db") or "") or None)
        confirm = str(f.get("confirm") or "") in ("1", "true", "on", "yes")
        confirm_text = str(f.get("confirm_text") or "") or None
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                service.admin_redis_run, project, connection,
                str(f.get("command") or ""), _caller(req), confirm, db, confirm_text)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/redis/command-doc", methods=["GET"])
    @guard
    async def _redis_command_doc(req: Request) -> JSONResponse:
        from ..redis_docs import lookup
        doc = lookup(req.query_params.get("cmd", ""))
        return JSONResponse({"ok": True, "doc": doc})

    @mcp.custom_route("/admin/redis/connections", methods=["GET"])
    @guard
    async def _redis_connections(_req: Request) -> JSONResponse:
        conns = []
        for pname, proj in sorted(service.config.projects.items()):
            for cname, c in sorted(proj.connections.items()):
                if c.engine != "redis":
                    continue
                conns.append({"value": f"{pname}/{cname}", "project": pname, "connection": cname,
                              "environment": c.environment or ""})
        return JSONResponse({"ok": True, "connections": conns})

    @mcp.custom_route("/admin/redis", methods=["GET"])
    @guard
    async def _redis_console(_req: Request) -> HTMLResponse:
        return _shell("Redis", _redis_body(), doc=False)
