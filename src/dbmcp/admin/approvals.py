"""审批中心：列表、详情、批准 / 拒绝。"""

from __future__ import annotations

from functools import partial

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..approvals import ApprovalError
from .common import (
    _FAVICON_LINK,
    _STATUS_COLOR,
    _badge,
    _env_badge,
    _esc,
    _fmt_ts,
    _local_request_ok,
    _page,
    _pagehead,
)
from .context import AdminContext

# 一次性审批页上 SQL / 计划的展示上限：这一页是在手机上看的，超出的去后台审批页看全文
_ACT_SQL_MAX_LINES = 30
_ACT_SQL_MAX_CHARS = 2000


def _act_page(title: str, inner: str, theme: str) -> str:
    """通知链接落地页：没有侧栏（点进来的人没登录，导航到哪都是登录页），
    只有一张卡片；样式复用后台的 chrome/doc 两份 CSS 与主题。"""
    theme_cls = "theme-light" if theme == "light" else "theme-dark"
    return f"""<!doctype html>
<html lang="zh" class="{theme_cls}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · Quay</title>{_FAVICON_LINK}
<link rel="stylesheet" href="/admin/static/admin-chrome.css">
<link rel="stylesheet" href="/admin/static/admin-doc.css">
<style>main{{max-width:720px;margin:0 auto;padding:32px 20px}} .act-btns{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}}
.act-btns form{{display:inline}} .act-note{{width:100%;margin-top:10px}}</style>
</head><body><main>{inner}</main></body></html>"""


def _clip_sql(sql: str) -> tuple[str, bool]:
    lines = sql.splitlines()
    clipped = False
    if len(lines) > _ACT_SQL_MAX_LINES:
        lines, clipped = lines[:_ACT_SQL_MAX_LINES], True
    text = "\n".join(lines)
    if len(text) > _ACT_SQL_MAX_CHARS:
        text, clipped = text[:_ACT_SQL_MAX_CHARS], True
    return text, clipped

_LEVEL_COLOR = {
    "CRITICAL": "#b00020",
    "HIGH": "#e65100",
    "MEDIUM": "#f9a825",
    "LOW": "#2e7d32",
}


def _db_suffix(c) -> str:  # noqa: ANN001
    """审批列表里连接名后缀：PG 跨库的审批单标出执行库，免得和默认库上的同名表混淆。"""
    return f" <code>@{_esc(c.database)}</code>" if getattr(c, "database", "") else ""


def _db_row(c) -> str:  # noqa: ANN001
    if not getattr(c, "database", ""):
        return ""
    return f"\n  <dt>执行库</dt><dd><code>{_esc(c.database)}</code>（PostgreSQL database）</dd>"


def _bool_pill(value: object) -> str:
    """True→是(绿) / False→否(灰) / None→未知(黄)。"""
    if value is True:
        return '<span class="pill pill-yes">是</span>'
    if value is False:
        return '<span class="pill pill-no">否</span>'
    return '<span class="pill pill-na">未知</span>'


def _num(value: object, unknown: str = "未知") -> str:
    if value is None:
        return f'<span class="muted">{unknown}</span>'
    if isinstance(value, int):
        return f"约 {value:,}"
    return _esc(value)


def _impact_html(risk: dict) -> str:
    tables = risk.get("tables") or []
    tags = "".join(f'<span class="tag">{_esc(t)}</span>' for t in tables) or '<span class="muted">—</span>'
    return (
        '<dl class="kv">'
        f"<dt>影响表</dt><dd>{tags}</dd>"
        f"<dt>表行数量级</dt><dd>{_num(risk.get('row_estimate'))}</dd>"
        f"<dt>预估影响行数</dt><dd>{_num(risk.get('affected_estimate'), unknown='未知（取决于运行时数据）')}</dd>"
        f"<dt>含 WHERE 条件</dt><dd>{_bool_pill(risk.get('has_where'))}</dd>"
        f"<dt>命中索引</dt><dd>{_bool_pill(risk.get('uses_index'))}</dd>"
        "</dl>"
    )


# MySQL 对单行主键更新等语句的无信息量输出，转成人话
_PLAN_NO_INFO = "not executable by iterator executor"
_PLAN_NO_INFO_HTML = ('<div class="sec-title">执行计划</div>'
                      '<div class="muted">该语句为点查/单行定位更新，优化器无需生成可展示的查询计划。</div>')


