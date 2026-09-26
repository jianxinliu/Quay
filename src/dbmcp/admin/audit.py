"""操作审计页。"""

from __future__ import annotations


from starlette.requests import Request
from starlette.responses import HTMLResponse

from .common import _STATUS_COLOR, _badge, _env_badge, _esc, _fmt_ts, _format_sql, _pagehead
from .context import AdminContext


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _shell = ctx._shell

    @mcp.custom_route("/admin/audit", methods=["GET"])
    @guard
    async def _audit(req: Request) -> HTMLResponse:
        qp = req.query_params
        try:
            limit = min(max(int(qp.get("limit", "200")), 1), 1000)
            offset = max(int(qp.get("offset", "0")), 0)
        except ValueError:
            limit, offset = 200, 0
        # 服务端筛选（下推到 SQL）；session_id 按会话回溯、rw 按读/写（是否需审批）
        filters = {k: qp.get(k) for k in
                   ("project", "connection", "agent", "status", "session_id", "rw")
                   if qp.get(k)}
        # 默认是否隐藏查询台自身操作（agent=admin-ui）由系统设置 audit_hide_admin_ui 决定；
        # show_admin=1 或明确筛选它时始终显示
        _st = service.get_settings()
        _hide_default = bool(_st.get("audit_hide_admin_ui", True))
        show_admin = (qp.get("show_admin") == "1" or filters.get("agent") == "admin-ui"
                      or not _hide_default)
        query_filters = dict(filters)
        if not show_admin:
            query_filters["agent__ne"] = "admin-ui"
        total = service.store.count(query_filters)
        rows = service.store.recent(limit, offset, query_filters)

        # 会话列表（供会话筛选下拉与逐行会话名回显）：按当前 agent 过滤范围取
        sessions = service.store.list_sessions(limit=200, agent=filters.get("agent"))
        sess_map = {s["session_id"]: s for s in sessions}

        trs = []
        for i, r in enumerate(rows):
            # 结果列：有行数/耗时才显示；detail 截断 + 完整值放 title 悬浮
            stat = []
            if r["row_count"] is not None:
                stat.append(f"{r['row_count']} 行")
            if r["duration_ms"] is not None:
                stat.append(f"{r['duration_ms']}ms")
            statline = f"<span class='mono'>{' · '.join(stat)}</span>" if stat else ""
            # 结果详情：与 SQL 列一致，点击在下方展开完整内容（错误信息常很长）
            detail = r["detail"] or ""
            if detail:
                dtrunc = _esc(detail[:70]) + ("…" if len(detail) > 70 else "")
                dline = (f"<div class='cell-detail detail-toggle' data-i='{i}' "
                         f"title='点击展开完整结果'>{dtrunc}</div>")
                detail_expand = (f"<tr class='detail-full' id='detailfull-{i}' style='display:none'>"
                                 f"<td colspan='9'><pre>{_esc(detail)}</pre></td></tr>")
            else:
                dline = ""
                detail_expand = ""
            # SQL：点击展开该行下方的格式化完整 SQL
            raw_sql = r["sql"] or ""
            if raw_sql:
                truncated = _esc(raw_sql[:88]) + ("…" if len(raw_sql) > 88 else "")
                sqlcell = (f"<code class='cell-sql sql-toggle' data-i='{i}' title='点击展开完整 SQL'>"
                           f"{truncated}</code>")
                expand_row = (f"<tr class='sql-full' id='sqlfull-{i}' style='display:none'>"
                              f"<td colspan='9'><pre>{_esc(_format_sql(raw_sql, r['engine'] or ''))}</pre></td></tr>")
            else:
                sqlcell = "<span class='muted'>—</span>"
                expand_row = ""
            # 会话列：有登记名字则显示名字，否则显示短会话 id；点击下钻到该会话全部 SQL
            sid = r["session_id"] or ""
            if sid:
                sinfo = sess_map.get(sid)
                stitle = (sinfo or {}).get("title") if sinfo else None
                slabel = _esc(stitle) if stitle else f"<span class='mono muted'>{_esc(sid[:8])}</span>"
                sesscell = (f"<a href='/admin/audit?session_id={_esc(sid)}' "
                            f"title='{_esc(stitle or sid)}'>{slabel}</a>")
            else:
                sesscell = "<span class='muted'>—</span>"
            trs.append(
                f"<tr><td style='white-space:nowrap'><code>{_esc(r['project'])}/{_esc(r['connection'])}</code></td>"
                f"<td>{_env_badge(r['environment']) if r['environment'] else '<span class=muted>—</span>'}</td>"
                f"<td class='mono'>{_esc(r['agent'])}</td>"
                f"<td style='white-space:nowrap'>{sesscell}</td>"
                f"<td class='mono muted' style='white-space:nowrap'>{_esc(r['tool'])}</td>"
                f"<td>{sqlcell}</td>"
                f"<td>{_badge(r['status'], _STATUS_COLOR)}</td>"
                f"<td class='muted mono' style='white-space:nowrap'>{_esc(_fmt_ts(r['ts']))}</td>"
                f"<td class='muted'>{statline}{dline}</td></tr>"
                f"{expand_row}{detail_expand}"
            )
        table_rows = "".join(trs) or '<tr><td colspan="9" class="muted">（无匹配记录）</td></tr>'

        # 筛选下拉
        def _sel(name: str, label: str, values: list[str]) -> str:
            cur = filters.get(name, "")
            opts = "<option value=''>全部" + _esc(label) + "</option>" + "".join(
                f"<option value='{_esc(v)}'{' selected' if v == cur else ''}>{_esc(v)}</option>"
                for v in values)
            return f"<select name='{name}' onchange='this.form.submit()'>{opts}</select>"

        status_opts = ["ok", "rejected", "error"]

        # 会话下拉：value=session_id，显示登记的名字（无则短 id）+ agent + 操作数。
        # 当前筛选的会话若不在最近 200 个里（极少），补进选项，避免提交时丢失。
        cur_sess = filters.get("session_id", "")
        sess_values = list(sessions)
        if cur_sess and cur_sess not in sess_map:
            sess_values.insert(0, {"session_id": cur_sess, "title": None, "agent": "", "ops": "?"})

        def _sess_label(s: dict) -> str:
            name = s.get("title") or (s["session_id"][:8] + "…")
            return f"{name} · {s.get('agent') or '?'} · {s['ops']}条"

        sess_opts = "<option value=''>全部会话</option>" + "".join(
            f"<option value='{_esc(s['session_id'])}'"
            f"{' selected' if s['session_id'] == cur_sess else ''}>{_esc(_sess_label(s))}</option>"
            for s in sess_values)
        session_sel = f"<select name='session_id' onchange='this.form.submit()'>{sess_opts}</select>"

        # 读/写（是否需审批）下拉
        rw_cur = filters.get("rw", "")
        rw_opts = "".join(
            f"<option value='{v}'{' selected' if v == rw_cur else ''}>{_esc(lb)}</option>"
            for v, lb in [("", "全部读写"), ("write", "需审批（写）"), ("read", "不需审批（读）")])
        rw_sel = f"<select name='rw' onchange='this.form.submit()'>{rw_opts}</select>"

        filter_bar = (
            "<form method='get' class='filters' style='gap:8px'>"
            + _sel("project", "项目", service.store.distinct_values("project"))
            + _sel("connection", "连接", service.store.distinct_values("connection"))
            + _sel("agent", "agent", service.store.distinct_values("agent"))
            + session_sel
            + rw_sel
            + _sel("status", "状态", status_opts)
            + f"<input type='hidden' name='limit' value='{limit}'>"
            + ("<input type='hidden' name='show_admin' value='1'>" if show_admin else "")
            + "<a href='/admin/audit' style='margin-left:4px'>清除</a>"
            + (f"<a href='{_esc('/admin/audit?' + '&'.join([f'{k}={v}' for k, v in filters.items()]))}'"
               f" style='margin-left:4px'>隐藏 admin-ui</a>" if show_admin else
               f"<a href='{_esc('/admin/audit?show_admin=1&' + '&'.join([f'{k}={v}' for k, v in filters.items()]))}'"
               f" style='margin-left:4px'>显示 admin-ui</a>")
            + "<label style='margin:0 0 0 auto;display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted)'>"
            "<input type='checkbox' id='auto-refresh' style='width:auto'>自动刷新（5s）</label>"
            "</form>"
        )

        # 分页：按页码，始终显示，保留筛选参数
        def _url(off: int) -> str:
            parts = [f"limit={limit}", f"offset={off}"] + [f"{k}={_esc(v)}" for k, v in filters.items()]
            if show_admin:
                parts.append("show_admin=1")
            return "/admin/audit?" + "&".join(parts)
        pages = max((total + limit - 1) // limit, 1)
        cur_page = offset // limit + 1
        prev_btn = (f"<a class='btn btn-ghost pg' href='{_url(max(offset - limit, 0))}'>← 上一页</a>"
                    if offset > 0 else "<span class='btn btn-ghost pg disabled'>← 上一页</span>")
        next_btn = (f"<a class='btn btn-ghost pg' href='{_url(offset + limit)}'>下一页 →</a>"
                    if offset + limit < total else "<span class='btn btn-ghost pg disabled'>下一页 →</span>")
        pager = (f"<div class='pager'>{prev_btn}"
                 f"<span class='pager-info'>第 <b>{cur_page}</b> / {pages} 页 · 共 {total} 条</span>"
                 f"{next_btn}</div>")

        # 会话回溯提示条：正按某会话筛选时，展示其名字/简介与读写统计
        session_banner = ""
        if cur_sess:
            s = sess_map.get(cur_sess)
            if s:
                note = f" · {_esc(s['note'])}" if s.get("note") else ""
                session_banner = (
                    "<div class='muted' style='margin:8px 0 0;font-size:13px'>"
                    f"正在回溯会话 <b>{_esc(s.get('title') or cur_sess[:8])}</b>"
                    f"（agent {_esc(s.get('agent') or '?')} · 共 {s['ops']} 次操作，"
                    f"其中需审批的写 {s['writes']} 次）{note} "
                    "<a href='/admin/audit'>清除会话筛选</a></div>")
            else:
                session_banner = (
                    "<div class='muted' style='margin:8px 0 0;font-size:13px'>"
                    f"正在回溯会话 <b>{_esc(cur_sess[:12])}</b>（未登记名字）"
                    " <a href='/admin/audit'>清除会话筛选</a></div>")

        body = (
            # 审计表信息密度高：本页放开 main 宽度限制，表格占满可用宽度、列宽自适应
            "<style>main{max-width:none}</style>"
            + _pagehead("Audit Log", "操作审计", "每次数据库操作的完整留痕：谁、何时、在哪个库、跑了什么、结果如何")
            + f"<div class='card'>{filter_bar}{session_banner}"
            f"<div class='tablewrap'><table class='audit'><tr>"
            f"<th>连接</th><th>环境</th><th>agent</th><th>会话</th><th>工具</th>"
            f"<th>SQL</th><th>状态</th><th>时间</th><th>结果</th></tr>{table_rows}</table></div>{pager}</div>"
            "<script>(function(){"
            "var box=document.getElementById('auto-refresh');"
            f"var refreshDefault={'true' if bool(_st.get('audit_auto_refresh', False)) else 'false'};"
            "if(box){var ls=localStorage.getItem('dbm-audit-refresh');"
            "var on=ls===null?refreshDefault:ls==='1';box.checked=on;"
            "var t=on?setTimeout(function(){location.reload();},5000):null;"
            "box.addEventListener('change',function(){"
            "localStorage.setItem('dbm-audit-refresh',box.checked?'1':'0');"
            "if(box.checked)location.reload();else if(t)clearTimeout(t);});}"
            "[['.sql-toggle','sqlfull-'],['.detail-toggle','detailfull-']].forEach(function(p){"
            "document.querySelectorAll(p[0]).forEach(function(c){"
            "c.style.cursor='pointer';c.addEventListener('click',function(){"
            "var f=document.getElementById(p[1]+c.getAttribute('data-i'));"
            "if(f)f.style.display=f.style.display==='none'?'table-row':'none';});});});"
            "})();</script>"
        )
        return _shell("操作审计", body)
