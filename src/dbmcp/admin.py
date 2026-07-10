"""管理后台：审计查询 + 审批中心 + 连接管理。

挂在 MCP 应用同一 ASGI 服务下（custom_route），默认只随 daemon 监听 127.0.0.1。
服务端渲染，无外部依赖/资源，避免引入前端框架与 CSP 问题。

认证：所有 /admin/* 路由需登录（/admin/login 除外）。token 由 DBM_ADMIN_TOKEN 注入，
登录后下发签名 cookie（hmac(token)，不暴露 token 原文），httponly + samesite=lax。
"""

from __future__ import annotations

import hashlib
import hmac
import html
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .approvals import ApprovalError

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .service import DbmService

_COOKIE_NAME = "dbm_admin"


def _session_value(token: str) -> str:
    """cookie 值：hmac(token) 十六进制，cookie 泄露也不直接暴露 token 原文。"""
    return hmac.new(token.encode("utf-8"), b"dbm-admin-session", hashlib.sha256).hexdigest()


def _authed(req: Request, expected_cookie: str) -> bool:
    got = req.cookies.get(_COOKIE_NAME, "")
    return bool(got) and hmac.compare_digest(got, expected_cookie)

_LEVEL_COLOR = {
    "CRITICAL": "#b00020",
    "HIGH": "#e65100",
    "MEDIUM": "#f9a825",
    "LOW": "#2e7d32",
}
_STATUS_COLOR = {
    "pending": "#1565c0",
    "approved": "#2e7d32",
    "rejected": "#b00020",
    "consumed": "#555",
    "expired": "#999",
    "ok": "#2e7d32",
    "error": "#b00020",
}


def _esc(v: object) -> str:
    return html.escape(str(v if v is not None else ""))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · db-manage-mcp</title>
