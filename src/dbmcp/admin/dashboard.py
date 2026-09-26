"""看板页与数据接口。"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from .common import error_payload
from .context import AdminContext


def _dashboard_body() -> str:
    """看板页骨架。内容全部由 dashboard.js 拉 /admin/dashboard/data 填充——

    这一页每几秒就要整体换一遍数，服务端渲染出来立刻就会被 JS 覆盖掉，
    不如只给容器（其余服务端渲染页仍照旧，不改风格）。
    """
    return """<div class="pagehead">
 <div class="eyebrow">dashboard</div>
 <h2 style="font-size:22px">看板</h2>
 <div class="muted" style="margin-top:4px">连接占用、数据传输量与此刻正在执行的查询。
  传输量为结果集体积的估算值（按返回给使用者的单元格内容计），不含协议开销。</div>
</div>
<div id="dash">
 <div class="dash-bar">
  <div class="dash-win" id="dash-win"></div>
  <span class="dash-live-dot" id="dash-dot"></span>
  <label class="dash-auto"><input type="checkbox" id="dash-auto"> 自动刷新</label>
  <span class="spacer"></span>
  <span class="dash-meta" id="dash-updated">加载中…</span>
 </div>
 <div class="errbar" id="dash-err" style="display:none"></div>
 <div class="dash-tiles" id="dash-tiles"></div>
 <div class="dash-charts">
  <div class="card">
   <div class="chart-head"><h3>操作数</h3><span class="tot" id="dash-ops-total"></span></div>
   <div id="dash-chart-ops"></div>
  </div>
  <div class="card">
   <div class="chart-head"><h3>读出数据量</h3><span class="tot" id="dash-bytes-total"></span></div>
   <div id="dash-chart-bytes"></div>
  </div>
 </div>
 <div class="card"><h2>此刻正在执行</h2><div id="dash-live"></div></div>
 <div class="card"><h2>连接</h2><div id="dash-conns"></div></div>
 <div class="card"><h2>活跃会话 <span class="cardsub" id="dash-sessions-range"></span></h2>
  <div id="dash-sessions"></div></div>
 <div class="card"><h2>会话结果配额</h2>
  <div class="muted" style="margin-bottom:10px">agent 把多少数据搬进了自己的上下文。
   撞到上限后它会被拒绝取数，须先问你、你同意后它才能追加额度——「已放行」列即它问过几次。</div>
  <div id="dash-budgets"></div>
 </div>
 <div class="card"><h2>排行</h2><div class="dash-cols">
  <div><div class="sec-title">连接</div><div id="dash-top-conn"></div></div>
  <div><div class="sec-title">工具</div><div id="dash-top-tool"></div></div>
  <div><div class="sec-title">agent</div><div id="dash-top-agent"></div></div>
 </div></div>
</div>"""


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _shell = ctx._shell

    @mcp.custom_route("/admin", methods=["GET"])
    @guard
    async def _index(_req: Request) -> RedirectResponse:
        return RedirectResponse(url="/admin/approvals")

    @mcp.custom_route("/admin/dashboard", methods=["GET"])
    @guard
    async def _dashboard(_req: Request) -> HTMLResponse:
        # echarts 是 UMD 包，必须在任何 AMD loader 之前加载才会挂 window.echarts；
        # 本页不加载 Monaco，没有 loader 冲突。它是同步脚本，先于 defer 的 dashboard.js 执行。
        head = ('<link rel="stylesheet" href="/admin/static/dashboard.css">'
                '<script src="/admin/static/echarts.min.js"></script>'
                '<script defer src="/admin/static/dashboard.js"></script>')
        return _shell("看板", _dashboard_body(), extra_head=head)

    @mcp.custom_route("/admin/dashboard/data", methods=["GET"])
    @guard
    async def _dashboard_data(req: Request) -> JSONResponse:
        window = req.query_params.get("window") or "24h"
        try:
            # 只读 SQLite + 进程内状态，不触达任何业务库；仍卸到线程避免审计表大时
            # 的聚合查询阻塞同一 ASGI 上的 agent 调用。
            snap = await anyio.to_thread.run_sync(
                partial(service.dashboard_snapshot, window))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e), status_code=400)
        return JSONResponse(snap)
