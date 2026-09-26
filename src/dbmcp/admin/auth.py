"""登录 / 登出 / favicon。"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .common import _COOKIE_NAME, _FAVICON_LINK, _FAVICON_SVG, _authed, _esc, _local_request_ok
from .context import AdminContext


def _login_page(error: str = "") -> str:
    err = (f"<div style='background:#fef2f2;border:1px solid #fca5a5;color:#b91c1c;"
           f"padding:9px 13px;border-radius:8px;font-size:13px;margin-bottom:14px'>{_esc(error)}</div>"
           if error else "")
    mono = "ui-monospace,'SF Mono',Menlo,monospace"
    sans = "-apple-system,'SF Pro Text',system-ui,'PingFang SC',sans-serif"
    body = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>登录 · Quay</title>
{_FAVICON_LINK}
<style>
 *{{box-sizing:border-box}}
 body{{font-family:{sans};margin:0;min-height:100vh;display:flex;justify-content:center;align-items:center;
   background:#14181f;color:#e6e8ec;-webkit-font-smoothing:antialiased;
   background-image:radial-gradient(circle at 30% 20%,#1c2530 0,transparent 55%),radial-gradient(circle at 80% 90%,#122a27 0,transparent 55%)}}
 .box{{width:340px;padding:34px 30px}}
 .brand{{display:flex;align-items:center;gap:12px;margin-bottom:22px}}
 .brand svg{{width:40px;height:40px}}
 .brand b{{font-size:17px;color:#fff;font-weight:600}}
 .brand span{{display:block;font-family:{mono};font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#6b7280}}
 label{{font-family:{mono};font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:#9aa1ac;
   display:block;margin-bottom:8px}}
 input{{width:100%;padding:11px 13px;background:#1c222c;border:1px solid #2c3440;border-radius:9px;
   color:#fff;font-size:14px;font-family:{mono};transition:border-color .12s,box-shadow .12s}}
 input:focus{{outline:none;border-color:#0d9488;box-shadow:0 0 0 3px rgba(13,148,136,.2)}}
 button{{width:100%;margin-top:16px;padding:11px;background:#0d9488;color:#fff;border:none;border-radius:9px;
   font-size:14px;font-weight:500;cursor:pointer;font-family:{sans};transition:filter .12s}}
 button:hover{{filter:brightness(1.12)}}
</style></head><body>
<form class="box" method="post" action="/admin/login">
 <div class="brand">{_FAVICON_SVG}<div><b>Quay</b><span>gatekeeper</span></div></div>
 {err}
 <label>管理 token</label>
 <input type="password" name="token" placeholder="输入管理 token" autofocus>
 <button type="submit">进入控制台</button>
</form></body></html>"""
    return body


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    admin_token = ctx.admin_token
    expected_cookie = ctx.expected_cookie

    @mcp.custom_route("/favicon.ico", methods=["GET"])
    @mcp.custom_route("/favicon.svg", methods=["GET"])
    @mcp.custom_route("/admin/favicon.svg", methods=["GET"])
    async def _favicon(_req: Request) -> Response:
        return Response(
            _FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @mcp.custom_route("/admin/login", methods=["GET"])
    async def _login_form(req: Request) -> HTMLResponse:
        if _authed(req, expected_cookie):
            return RedirectResponse(url="/admin/approvals", status_code=303)
        return HTMLResponse(_login_page())

    @mcp.custom_route("/admin/login", methods=["POST"])
    async def _login_submit(req: Request) -> Response:
        if not _local_request_ok(req):
            return Response("forbidden", status_code=403)
        form = await req.form()
        token = str(form.get("token") or "")
        if token and hmac.compare_digest(token, admin_token):
            resp = RedirectResponse(url="/admin/approvals", status_code=303)
            resp.set_cookie(_COOKIE_NAME, expected_cookie, httponly=True,
                            samesite="lax", max_age=86400, path="/admin")
            return resp
        return HTMLResponse(_login_page("token 错误"), status_code=401)

    @mcp.custom_route("/admin/logout", methods=["GET"])
    async def _logout(_req: Request) -> Response:
        resp = RedirectResponse(url="/admin/login", status_code=303)
        resp.delete_cookie(_COOKIE_NAME, path="/admin")
        return resp
