"""临时导出文件页。"""

from __future__ import annotations


from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .common import _esc, _pagehead
from .context import AdminContext


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _shell = ctx._shell

    @mcp.custom_route("/admin/exports", methods=["GET"])
    @guard
    async def _exports(_req: Request) -> HTMLResponse:
        from datetime import datetime

        exports = service.list_mcp_exports()
        rows = []
        for item in exports:
            fields = "、".join(str(v) for v in item.get("fields") or [])
            masked = "、".join(str(v) for v in item.get("masked_columns") or []) or "—"
            expires = datetime.fromtimestamp(int(item["expires_at"])).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            size = int(item.get("byte_size") or 0)
            size_text = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
            rows.append(
                f"<tr><td><b>{_esc(item.get('filename'))}</b>"
                f"<br><span class='muted mono'>{_esc(item.get('format', '').upper())}"
                f" · {_esc(size_text)}</span></td>"
                f"<td><code>{_esc(item.get('project'))}/{_esc(item.get('connection'))}</code>"
                f"<br><span class='muted'>{_esc(item.get('database') or '默认库')}"
                f" · {_esc(item.get('table'))}</span></td>"
                f"<td>{int(item.get('row_count') or 0)}</td>"
                f"<td class='muted' title='{_esc(fields)}'>{_esc(fields[:80])}</td>"
                f"<td class='muted'>{_esc(masked)}</td>"
                f"<td class='muted mono'>{_esc(expires)}</td>"
                "<td style='white-space:nowrap'>"
                f"<a class='btn btn-ghost' href='/admin/exports/{_esc(item.get('token'))}/preview'>预览</a> "
                f"<a class='btn btn-primary' href='{_esc(item.get('download_url'))}'>下载</a> "
                "<form method='post' action='/admin/exports/delete' style='display:inline' "
                "onsubmit='return dbmConfirm(this)'>"
                f"<input type='hidden' name='token' value='{_esc(item.get('token'))}'>"
                "<button class='btn btn-reject' type='submit'>删除</button></form></td></tr>"
            )
        table = "".join(rows) or (
            '<tr><td colspan="7" class="muted">（暂无临时导出文件）</td></tr>'
        )
        body = (
            _pagehead(
                "Exports",
                "临时导出",
                "由 MCP export_table 生成；文件一小时后自动清理，也可在此提前删除",
            )
            + "<div class='card'><div class='tablewrap'><table>"
            "<tr><th>文件</th><th>来源</th><th>行数</th><th>字段</th>"
            "<th>脱敏字段</th><th>过期时间</th><th>操作</th></tr>"
            f"{table}</table></div></div>"
        )
        return _shell("临时导出", body)

    @mcp.custom_route("/admin/exports/{token:str}/preview", methods=["GET"])
    @guard
    async def _preview_export(req: Request) -> HTMLResponse:
        token = req.path_params["token"]
        preview = service.preview_mcp_export(token)
        if preview is None:
            return HTMLResponse("export not found or expired", status_code=404)
        meta = preview["metadata"]
        columns = preview["columns"]
        rows = preview["rows"]
        if preview["raw"] is not None:
            content = f"<pre style='max-height:68vh;overflow:auto'>{_esc(preview['raw'])}</pre>"
        else:
            head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
            body_rows = []
            for row in rows:
                cells = "".join(
                    f"<td class='mono'>{_esc(str(value)[:500] if value is not None else '')}</td>"
                    for value in row
                )
                body_rows.append(f"<tr>{cells}</tr>")
            content = (
                "<div class='tablewrap' style='max-height:68vh'><table>"
                f"<tr>{head}</tr>{''.join(body_rows)}</table></div>"
            )
        note = (
            f"预览前 {len(rows)} 行"
            if preview["raw"] is None
            else "文本预览"
        )
        if preview["truncated"]:
            note += "（内容已截断）"
        body = (
            _pagehead("Preview", str(meta.get("filename") or "导出预览"), note)
            + f"<div class='card'>{content}</div>"
            "<p><a href='/admin/exports'>← 返回临时导出</a></p>"
        )
        return _shell("导出预览", body)

    @mcp.custom_route("/admin/exports/delete", methods=["POST"])
    @guard
    async def _delete_export(req: Request) -> RedirectResponse:
        form = await req.form()
        service.delete_mcp_export(str(form.get("token") or ""))
        return RedirectResponse(url="/admin/exports", status_code=303)