<style>
 body{{font-family:-apple-system,system-ui,'PingFang SC',sans-serif;margin:0;background:#f5f6f8;color:#222}}
 header{{background:#1e293b;color:#fff;padding:12px 20px;display:flex;gap:20px;align-items:center}}
 header a{{color:#cbd5e1;text-decoration:none;font-size:14px}} header a:hover{{color:#fff}}
 header .brand{{font-weight:600;color:#fff}}
 main{{max-width:1100px;margin:20px auto;padding:0 16px}}
 table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #eef0f3;font-size:14px;vertical-align:top}}
 th{{background:#fafbfc;font-weight:600;color:#555}}
 .badge{{display:inline-block;padding:2px 8px;border-radius:10px;color:#fff;font-size:12px;font-weight:600}}
 .card{{background:#fff;border-radius:8px;padding:16px 20px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:16px}}
 pre{{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:6px;overflow-x:auto;font-size:13px}}
 .btn{{display:inline-block;padding:8px 16px;border-radius:6px;border:none;color:#fff;font-size:14px;cursor:pointer;text-decoration:none}}
 .btn-approve{{background:#2e7d32}} .btn-reject{{background:#b00020}}
 input,textarea{{font-family:inherit;font-size:14px;padding:6px 8px;border:1px solid #cbd5e1;border-radius:5px}}
 label{{font-size:13px;color:#555;display:block;margin:8px 0 4px}}
 .muted{{color:#888;font-size:13px}} .row{{display:flex;gap:24px;flex-wrap:wrap}}
 .filters{{margin-bottom:12px}} .filters a{{margin-right:10px;font-size:13px}}
</style></head><body>
<header>
 <span class="brand">db-manage-mcp</span>
 <a href="/admin/approvals">审批中心</a>
 <a href="/admin/audit">操作审计</a>
 <a href="/admin/connections">连接管理</a>
 <a href="/admin/logout" style="margin-left:auto">退出</a>
</header>
<main>{body}</main></body></html>"""


def _login_page(error: str = "") -> str:
    err = f"<p style='color:#b00020'>{_esc(error)}</p>" if error else ""
    body = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>登录 · db-manage-mcp</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#f5f6f8;display:flex;
 justify-content:center;align-items:center;height:100vh;margin:0}}
 .box{{background:#fff;padding:32px;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.1);width:320px}}
 h1{{font-size:18px;margin:0 0 16px}} input{{width:100%;box-sizing:border-box;padding:10px;
 border:1px solid #cbd5e1;border-radius:6px;font-size:14px}} button{{width:100%;margin-top:12px;
 padding:10px;background:#1e293b;color:#fff;border:none;border-radius:6px;font-size:14px;cursor:pointer}}</style>
</head><body><form class="box" method="post" action="/admin/login">
 <h1>db-manage-mcp 管理后台</h1>{err}
 <input type="password" name="token" placeholder="管理 token" autofocus>
 <button type="submit">登录</button></form></body></html>"""
    return body


def _badge(text: str, color_map: dict) -> str:
    color = color_map.get(str(text).lower(), color_map.get(str(text).upper(), "#666"))
    return f'<span class="badge" style="background:{color}">{_esc(text)}</span>'


def _keyring_available() -> bool:
    try:
        import keyring  # noqa: PLC0415, F401
        return True
    except ImportError:
        return False


def _field(label: str, name: str, value: object = "", *, ph: str = "", typ: str = "text",
           width: str = "260px") -> str:
    return (f"<label>{_esc(label)}</label>"
            f"<input type='{typ}' name='{name}' value='{_esc(value)}' placeholder='{_esc(ph)}' "
            f"style='width:{width}'>")


def _connection_form(project: str, connection: str, cfg) -> str:  # noqa: ANN001
    """连接增删改表单。编辑时锁定 project/connection，密码留空表示不改。"""
    is_edit = cfg is not None
    ro = "readonly" if is_edit else ""
    engines_opts = "".join(
        f"<option value='{e}'{' selected' if cfg and cfg.engine == e else ''}>{e}</option>"
        for e in ("mysql", "postgres", "redis", "sqlite")
    )
    envs_opts = "".join(
        f"<option value='{e}'{' selected' if cfg and cfg.environment == e else ''}>{e}</option>"
        for e in ("local", "dev", "staging", "prod")
    )
    key_path = ""
    ssh_extra = ""
    if cfg and cfg.ssh_options:
        opts = list(cfg.ssh_options)
        if "-i" in opts:
            i = opts.index("-i")
            if i + 1 < len(opts):
                key_path = opts[i + 1]
                opts = opts[:i] + opts[i + 2:]
        ssh_extra = " ".join(opts)
    jump = ", ".join(cfg.jump_hosts) if cfg else ""
    masks = ", ".join(cfg.policy.mask_columns) if cfg else ""
    pw_ph = "留空表示不修改" if is_edit else "写入系统 keyring，配置只存引用"
    writer_user = cfg.writer.user if cfg and cfg.writer else ""
    return f"""<form method="post" action="/admin/connections/save">
 <div class="row">
  <div>{_field("项目", "project", project, ph="local")}</div>
  <div><label>连接名</label><input name="connection" value="{_esc(connection)}" {ro} style="width:260px"></div>
 </div>
 <div class="row">
  <div><label>引擎</label><br><select name="engine" style="padding:6px">{engines_opts}</select></div>
  <div><label>环境</label><br><select name="environment" style="padding:6px">{envs_opts}</select></div>
 </div>
 <div class="row">
  <div>{_field("host", "host", cfg.host if cfg else "", ph="127.0.0.1")}</div>
  <div>{_field("port", "port", cfg.port if cfg else "", ph="3306", typ="number", width="120px")}</div>
  <div>{_field("database", "database", cfg.database if cfg else "")}</div>
 </div>
 <div class="row">
  <div>{_field("user", "user", cfg.user if cfg else "")}</div>
  <div>{_field("password", "password", "", ph=pw_ph, typ="password")}</div>
 </div>
 <div class="row">
  <div>{_field("writer user（可选，写操作用）", "writer_user", writer_user)}</div>
  <div>{_field("writer password", "writer_password", "", ph=pw_ph, typ="password")}</div>
 </div>
 <hr style="border:none;border-top:1px solid #eee;margin:12px 0">
 <div class="row">
  <div>{_field("SSH 跳板（逗号分隔，按序）", "jump_hosts", jump, ph="bastion1, bastion2", width="340px")}</div>
 </div>
 <div class="row">
  <div>{_field("SSH key 文件路径", "ssh_key_path", key_path, ph="/Users/you/.ssh/prod_key", width="340px")}</div>
  <div>{_field("其它 ssh 选项（空格分隔）", "ssh_options_extra", ssh_extra, ph="-o ConnectTimeout=5", width="280px")}</div>
 </div>
 <div class="row">
  <div>{_field("max_rows", "max_rows", cfg.policy.max_rows if cfg else 500, typ="number", width="120px")}</div>
  <div>{_field("脱敏列（逗号分隔）", "mask_columns", masks, ph="email, phone")}</div>
 </div>
 <br><button class="btn btn-approve" type="submit">{'保存修改' if is_edit else '创建连接'}</button>
 {"<a href='/admin/connections' style='margin-left:12px'>取消编辑</a>" if is_edit else ""}
</form>"""


def mount_admin(mcp: "FastMCP", service: "DbmService", admin_token: str) -> None:
    expected_cookie = _session_value(admin_token)

    def guard(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        """未认证访问受保护路由 → 重定向登录页。"""
        @wraps(handler)
        async def _wrapped(req: Request) -> Response:
            if not _authed(req, expected_cookie):
                return RedirectResponse(url="/admin/login", status_code=303)
            return await handler(req)
        return _wrapped

    @mcp.custom_route("/admin/login", methods=["GET"])
    async def _login_form(req: Request) -> HTMLResponse:
        if _authed(req, expected_cookie):
            return RedirectResponse(url="/admin/approvals", status_code=303)
        return HTMLResponse(_login_page())

    @mcp.custom_route("/admin/login", methods=["POST"])
    async def _login_submit(req: Request) -> Response:
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

    @mcp.custom_route("/admin", methods=["GET"])
    @guard
    async def _index(_req: Request) -> RedirectResponse:
        return RedirectResponse(url="/admin/approvals")

    @mcp.custom_route("/admin/approvals", methods=["GET"])
    @guard
    async def _approvals(_req: Request) -> HTMLResponse:
        pending = service.list_changes("pending")
        recent = [c for c in service.list_changes() if c.effective_status() != "pending"][:30]

        def _rows(changes: list) -> str:
            if not changes:
                return '<tr><td colspan="6" class="muted">（无）</td></tr>'
            out = []
            for c in changes:
                st = c.effective_status()
                out.append(
                    f"<tr><td><a href='/admin/approvals/{c.id}'>#{c.id}</a></td>"
                    f"<td>{_esc(c.project)}/{_esc(c.connection)}<br><span class='muted'>{_esc(c.environment)}</span></td>"
                    f"<td>{_badge(c.risk_level, _LEVEL_COLOR)}</td>"
                    f"<td><code>{_esc(c.sql[:80])}</code></td>"
                    f"<td>{_badge(st, _STATUS_COLOR)}</td>"
                    f"<td class='muted'>{_esc(c.created_at)}</td></tr>"
                )
            return "".join(out)

        body = (
            f"<div class='card'><h2>待审批 ({len(pending)})</h2>"
            f"<table><tr><th>单号</th><th>连接</th><th>风险</th><th>SQL</th><th>状态</th><th>提交时间</th></tr>"
            f"{_rows(pending)}</table></div>"
            f"<div class='card'><h2>近期已决策</h2>"
            f"<table><tr><th>单号</th><th>连接</th><th>风险</th><th>SQL</th><th>状态</th><th>提交时间</th></tr>"
            f"{_rows(recent)}</table></div>"
        )
        return HTMLResponse(_page("审批中心", body))

    @mcp.custom_route("/admin/approvals/{change_id:int}", methods=["GET"])
    @guard
    async def _approval_detail(req: Request) -> HTMLResponse:
        change_id = req.path_params["change_id"]
        try:
            c = service.get_change(change_id)
        except ApprovalError as e:
            return HTMLResponse(_page("审批单", f"<div class='card'>{_esc(e)}</div>"), status_code=404)

        st = c.effective_status()
        risk = c.risk_report
        reasons = "".join(f"<li>{_esc(r)}</li>" for r in risk.get("reasons", []))
        warnings = "".join(f"<li>⚠️ {_esc(w)}</li>" for w in risk.get("warnings", []))
        impact = (
            f"影响表: {_esc(', '.join(risk.get('tables', [])) or '—')}<br>"
            f"表行数量级: {_esc(risk.get('row_estimate'))}<br>"
            f"预估影响行数: {_esc(risk.get('affected_estimate'))}<br>"
            f"含 WHERE: {_esc(risk.get('has_where'))} · 命中索引: {_esc(risk.get('uses_index'))}"
        )

        actions = ""
        if st == "pending":
            actions = f"""
<div class='card'><h3>审批决策</h3>
 <form method='post' action='/admin/approvals/{c.id}/approve' style='display:inline'>
  <label>审批人</label><input name='by' value='admin@localhost'>
  <label>备注（可选）</label><input name='note' style='width:320px'>
  <br><br><button class='btn btn-approve' type='submit'>批准</button>
 </form>
 <form method='post' action='/admin/approvals/{c.id}/reject' style='margin-top:16px'>
  <label>拒绝理由（会返回给 agent）</label>
  <textarea name='note' rows='2' style='width:100%'></textarea>
  <input type='hidden' name='by' value='admin@localhost'>
  <button class='btn btn-reject' type='submit'>拒绝</button>
 </form></div>"""
        elif c.decided_by:
            actions = (
                f"<div class='card'>决策: {_badge(st, _STATUS_COLOR)} by {_esc(c.decided_by)} "
                f"@ {_esc(c.decided_at)}<br>备注: {_esc(c.decision_note) or '—'}</div>"
            )

        body = f"""
<div class='card'>
 <h2>审批单 #{c.id} {_badge(st, _STATUS_COLOR)} {_badge(c.risk_level, _LEVEL_COLOR)}</h2>
 <div class='muted'>连接 {_esc(c.project)}/{_esc(c.connection)} · 环境 {_esc(c.environment)} · 引擎 {_esc(c.engine)}</div>
 <div class='muted'>提交 agent: {_esc(c.agent)} · 提交时间 {_esc(c.created_at)} · 有效期至 {_esc(c.expires_at)}</div>
 <p><b>变更原因:</b> {_esc(c.reason) or '—'}</p>
 <b>SQL</b><pre>{_esc(c.sql)}</pre>
</div>
<div class='card'><h3>风险报告</h3>
 <div class='row'><div><b>影响范围</b><br>{impact}</div></div>
 <b>判定依据</b><ul>{reasons}</ul>
 {'<b>告警</b><ul>' + warnings + '</ul>' if warnings else ''}
 {'<b>执行计划（EXPLAIN）</b><pre>' + _esc(risk.get('explain')) + '</pre>' if risk.get('explain') else ''}
</div>
{actions}
<p><a href='/admin/approvals'>← 返回列表</a></p>"""
        return HTMLResponse(_page(f"审批单 #{c.id}", body))

    @mcp.custom_route("/admin/approvals/{change_id:int}/approve", methods=["POST"])
    @guard
    async def _approve(req: Request) -> RedirectResponse:
        change_id = req.path_params["change_id"]
        form = await req.form()
        by = str(form.get("by") or "admin@localhost")
        note = str(form.get("note") or "")
        try:
            service.approve_change(change_id, decided_by=by, note=note)
        except ApprovalError:
            pass  # 已决策/过期，详情页会展示最新状态
        return RedirectResponse(url=f"/admin/approvals/{change_id}", status_code=303)

    @mcp.custom_route("/admin/approvals/{change_id:int}/reject", methods=["POST"])
    @guard
    async def _reject(req: Request) -> RedirectResponse:
        change_id = req.path_params["change_id"]
        form = await req.form()
        by = str(form.get("by") or "admin@localhost")
        note = str(form.get("note") or "")
        try:
            service.reject_change(change_id, decided_by=by, note=note)
        except ApprovalError:
            pass
        return RedirectResponse(url=f"/admin/approvals/{change_id}", status_code=303)

    @mcp.custom_route("/admin/audit", methods=["GET"])
    @guard
    async def _audit(req: Request) -> HTMLResponse:
        try:
            limit = min(max(int(req.query_params.get("limit", "200")), 1), 1000)
            offset = max(int(req.query_params.get("offset", "0")), 0)
        except ValueError:
            limit, offset = 200, 0
        total = service.store.count()
        rows = service.store.recent(limit, offset)
        f_status = req.query_params.get("status")
        f_conn = req.query_params.get("connection")
        if f_status:
            rows = [r for r in rows if r["status"] == f_status]
        if f_conn:
            rows = [r for r in rows if r["connection"] == f_conn]

        trs = []
        for r in rows:
            sql = _esc((r["sql"] or "")[:80])
            trs.append(
                f"<tr><td class='muted'>{_esc(r['ts'])}</td>"
                f"<td>{_esc(r['agent'])}</td>"
                f"<td>{_esc(r['project'])}/{_esc(r['connection'])}<br><span class='muted'>{_esc(r['environment'])}</span></td>"
                f"<td>{_esc(r['tool'])}</td>"
                f"<td><code>{sql}</code></td>"
                f"<td>{_badge(r['status'], _STATUS_COLOR)}</td>"
                f"<td class='muted'>{_esc(r['row_count'])} 行 / {_esc(r['duration_ms'])}ms<br>{_esc((r['detail'] or '')[:60])}</td></tr>"
            )
        table_rows = "".join(trs) or '<tr><td colspan="7" class="muted">（无记录）</td></tr>'
        status_q = f"&status={_esc(f_status)}" if f_status else ""
        filters = (
            "<div class='filters'>筛选状态: "
            "<a href='/admin/audit'>全部</a>"
            "<a href='/admin/audit?status=ok'>成功</a>"
            "<a href='/admin/audit?status=rejected'>被拒</a>"
            "<a href='/admin/audit?status=error'>出错</a></div>"
        )
        pager_parts = []
        if offset > 0:
            prev_off = max(offset - limit, 0)
            pager_parts.append(f"<a href='/admin/audit?limit={limit}&offset={prev_off}{status_q}'>← 较新</a>")
        if offset + limit < total:
            pager_parts.append(f"<a href='/admin/audit?limit={limit}&offset={offset + limit}{status_q}'>较旧 →</a>")
        pager = (
            f"<div class='filters'>共 {total} 条 · 第 {offset + 1}–{min(offset + limit, total)} 条 "
            + " · ".join(pager_parts) + "</div>"
        )
        body = (
            f"<div class='card'><h2>操作审计</h2>{filters}"
            f"<table><tr><th>时间</th><th>agent</th><th>连接</th><th>工具</th><th>SQL</th><th>状态</th><th>结果</th></tr>"
            f"{table_rows}</table>{pager}</div>"
        )
        return HTMLResponse(_page("操作审计", body))

    def _caller(req: Request) -> "CallerInfo":
        from .service import CallerInfo
        return CallerInfo(agent="admin-ui", session_id=req.cookies.get(_COOKIE_NAME, "")[:12])

    @mcp.custom_route("/admin/connections", methods=["GET"])
    @guard
    async def _connections(req: Request) -> HTMLResponse:
        editing = req.query_params.get("edit")  # "project/connection"
        edit_cfg = None
        e_project = e_conn = ""
        if editing and "/" in editing:
            e_project, e_conn = editing.split("/", 1)
            proj = service.config.projects.get(e_project)
            edit_cfg = proj.connections.get(e_conn) if proj else None

        rows = []
        for pname, proj in sorted(service.config.projects.items()):
            for cname, c in sorted(proj.connections.items()):
                jump = " → ".join(c.jump_hosts) if c.jump_hosts else "—"
                rows.append(
                    f"<tr><td>{_esc(pname)}/{_esc(cname)}</td><td>{_esc(c.engine)}</td>"
                    f"<td>{_esc(c.environment)}</td><td>{_esc(c.host)}:{_esc(c.port)}</td>"
                    f"<td>{_esc(c.database)}</td><td class='muted'>{_esc(jump)}</td>"
                    f"<td><a href='/admin/connections?edit={_esc(pname)}/{_esc(cname)}'>编辑</a> · "
                    f"<form method='post' action='/admin/connections/delete' style='display:inline' "
                    f"onsubmit='return confirm(\"删除连接 {_esc(pname)}/{_esc(cname)}？\")'>"
                    f"<input type='hidden' name='project' value='{_esc(pname)}'>"
                    f"<input type='hidden' name='connection' value='{_esc(cname)}'>"
                    f"<button class='btn btn-reject' style='padding:2px 10px'>删除</button></form></td></tr>"
                )
        table = "".join(rows) or '<tr><td colspan="7" class="muted">（无连接）</td></tr>'

        keyring_note = "" if _keyring_available() else (
            "<p style='color:#b00020'>⚠️ 未安装 keyring，无法安全存储密码。"
            "请 <code>pip install 'db-manage-mcp[keyring]'</code> 后重启。</p>"
        )
        form = _connection_form(e_project, e_conn, edit_cfg)
        body = (
            f"<div class='card'><h2>连接列表</h2>"
            f"<table><tr><th>连接</th><th>引擎</th><th>环境</th><th>地址</th><th>库</th>"
            f"<th>跳板</th><th>操作</th></tr>{table}</table></div>"
            f"<div class='card'><h2>{'编辑' if edit_cfg else '新增'}连接</h2>{keyring_note}{form}</div>"
        )
        return HTMLResponse(_page("连接管理", body))

    @mcp.custom_route("/admin/connections/save", methods=["POST"])
    @guard
    async def _connection_save(req: Request) -> Response:
        from .connections import ConnectionAdminError
        from .service import QueryRejected
        f = await req.form()

        def _list(v: str, sep: str) -> list[str]:
            return [x.strip() for x in str(f.get(v) or "").split(sep) if x.strip()]

        try:
            port_raw = str(f.get("port") or "").strip()
            service.upsert_connection(
                str(f.get("project") or "").strip(),
                str(f.get("connection") or "").strip(),
                _caller(req),
                engine=str(f.get("engine") or "").strip(),
                environment=str(f.get("environment") or "dev").strip(),
                host=str(f.get("host") or "").strip() or None,
                port=int(port_raw) if port_raw else None,
                database=str(f.get("database") or "").strip() or None,
                user=str(f.get("user") or "").strip() or None,
                password=str(f.get("password") or "") or None,
                writer_user=str(f.get("writer_user") or "").strip() or None,
                writer_password=str(f.get("writer_password") or "") or None,
                jump_hosts=_list("jump_hosts", ","),
                ssh_key_path=str(f.get("ssh_key_path") or "").strip() or None,
                ssh_options_extra=_list("ssh_options_extra", " "),
                max_rows=int(str(f.get("max_rows") or "500")),
                mask_columns=_list("mask_columns", ","),
            )
        except (ConnectionAdminError, QueryRejected, ValueError) as e:
            body = f"<div class='card'><h2>保存失败</h2><p style='color:#b00020'>{_esc(e)}</p>" \
                   f"<a href='/admin/connections'>← 返回</a></div>"
            return HTMLResponse(_page("保存失败", body), status_code=400)
        return RedirectResponse(url="/admin/connections", status_code=303)

    @mcp.custom_route("/admin/connections/delete", methods=["POST"])
    @guard
    async def _connection_delete(req: Request) -> Response:
        from .connections import ConnectionAdminError
        from .service import QueryRejected
        f = await req.form()
        try:
            service.delete_connection(str(f.get("project")), str(f.get("connection")), _caller(req))
        except (ConnectionAdminError, QueryRejected) as e:
            return HTMLResponse(_page("删除失败", f"<div class='card'>{_esc(e)}</div>"), status_code=400)
        return RedirectResponse(url="/admin/connections", status_code=303)
