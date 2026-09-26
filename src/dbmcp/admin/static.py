"""静态资源路由（不加 guard，见 CLAUDE.md「Monaco 无构建」教训）。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import Response

from .context import AdminContext


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp

    _STATIC_ROOT = (Path(__file__).parent.parent / "static").resolve()
    # 静态资源（Monaco/Vue/console.*）：公共库文件、无敏感数据，且 Monaco 的 web worker
    # 用 data-URI importScripts 拉 workerMain.js，带 cookie 鉴权会被 303 到登录页而崩，
    # 故**不加 @guard**。支持子路径（Monaco AMD loader 按路径拉几十个文件）。
    _STATIC_CT = {
        "js": "application/javascript; charset=utf-8", "css": "text/css; charset=utf-8",
        "json": "application/json; charset=utf-8", "map": "application/json; charset=utf-8",
        "ttf": "font/ttf", "woff": "font/woff", "woff2": "font/woff2", "svg": "image/svg+xml",
        "html": "text/html; charset=utf-8",
    }

    @mcp.custom_route("/admin/static/{path:path}", methods=["GET"])
    async def _static(req: Request) -> Response:
        rel = req.path_params["path"]
        target = (_STATIC_ROOT / rel).resolve()
        # 目录穿越防护：resolve 后必须仍在静态根内
        if not str(target).startswith(str(_STATIC_ROOT) + "/") or not target.is_file():
            return Response("not found", status_code=404)
        ct = _STATIC_CT.get(target.suffix.lstrip(".").lower(), "application/octet-stream")
        # 自家 console.* 迭代频繁 → no-cache（每次校验新鲜度）；vendor（monaco/vue）不变 → 长缓存
        vendor = rel.startswith("monaco/") or rel.startswith("vue.") or rel.startswith("echarts")
        cache = "public, max-age=86400" if vendor else "no-cache"
        return Response(target.read_bytes(), media_type=ct,
                        headers={"Cache-Control": cache})
