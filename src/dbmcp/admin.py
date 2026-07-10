"""管理后台：审计查询 + 审批中心。

挂在 MCP 应用同一 ASGI 服务下（custom_route），默认只随 daemon 监听 127.0.0.1。
服务端渲染，无外部依赖/资源，避免引入前端框架与 CSP 问题。
后续可加登录；当前审批人身份由表单字段 `by` 提供（默认 admin@localhost）。
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .approvals import ApprovalError

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .service import DbmService

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
</header>
<main>{body}</main></body></html>"""


def _badge(text: str, color_map: dict) -> str:
    color = color_map.get(str(text).lower(), color_map.get(str(text).upper(), "#666"))
    return f'<span class="badge" style="background:{color}">{_esc(text)}</span>'


def mount_admin(mcp: "FastMCP", service: "DbmService") -> None:
    @mcp.custom_route("/admin", methods=["GET"])
    async def _index(_req: Request) -> RedirectResponse:
        return RedirectResponse(url="/admin/approvals")

    @mcp.custom_route("/admin/approvals", methods=["GET"])
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
</div>
{actions}
<p><a href='/admin/approvals'>← 返回列表</a></p>"""
        return HTMLResponse(_page(f"审批单 #{c.id}", body))

    @mcp.custom_route("/admin/approvals/{change_id:int}/approve", methods=["POST"])
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
    async def _audit(req: Request) -> HTMLResponse:
        rows = service.store.recent(200)
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
        filters = (
            "<div class='filters'>筛选状态: "
            "<a href='/admin/audit'>全部</a>"
            "<a href='/admin/audit?status=ok'>成功</a>"
            "<a href='/admin/audit?status=rejected'>被拒</a>"
            "<a href='/admin/audit?status=error'>出错</a></div>"
        )
        body = (
            f"<div class='card'><h2>操作审计（最近 200 条）</h2>{filters}"
            f"<table><tr><th>时间</th><th>agent</th><th>连接</th><th>工具</th><th>SQL</th><th>状态</th><th>结果</th></tr>"
            f"{table_rows}</table></div>"
        )
        return HTMLResponse(_page("操作审计", body))