def _explain_html(risk: dict) -> str:
    plan = risk.get("explain")
    if not plan:
        return ""
    # 老审批单存的是 " | " 拼接的纯文本计划（没有列名），原样展示
    if isinstance(plan, str):
        if _PLAN_NO_INFO in plan:
            return _PLAN_NO_INFO_HTML
        return f'<div class="sec-title">执行计划（EXPLAIN）</div><pre>{_esc(plan)}</pre>'
    columns, rows = plan.get("columns") or [], plan.get("rows") or []
    if not rows:
        return ""
    if any(_PLAN_NO_INFO in str(v) for row in rows for v in row):
        return _PLAN_NO_INFO_HTML
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(
            '<td><span class="muted">NULL</span></td>' if v is None else f"<td>{_esc(v)}</td>"
            for v in row
        ) + "</tr>"
        for row in rows
    )
    return ('<div class="sec-title">执行计划（EXPLAIN）</div>'
            f'<div class="plan-wrap"><table class="plan-tbl">'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _shell = ctx._shell
    _theme = ctx._theme

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
                    f"<td>{_esc(c.project)}/{_esc(c.connection)}{_db_suffix(c)}<br><span class='muted'>{_esc(c.environment)}</span></td>"
                    f"<td>{_badge(c.risk_level, _LEVEL_COLOR)}</td>"
                    f"<td><code>{_esc(c.sql[:80])}</code></td>"
                    f"<td>{_badge(st, _STATUS_COLOR)}</td>"
                    f"<td class='muted mono'>{_esc(_fmt_ts(c.created_at))}</td></tr>"
                )
            return "".join(out)

        body = (
            _pagehead("Approvals", "审批中心", "数据变更操作在此人工授权；批准后 agent 带 change_id 重提执行")
            + f"<div class='card'><h2>待审批 <span class='muted'>({len(pending)})</span></h2>"
            f"<div class='tablewrap'><table><tr><th>单号</th><th>连接</th><th>风险</th><th>SQL</th><th>状态</th><th>提交时间</th></tr>"
            f"{_rows(pending)}</table></div></div>"
            f"<div class='card'><h2>近期已决策</h2>"
            f"<div class='tablewrap'><table><tr><th>单号</th><th>连接</th><th>风险</th><th>SQL</th><th>状态</th><th>提交时间</th></tr>"
            f"{_rows(recent)}</table></div></div>"
        )
        return _shell("审批中心", body)

    @mcp.custom_route("/admin/approvals/{change_id:int}", methods=["GET"])
    @guard
    async def _approval_detail(req: Request) -> HTMLResponse:
        change_id = req.path_params["change_id"]
        try:
            c = service.get_change(change_id)
        except ApprovalError as e:
            return HTMLResponse(_page("审批单", f"<div class='card'>{_esc(e)}</div>", theme=_theme()),
                                status_code=404)

        st = c.effective_status()
        risk = c.risk_report
        reasons = "".join(f"<li>{_esc(r)}</li>" for r in risk.get("reasons", []))
        warnings = "".join(f"<li>⚠️ {_esc(w)}</li>" for w in risk.get("warnings", []))

        # agent 提交时写下的「改动前是什么值 / 怎么回滚」——审批人判断可回滚性的关键信息
        rollback_row = (
            f"<dt>回滚参考</dt><dd><pre>{_esc(c.rollback_note)}</pre></dd>"
            if c.rollback_note else ""
        )

        actions = ""
        if st == "pending":
            actions = f"""
<div class='card'><h3>审批决策</h3>
 <form method='post' action='/admin/approvals/{c.id}/approve' style='display:inline'>
  <label>审批人</label><input name='by' value='admin@localhost'>
  <label>备注（可选）</label><input name='note' style='width:320px'>
  <!-- 「仅批准」放在前面：表单里输入框按回车会触发第一个提交按钮，
       默认动作必须是不写库的那个，执行必须是显式点击。 -->
  <br><br><button class='btn' type='submit'>仅批准（由 agent 重提执行）</button>
  <button class='btn btn-approve' type='submit' name='exec' value='1'
          style='margin-left:8px'>批准并立即执行</button>
  <div class="muted" style="margin-top:8px">「批准并立即执行」当场用 writer 账号执行审批单里的{"计划（重新从源库取数）" if c.kind == "sync" else "SQL"}；
   等待中的 agent 会收到执行结果，不必再重提。</div>
 </form>
 <form method='post' action='/admin/approvals/{c.id}/reject' style='margin-top:16px'>
  <label>拒绝理由（会返回给 agent）</label>
  <textarea name='note' rows='2' style='width:100%'></textarea>
  <input type='hidden' name='by' value='admin@localhost'>
  <button class='btn btn-reject' type='submit'>拒绝</button>
 </form></div>"""
        elif c.decided_by:
            ex = c.exec_result or {}
            exec_line = (
                f"<br>执行: 影响 {ex.get('affected_rows', '?')} 行 · "
                f"{ex.get('duration_ms', '?')} ms · 由 {_esc(str(ex.get('executed_by') or '—'))} 触发"
            ) if ex else ""
            actions = (
                f"<div class='card'>决策: {_badge(st, _STATUS_COLOR)} by {_esc(c.decided_by)} "
                f"@ {_esc(_fmt_ts(c.decided_at))}<br>备注: {_esc(c.decision_note) or '—'}"
                f"{exec_line}</div>"
            )

        body = f"""
{_pagehead("Change #" + str(c.id), f"审批单 #{c.id}")}
<div class='card'>
 <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">{_badge(st, _STATUS_COLOR)} {_badge(c.risk_level, _LEVEL_COLOR)} <span class="tag">{_esc(c.engine)}</span></div>
 <dl class="kv">
  <dt>连接</dt><dd><code>{_esc(c.project)}/{_esc(c.connection)}</code> · {_env_badge(c.environment)}</dd>{_db_row(c)}
  <dt>提交 agent</dt><dd>{_esc(c.agent)}</dd>
  <dt>提交时间</dt><dd>{_esc(_fmt_ts(c.created_at))} · 有效期至 {_esc(_fmt_ts(c.expires_at))}</dd>
  <dt>变更原因</dt><dd>{_esc(c.reason) or '—'}</dd>
  {rollback_row}
 </dl>
 <div class="sec-title">{"同步计划" if c.kind == "sync" else "SQL"}</div><pre>{_esc(c.sql)}</pre>
</div>
<div class='card'><h3>风险报告 {_badge(c.risk_level, _LEVEL_COLOR)}</h3>
 <div class="sec-title">影响范围</div>
 {_impact_html(risk)}
 <div class="sec-title">判定依据</div><ul>{reasons}</ul>
 {'<div class="sec-title">告警</div><ul>' + warnings + '</ul>' if warnings else ''}
 {_explain_html(risk)}
</div>
{actions}
<p style="margin-top:16px"><a href='/admin/approvals'>← 返回审批列表</a></p>"""
        return _shell(f"审批单 #{c.id}", body)

    @mcp.custom_route("/admin/approvals/{change_id:int}/approve", methods=["POST"])
    @guard
    async def _approve(req: Request) -> RedirectResponse:
        change_id = req.path_params["change_id"]
        form = await req.form()
        by = str(form.get("by") or "admin@localhost")
        note = str(form.get("note") or "")
        try:
            if form.get("exec"):
                # 批准并立即执行：走与 agent 重提相同的核销路径（执行审批单里存的 SQL），
                # 只是触发方是后台操作者。等待中的 agent 从 exec_result 收到结果。
                await anyio.to_thread.run_sync(
                    partial(service.approve_and_execute_change, change_id, decided_by=by, note=note)
                )
            else:
                service.approve_change(change_id, decided_by=by, note=note)
        except ApprovalError:
            pass  # 已决策/过期，详情页会展示最新状态
        except Exception as e:  # noqa: BLE001 - 执行失败要让审批人看到原因，不能静默重定向
            return HTMLResponse(
                _page("执行失败", f"<div class='card'><h3>审批单 #{change_id} 执行失败</h3>"
                      f"<pre>{_esc(f'{type(e).__name__}: {e}')}</pre>"
                      f"<p>审批单已被核销，如需重试请让 agent 重新提交。</p>"
                      f"<p><a href='/admin/approvals/{change_id}'>← 返回审批单</a></p></div>",
                      theme=_theme()),
                status_code=500,
            )
        return RedirectResponse(url=f"/admin/approvals/{change_id}", status_code=303)

    # ---- 通知里的一次性审批链接：不走 cookie 认证（收通知的手机上没登录），
    #      靠审批单上的一次性令牌鉴权；Host/Origin 校验照旧，GET 只展示、POST 才决策 ----

    @mcp.custom_route("/admin/approvals/{change_id:int}/act", methods=["GET"])
    async def _act_form(req: Request) -> Response:
        if not _local_request_ok(req):
            return Response("forbidden: request must originate from an allowed host", status_code=403)
        change_id = req.path_params["change_id"]
        token = str(req.query_params.get("t") or "")
        theme = _theme()
        if not service.approvals.action_token_matches(change_id, token):
            return HTMLResponse(_act_page("链接无效", "<div class='card'><h2>链接无效</h2>"
                                          "<p class='muted'>这个审批链接不存在、已经用过，或审批单已被处理。"
                                          "请登录后台查看审批中心。</p></div>", theme), status_code=403)
        c = service.approvals.get(change_id)
        st = c.effective_status()
        if st != "pending":
            return HTMLResponse(_act_page("审批单已处理", f"<div class='card'><h2>审批单 #{c.id} 已处理</h2>"
                                          f"<p>当前状态：{_badge(st, _STATUS_COLOR)}</p></div>", theme))
        sql, clipped = _clip_sql(c.sql)
        risk = c.risk_report or {}
        reasons = "".join(f"<li>{_esc(r)}</li>" for r in risk.get("reasons", [])[:6])
        rollback = (f"<div class='sec-title'>回滚参考</div><pre>{_esc(c.rollback_note)}</pre>"
                    if c.rollback_note else "")
        inner = f"""<div class='card'>
 <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">{_badge(c.risk_level, _LEVEL_COLOR)} <span class="tag">{_esc(c.engine)}</span></div>
 <h2>审批单 #{c.id}</h2>
 <dl class="kv">
  <dt>连接</dt><dd><code>{_esc(c.project)}/{_esc(c.connection)}</code> · {_env_badge(c.environment)}</dd>{_db_row(c)}
  <dt>提交 agent</dt><dd>{_esc(c.agent)}</dd>
  <dt>有效期至</dt><dd>{_esc(_fmt_ts(c.expires_at))}</dd>
  <dt>变更原因</dt><dd>{_esc(c.reason) or '—'}</dd>
 </dl>
 <div class="sec-title">{"同步计划" if c.kind == "sync" else "SQL"}</div><pre>{_esc(sql)}</pre>
 {"<p class='muted'>已截断，完整内容登录后台在审批页查看。</p>" if clipped else ""}
 {"<div class='sec-title'>判定依据</div><ul>" + reasons + "</ul>" if reasons else ""}
 {rollback}
 <div class="act-btns">
  <form method='post' action='/admin/approvals/{c.id}/act'>
   <input type='hidden' name='t' value='{_esc(token)}'><input type='hidden' name='decision' value='approve'>
   <button class='btn btn-approve' type='submit'>批准（由 agent 执行）</button>
  </form>
  <form method='post' action='/admin/approvals/{c.id}/act'>
   <input type='hidden' name='t' value='{_esc(token)}'><input type='hidden' name='decision' value='reject'>
   <input class='act-note' name='note' placeholder='拒绝理由（可选，会返回给 agent）'>
   <button class='btn btn-reject' type='submit' style='margin-top:8px'>拒绝</button>
  </form>
 </div>
 <p class="muted" style="margin-top:14px">这个链接只对这一张审批单有效，点过一次即作废。</p>
</div>"""
        return HTMLResponse(_act_page(f"审批单 #{c.id}", inner, theme))

    @mcp.custom_route("/admin/approvals/{change_id:int}/act", methods=["POST"])
    async def _act_decide(req: Request) -> Response:
        if not _local_request_ok(req):
            return Response("forbidden: request must originate from an allowed host", status_code=403)
        change_id = req.path_params["change_id"]
        form = await req.form()
        token = str(form.get("t") or "")
        decision = str(form.get("decision") or "")
        note = str(form.get("note") or "")
        theme = _theme()
        # 先作废令牌再决策：并发重放同一链接只有一次能走到这里
        if decision not in ("approve", "reject") or not service.approvals.redeem_action_token(change_id, token):
            return HTMLResponse(_act_page("链接无效", "<div class='card'><h2>链接无效</h2>"
                                          "<p class='muted'>这个审批链接已经用过、已过期，或审批单已被处理。</p></div>",
                                          theme), status_code=403)
        by = "notify-link"
        try:
            if decision == "approve":
                c = service.approve_change(change_id, decided_by=by, note=note or "经通知里的一次性链接批准")
                msg = (f"<h2>已批准审批单 #{c.id}</h2><p>等待中的 agent 会自动执行审批单里存的"
                       f"{'计划' if c.kind == 'sync' else 'SQL'}；若它已超时退出，会在下次续等时拿到结果。</p>")
            else:
                c = service.reject_change(change_id, decided_by=by, note=note or "经通知里的一次性链接拒绝")
                msg = f"<h2>已拒绝审批单 #{c.id}</h2><p>拒绝理由会返回给 agent。</p>"
        except ApprovalError as e:
            return HTMLResponse(_act_page("无法决策", f"<div class='card'><h2>无法决策</h2><p>{_esc(e)}</p></div>",
                                          theme), status_code=409)
        return HTMLResponse(_act_page(f"审批单 #{change_id}", f"<div class='card'>{msg}</div>", theme))

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
