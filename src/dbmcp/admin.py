"""管理后台：审计查询 + 审批中心 + 连接管理。

挂在 MCP 应用同一 ASGI 服务下（custom_route），默认只随 daemon 监听 127.0.0.1。
服务端渲染，无外部依赖/资源，避免引入前端框架与 CSP 问题。

认证：所有 /admin/* 路由需登录（/admin/login 除外）。token 由 DBM_ADMIN_TOKEN 注入，
登录后下发签名 cookie（hmac(token)，不暴露 token 原文），httponly + samesite=lax。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
from collections.abc import Awaitable, Callable
from functools import partial, wraps
from pathlib import Path
from typing import TYPE_CHECKING

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from .approvals import ApprovalError

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .service import CallerInfo, DbmService

_COOKIE_NAME = "dbm_admin"


def _session_value(token: str) -> str:
    """cookie 值：hmac(token) 十六进制，cookie 泄露也不直接暴露 token 原文。"""
    return hmac.new(token.encode("utf-8"), b"dbm-admin-session", hashlib.sha256).hexdigest()


def _authed(req: Request, expected_cookie: str) -> bool:
    got = req.cookies.get(_COOKIE_NAME, "")
    return bool(got) and hmac.compare_digest(got, expected_cookie)


def _wants_json(req: Request) -> bool:
    """请求是否来自前端 fetch（期望 JSON）而非浏览器页面导航。
    查询台/Redis 控制台的 apiGet/apiPost 都带 Accept: application/json；
    对这类请求鉴权失败应返回 JSON 401，而不是 303 到 HTML 登录页——
    否则 fetch 静默跟随重定向拿到登录页 HTML，前端 r.json() 报出
    「Unexpected token '<', "<!doctype "... is not valid JSON」的误导错误。"""
    return "application/json" in req.headers.get("accept", "")


# 本地进程模式下管理后台只应从本机访问。校验 Host / Origin 防两类攻击：
#   - DNS rebinding：恶意网页把自家域名解析到 127.0.0.1，浏览器带的是攻击者的 Host。
#   - 跨站状态变更（CSRF）：SameSite=Lax 已挡多数场景，Origin 校验作为纵深防御补齐写请求。
# 允许的 Host 默认 = 本机回环名；如需在反代/LAN 后使用，用 DBM_ADMIN_ALLOWED_HOSTS 显式配置。
_DEFAULT_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _allowed_hosts() -> frozenset[str]:
    extra = os.environ.get("DBM_ADMIN_ALLOWED_HOSTS", "")
    names = {h.strip().lower() for h in extra.split(",") if h.strip()}
    return _DEFAULT_LOCAL_HOSTS | frozenset(names)


def _hostname_of(value: str) -> str:
    """从 Host / Origin 值里取出主机名（去端口、去 IPv6 方括号），小写。"""
    v = (value or "").strip().lower()
    if v.startswith("http://"):
        v = v[7:]
    elif v.startswith("https://"):
        v = v[8:]
    v = v.split("/", 1)[0]           # 去掉路径
    if v.startswith("["):            # IPv6：[::1]:8100 → ::1
        end = v.find("]")
        return v[1:end] if end > 0 else v
    return v.rsplit(":", 1)[0] if ":" in v else v


def _local_request_ok(req: Request) -> bool:
    """校验请求来自允许的本机 Host，且（若是写请求）Origin 同源。返回是否放行。"""
    allowed = _allowed_hosts()
    host = _hostname_of(req.headers.get("host", ""))
    if host not in allowed:
        return False
    # 状态变更方法额外校验 Origin（浏览器在跨站/同站 POST 都会带 Origin）。
    # 非浏览器客户端（curl/后台脚本）通常不带 Origin —— 已过 Host 白名单 + 认证，放行。
    if req.method not in ("GET", "HEAD", "OPTIONS"):
        origin = req.headers.get("origin", "")
        if origin and _hostname_of(origin) not in allowed:
            return False
    return True

_LEVEL_COLOR = {
    "CRITICAL": "#b00020",
    "HIGH": "#e65100",
    "MEDIUM": "#f9a825",
    "LOW": "#2e7d32",
}
# 环境配色：越靠生产越醒目（红），本地/开发偏冷色
_ENV_COLOR = {
    "local": "#64748b",     # 灰
    "dev": "#2563eb",       # 蓝
    "staging": "#d97706",   # 橙
    "prod": "#dc2626",      # 红
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


def _fmt_ts(ts: object) -> str:
    """ISO 时间（多为 UTC）→ 本机时区 'YYYY-MM-DD HH:MM:SS'；解析失败原样返回。"""
    if not ts:
        return ""
    try:
        from datetime import datetime  # noqa: PLC0415
        return datetime.fromisoformat(str(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def error_payload(e: BaseException) -> dict:
    """异常 → 查询台可行动的 JSON 错误。

    连接类错误额外带 `error_kind`，前端据此在错误处**直接给出「重连数据库」按钮**
    （原先只能在左树右键菜单里找，用户反馈难发现）：
    - `connection_unavailable` / `connection_exhausted`：健康位已判不可用，后台正在退避重连
    - `connection_error`：这一次刚撞上连接级错误（健康位可能还没来得及打标）

    消息一律过 `sanitize_db_message`：管理后台虽已认证，也不该把 DSN 里的密码、
    绑定参数原样打到页面上。
    """
    from .errors import error_label, translate_db_error
    from .health import ConnectionUnavailable, is_connection_error
    from .service import QueryRejected

    if isinstance(e, ConnectionUnavailable):
        return {"ok": False, "error": str(e), "error_kind": f"connection_{e.state}"}
    if isinstance(e, (QueryRejected, KeyError, ValueError)):
        return {"ok": False, "error": str(e)}
    # 走与 agent 侧同一套分类/脱敏（errors.py），只把标签换成中文——原来直接拼
    # `type(e).__name__` 再脱敏，PG 上会渲染成
    # 「ProgrammingError: (psycopg.errors.InsufficientPrivilege) permission denied for …」，
    # 驱动类名对使用者是纯噪音（用户实测反馈）。
    info = translate_db_error(e)
    label = error_label(info.kind)
    msg = f"{label}：{info.message}" if label else info.message
    if is_connection_error(e):
        return {"ok": False, "error": msg, "error_kind": "connection_error"}
    return {"ok": False, "error": msg}


def _format_sql(sql: str, engine: str) -> str:
    """用 sqlglot 美化 SQL（缩进/关键字对齐）；解析失败则原样返回。"""
    if not sql:
        return ""
    dialect = {"mysql": "mysql", "postgres": "postgres", "sqlite": "sqlite",
               "clickhouse": "clickhouse"}.get(engine)
    try:
        import sqlglot  # noqa: PLC0415
        out = sqlglot.transpile(sql, read=dialect, write=dialect, pretty=True)
        return ";\n".join(out) if out else sql
    except Exception:
        return sql


# 图标：数据库柱形 + 审批勾徽章。内联为 data URI（零外部文件/网络），同时由路由 serve。
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#1e293b"/>'
    '<g fill="none" stroke="#e2e8f0" stroke-width="3.5" stroke-linecap="round">'
    '<ellipse cx="26" cy="19" rx="14" ry="5.5"/>'
    '<path d="M12 19 v22 c0 3 6.3 5.5 14 5.5 s14 -2.5 14 -5.5 V19"/>'
    '<path d="M12 30 c0 3 6.3 5.5 14 5.5 s14 -2.5 14 -5.5"/></g>'
    '<circle cx="46" cy="46" r="13" fill="#22c55e" stroke="#1e293b" stroke-width="3.5"/>'
    '<path d="M40 46 l4.2 4.2 L52 41" fill="none" stroke="#fff" stroke-width="4"'
    ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_FAVICON_HREF = "data:image/svg+xml;base64," + base64.b64encode(_FAVICON_SVG.encode()).decode()
_FAVICON_LINK = f'<link rel="icon" type="image/svg+xml" href="{_FAVICON_HREF}">'


def _page(title: str, body: str, pending: int = 0, doc: bool = True,
          font_size: int | None = None, extra_head: str = "") -> str:
    nav_badge = f"<span class='nav-count'>{pending}</span>" if pending else ""
    banner = (f"<a class='pending-banner' href='/admin/approvals'>"
              f"⚠ <b>{pending}</b> 条数据变更待审批，点此处理 →</a>" if pending else "")
    doc_css = ('<link rel="stylesheet" href="/admin/static/admin-doc.css">'
               if doc else '')
    # 整体字号（系统设置 ui_font_size）：只作用于服务端渲染页，SPA（查询台/Redis）另有字号设置
    font_css = f"<style>body{{font-size:{font_size}px}}</style>" if font_size else ""
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · Quay</title>
{_FAVICON_LINK}
<link rel="stylesheet" href="/admin/static/admin-chrome.css">{doc_css}{font_css}{extra_head}</head><body>
<div class="shell">
 <aside class="side">
  <div class="brand">{_FAVICON_SVG}<div><b>Quay</b><span>gatekeeper</span></div></div>
  <nav>
   <a href="/admin/dashboard"><span class="nico nico-dash"></span><span class="nlabel">看板</span></a>
   <a href="/admin/sql"><span class="nico nico-sql"></span><span class="nlabel">查询台</span></a>
   <a href="/admin/redis"><span class="nico nico-redis"></span><span class="nlabel">Redis</span></a>
   <a href="/admin/workflows"><span class="nico nico-flow"></span><span class="nlabel">流程</span></a>
   <a href="/admin/exports"><span class="nico nico-export"></span><span class="nlabel">临时导出</span></a>
   <a href="/admin/approvals"><span class="nico nico-approve"></span><span class="nlabel">审批中心</span>{nav_badge}</a>
   <a href="/admin/audit"><span class="nico nico-audit"></span><span class="nlabel">操作审计</span></a>
   <a href="/admin/settings"><span class="nico nico-settings"></span><span class="nlabel">系统设置</span></a>
  </nav>
  <div class="foot"><a href="/admin/logout"><span class="nlabel">退出登录</span></a></div>
 </aside>
 <main>{banner}{body}</main>
</div>
<script src="/admin/static/admin.js"></script>
</body></html>"""


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


def _badge(text: str, color_map: dict) -> str:
    color = color_map.get(str(text).lower(), color_map.get(str(text).upper(), "#666"))
    return f'<span class="badge" style="background:{color}">{_esc(text)}</span>'


def _pagehead(eyebrow: str, title: str, sub: str = "") -> str:
    subline = f'<div class="muted" style="margin-top:4px">{_esc(sub)}</div>' if sub else ""
    return (f'<div class="pagehead"><div class="eyebrow">{_esc(eyebrow)}</div>'
            f'<h2 style="font-size:22px">{_esc(title)}</h2>{subline}</div>')


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


def _env_badge(env: str) -> str:
    color = _ENV_COLOR.get(env, "#64748b")
    return f'<span class="badge" style="background:{color}">{_esc(env or "—")}</span>'


# 引擎 → 真实品牌 logo 文件名（devicon，vendored 到 static/db-icons/；postgres 文件名为 postgresql）
_ENGINE_ICON_FILE = {
    "mysql": "mysql", "postgres": "postgresql", "sqlite": "sqlite",
    "clickhouse": "clickhouse", "redis": "redis", "duckdb": "duckdb",
}


def _engine_icon(engine: str) -> str:
    """连接列表里引擎名前的品牌 logo（无对应图标则空串）。"""
    f = _ENGINE_ICON_FILE.get(engine)
    if not f:
        return ""
    return (f"<img src='/admin/static/db-icons/{f}.svg' alt='' title='{_esc(engine)}' "
            f"style='width:15px;height:15px;vertical-align:middle;margin-right:6px'>")


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


def _ident_select(selected: str, identities: list[str]) -> str:
    """跳板行里的证书下拉：空值＝内联路径，其余为证书库名字。"""
    opts = [f"<option value=''{'' if selected else ' selected'}>（内联路径）</option>"]
    for n in identities:
        opts.append(f"<option value='{_esc(n)}'{' selected' if n == selected else ''}>{_esc(n)}</option>")
    return f"<select name='hop_identity' class='hop-ident' style='padding:5px'>{''.join(opts)}</select>"


def _hop_row(hop, identities: list[str]) -> str:  # noqa: ANN001
    """一行跳板配置：host / user / port / 证书 / 内联 key。hop=None 为空行模板。"""
    host = _esc(hop.host) if hop else ""
    user = _esc(hop.user or "") if hop else ""
    port = _esc(hop.port or "") if hop else ""
    identity = (hop.identity or "") if hop else ""
    keyp = _esc(hop.key_path or "") if hop else ""
    return (
        "<div class='hop-row' style='display:flex;gap:6px;align-items:center;"
        "margin-bottom:6px;flex-wrap:wrap'>"
        f"<input name='hop_host' value='{host}' placeholder='host（引用配置时可空）' style='width:170px'>"
        f"<input name='hop_user' value='{user}' placeholder='user' style='width:88px'>"
        f"<input name='hop_port' value='{port}' placeholder='22' style='width:60px'>"
        f"{_ident_select(identity, identities)}"
        f"<input name='hop_key_path' class='hop-key' value='{keyp}' "
        "placeholder='/path/key（内联）' style='width:180px'>"
        "<button type='button' class='btn btn-ghost hop-del' "
        "style='padding:2px 9px'>✕</button></div>"
    )


def _parse_hop_rows(f) -> list[dict]:  # noqa: ANN001
    """从表单的平行数组字段还原跳板列表。

    一行保留的条件：填了 host **或** 引用了一条 SSH 配置（identity）——仅引用配置时
    host/user/port 可留空，由配置继承。两者都空的行才跳过。
    """
    hosts = f.getlist("hop_host")
    users = f.getlist("hop_user")
    ports = f.getlist("hop_port")
    idents = f.getlist("hop_identity")
    keys = f.getlist("hop_key_path")

    def _at(seq, i):  # noqa: ANN001
        return str(seq[i]).strip() if i < len(seq) else ""

    out: list[dict] = []
    n = max(len(hosts), len(idents))
    for i in range(n):
        host = _at(hosts, i)
        identity = _at(idents, i)
        if not host and not identity:
            continue
        hop: dict = {}
        if host:
            hop["host"] = host
        if _at(users, i):
            hop["user"] = _at(users, i)
        if _at(ports, i):
            hop["port"] = int(_at(ports, i))
        if identity:
            hop["identity"] = identity
        elif _at(keys, i):
            hop["key_path"] = _at(keys, i)
        out.append(hop)
    return out


def _connection_form(project: str, connection: str, cfg, identities: list[str]) -> str:  # noqa: ANN001
    """连接增删改表单。编辑时锁定 project/connection，密码留空表示不改。

    按「身份 → 连到哪 → 用什么账号 → 怎么到达 → 取多少/给 agent 看多少」分区，
    与系统设置同一套账本行。**每个 `cf-*` 类名都是前端按引擎显隐整行的钩子**
    （sqlite 不要账号与跳板、redis 不要 user 与 writer），改结构时不能丢。
    """
    is_edit = cfg is not None
    ro = "readonly" if is_edit else ""
    engines_opts = "".join(
        f"<option value='{e}'{' selected' if cfg and cfg.engine == e else ''}>{e}</option>"
        for e in ("mysql", "postgres", "clickhouse", "redis", "sqlite")
    )
    envs_opts = "".join(
        f"<option value='{e}'{' selected' if cfg and cfg.environment == e else ''}>{e}</option>"
        for e in ("local", "dev", "staging", "prod")
    )
    ssh_extra = " ".join(cfg.ssh_options) if cfg and cfg.ssh_options else ""
    hop_rows = "".join(_hop_row(h, identities) for h in (cfg.jump_hosts if cfg else []))
    empty_hop = _hop_row(None, identities)
    masks = ", ".join(cfg.policy.mask_columns) if cfg else ""
    mask_mode = "" if not cfg or cfg.policy.mask_default_patterns is None else (
        "1" if cfg.policy.mask_default_patterns else "0")
    mask_opts = "".join(
        f"<option value='{v}'{' selected' if mask_mode == v else ''}>{_esc(t)}</option>"
        for v, t in (("", "跟随全局设置"), ("1", "开：自动脱敏"), ("0", "关：返回真实值")))
    pw_ph = "留空 = 不修改" if is_edit else "写入系统钥匙串"
    writer_user = cfg.writer.user if cfg and cfg.writer else ""
    is_edit_js = "true" if is_edit else "false"

    def txt(name: str, value: object = "", ph: str = "", typ: str = "text") -> str:
        return (f"<input type='{typ}' name='{name}' value='{_esc(str(value or ''))}' "
                f"placeholder='{_esc(ph)}'>")

    basic = _set_section(
        "身份", "连接在项目下按名字寻址，agent 用 <code>项目/连接名</code> 指定要连哪个库。",
        _set_row("project", "项目", "同一个业务的连接放一个项目里。",
                 txt("project", project, "local"))
        + _set_row("connection", "连接名",
                   "创建后不可改名。" if is_edit else "如 <code>orders-prod</code>，取个一眼认得出的名字。",
                   f"<input type='text' name='connection' value='{_esc(connection)}' {ro}>")
        + _set_row("engine", "引擎", "决定下面要填哪些字段。",
                   f"<select name='engine'>{engines_opts}</select>")
        + _set_row("environment", "环境",
                   "prod 会在查询台整页套红框、写操作强制审批，也不允许作为表同步的目标。",
                   f"<select name='environment'>{envs_opts}</select>"))

    where = _set_section(
        "连到哪",
        "走 SSH 跳板时这里仍填<b>数据库自己的</b>地址，隧道由下面的跳板链负责打通。",
        _set_row("host", "主机", "", txt("host", cfg.host if cfg else "", "127.0.0.1"),
                 cls="cf-hostport")
        + _set_row("port", "端口", "",
                   txt("port", cfg.port if cfg else "", "3306", "number"), cls="cf-hostport")
        + _set_row("database", "库",
                   "<span class='cf-db-mysql'>留空 = 连到实例但不绑定默认库，"
                   "查询要用「库名.表名」全限定。</span>"
                   "<span class='cf-db-sqlite'><b>必填</b>：SQLite 文件路径，"
                   "或 <code>:memory:</code>。</span>"
                   "<span class='cf-db-redis'>Redis 逻辑库编号，默认 0。</span>",
                   txt("database", cfg.database if cfg else "")))

    creds = _set_section(
        "账号",
        "两个账号是这套系统的地基：日常查询走只读账号，只有审批通过的写操作才切到 writer。",
        _set_row("user", "只读账号",
                 "<span class='cf-cred-note'>必须是<b>最小权限的只读账号</b>。"
                 "保存时会实地校验，发现它有写权限或是超级用户会被拦下。</span>"
                 "<span class='cf-redis-pw-note'>Redis 没有用户名，密码即 requirepass。</span>",
                 txt("user", cfg.user if cfg else ""), cls="cf-cred cf-cred-user")
        + _set_row("password", "只读账号密码", "", txt("password", "", pw_ph, "password"),
                   cls="cf-cred cf-cred-pw")
        + _set_row("writer_user", "writer 账号",
                   "留空 = 这条连接只能读，agent 的写操作会被直接拒绝。",
                   txt("writer_user", writer_user), cls="cf-writer")
        + _set_row("writer_password", "writer 密码", "",
                   txt("writer_password", "", pw_ph, "password"), cls="cf-writer")
        + _set_row("force_privileged", "允许高权限账号",
                   "只读账号校验不通过时，勾选它强行保存。"
                   "<b>意味着日常查询也用着一个能写的账号</b>，只在你清楚后果时用。",
                   "<label class='sw'><input type='checkbox' name='force_privileged' value='1'>"
                   "<span class='track'></span><span class='state'>关闭</span></label>"),
        guard=True)

    ssh = _set_section(
        "SSH 跳板链",
        "按顺序打通，最后一跳落地转发到上面填的数据库地址。无跳板 = 直连。"
        "每跳可引用一条<a href='/admin/settings?tab=ssh'>已保存的 SSH 配置</a>"
        "（主机/用户/私钥都从配置来，跳板处留空即继承），也可以就地填内联主机与私钥。",
        f"<div class='set-row wide'><div class='ctl' style='margin-top:0'>"
        f"<div id='hops'>{hop_rows}</div>"
        f"<template id='hop-tpl'>{empty_hop}</template>"
        "<button type='button' id='add-hop' class='btn btn-ghost btn-sm'>＋ 加一跳</button>"
        "</div></div>"
        + _set_row("ssh_options_extra", "其它 ssh 选项",
                   "空格分隔，作用于最终目标。如 <code>-o ConnectTimeout=5</code>。",
                   txt("ssh_options_extra", ssh_extra, "-o ConnectTimeout=5"), wide=True),
        cls="cf-ssh")

    policy = _set_section(
        "取多少数据",
        "这条连接上的取数上限，比系统设置里的全局值更具体。",
        _set_row("max_rows", "单次行上限",
                 "缺 LIMIT 的查询自动兜底到这个行数。",
                 txt("max_rows", cfg.policy.max_rows if cfg else 500, "500", "number"))
        + _set_row("statement_timeout_s", "读超时",
                   "只约束只读查询（SELECT）。",
                   txt("statement_timeout_s",
                       cfg.policy.statement_timeout_s if cfg else 30, "30", "number")
                   + "<span class='unit'>秒</span>", cls="cf-timeouts")
        + _set_row("write_timeout_s", "写超时",
                   "给 writer 账号的大 DELETE/UPDATE 留足时间，避免 socket 提前断开报 2013。"
                   "跑飞的写可以在查询台点「取消」直接 KILL。",
                   txt("write_timeout_s",
                       cfg.policy.write_timeout_s if cfg else 600, "600", "number")
                   + "<span class='unit'>秒</span>", cls="cf-timeouts"))

    mask = _set_section(
        "给 agent 看多少",
        "只影响 agent 的 query / sample_rows。你在查询台和导出里看到的一直是真实值。",
        _set_row("mask_columns", "脱敏列",
                 "逗号分隔，点名的列<b>始终</b>脱敏，不受下面的开关影响。",
                 txt("mask_columns", masks, "email, phone"))
        + _set_row("mask_default_patterns", "敏感列自动脱敏",
                   "按内置词表（password / token / secret / id_card…）猜哪些列敏感。"
                   "默认跟随系统设置里的全局开关，这里可以为这条连接单独定。",
                   f"<select name='mask_default_patterns'>{mask_opts}</select>"),
        guard=True)

    return f"""<div id="conn-err" class="errbar" style="display:none"></div>
<form id="conn-form" method="post" action="/admin/connections/save">
{basic}{where}{creds}{ssh}{policy}{mask}
 <div id="conn-test-result" style="display:none"></div>
 <div class="conn-actions">
  <button class="btn btn-ghost" type="button" id="btn-test">测试连接</button>
  <button class="btn btn-ghost" type="button" id="btn-test-ssh">测试 SSH 隧道</button>
  <span class="spacer"></span>
  {"<a class='btn btn-ghost' href='/admin/settings?tab=connections'>取消</a>" if is_edit else ""}
  <button class="btn btn-primary" type="submit">{'保存修改' if is_edit else '创建连接'}</button>
 </div>
</form>
<script>
(function(){{
  var form = document.getElementById('conn-form');
  var err = document.getElementById('conn-err');
  if (!form) return;

  // SSH 跳板行：选证书时隐藏内联 key 输入；可增删
  var hops = document.getElementById('hops');
  var hopTpl = document.getElementById('hop-tpl');
  function wireHop(row){{
    var sel = row.querySelector('.hop-ident');
    var key = row.querySelector('.hop-key');
    function toggle(){{ key.style.display = sel.value ? 'none' : ''; }}
    sel.addEventListener('change', toggle); toggle();
    row.querySelector('.hop-del').addEventListener('click', function(){{ row.remove(); }});
  }}
  if (hops) Array.prototype.forEach.call(hops.querySelectorAll('.hop-row'), wireHop);
  var addHop = document.getElementById('add-hop');
  if (addHop) addHop.addEventListener('click', function(){{
    var row = hopTpl.content.firstElementChild.cloneNode(true);
    hops.appendChild(row); wireHop(row);
  }});

  // 新增模式：引擎决定默认端口，local 环境默认 host 127.0.0.1（不覆盖用户手改的值）
  if (!{is_edit_js}) {{
    var DEFAULT_PORTS = {{mysql:'3306', postgres:'5432', redis:'6379', sqlite:''}};
    var AUTO_PORTS = ['', '3306', '5432', '6379'];
    var engineSel = form.querySelector('[name=engine]');
    var envSel = form.querySelector('[name=environment]');
    var hostInput = form.querySelector('[name=host]');
    var portInput = form.querySelector('[name=port]');
    function applyEngineDefault(){{
      // 仅当端口为空或仍是某个默认端口（说明用户没定制）时才跟随引擎变化
      if (AUTO_PORTS.indexOf(portInput.value) >= 0) portInput.value = DEFAULT_PORTS[engineSel.value] || '';
    }}
    function applyEnvDefault(){{
      if (envSel.value === 'local' && (hostInput.value === '' || hostInput.value === '127.0.0.1'))
        hostInput.value = '127.0.0.1';
    }}
    engineSel.addEventListener('change', applyEngineDefault);
    envSel.addEventListener('change', applyEnvDefault);
    applyEngineDefault(); applyEnvDefault();  // 初始填一次
  }}

  // 按引擎显隐字段（新增/编辑都生效）：sqlite 只要 database；redis 无 user/writer/超时。
  var engineSelV = form.querySelector('[name=engine]');
  function setShow(cls, on){{
    Array.prototype.forEach.call(form.querySelectorAll('.' + cls), function(el){{
      el.style.display = on ? '' : 'none';
    }});
  }}
  function applyEngineVisibility(){{
    var e = engineSelV.value;
    var isSqlite = e === 'sqlite', isRedis = e === 'redis', isCH = e === 'clickhouse';
    var isSql = e === 'mysql' || e === 'postgres';
    var hasUser = isSql || isCH;         // 有 user 账号的引擎（clickhouse 需要 user、可空密码）
    setShow('cf-hostport', !isSqlite);
    setShow('cf-cred', !isSqlite);       // sqlite 无账号
    setShow('cf-cred-user', hasUser);    // redis 无 user（密码即 requirepass）
    setShow('cf-cred-pw', !isSqlite);
    setShow('cf-cred-note', hasUser);
    setShow('cf-redis-pw-note', isRedis);
    setShow('cf-writer', isSql);         // 双账号仅 mysql/pg（clickhouse 本期只读，无 writer）
    setShow('cf-ssh', !isSqlite);        // sqlite 本地文件无需隧道
    setShow('cf-timeouts', hasUser);
    setShow('cf-db-mysql', hasUser);     // clickhouse 也用「库名」文本字段（默认 default 库）
    setShow('cf-db-sqlite', isSqlite);
    setShow('cf-db-redis', isRedis);
  }}
  engineSelV.addEventListener('change', applyEngineVisibility);
  applyEngineVisibility();

  // 测试按钮：用当前表单值探测，结果 inline 显示，不保存
  var resultBox = document.getElementById('conn-test-result');
  function showResult(ok, html){{
    resultBox.style.display = 'block';
    resultBox.style.padding = '10px 14px';
    resultBox.style.borderRadius = '8px';
    resultBox.style.fontSize = '14px';
    resultBox.style.background = ok ? '#f0fdf4' : '#fef2f2';
    resultBox.style.border = '1px solid ' + (ok ? '#86efac' : '#fca5a5');
    resultBox.style.color = ok ? '#166534' : '#b00020';
    resultBox.innerHTML = html;
  }}
  async function runTest(url, btn){{
    resultBox.style.display = 'none';
    btn.disabled = true; var old = btn.textContent; btn.textContent = '测试中…';
    try {{
      var resp = await fetch(url, {{method:'POST', headers:{{'Accept':'application/json'}}, body:new FormData(form)}});
      var d = await resp.json();
      var msg = (d.ok ? '✓ ' : '✗ ') + (d.message || '');
      if (d.detail) msg += '<br><span style="font-size:13px">' + d.detail + '</span>';
      showResult(d.ok, msg);
    }} catch (ex) {{ showResult(false, '✗ 请求失败：' + ex); }}
    finally {{ btn.disabled = false; btn.textContent = old; }}
  }}
  var bt = document.getElementById('btn-test');
  var bs = document.getElementById('btn-test-ssh');
  if (bt) bt.addEventListener('click', function(){{ runTest('/admin/connections/test', bt); }});
  if (bs) bs.addEventListener('click', function(){{ runTest('/admin/connections/test-ssh', bs); }});

  form.addEventListener('submit', async function(e){{
    e.preventDefault();
    err.style.display = 'none';
    var btn = form.querySelector('button[type=submit]');
    btn.disabled = true; btn.style.opacity = '.6';
    try {{
      var resp = await fetch('/admin/connections/save', {{
        method: 'POST',
        headers: {{'Accept': 'application/json'}},
        body: new FormData(form)
      }});
      var data = await resp.json();
      if (data.ok) {{ window.location = '/admin/settings?tab=connections'; return; }}
      err.textContent = '⚠ ' + (data.error || '保存失败');
      err.style.display = 'block';
      err.scrollIntoView({{behavior: 'smooth', block: 'center'}});
    }} catch (ex) {{
      err.textContent = '⚠ 请求失败：' + ex;
      err.style.display = 'block';
    }} finally {{
      btn.disabled = false; btn.style.opacity = '1';
    }}
  }});
}})();
</script>"""


# 查询台页面：Vue 3 + Monaco 深色 IDE。页面只给挂载点与脚本，逻辑在 static/console.js，
# 数据全走 /admin/sql/* JSON 接口（连接/表/结构/执行/导出/片段），与服务端渲染解耦。
def _lint_one(sql: str, dialect: str) -> list[dict]:
    """对单块 SQL 做 sqlglot 语法检查，返回错误列表（行列相对本块文本）。"""
    import re as _re

    import sqlglot
    from sqlglot.errors import ParseError, SqlglotError

    from .audit.classify import normalize_sql_for_parse

    if not sql.strip():
        return []
    try:
        # DB 合法但 sqlglot 解析不了的语法（如 MySQL 无括号 DROP PARTITION）先归一化，
        # 避免编辑器对合法语句误标红波浪线（dialect 名与 engine 名一致）。
        sqlglot.parse(normalize_sql_for_parse(sql, dialect), read=dialect)
        return []
    except ParseError as e:
        out = []
        for err in getattr(e, "errors", [])[:5]:
            out.append({"line": err.get("line"), "col": err.get("col"),
                        "message": err.get("description") or str(e)})
        return out or [{"line": 1, "col": 1, "message": str(e)}]
    except SqlglotError as e:  # 词法错误等（版本间类名有变：TokenizeError/TokenError）→ 基类兜底
        m = _re.search(r"Line (\d+), Col: (\d+)", str(e))
        return [{"line": int(m.group(1)) if m else 1, "col": int(m.group(2)) if m else 1,
                 "message": str(e).split("\n")[0][:200]}]
    except Exception:  # noqa: BLE001  lint 永不抛错影响编辑
        return []


def _split_blank_line_blocks(sql: str) -> list[tuple[int, str]]:
    """按**空行**把 SQL 拆成块（连续非空行为一块），返回 [(起始行号 1-indexed, 文本)]。
    与前端 stmtRanges 的「空行也分隔语句」一致。"""
    blocks: list[tuple[int, str]] = []
    cur: list[str] = []
    start = 0
    for i, ln in enumerate(sql.split("\n"), 1):
        if ln.strip() == "":
            if cur:
                blocks.append((start, "\n".join(cur)))
                cur = []
        else:
            if not cur:
                start = i
            cur.append(ln)
    if cur:
        blocks.append((start, "\n".join(cur)))
    return blocks


def _lint_sql(sql: str, dialect: str) -> list[dict]:
    """sqlglot 语法检查（查询台编辑器标红）。只报错误，不阻断任何执行路径——
    执行时的「默认拒绝」判定在 classify/assess，与这里无关。

    关键：sqlglot 只按分号拆多语句、不认空行分隔。所以「无分号 + 空行分隔的多条」会被当成
    一条、在下一条起始处误报语法错（红波浪线标到错的语句上，用户反馈的坑）。修法：整体能解析
    就直接放行（避免拆块假阳性，如字符串内的空行）；整体报错时才按空行拆块逐块 lint，把错误
    行号回填到原文——这样每一块独立解析，合法块不报错、错误也落在真正出错的那条上。"""
    if not sql.strip():
        return []
    errs = _lint_one(sql, dialect)
    if not errs:
        return []
    blocks = _split_blank_line_blocks(sql)
    if len(blocks) <= 1:
        return errs   # 只有一块 → 就是这块的错，行号已正确
    out: list[dict] = []
    for start_line, text in blocks:
        for e in _lint_one(text, dialect):
            e = dict(e)
            if e.get("line"):
                e["line"] = e["line"] + start_line - 1   # 块内行号 → 原文行号（列不变，块从行首起）
            out.append(e)
        if len(out) >= 5:
            break
    return out[:5]


def _console_body() -> str:
    return (
        '<link rel="stylesheet" href="/admin/static/console.css">'
        '<div id="dbm-console"></div>'
        '<script src="/admin/static/vue.global.prod.js"></script>'
        # echarts 必须先于 Monaco 的 AMD loader：loader.js 定义 define.amd 后，
        # echarts 的 UMD 会走 AMD 注册而不挂 window.echarts
        '<script src="/admin/static/echarts.min.js"></script>'
        '<script src="/admin/static/monaco/vs/loader.js"></script>'
        # MySQL 内置函数文档（编辑器 hover 用），须先于 console.js 加载
        '<script src="/admin/static/sqlfuncs.js"></script>'
        # 共享自绘下拉（console/redis/workflows 三页共用），须先于 console.js
        '<script src="/admin/static/dg-select.js"></script>'
        # 「用户与权限」弹窗组件（左树右键菜单打开），同样须先于 console.js
        '<link rel="stylesheet" href="/admin/static/privileges.css">'
        '<script src="/admin/static/privileges.js"></script>'
        '<script src="/admin/static/console.js"></script>'
    )


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


_SETTINGS_TABS = [("general", "整体设置"), ("db", "DB"), ("redis", "Redis"),
                  ("ai", "AI 助手"), ("notify", "通知"),
                  ("connections", "连接管理"), ("ssh", "SSH 配置"), ("info", "系统信息")]

# 分区按「做什么用」分组，而不是平铺八个 tab——组本身就是信息：
# 偏好=看着舒服，能力=接外部服务，资源=连库与凭证，系统=只读信息。
_SETTINGS_GROUPS = [
    ("偏好", [("general", "整体"), ("db", "查询与 Agent"), ("redis", "Redis")]),
    ("能力", [("ai", "AI 助手"), ("notify", "通知")]),
    ("资源", [("connections", "连接管理"), ("ssh", "SSH 配置")]),
    ("", [("info", "系统信息")]),
]
_SETTINGS_TABS = [(k, label) for _, items in _SETTINGS_GROUPS for k, label in items]


def _settings_nav(active: str) -> str:
    parts = []
    for gi, (_, items) in enumerate(_SETTINGS_GROUPS):
        if gi:
            parts.append("<span class='sep'></span>")
        links = "".join(
            f"<a class='{'on' if active == k else ''}' href='/admin/settings?tab={k}'>"
            f"{_esc(label)}</a>" for k, label in items)
        parts.append(f"<div class='grp'>{links}</div>")
    return f"<nav class='set-nav'>{''.join(parts)}</nav>"


def _settings_head(searchable: bool) -> str:
    """页头：标题 + 一句话说明；表单页额外给一个搜索框。"""
    box = ("<div class='set-search'><input type='search' placeholder='搜索设置…' "
           "aria-label='搜索设置'></div>" if searchable else "")
    return ("<div class='set-head'><div>"
            "<h2>系统设置</h2>"
            "<div class='sub'>改动保存在服务端，所有浏览器一致。</div>"
            "</div><div class='spacer'></div>" + box + "</div>")


def _set_changed(name: str, s: dict) -> bool:
    """当前值是否偏离默认。治理工具里「我把哪道护栏放松了」必须一眼可见。"""
    from .settings import DEFAULTS  # noqa: PLC0415
    if name not in DEFAULTS or name not in s:
        return False
    return str(s.get(name)) != str(DEFAULTS[name])


def _set_row(name: str, label: str, desc: str, control: str, s: dict | None = None,
             wide: bool = False, more: str = "", cls: str = "") -> str:
    """一条账本行：左边「这是什么」，右边「现在是多少」。

    cls：额外类名。连接表单靠它挂 `cf-*` 钩子，让前端按引擎显隐整行
    （sqlite 不要账号、redis 不要 writer…）。
    """
    tag = ("<span class='set-tag'>已改</span>"
           if s is not None and _set_changed(name, s) else "")
    extra = (f"<details class='set-more'><summary>展开说明</summary>"
             f"<div class='body'>{more}</div></details>" if more else "")
    classes = " ".join(x for x in ("set-row", "wide" if wide else "", cls) if x)
    return (f"<div class='{classes}'>"
            f"<div><div class='nm'>{_esc(label)}{tag}</div>"
            + (f"<p class='desc'>{desc}</p>" if desc else "")
            + f"{extra}</div>"
            f"<div class='ctl'>{control}</div></div>")


def _set_section(title: str, desc: str, rows: str, guard: bool = False,
                 folded: bool = False, cls: str = "") -> str:
    """一个分区。folded=True 时默认收起——留给提示词这类「几十行文本框、平时不看」的内容，
    否则它们会把整页撑长，把真正要调的旋钮挤到屏幕外。"""
    head = (f"<h3>{_esc(title)}</h3>" + (f"<p>{desc}</p>" if desc else ""))
    if folded:
        return (f"<section class='set-sec fold'><details><summary>{head}</summary>"
                f"{rows}</details></section>")
    classes = " ".join(x for x in ("set-sec", "guard" if guard else "", cls) if x)
    return f"<section class='{classes}'><header>{head}</header>{rows}</section>"


def _settings_layout(sections: str, active: str, actions: str = "") -> str:
    """表单型设置页的外壳：分区导航 + 表单 + 浮出的改动条。"""
    return ("<div class='set-page'>"
            + _settings_head(True) + _settings_nav(active)
            + "<form class='settings-form'>" + sections
            + "<div class='set-empty' style='display:none'>没有匹配的设置项。</div>"
            + "<div class='set-bar'><span class='n'></span><span class='spacer'></span>"
            + "<span class='msg'></span>" + actions
            + "<button type='button' class='reset bar-reset'>放弃</button>"
            + "<button type='button' class='save'>保存改动</button></div>"
            + "</form></div>")


def _plain_settings_page(content: str, active: str) -> str:
    """非表单型 tab（连接管理 / SSH / 系统信息）：同一套页头与分区导航。

    比表单页宽：这几个 tab 装的是表格，不是需要控制行长的正文，
    920px 会把「地址」「跳板」这种两字表头挤到折行。
    """
    return (f"<div class='set-page wide'>{_settings_head(False)}"
            f"{_settings_nav(active)}{content}</div>")


def _num_setting(label: str, name: str, s: dict, default: object, hint: str,
                 unit: str = "", read: str = "", more: str = "") -> str:
    """数字设置。read=bytes/tokens 时前端把原始值换算成人话显示在输入框下面。"""
    val = _esc(str(s.get(name, default)))
    suffix = f"<span class='unit'>{_esc(unit)}</span>" if unit else ""
    ctl = (f"<input type='number' name='{name}' value='{val}' placeholder='{_esc(str(default))}'>"
           f"{suffix}<span class='read' data-kind='{read}'></span>")
    return _set_row(name, label, hint, ctl, s, more=more)


def _bool_setting(label: str, name: str, s: dict, default: bool,
                  on_text: str, off_text: str, hint: str, more: str = "") -> str:
    """开关。二元状态就该长得像二元状态——原来用 <select> 装「开/关」，
    既占一整个下拉的宽度，也要点开才知道当前是哪一档。

    值由隐藏 input 承载：未勾选的复选框不会进 FormData，而保存接口要显式的 true/false。
    """
    on = bool(s.get(name, default))
    ctl = (f"<input type='hidden' name='{name}' value='{'true' if on else 'false'}'>"
           f"<label class='sw'><input type='checkbox' data-field='{name}'"
           f"{' checked' if on else ''} data-on='{_esc(on_text)}' data-off='{_esc(off_text)}'>"
           f"<span class='track'></span><span class='state'></span></label>")
    return _set_row(name, label, hint, ctl, s, more=more)


def _select_setting(label: str, name: str, s: dict, default: str,
                    options: list[tuple[str, str]], hint: str, more: str = "") -> str:
    cur = str(s.get(name, default))
    opts = "".join(f"<option value='{v}'{' selected' if cur == v else ''}>{_esc(t)}</option>"
                   for v, t in options)
    return _set_row(name, label, hint, f"<select name='{name}'>{opts}</select>", s, more=more)


def _settings_general_body(s: dict) -> str:
    return _settings_layout(
        _set_section(
            "外观", "后台各页面的基础观感。查询台与 Redis 是独立的深色 IDE，主题在这里切。",
            _select_setting("界面主题", "theme", s, "dark",
                            [("dark", "深色（默认）"), ("light", "浅色")],
                            "作用于查询台与 Redis 控制台；后台其余页面始终是浅色。")
            + _num_setting("后台字号", "ui_font_size", s, 14,
                           "后台各页面的基础字号，10–20 之间。", unit="px"))
        + _set_section(
            "操作审计页", "打开审计页时的默认视图，随时可在页面上临时切换。",
            _bool_setting("自动刷新", "audit_auto_refresh", s, False,
                          "每 5 秒", "关闭",
                          "打开审计页时是否默认每 5 秒拉一次最新记录。")
            + _bool_setting("隐藏后台自身操作", "audit_hide_admin_ui", s, True,
                            "隐藏", "显示",
                            "查询台自己跑的 SQL 也会进审计（agent=admin-ui）。"
                            "默认隐藏，只看 agent 的操作。")),
        "general")


def _settings_db_body(s: dict) -> str:
    return _settings_layout(
        _set_section(
            "给 Agent 的护栏",
            "决定 agent 一次、一个会话最多能把多少数据拿进它的上下文。"
            "放松这些值不会有二次确认，改动会标上「已改」。",
            _num_setting("单次结果预算", "agent_max_result_chars", s, 40000,
                         "一次 query / sample_rows 最多返回多少字符，超出即截断并提示收窄。"
                         "单个连接可在连接管理里覆盖。",
                         unit="字符", read="tokens")
            + _num_setting("会话累计配额", "agent_session_budget_chars", s, 400000,
                           "一个会话累计返回多少字符后停止取数。撞到上限时 agent 必须先问你，"
                           "你同意后它才能追加额度。填 0 = 不限制。",
                           unit="字符", read="tokens",
                           more="单次预算管不住「一直查」——一次 1 万字符查两百次照样烧掉几十万 token，"
                                "而且这种情况多半是 agent 陷进了反复重拉同一份数据的循环。"
                                "追加额度的次数与理由显示在看板的「会话结果配额」里，"
                                "你可以核对它到底问没问过你。")
            + _bool_setting("敏感列自动脱敏", "mask_sensitive_columns", s, True,
                            "开启", "关闭",
                            "按内置词表（password / token / secret / id_card…）猜哪些列敏感，"
                            "命中即以 <code>***MASKED***</code> 返回给 agent。",
                            more="<b>只作用于 agent 的 query / sample_rows</b>——你自己在查询台看到的、"
                                 "导出文件里的，任何开关下都是真实值。单个连接可在连接管理里覆盖本开关；"
                                 "连接上手动点名的「脱敏列」始终脱敏，不受影响。")
            + _bool_setting("首次调用附带使用说明", "agent_guide_on_first_call", s, True,
                            "开启", "关闭",
                            "agent 每个会话第一次调用工具时，随结果附一份用法与最佳实践，"
                            "减少误用与无效重试。",
                            more="MCP 的 instructions 各客户端处理不一（截断、折叠、只在最外层放一次），"
                                 "实测 agent 常常读不到。改在它正要用工具时送达，一个会话只发一次。"
                                 "关掉后 agent 仍可主动调 <code>usage_guide</code> 读。"),
            guard=True)
        + _set_section(
            "表同步上限",
            "agent 用 sync_table 把线上表拉到本地时的两道闸门。它是取样本用的，不是迁移工具。",
            _num_setting("单次行数", "sync_max_rows", s, 10000,
                         "agent 传的 limit 会被夹到这个值以内。", unit="行")
            + _num_setting("单次体积", "sync_max_bytes", s, 64 * 1024 * 1024,
                           "累计到这里就停下，并在结果里说明是撞了体积而不是行数。",
                           unit="字节", read="bytes",
                           more="行数管不住「行很宽」的表——1 万行 BLOB 可能有几个 GB。"
                                "注意它保护的是本机内存与目标库：源库那边已经按 LIMIT 把行发过来了，"
                                "要减轻源库压力只能调小 limit 或收窄 where。"),
            guard=True)
        + _set_section(
            "查询台",
            "SQL 编辑器与结果表格的观感，改完重新打开查询台生效。",
            _num_setting("结果每页行数", "sql_page_size", s, 100,
                         "结果表格一页显示多少行。", unit="行")
            + _num_setting("编辑器字号", "sql_font_size", s, 13,
                           "SQL 编辑器的字号，10–24 之间。", unit="px")
            + _bool_setting("代码缩略图", "sql_minimap", s, True, "显示", "隐藏",
                            "编辑器右侧的 minimap，隐藏可让出更多编辑宽度。")
            + _bool_setting("自动换行", "sql_word_wrap", s, False, "开启", "关闭",
                            "超出宽度的长 SQL 是否折行显示。")
            + _num_setting("结果行上限", "sql_max_rows", s, 1000,
                           "缺 LIMIT 的查询自动兜底的行上限，也是非分页读取的截断上限。",
                           unit="行")
            + _num_setting("单元格字符上限", "sql_max_cell_chars", s, 4096,
                           "超长 TEXT / BLOB 单元格截断到多少字符。", unit="字符"))
        + _set_section(
            "审批与并发",
            "写操作等你审批多久，以及这台服务同时能跑多少条 SQL。",
            _num_setting("审批等待时长", "approval_wait_seconds", s, 120,
                         "agent 提交写操作后，服务端等你决策的秒数。你在审批页点批准，"
                         "它那边即刻自动执行，无需你回会话里说一声。填 0 = 不等待。",
                         unit="秒",
                         more="如果你的 MCP 客户端单次工具调用超时更短（Codex 的 "
                              "<code>tool_timeout_sec</code>、DeepSeek 的 "
                              "<code>toolCallTimeoutMs</code> 默认常是 60 秒），把这里调小，"
                              "agent 会用 <code>wait_for_change</code> 分多次续等，不会丢单。")
            + _num_setting("MCP 最大并发", "mcp_max_concurrency", s, 40,
                           "同时并行的阻塞 DB 调用数，决定「多少个不同连接能同时跑」。"
                           "10–500，改后即时生效。", unit="个")
            + _num_setting("单连接引擎池", "engine_pool_size", s, 15,
                           "单个引擎（连接 × 角色 × 库）的最大连接数，决定「同一连接上能并行"
                           "多少条 SQL」。5–100，改后回收旧引擎按新大小重建。", unit="条")),
        "db")


def _text_setting(label: str, name: str, s: dict, default: str, hint: str,
                  wide: bool = False, more: str = "") -> str:
    val = _esc(str(s.get(name, default)))
    ctl = f"<input type='text' name='{name}' value='{val}' placeholder='{_esc(str(default))}'>"
    return _set_row(name, label, hint, ctl, s, wide=wide, more=more)


def _ai_api_key_present() -> bool:
    """当前 keyring 里是否已存 AI API key（只判有无，不取值）。"""
    try:
        import keyring  # noqa: PLC0415
        from .ai import AI_API_KEY_ACCOUNT  # noqa: PLC0415
        from .secrets import KEYRING_SERVICE  # noqa: PLC0415
        return bool(keyring.get_password(KEYRING_SERVICE, AI_API_KEY_ACCOUNT))
    except Exception:  # noqa: BLE001
        return False


# provider 切换时按后端显隐 CLI / API 专属配置（CLI 组仅命令行后端显示，API 组仅 api 显示）
_AI_PROVIDER_TOGGLE_JS = """<script>
(function(){
  var sel=document.querySelector("select[name='ai_provider']"); if(!sel) return;
  function upd(){
    var api = sel.value==='api';
    document.querySelectorAll('.ai-api-only').forEach(function(e){e.style.display=api?'':'none';});
    document.querySelectorAll('.ai-cli-only').forEach(function(e){e.style.display=api?'none':'';});
  }
  sel.addEventListener('change', upd); upd();
})();
</script>"""


def _settings_ai_body(s: dict) -> str:
    # provider 的显隐由 _AI_PROVIDER_TOGGLE_JS 在前端做（切后端即时生效，不必回服务端）
    key_hint = ("已存储（留空 = 不改，填新值 = 覆盖）" if _ai_api_key_present() else "未存储")

    api_key_ctl = (
        "<input type='password' name='ai_api_key' value='' autocomplete='new-password' "
        "placeholder='填入以覆盖'>"
        "<label class='clearkey'><input type='checkbox' name='ai_api_key_clear' value='1'>"
        " 清除已存</label>")

    backend = _set_section(
        "AI 后端", "生成 SQL 与流程图的模型从哪来。产物只回填编辑器，绝不自动执行。",
        _bool_setting("启用 AI 辅助", "ai_enabled", s, True, "启用", "关闭",
                      "开启后查询台与流程画布上会出现「✨ AI」按钮。")
        + _select_setting("后端", "ai_provider", s, "claude",
                          [("claude", "Claude CLI（claude -p）"),
                           ("codex", "CodeX CLI（codex exec）"),
                           ("api", "HTTP API（直连）")],
                          "claude / codex 调本机已登录的命令行 AI；api 直连 HTTP 端点，"
                          "接入面更广、也更省 token。")
        + "<div class='ai-cli-only'>"
        + _text_setting("CLI 路径", "ai_cli_path", s, "",
                        "留空用后端默认的二进制名；不在 PATH 里就填绝对路径。")
        + "</div>"
        + _text_setting("模型", "ai_model", s, "claude-sonnet-5",
                        "Claude 用 claude-*；CodeX 用其账号支持的模型名；api 用对应厂商模型名。"))

    api = ("<div class='ai-api-only'>" + _set_section(
        "HTTP API", "直连模型厂商的端点。密钥存进系统钥匙串，绝不写入设置库或日志。",
        _select_setting("请求格式", "ai_api_format", s, "anthropic",
                        [("anthropic", "Anthropic Messages"),
                         ("openai", "OpenAI Chat Completions")],
                        "按你的端点选一种。")
        + _text_setting("根地址", "ai_api_base", s, "https://api.anthropic.com",
                        "如 https://api.anthropic.com 或 https://api.openai.com。")
        + _set_row("ai_api_key", "API Key", f"当前：<b>{key_hint}</b>。", api_key_ctl)
        + _text_setting("兜底环境变量名", "ai_api_key_env", s, "DBM_AI_API_KEY",
                        "钥匙串里没有时从这个环境变量读，值同样不入设置库。"))
        + "</div>")

    advanced = _set_section(
        "提示词与限额", "不常调；改坏了清空并保存即恢复默认。", folded=True, rows=
        _num_setting("生成超时", "ai_timeout_s", s, 60,
                     "单次生成最长等多久，10–600。", unit="秒")
        + _num_setting("最大表数", "ai_max_tables", s, 40,
                       "「整库」模式下最多把多少张表的结构发给 AI，超出会要求你勾选具体表。",
                       unit="张")
        + _set_row("ai_sql_prompt", "SQL 生成提示词",
                   "生成 SQL 时的系统提示：角色设定与 SQL 约束。",
                   f"<textarea name='ai_sql_prompt' rows='10'>{_esc(s.get('ai_sql_prompt', ''))}</textarea>",
                   s, wide=True)
        + _set_row("ai_workflow_prompt", "流程生成提示词",
                   "生成可视化流程（DAG 画布）时的系统提示。",
                   f"<textarea name='ai_workflow_prompt' rows='6'>{_esc(s.get('ai_workflow_prompt', ''))}</textarea>",
                   s, wide=True))

    return _settings_layout(backend + api + advanced, "ai") + _AI_PROVIDER_TOGGLE_JS


# 通知主渠道切换：按选中的 provider 显隐对应字段块
_NOTIFY_PROVIDER_TOGGLE_JS = """<script>
(function(){
  var radios=document.querySelectorAll("input[name='notify_primary']"); if(!radios.length) return;
  function upd(){
    var v='none';
    radios.forEach(function(r){ if(r.checked) v=r.value; });
    ['bark','wecom','feishu'].forEach(function(k){
      document.querySelectorAll('.notify-'+k).forEach(function(e){ e.style.display=(v===k?'':'none'); });
    });
  }
  radios.forEach(function(r){ r.addEventListener('change', upd); });
  upd();
})();
</script>"""


def _settings_notify_body(s: dict) -> str:
    """通知设置：外部主渠道单选（Bark / 企微 / 飞书）+ 可选 macOS 本地通知。

    后台内推（铃铛）恒开、不在这里出现开关；保留 7 天由 housekeeping 自动清。
    """
    primary = str(s.get("notify_primary") or "none").lower()

    def choice(v: str, title: str, hint: str) -> str:
        return (f"<label><input type='radio' name='notify_primary' value='{v}'"
                f"{' checked' if primary == v else ''}>"
                f"<span><span class='t'>{_esc(title)}</span>"
                f"<span class='h'>{_esc(hint)}</span></span></label>")

    macos_hint = ("把提醒发到 macOS 通知中心。仅本机进程模式有效。"
                  if _platform_is_macos() else
                  "当前不是 macOS 环境，开了也不会有效果——请改用上面的外部渠道。")

    channel = _set_section(
        "外部渠道",
        "后台铃铛里的站内通知始终开启、保留 7 天。这里选一个把重要提醒推到手机或群里的渠道。",
        _set_row("notify_primary", "推到哪里",
                 "只有审批单创建会触发通知——「安静即正常」，没有消息就是没有事等你处理。",
                 "", wide=True).replace(
            "<div class='ctl'></div>",
            "<div class='ctl'><div class='set-choice'>"
            + choice("none", "只在后台铃铛里", "不往外发")
            + choice("bark", "Bark", "iOS / macOS 推送，支持自建 server")
            + choice("wecom", "企业微信群机器人", "推到内部群")
            + choice("feishu", "飞书群机器人", "推到内部群")
            + "</div>"
            + "<div class='set-fields notify-bark'>"
            + _text_setting("Server URL", "notify_bark_server", s, "https://api.day.app",
                            "官方地址或你的自建 server，不含末尾斜杠。")
            + _text_setting("Device key", "notify_bark_key", s, "",
                            "Bark App 首页顶部那串 key。")
            + "</div>"
            + "<div class='set-fields notify-wecom'>"
            + _text_setting("Webhook", "notify_wecom_webhook", s, "",
                            "群设置 → 机器人 → 添加/管理 → 复制完整 URL。", wide=True)
            + "</div>"
            + "<div class='set-fields notify-feishu'>"
            + _text_setting("Webhook", "notify_feishu_webhook", s, "",
                            "群设置 → 机器人 → 添加自定义机器人 → 复制完整 URL。", wide=True)
            + "</div></div>")
        + _text_setting("通知里的跳转地址", "admin_base_url", s, "http://127.0.0.1:8100",
                        "通知里「前往处理」用的 URL 前缀。走反向代理或 Docker 时填对外地址，"
                        "本机运行保持默认即可。", wide=True)
        + _bool_setting("macOS 本地通知", "notify_macos_enabled", s, False,
                        "开启", "关闭", macos_hint))

    test_btn = "<button type='button' class='reset' id='notify-test'>发送测试</button>"
    return (_settings_layout(channel, "notify", actions=test_btn)
            + _NOTIFY_PROVIDER_TOGGLE_JS
            + """<script>
document.getElementById('notify-test')?.addEventListener('click', async function(){
  var m=document.querySelector('.set-bar .msg'), bar=document.querySelector('.set-bar');
  bar?.classList.add('on'); if(m)m.textContent='发送中…';
  try{
    var r=await fetch('/admin/notifications/test', {method:'POST'});
    var d=await r.json();
    if(m)m.textContent=d.ok?'已发送，看一眼铃铛和你选的渠道':'发送失败：'+d.error;
  }catch(err){ if(m)m.textContent='发送失败：'+err; }
});
</script>""")


def _platform_is_macos() -> bool:
    """给通知设置页判环境用（供文案切换），与 notify.is_macos 语义一致。"""
    from .notify import is_macos  # noqa: PLC0415
    return is_macos()


def _settings_redis_body(s: dict) -> str:
    return _settings_layout(
        _set_section(
            "键浏览", "左侧键树一次拉多少、拉多快。库大时把上限调小能显著加快首屏。",
            _num_setting("键列表加载上限", "redis_key_limit", s, 1000,
                         "键树一次 SCAN 最多加载多少个键。", unit="个")
            + _num_setting("SCAN 每批大小", "redis_scan_count", s, 500,
                           "每轮 SCAN 取多少，越大越快但单次更阻塞。50–10000。", unit="个")
            + _num_setting("库切换器最少列几个", "redis_min_dbs", s, 16,
                           "底部数据库切换器至少列出多少个逻辑库；有数据的库始终会出现。"
                           "1–256。", unit="个"))
        + _set_section(
            "值的展示", "键详情与命令结果怎么显示。",
            _num_setting("结果每页行数", "redis_page_size", s, 100,
                         "hash / list / set / zset 详情与命令结果的分页大小。", unit="行")
            + _bool_setting("msgpack 解码", "redis_msgpack_decode", s, True,
                            "开启", "关闭",
                            "非 UTF-8 的值先试着按 msgpack 解成结构展示，解不出再退回十六进制。")),
        "redis")


def _settings_info_body(service: "DbmService", req: "Request") -> str:
    """系统信息 tab：只读展示项目/数据/日志路径、运行时信息、登录 token 获取与更新指引。"""
    import os
    from pathlib import Path

    from .secrets import KEYRING_SERVICE
    try:
        from importlib.metadata import version as _pkgver
        ver = _pkgver("db-manage-mcp")
    except Exception:  # noqa: BLE001
        ver = "0.1.0"

    def _size(p: object) -> str | None:
        try:
            if p and os.path.isfile(str(p)):
                n = float(os.path.getsize(str(p)))
                for unit in ("B", "KB", "MB", "GB"):
                    if n < 1024 or unit == "GB":
                        return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                    n /= 1024
        except Exception:  # noqa: BLE001
            pass
        return None

    def ap(p: object) -> str:
        try:
            return str(Path(str(p)).resolve()) if p else "（未配置）"
        except Exception:  # noqa: BLE001
            return str(p)

    def row(label: str, value: str, note: str = "") -> str:
        cp = (f"<button class='ic-copy' data-copy='{_esc(value)}' title='复制'>⧉</button>"
              if value and value != "（未配置）" else "")
        nt = f"<span class='muted' style='margin-left:8px'>{_esc(note)}</span>" if note else ""
        return (f"<tr><td class='ik'>{_esc(label)}</td>"
                f"<td class='iv'><code>{_esc(value)}</code>{cp}{nt}</td></tr>")

    analysis_root = getattr(getattr(service, "analysis", None), "root", None)
    data_dir = analysis_root.parent if analysis_root is not None else None
    db_path = (data_dir / "dbm.sqlite3") if data_dir is not None else None
    config_path = getattr(service, "config_path", None)
    env_file = os.environ.get("DBM_ENV_FILE") or os.path.expanduser("~/.config/db-manage-mcp/env")
    log_path = os.path.expanduser("~/Library/Logs/db-manage-mcp.log")
    host = req.url.hostname or "127.0.0.1"
    port = req.url.port or 8100

    ws_note = ""
    if analysis_root is not None and Path(str(analysis_root)).exists():
        ws_note = f"{len(list(Path(str(analysis_root)).glob('*.duckdb')))} 个工作区"
    db_note = ("存在 · " + (_size(db_path) or "")) if db_path and os.path.isfile(str(db_path)) else "尚未创建"

    paths = "".join([
        row("项目工作目录", os.getcwd()),
        row("配置文件（连接/账密引用）", ap(config_path)),
        row("SQLite 库（审计/审批/设置/片段/workflow）", ap(db_path), db_note),
        row("分析工作区目录（DuckDB）", ap(analysis_root) if analysis_root else "（未启用）", ws_note),
        row("密钥 env 文件", env_file, "存在" if os.path.isfile(env_file) else "不存在（或用环境变量注入）"),
        row("launchd 日志", log_path, "存在" if os.path.isfile(log_path) else "非 launchd 则输出到 stdout"),
    ])
    runtime = "".join([
        row("监听地址", f"{host}:{port}"),
        row("MCP 端点（给 agent）", f"http://{host}:{port}/mcp"),
        row("keyring 服务名（密码存储处）", KEYRING_SERVICE),
        row("版本", ver),
    ])
    token = (
        "<div class='card'><h3>登录 Token（获取与更新）</h3>"
        "<p class='muted'>后台登录用的 <code>DBM_ADMIN_TOKEN</code>。出于安全，本页<b>不显示明文</b>。</p>"
        f"<table class='info-tbl'>{row('存储位置', env_file, '文件中的 DBM_ADMIN_TOKEN=…，或由环境变量注入')}</table>"
        "<p style='margin:14px 0 4px'><b>查看当前 token</b></p>"
        f"<pre class='cmd'>grep DBM_ADMIN_TOKEN {_esc(env_file)}</pre>"
        "<p style='margin:14px 0 4px'><b>更新 token</b>（改完热重载，旧 cookie 失效需重新登录）</p>"
        "<pre class='cmd'># 编辑 env 文件把 DBM_ADMIN_TOKEN 改成新值，然后热重载（幂等）：\n"
        "bash scripts/install-launchd.sh</pre>"
        "<p class='muted' style='margin-top:10px'>想让服务重新随机生成：删掉该行再跑上面命令，"
        f"新 token 会打印在日志里（<code>tail -f {_esc(log_path)}</code>）。</p></div>"
    )
    css = ("<style>"
           ".info-tbl{width:100%;border-collapse:collapse}"
           ".info-tbl td{padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}"
           ".info-tbl tr:last-child td{border-bottom:none}"
           ".info-tbl td.ik{color:var(--muted);white-space:nowrap;width:290px}"
           ".info-tbl td.iv code{background:var(--paper);padding:2px 6px;border-radius:5px;word-break:break-all}"
           ".ic-copy{margin-left:8px;background:none;border:1px solid var(--border);color:var(--faint);"
           "border-radius:5px;cursor:pointer;padding:1px 6px;font-size:12px}"
           ".ic-copy:hover{color:var(--accent-ink);border-color:var(--accent)}"
           "pre.cmd{background:var(--ink);color:#d7dde6;border-radius:8px;padding:10px 12px;overflow-x:auto;"
           "font-family:var(--mono);font-size:12.5px;margin:0}"
           "</style>")
    script = ("<script>document.querySelectorAll('.ic-copy').forEach(function(b){"
              "b.addEventListener('click',function(){var t=b.getAttribute('data-copy');"
              "if(navigator.clipboard){navigator.clipboard.writeText(t);var o=b.textContent;"
              "b.textContent='✓';setTimeout(function(){b.textContent=o},1200);}});});</script>")
    return (css
            + f"<div class='card'><h3>路径</h3><table class='info-tbl'>{paths}</table></div>"
            + f"<div class='card'><h3>运行时</h3><table class='info-tbl'>{runtime}</table></div>"
            + token + script)


def _connections_body(service: "DbmService", editing: str | None) -> str:
    """连接管理：连接列表 + 新增/编辑面板。

    列表按项目分组，每行给出「这条连接能做什么」——有没有 writer（能不能写）、
    走不走跳板、有没有脱敏。这些原来要点进编辑面板才看得到，而它们恰恰决定了
    这条连接的风险面。
    """
    edit_cfg = None
    e_project = e_conn = ""
    if editing and "/" in editing:
        e_project, e_conn = editing.split("/", 1)
        proj = service.config.projects.get(e_project)
        edit_cfg = proj.connections.get(e_conn) if proj else None

    # 环境 → 项目 → 连接三层，同在一张表里（分表的话各表列宽各算各的，扫视时没有一条
    # 竖线可依）。环境在最外层是因为它决定风险：prod 排最前，你最该先看见的就是它们。
    # 环境成了分组标题，行里就不必再重复一个环境徽章。
    ENV_ORDER = ("prod", "staging", "dev", "local")
    tree: dict[str, dict[str, list]] = {}
    for pname, proj in service.config.projects.items():
        for cname, c in proj.connections.items():
            tree.setdefault(c.environment or "—", {}).setdefault(pname, []).append((cname, c))

    def env_key(env: str) -> tuple:
        return (ENV_ORDER.index(env), "") if env in ENV_ORDER else (len(ENV_ORDER), env)

    body = []
    for env in sorted(tree, key=env_key):
        projects = tree[env]
        n = sum(len(v) for v in projects.values())
        body.append(
            f"<tr class='env-row' style='--env:{_ENV_COLOR.get(env, '#64748b')}'>"
            f"<td colspan='4'>{_env_badge(env)}<span class='n'>{n} 条连接</span></td></tr>")
        for pname in sorted(projects):
            body.append(f"<tr class='proj-row'><td colspan='4'>{_esc(pname)}</td></tr>")
            for cname, c in sorted(projects[pname]):
                where = (_esc(c.database) if c.engine == "sqlite"
                         else f"{_esc(c.host)}:{_esc(c.port)}"
                              + (f" · {_esc(c.database)}" if c.database else ""))
                caps = []
                if c.writer is not None:
                    caps.append("<span class='cap cap-w' title='配了 writer 账号，"
                                "审批通过的写操作用它执行'>可写</span>")
                else:
                    caps.append("<span class='cap' title='没有 writer 账号，"
                                "这条连接只能读'>只读</span>")
                if c.jump_hosts:
                    hops = " → ".join(h.label() for h in c.jump_hosts)
                    caps.append(f"<span class='cap cap-ssh' title='{_esc(hops)}'>"
                                f"{len(c.jump_hosts)} 跳</span>")
                if c.policy.mask_columns:
                    cols = "、".join(c.policy.mask_columns)
                    caps.append(f"<span class='cap' title='{_esc(cols)}'>"
                                f"脱敏 {len(c.policy.mask_columns)} 列</span>")
                edit_url = (f"/admin/settings?tab=connections"
                            f"&edit={_esc(pname)}/{_esc(cname)}")
                body.append(
                    "<tr class='conn-row'>"
                    f"<td><a class='conn-name' href='{edit_url}'>{_esc(cname)}</a>"
                    f"<div class='conn-where mono muted' title='{where}'>{where}</div></td>"
                    f"<td class='eng'>{_engine_icon(c.engine)}"
                    f"<span class='mono muted'>{_esc(c.engine)}</span></td>"
                    f"<td class='caps'>{''.join(caps)}</td>"
                    "<td class='acts'>"
                    f"<a class='btn btn-ghost btn-sm' href='{edit_url}'>编辑</a>"
                    "<form method='post' action='/admin/connections/delete' "
                    "onsubmit='return dbmConfirm(this)'>"
                    f"<input type='hidden' name='project' value='{_esc(pname)}'>"
                    f"<input type='hidden' name='connection' value='{_esc(cname)}'>"
                    "<button class='btn btn-reject btn-sm'>删除</button></form>"
                    "</td></tr>"
                )
    groups = ["<div class='tablewrap'><table class='conn-tbl'>"
              "<colgroup><col><col style='width:120px'>"
              "<col style='width:210px'><col style='width:150px'></colgroup>"
              "<thead><tr><th>连接</th><th>引擎</th><th>能力</th><th></th></tr>"
              f"</thead><tbody>{''.join(body)}</tbody></table></div>"] if body else []

    listing = "".join(groups) or (
        "<div class='set-empty'>还没有任何连接。点右上角「新增连接」，"
        "填好后可以先「测试连接」再保存。</div>")
    keyring_note = "" if _keyring_available() else (
        "<div class='errbar' style='margin-bottom:14px'>未安装 keyring，密码无法安全存储。"
        "请先 <code>pip install 'db-manage-mcp[keyring]'</code> 再重启服务。</div>")

    form = _connection_form(e_project, e_conn, edit_cfg, sorted(service.config.ssh_identities))
    back = "/admin/settings?tab=connections"
    auto_open = "document.getElementById('conn-modal').classList.add('open');" if edit_cfg else ""
    title = f"编辑 {_esc(e_project)}/{_esc(e_conn)}" if edit_cfg else "新增连接"
    return (
        f"{keyring_note}"
        "<section class='set-sec conn-sec'><header class='conn-hd'>"
        "<div><h3>连接</h3><p>agent 与查询台能连的库都在这里。密码写进系统钥匙串，"
        "配置文件只存引用。</p></div>"
        "<button class='btn btn-primary' "
        "onclick=\"document.getElementById('conn-modal').classList.add('open')\">"
        "＋ 新增连接</button>"
        f"</header>{listing}</section>"
        f"<div class='modalbg' id='conn-modal'><div class='modalbox conn-modal'>"
        f"<div class='conn-modal-hd'><h2>{title}</h2>"
        f"<button class='mclose' onclick=\"document.getElementById('conn-modal')"
        f".classList.remove('open');"
        f"if(location.search.indexOf('edit=')>=0)location.href='{back}'\">✕</button></div>"
        f"<div class='conn-modal-body'>{form}</div></div></div>"
        f"<script>{auto_open}"
        "document.getElementById('conn-modal').addEventListener('click',function(e){"
        "if(e.target===this){this.classList.remove('open');"
        f"if(location.search.indexOf('edit=')>=0)location.href='{back}';}}}});</script>"
    )


def _ssh_identities_body(service: "DbmService") -> str:
    """SSH 配置库（系统设置『SSH 配置』tab）：列表 + 新增表单。只存路径引用。

    一条 SSH 配置 = 主机/用户/端口/私钥/known_hosts。连接的跳板可直接引用一条配置，
    跳板处不必再重复填主机等（跳板留空即继承本配置）。
    """
    from .connections import identity_referers

    idents = service.config.ssh_identities
    rows = []
    for name in sorted(idents):
        ident = idents[name]
        refs = identity_referers(service.config, name)
        ref_txt = "、".join(refs) if refs else "<span class='muted'>—</span>"
        kh = f"<code>{_esc(ident.known_hosts_path)}</code>" if ident.known_hosts_path \
            else "<span class='muted'>—</span>"
        target = ident.host or ""
        if target and ident.user:
            target = f"{ident.user}@{target}"
        if target and ident.port:
            target += f":{ident.port}"
        target_html = f"<code>{_esc(target)}</code>" if target else "<span class='muted'>—</span>"
        if refs:
            del_btn = ("<button class='btn btn-ghost' disabled "
                       "style='padding:3px 11px;font-size:12.5px' "
                       "title='被连接引用，不能删除'>删除</button>")
        else:
            del_btn = (
                "<form method='post' action='/admin/ssh-identities/delete' style='display:inline' "
                "onsubmit='return dbmConfirm(this)'>"
                f"<input type='hidden' name='name' value='{_esc(name)}'>"
                "<button class='btn btn-reject' style='padding:3px 11px;font-size:12.5px'>删除</button></form>")
        rows.append(
            f"<tr><td><code>{_esc(name)}</code></td>"
            f"<td class='mono'>{target_html}</td>"
            f"<td class='mono'><code>{_esc(ident.key_path)}</code></td>"
            f"<td class='mono'>{kh}</td><td class='muted mono'>{ref_txt}</td>"
            f"<td style='white-space:nowrap'>{del_btn}</td></tr>"
        )
    table = "".join(rows) or '<tr><td colspan="6" class="muted">（无配置）</td></tr>'
    return (
        "<div class='card'><h2 style='margin-top:0'>SSH 配置库</h2>"
        "<p class='muted' style='margin-top:-4px'>可复用的 SSH 配置（主机/用户/端口/私钥/known_hosts）。"
        "新建连接的跳板可直接引用，跳板处不必再重复填主机。只保存<b>路径引用</b>，绝不读取或存储密钥内容。</p>"
        "<div class='tablewrap'><table><tr><th>名字</th><th>主机（user@host:port）</th><th>私钥路径</th>"
        "<th>known_hosts</th><th>被引用</th><th>操作</th></tr>"
        f"{table}</table></div></div>"
        "<div class='card' style='max-width:620px'><h2 style='margin-top:0'>新增 / 覆盖配置</h2>"
        "<form method='post' action='/admin/ssh-identities/save'>"
        "<div class='row'>"
        f"<div>{_field('名字', 'name', ph='prod-bastion', width='200px')}</div>"
        f"<div>{_field('主机 host（可选）', 'host', ph='bastion.example.com', width='320px')}</div>"
        "</div><div class='row'>"
        f"<div>{_field('用户 user（可选）', 'user', ph='ops', width='150px')}</div>"
        f"<div>{_field('端口 port（可选）', 'port', ph='22', typ='number', width='110px')}</div>"
        "</div><div class='row'>"
        f"<div>{_field('私钥路径', 'key_path', ph='~/.ssh/prod_key', width='320px')}</div>"
        "</div><div class='row'>"
        f"<div>{_field('known_hosts 路径（可选）', 'known_hosts_path', ph='~/.ssh/known_hosts_prod', width='320px')}</div>"
        "</div>"
        "<div class='muted' style='margin:4px 0 10px'>同名覆盖即更新。主机/用户/端口留空则由跳板处填写。"
        "私钥需权限≤600，否则 ssh 会拒绝。</div>"
        "<button class='btn btn-primary' type='submit'>保存配置</button></form></div>"
    )


def mount_admin(mcp: "FastMCP", service: "DbmService", admin_token: str,
                *, no_auth: bool = False) -> None:
    """挂载管理后台。no_auth=True 时跳过认证——仅供本机测试脚手架，绝不用于生产。"""
    expected_cookie = _session_value(admin_token)

    def guard(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        """未认证访问受保护路由 → 重定向登录页；非本机来源（Host/Origin 不符）→ 403。

        no_auth 模式跳过认证检查（本机测试专用）；Host/Origin 校验仍保留。
        """
        @wraps(handler)
        async def _wrapped(req: Request) -> Response:
            if not _local_request_ok(req):
                if _wants_json(req):
                    return JSONResponse(
                        {"ok": False, "error": "请求被拒绝：管理后台只能从本机访问"},
                        status_code=403)
                return Response("forbidden: request must originate from localhost",
                                status_code=403)
            if not no_auth and not _authed(req, expected_cookie):
                # 前端 fetch 期望 JSON → 返回 401 JSON（前端据此提示重新登录）；
                # 浏览器页面导航 → 仍 303 到登录页。
                if _wants_json(req):
                    return JSONResponse(
                        {"ok": False, "error": "登录已过期，请刷新页面重新登录"},
                        status_code=401)
                return RedirectResponse(url="/admin/login", status_code=303)
            return await handler(req)
        return _wrapped

    def _shell(title: str, body: str, doc: bool = True,
               extra_head: str = "") -> HTMLResponse:
        """渲染登录后的页面，自动注入待审批数（侧栏角标 + 顶部横幅）。"""
        try:
            # 惰性过期：存储态还是 pending 但已过 TTL 的单不该继续闪红点
            pending = len([c for c in service.list_changes("pending")
                           if c.effective_status() == "pending"])
        except Exception:
            pending = 0
        fs = None
        if doc:
            try:
                fs = int(service.get_settings().get("ui_font_size") or 14)
            except Exception:
                fs = None
        return HTMLResponse(_page(title, body, pending=pending, doc=doc, font_size=fs,
                                  extra_head=extra_head))

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
            return HTMLResponse(_page("审批单", f"<div class='card'>{_esc(e)}</div>"), status_code=404)

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
  <dt>连接</dt><dd><code>{_esc(c.project)}/{_esc(c.connection)}</code> · {_env_badge(c.environment)}</dd>
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
                      f"<p><a href='/admin/approvals/{change_id}'>← 返回审批单</a></p></div>"),
                status_code=500,
            )
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

    def _caller(req: Request) -> "CallerInfo":
        from .service import CallerInfo
        return CallerInfo(agent="admin-ui", session_id=req.cookies.get(_COOKIE_NAME, "")[:12])

    @mcp.custom_route("/admin/connections", methods=["GET"])
    @guard
    async def _connections(req: Request) -> Response:
        # 连接管理已并入系统设置的『连接管理』tab；保留旧地址做重定向（含编辑参数）
        edit = req.query_params.get("edit")
        url = "/admin/settings?tab=connections" + (f"&edit={edit}" if edit else "")
        return RedirectResponse(url=url, status_code=303)

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
                jump_hosts=_parse_hop_rows(f),
                ssh_options_extra=_list("ssh_options_extra", " "),
                max_rows=int(str(f.get("max_rows") or "500")),
                mask_columns=_list("mask_columns", ","),
                mask_default_patterns=(
                    None if str(f.get("mask_default_patterns") or "") == ""
                    else str(f.get("mask_default_patterns")) in ("1", "on", "true")),
                force_privileged=str(f.get("force_privileged") or "") in ("1", "on", "true"),
                statement_timeout_s=int(str(f.get("statement_timeout_s") or "30")),
                write_timeout_s=int(str(f.get("write_timeout_s") or "600")),
            )
        except (ConnectionAdminError, QueryRejected, ValueError) as e:
            # 前端用 fetch 提交（Accept: json）→ 返回 JSON，页面 inline 提示、不清空表单
            if "application/json" in req.headers.get("accept", ""):
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            body = f"<div class='card'><h2>保存失败</h2><p style='color:#b00020'>{_esc(e)}</p>" \
                   f"<a href='/admin/settings?tab=connections'>← 返回</a></div>"
            return HTMLResponse(_page("保存失败", body), status_code=400)
        if "application/json" in req.headers.get("accept", ""):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/admin/settings?tab=connections", status_code=303)

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
        return RedirectResponse(url="/admin/settings?tab=connections", status_code=303)

    @mcp.custom_route("/admin/ssh-identities/save", methods=["POST"])
    @guard
    async def _ssh_identity_save(req: Request) -> Response:
        from .connections import ConnectionAdminError
        f = await req.form()
        try:
            service.upsert_ssh_identity(
                str(f.get("name") or "").strip(),
                str(f.get("key_path") or "").strip(),
                str(f.get("known_hosts_path") or "").strip() or None,
                _caller(req),
                host=str(f.get("host") or "").strip() or None,
                user=str(f.get("user") or "").strip() or None,
                port=str(f.get("port") or "").strip() or None,
            )
        except ConnectionAdminError as e:
            body = (f"<div class='card'><h2>保存失败</h2><p style='color:#b00020'>{_esc(e)}</p>"
                    "<a href='/admin/settings?tab=ssh'>← 返回</a></div>")
            return HTMLResponse(_page("保存失败", body), status_code=400)
        return RedirectResponse(url="/admin/settings?tab=ssh", status_code=303)

    @mcp.custom_route("/admin/ssh-identities/delete", methods=["POST"])
    @guard
    async def _ssh_identity_delete(req: Request) -> Response:
        from .connections import ConnectionAdminError
        f = await req.form()
        try:
            service.delete_ssh_identity(str(f.get("name") or "").strip(), _caller(req))
        except ConnectionAdminError as e:
            body = (f"<div class='card'><h2>删除失败</h2><p style='color:#b00020'>{_esc(e)}</p>"
                    "<a href='/admin/settings?tab=ssh'>← 返回</a></div>")
            return HTMLResponse(_page("删除失败", body), status_code=400)
        return RedirectResponse(url="/admin/settings?tab=ssh", status_code=303)

    def _form_fields(f) -> dict:  # noqa: ANN001
        port_raw = str(f.get("port") or "").strip()
        return {
            "engine": str(f.get("engine") or "").strip(),
            "environment": str(f.get("environment") or "dev").strip(),
            "host": str(f.get("host") or "").strip() or None,
            "port": int(port_raw) if port_raw else None,
            "database": str(f.get("database") or "").strip() or None,
            "user": str(f.get("user") or "").strip() or None,
            "password": str(f.get("password") or "") or None,
            "jump_hosts": _parse_hop_rows(f),
            "ssh_options": [x for x in str(f.get("ssh_options_extra") or "").split(" ") if x],
            "max_rows": int(str(f.get("max_rows") or "500")),
        }

    def _existing_password(project: str, connection: str) -> str | None:
        proj = service.config.projects.get(project)
        c = proj.connections.get(connection) if proj else None
        return c.password if c else None

    @mcp.custom_route("/admin/connections/test", methods=["POST"])
    @guard
    async def _connection_test(req: Request) -> Response:
        f = await req.form()
        fields = _form_fields(f)
        # 编辑时密码留空 → 用已存的引用测
        existing_pw = None
        if not fields["password"]:
            existing_pw = _existing_password(str(f.get("project") or ""), str(f.get("connection") or ""))
        res = service.probe_connection_fields(fields, existing_password=existing_pw)
        detail = []
        if res.version:
            detail.append(f"版本 {res.version}")
        if res.has_write is not None:
            if res.privileged:
                bits = []
                if res.is_superuser:
                    bits.append("超级用户/root")
                if res.has_write:
                    bits.append("有写权限")
                detail.append("⚠ 账号" + "、".join(bits) + "（只读连接不应使用）")
            else:
                detail.append("✓ 账号为最小权限只读账号")
        return JSONResponse({"ok": res.ok, "message": res.message,
                             "detail": " · ".join(detail), "privileged": res.privileged})

    @mcp.custom_route("/admin/connections/test-ssh", methods=["POST"])
    @guard
    async def _connection_test_ssh(req: Request) -> Response:
        f = await req.form()
        res = service.probe_ssh_fields(_form_fields(f))
        return JSONResponse({"ok": res.ok, "message": res.message})

    # ---------- 查询台（DataGrip 风格：元信息浏览 + SQL 执行 + 导出）----------

    def _analysis_ws(raw: str) -> str | None:
        """conn 形如 "analysis/<workspace>" 时返回工作区名（查询台把工作区当连接用）。"""
        return raw.split("/", 1)[1] if raw.startswith("analysis/") else None

    def _resolve_conn(raw: str) -> tuple[str, str]:
        """解析 "project/connection"，校验存在，返回 (project, connection)。"""
        if not raw or "/" not in raw:
            raise KeyError("请选择连接")
        project, connection = raw.split("/", 1)
        service.config.get_connection(project, connection)  # 不存在会抛
        return project, connection

    _STATIC_ROOT = (Path(__file__).parent / "static").resolve()
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

    @mcp.custom_route("/admin/sql", methods=["GET"])
    @guard
    async def _sql_console(_req: Request) -> HTMLResponse:
        return _shell("查询台", _console_body(), doc=False)

    @mcp.custom_route("/admin/sql/connections", methods=["GET"])
    @guard
    async def _sql_connections(_req: Request) -> JSONResponse:
        conns = []
        for pname, proj in sorted(service.config.projects.items()):
            for cname, c in sorted(proj.connections.items()):
                conns.append({
                    "value": f"{pname}/{cname}", "project": pname, "connection": cname,
                    "engine": c.engine, "environment": c.environment or "",
                    "database": c.database or "",
                })
        workspaces = []
        if service.analysis is not None:
            try:
                workspaces = [w["workspace"] for w in service.analysis.list_workspaces()]
            except Exception:
                workspaces = []
        return JSONResponse({"ok": True, "connections": conns, "workspaces": workspaces,
                             "ai_enabled": bool(service.get_settings().get("ai_enabled"))})

    @mcp.custom_route("/admin/sql/databases", methods=["GET"])
    @guard
    async def _sql_databases(req: Request) -> JSONResponse:
        """列 schema（MySQL 下即库）。PG 带 db= 时列的是该 database 里的 schema。"""
        db = req.query_params.get("db") or None
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            dbs = await anyio.to_thread.run_sync(
                service.list_databases, project, connection, _caller(req), db)
        except Exception as e:
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, "databases": dbs})

    @mcp.custom_route("/admin/sql/server_databases", methods=["GET"])
    @guard
    async def _sql_server_databases(req: Request) -> JSONResponse:
        """列服务器上的 database。PG 才有意义（库与 schema 是两层）；其它引擎等价于上面那个。"""
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            dbs = await anyio.to_thread.run_sync(
                service.list_server_databases, project, connection, _caller(req))
        except Exception as e:
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, "databases": dbs})

    @mcp.custom_route("/admin/sql/tables", methods=["GET"])
    @guard
    async def _sql_tables(req: Request) -> JSONResponse:
        schema = req.query_params.get("schema") or None
        ws = _analysis_ws(req.query_params.get("conn", ""))
        if ws:
            try:
                datasets = await anyio.to_thread.run_sync(service.analysis.list_datasets, ws)
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True,
                                 "tables": [d["name"] for d in datasets],
                                 "sizes": {}})
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            tables = await anyio.to_thread.run_sync(
                service.list_tables, project, connection, _caller(req), schema,
                req.query_params.get("db") or None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        try:  # 表容量（树右侧分级展示）：拿不到不阻断表列表
            sizes = await anyio.to_thread.run_sync(
                service.admin_table_sizes, project, connection, _caller(req), schema,
                req.query_params.get("db") or None)
        except Exception:
            sizes = {}
        return JSONResponse({"ok": True, "tables": tables, "sizes": sizes})

    @mcp.custom_route("/admin/sql/table", methods=["GET"])
    @guard
    async def _sql_table(req: Request) -> JSONResponse:
        schema = req.query_params.get("schema") or None
        ws = _analysis_ws(req.query_params.get("conn", ""))
        if ws:
            try:
                info = await anyio.to_thread.run_sync(
                    service.analysis.describe_dataset, ws, req.query_params.get("table", ""))
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True, **info})
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            table = req.query_params.get("table", "")
            info = await anyio.to_thread.run_sync(
                service.describe_table, project, connection, table, _caller(req), schema,
                req.query_params.get("db") or None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **info})

    # ---------- Redis 浏览 / 命令窗口（对标 Medis）----------

    def _db_param(raw: str | None) -> int | None:
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            return None

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
        from .redis_docs import lookup
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
        from .privileges import SUPPORTED_ENGINES
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

    # ---------- 系统设置 ----------

    @mcp.custom_route("/admin/settings/get", methods=["GET"])
    @guard
    async def _settings_get(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "settings": service.get_settings()})

    @mcp.custom_route("/admin/settings/save", methods=["POST"])
    @guard
    async def _settings_save(req: Request) -> JSONResponse:
        f = await req.form()
        # 白名单即全部已知设置项；SettingsStore.save 还会再忽略未知键并夹取区间
        from .settings import DEFAULTS as _SETTING_DEFAULTS
        updates = {key: str(f.get(key)) for key in _SETTING_DEFAULTS if key in f}
        # AI API key 单独处理：存钥匙串、绝不入设置库；勾选清除则删除
        from .ai import AI_API_KEY_ACCOUNT
        from .secrets import (KEYRING_SERVICE, SecretResolveError,
                              delete_keyring_secret, store_keyring_secret)
        key_val = str(f.get("ai_api_key") or "").strip()
        clear_key = str(f.get("ai_api_key_clear") or "") in ("1", "on", "true")
        try:
            settings = service.save_settings(updates)
            if clear_key:
                delete_keyring_secret(f"keyring://{KEYRING_SERVICE}/{AI_API_KEY_ACCOUNT}")
            elif key_val:
                store_keyring_secret(AI_API_KEY_ACCOUNT, key_val)
        except SecretResolveError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "settings": settings})

    # ---------- 通知（站内铃铛 + SSE + 测试发送）----------

    def _get_with_timeout(q, timeout: float):  # noqa: ANN001
        """SSE 用：把 queue.get(timeout) 交给线程池（run_sync 只接受位置参数）。"""
        return q.get(True, timeout)

    @mcp.custom_route("/admin/notifications/unread_count", methods=["GET"])
    @guard
    async def _notify_unread(_req: Request) -> JSONResponse:
        if service.inbox is None:
            return JSONResponse({"ok": True, "count": 0})
        return JSONResponse({"ok": True, "count": service.inbox.unread_count()})

    @mcp.custom_route("/admin/notifications/list", methods=["GET"])
    @guard
    async def _notify_list(req: Request) -> JSONResponse:
        if service.inbox is None:
            return JSONResponse({"ok": True, "items": []})
        try:
            limit = min(int(req.query_params.get("limit") or 20), 200)
        except ValueError:
            limit = 20
        unread_only = req.query_params.get("unread") in ("1", "true", "yes")
        items = [n.to_dict() for n in service.inbox.list_recent(limit=limit, unread_only=unread_only)]
        return JSONResponse({"ok": True, "items": items})

    @mcp.custom_route("/admin/notifications/mark_read", methods=["POST"])
    @guard
    async def _notify_mark_read(req: Request) -> JSONResponse:
        if service.inbox is None:
            return JSONResponse({"ok": True, "updated": 0})
        f = await req.form()
        ids_raw = str(f.get("ids") or "").strip()
        all_flag = str(f.get("all") or "") in ("1", "true", "yes", "on")
        if all_flag:
            n = service.inbox.mark_all_read()
        else:
            ids: list[int] = []
            for part in ids_raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    ids.append(int(part))
                except ValueError:
                    continue
            n = service.inbox.mark_read(ids)
        return JSONResponse({"ok": True, "updated": n})

    @mcp.custom_route("/admin/notifications/test", methods=["POST"])
    @guard
    async def _notify_test(_req: Request) -> JSONResponse:
        try:
            service.notifier.send(
                title="Quay 测试通知",
                body="如果你收到了，说明当前配置的通知渠道工作正常。",
                meta={"kind": "test"},
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True})

    @mcp.custom_route("/admin/notifications/stream", methods=["GET"])
    @guard
    async def _notify_stream(req: Request) -> StreamingResponse:
        """SSE 流：每条新通知实时推给已登录的后台页面。

        用 anyio.to_thread 把阻塞的 queue.get 挪到线程池，避免霸占 event loop。
        客户端断连时 anyio 抛 CancelledError，我们清订阅退出。
        """
        if service.inbox is None:
            async def _empty():
                yield b": inbox disabled\n\n"
            return StreamingResponse(_empty(), media_type="text/event-stream")

        inbox = service.inbox
        q = inbox.subscribe()

        async def _gen():
            import json as _json  # noqa: PLC0415
            import queue as _q  # noqa: PLC0415
            try:
                # 打招呼：让客户端立即知道连接成功
                yield b": ok\n\n"
                while True:
                    if await req.is_disconnected():
                        return
                    # 等下一条（最多 25s），到点发心跳；断连由 is_disconnected 兜底
                    try:
                        n = await anyio.to_thread.run_sync(_get_with_timeout, q, 25)
                    except _q.Empty:
                        yield b": ping\n\n"
                        continue
                    if n is None:  # close sentinel
                        return
                    payload = _json.dumps(n.to_dict(), ensure_ascii=False)
                    yield f"event: notification\ndata: {payload}\n\n".encode("utf-8")
            finally:
                inbox.unsubscribe(q)

        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @mcp.custom_route("/admin/settings", methods=["GET"])
    @guard
    async def _settings_page(req: Request) -> HTMLResponse:
        tab = req.query_params.get("tab") or "general"
        s = service.get_settings()
        # 表单型 tab 自带页头/导航/改动条；非表单型（连接、SSH、系统信息）套同一个外壳
        forms = {
            "db": lambda: _settings_db_body(s),
            "redis": lambda: _settings_redis_body(s),
            "ai": lambda: _settings_ai_body(s),
            "notify": lambda: _settings_notify_body(s),
            "general": lambda: _settings_general_body(s),
        }
        plain = {
            "connections": lambda: _connections_body(service, req.query_params.get("edit")),
            "ssh": lambda: _ssh_identities_body(service),
            "info": lambda: _settings_info_body(service, req),
        }
        if tab in plain:
            body = _plain_settings_page(plain[tab](), tab)
        else:
            tab = tab if tab in forms else "general"
            body = forms[tab]()
        head = ('<link rel="stylesheet" href="/admin/static/settings.css">'
                '<script defer src="/admin/static/settings.js"></script>')
        return _shell("系统设置", body, extra_head=head)

    @mcp.custom_route("/admin/workflows/list", methods=["GET"])
    @guard
    async def _wf_list(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "workflows": service.workflow_list()})

    @mcp.custom_route("/admin/workflows", methods=["GET"])
    @guard
    async def _wf_page(_req: Request) -> HTMLResponse:
        """流程独立页——Vue 挂载点，SPA 内部路由（列表页 / 详情页）由 workflows.js 判断。"""
        body = ('<div id="wf-app"></div>'
                '<link rel="stylesheet" href="/admin/static/workflows.css">'
                # echarts UMD 必须先于任何 AMD loader 加载才会挂 window.echarts
                # （见 CLAUDE.md 里 UMD/AMD 坑记录）；本页无 monaco loader，纯预防
                '<script src="/admin/static/echarts.min.js"></script>'
                '<script src="/admin/static/vue.global.prod.js"></script>'
                '<script src="/admin/static/dg-select.js"></script>'
                '<script src="/admin/static/workflows.js"></script>')
        return _shell("流程", body, doc=False)

    @mcp.custom_route("/admin/workflows/save", methods=["POST"])
    @guard
    async def _wf_save(req: Request) -> JSONResponse:
        import json as _json

        f = await req.form()
        chart_raw = str(f.get("chart") or "")
        graph_raw = str(f.get("graph") or "")
        try:
            chart = _json.loads(chart_raw) if chart_raw else None
            if chart is not None and not isinstance(chart, dict):
                chart = None
            graph = _json.loads(graph_raw) if graph_raw else None
            if graph is not None and not isinstance(graph, dict):
                graph = None
            wf = await anyio.to_thread.run_sync(
                service.workflow_save, str(f.get("name") or ""),
                str(f.get("workspace") or ""), str(f.get("script") or ""), _caller(req),
                chart, graph)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "workflow": wf})

    @mcp.custom_route("/admin/workflows/delete", methods=["POST"])
    @guard
    async def _wf_delete(req: Request) -> JSONResponse:
        f = await req.form()
        try:
            await anyio.to_thread.run_sync(service.workflow_delete, str(f.get("name") or ""))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/admin/workflows/ai", methods=["POST"])
    @guard
    async def _wf_ai(req: Request) -> JSONResponse:
        """让 AI 按连接/表结构 + 需求生成/修改 workflow DAG（compile 校验+重修）。回前端载到画布，不执行。

        current_graph 传当前流程 JSON 时走修改模式（AI 在此基础上按需求增/删/改）；否则新建。
        """
        from .service import QueryRejected
        if not service.get_settings().get("ai_enabled"):
            return JSONResponse({"ok": False, "error": "AI 辅助未开启"}, status_code=403)
        f = await req.form()
        question = str(f.get("question") or "")
        schema = str(f.get("schema") or "").strip() or None
        try:
            tables = json.loads(str(f.get("tables") or "[]"))
            tables = [str(t).strip() for t in tables if str(t).strip()] or None
        except (ValueError, TypeError):
            tables = None
        current_graph = None
        cg_raw = str(f.get("current_graph") or "").strip()
        if cg_raw:
            try:
                cg = json.loads(cg_raw)
                if isinstance(cg, dict) and (cg.get("nodes") or []):
                    current_graph = cg
            except (ValueError, TypeError):
                pass
        caller = _caller(req)
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                lambda: service.ai_generate_workflow(
                    project, connection, question, caller,
                    schema=schema, tables=tables, current_graph=current_graph))
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/workflows/run", methods=["POST"])
    @guard
    async def _wf_run(req: Request) -> JSONResponse:
        f = await req.form()
        name = str(f.get("name") or "")
        caller = _caller(req)

        def _work_wf(_register) -> dict:  # noqa: ANN001
            return {"kind": "workflow", **service.workflow_run(name, caller)}

        job_id = _jobmgr.submit(_solo_key(), _work_wf)
        return JSONResponse({"ok": True, "job_id": job_id})

    @mcp.custom_route("/admin/workflows/run_graph", methods=["POST"])
    @guard
    async def _wf_run_graph(req: Request) -> JSONResponse:
        """直接运行画布 DAG（无需先保存）。异步 job，结果 kind=workflow（steps 带 node id）。"""
        import json as _json
        f = await req.form()
        workspace = str(f.get("workspace") or "")
        try:
            graph = _json.loads(str(f.get("graph") or ""))
        except ValueError:
            return JSONResponse({"ok": False, "error": "graph 不是合法 JSON"}, status_code=400)
        caller = _caller(req)

        def _work_graph(_register) -> dict:  # noqa: ANN001
            return {"kind": "workflow", **service.workflow_run_graph(workspace, graph, caller)}

        job_id = _jobmgr.submit(_solo_key(), _work_graph)
        return JSONResponse({"ok": True, "job_id": job_id})

    @mcp.custom_route("/admin/workflows/preview_columns", methods=["POST"])
    @guard
    async def _wf_preview_columns(req: Request) -> JSONResponse:
        """拿目标节点输出的列 schema（编译到该节点前依赖，DESCRIBE 该 view）。

        Body: graph=<json>, workspace, node, refresh?=0/1。
        返回 {ok, columns:[{name,type}]} 或 {ok, columns:[], error:"..."}。
        """
        import json as _json
        f = await req.form()
        workspace = str(f.get("workspace") or "").strip()
        node_id = str(f.get("node") or "").strip()
        refresh = str(f.get("refresh") or "").strip() in ("1", "true", "yes")
        if not workspace or not node_id:
            return JSONResponse({"ok": False, "error": "workspace 与 node 必填"}, status_code=400)
        try:
            graph = _json.loads(str(f.get("graph") or ""))
            if not isinstance(graph, dict):
                raise ValueError("graph 必须是对象")
        except ValueError as e:
            return JSONResponse({"ok": False, "error": f"graph JSON 非法：{e}"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.workflow_preview_columns(workspace, graph, node_id,
                                                        _caller(req), refresh))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/workflows/preview_node", methods=["POST"])
    @guard
    async def _wf_preview_node(req: Request) -> JSONResponse:
        """预览目标节点输出的前 N 行（模块视图「查看输出」）。"""
        import json as _json
        f = await req.form()
        workspace = str(f.get("workspace") or "").strip()
        node_id = str(f.get("node") or "").strip()
        try:
            limit = int(str(f.get("limit") or "100"))
        except ValueError:
            limit = 100
        if not workspace or not node_id:
            return JSONResponse({"ok": False, "error": "workspace 与 node 必填"}, status_code=400)
        try:
            graph = _json.loads(str(f.get("graph") or ""))
            if not isinstance(graph, dict):
                raise ValueError("graph 必须是对象")
        except ValueError as e:
            return JSONResponse({"ok": False, "error": f"graph JSON 非法：{e}"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.workflow_preview_node(workspace, graph, node_id, _caller(req),
                                                     limit))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/workflows/workspaces", methods=["GET"])
    @guard
    async def _wf_workspaces(_req: Request) -> JSONResponse:
        """列已有工作区（新页新建 workflow 时下拉用）。"""
        try:
            out = await anyio.to_thread.run_sync(service.analysis_overview)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "workspaces": out})

    @mcp.custom_route("/admin/workflows/workspace_create", methods=["POST"])
    @guard
    async def _wf_workspace_create(req: Request) -> JSONResponse:
        """新建分析工作区（新页新建 workflow 时的「＋ 新建工作区…」入口）。"""
        f = await req.form()
        name = str(f.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "工作区名不能为空"}, status_code=400)
        try:
            store = service._require_analysis()  # noqa: SLF001
            await anyio.to_thread.run_sync(store.create_workspace, name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "workspace": name})

    # ---------- 调度（PR-2） ----------

    @mcp.custom_route("/admin/workflows/schedule", methods=["GET"])
    @guard
    async def _wf_schedule_get(req: Request) -> JSONResponse:
        """取指定 workflow 的调度配置：?name=xxx"""
        name = req.query_params.get("name") or ""
        if not name:
            return JSONResponse({"ok": False, "error": "name 必填"}, status_code=400)
        try:
            sched = await anyio.to_thread.run_sync(service.workflow_schedule_get, name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "schedule": sched})

    @mcp.custom_route("/admin/workflows/schedule", methods=["POST"])
    @guard
    async def _wf_schedule_upsert(req: Request) -> JSONResponse:
        """增/改调度：name / cron_type / cron_value / enabled(0/1) / notify_on /
        attach_kinds(逗号分隔或 JSON)。"""
        import json as _json
        f = await req.form()
        name = str(f.get("name") or "").strip()
        cron_type = str(f.get("cron_type") or "").strip()
        cron_value = str(f.get("cron_value") or "").strip()
        enabled = str(f.get("enabled") or "1").strip() not in ("0", "false", "no", "")
        notify_on = str(f.get("notify_on") or "failure").strip()
        raw_kinds = str(f.get("attach_kinds") or "").strip()
        kinds: list[str] | None = None
        if raw_kinds:
            try:
                parsed = _json.loads(raw_kinds)
                if isinstance(parsed, list):
                    kinds = [str(x) for x in parsed]
            except ValueError:
                kinds = [x.strip() for x in raw_kinds.split(",") if x.strip()]
        if not name or not cron_type or not cron_value:
            return JSONResponse(
                {"ok": False, "error": "name / cron_type / cron_value 必填"},
                status_code=400)
        try:
            sched = await anyio.to_thread.run_sync(
                lambda: service.workflow_schedule_upsert(
                    name, cron_type, cron_value,
                    enabled=enabled, notify_on=notify_on, attach_kinds=kinds))
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "schedule": sched})

    @mcp.custom_route("/admin/workflows/schedule/delete", methods=["POST"])
    @guard
    async def _wf_schedule_delete(req: Request) -> JSONResponse:
        f = await req.form()
        name = str(f.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name 必填"}, status_code=400)
        try:
            await anyio.to_thread.run_sync(service.workflow_schedule_delete, name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    # ---------- 定时任务全局管理 ----------

    @mcp.custom_route("/admin/workflows/schedules", methods=["GET"])
    @guard
    async def _wf_schedules_list(req: Request) -> JSONResponse:
        """所有定时任务列表（附 workflow_exists / running 标记）。定时任务管理页用。"""
        try:
            rows = await anyio.to_thread.run_sync(service.workflow_schedules_enriched)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "schedules": rows})

    @mcp.custom_route("/admin/workflows/schedule/trigger", methods=["POST"])
    @guard
    async def _wf_schedule_trigger(req: Request) -> JSONResponse:
        """立即触发一次调度（后台线程跑，走跟 cron 到点相同的链路）。"""
        form = await req.form()
        name = (form.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name 必填"}, status_code=400)
        try:
            await anyio.to_thread.run_sync(service.workflow_schedule_trigger_now, name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    # ---------- 运行历史 & 详情 & xlsx 下载 ----------

    @mcp.custom_route("/admin/workflows/running", methods=["GET"])
    @guard
    async def _wf_running(req: Request) -> JSONResponse:
        """当前调度触发正在执行的 workflow 列表。前端 5s 轮询。

        默认只列 triggered_by=schedule（手动触发的用户在前端等结果、不进管理面板）；
        ?triggered_by=all 可看全部。"""
        raw = (req.query_params.get("triggered_by") or "schedule").strip()
        filt: str | None = None if raw == "all" else raw
        try:
            runs = await anyio.to_thread.run_sync(
                lambda: service.workflow_running_list(filt))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "runs": runs})

    @mcp.custom_route("/admin/workflows/runs", methods=["GET"])
    @guard
    async def _wf_runs(req: Request) -> JSONResponse:
        """?name=xxx&limit=50 → 该 workflow 的运行历史（started_at DESC）。"""
        name = req.query_params.get("name") or ""
        try:
            limit = int(req.query_params.get("limit") or "50")
        except ValueError:
            limit = 50
        if not name:
            return JSONResponse({"ok": False, "error": "name 必填"}, status_code=400)
        try:
            runs = await anyio.to_thread.run_sync(
                lambda: service.workflow_runs_list(name, limit))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "runs": runs})

    @mcp.custom_route("/admin/workflows/runs/{run_id:int}/detail", methods=["GET"])
    @guard
    async def _wf_run_detail(req: Request) -> JSONResponse:
        try:
            run_id = int(req.path_params["run_id"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "run_id 非法"}, status_code=400)
        try:
            run = await anyio.to_thread.run_sync(service.workflow_run_get, run_id)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if run is None:
            return JSONResponse({"ok": False, "error": "运行记录不存在"}, status_code=404)
        return JSONResponse({"ok": True, "run": run})

    @mcp.custom_route("/admin/workflows/runs/{run_id:int}", methods=["GET"])
    @guard
    async def _wf_run_page(req: Request) -> HTMLResponse:
        """运行详情页 HTML shell（Vue 挂载点由 workflows.js 里的 App 处理）。"""
        body = ('<div id="wf-app"></div>'
                '<link rel="stylesheet" href="/admin/static/workflows.css">'
                '<script src="/admin/static/echarts.min.js"></script>'
                '<script src="/admin/static/vue.global.prod.js"></script>'
                '<script src="/admin/static/dg-select.js"></script>'
                '<script src="/admin/static/workflows.js"></script>')
        return _shell("运行详情", body, doc=False)

    @mcp.custom_route(
        "/admin/workflows/runs/{run_id:int}/download/output.xlsx", methods=["GET"])
    @guard
    async def _wf_run_xlsx(req: Request) -> Response:
        """下载 workflow 运行的 xlsx 产物（仅当 attach_kinds 含 xlsx_link 时存在）。"""
        from pathlib import Path
        try:
            run_id = int(req.path_params["run_id"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "run_id 非法"}, status_code=400)
        run = service.workflow_run_get(run_id)
        if not run or not run.get("xlsx_path"):
            return JSONResponse({"ok": False, "error": "xlsx 不存在"}, status_code=404)
        if not service.data_dir:
            return JSONResponse({"ok": False, "error": "data_dir 未配置"}, status_code=500)
        full = Path(service.data_dir) / run["xlsx_path"]
        try:
            data = full.read_bytes()
        except OSError:
            return JSONResponse({"ok": False, "error": "xlsx 文件缺失"}, status_code=404)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition":
                     f'attachment; filename="workflow-run-{run_id}.xlsx"'})

    @mcp.custom_route("/admin/sql/search_tables", methods=["GET"])
    @guard
    async def _sql_search_tables(req: Request) -> JSONResponse:
        conn = req.query_params.get("conn") or ""
        q = req.query_params.get("q") or ""
        if "/" not in conn or not q.strip():
            return JSONResponse({"ok": True, "results": []})
        project, connection = conn.split("/", 1)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.admin_search_tables(
                    project, connection, q, _caller(req),
                    req.query_params.get("db") or None))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "results": out})

    @mcp.custom_route("/admin/sql/lint", methods=["POST"])
    @guard
    async def _sql_lint(req: Request) -> JSONResponse:
        """编辑器实时语法检查：sqlglot 按方言 parse，返回首个错误的行列。"""
        f = await req.form()
        sql = str(f.get("sql") or "")
        dialect = str(f.get("dialect") or "mysql")
        if dialect not in ("mysql", "postgres", "sqlite", "duckdb", "clickhouse"):
            dialect = "mysql"
        return JSONResponse({"ok": True, "errors": _lint_sql(sql, dialect)})

    @mcp.custom_route("/admin/sql/import", methods=["POST"])
    @guard
    async def _sql_import(req: Request) -> JSONResponse:
        """查询台数据导入：前端解析 CSV/粘贴为 rows JSON，此处参数化批量 INSERT。"""
        import json as _json
        f = await req.form()
        conn = str(f.get("conn") or "")
        if "/" not in conn:
            return JSONResponse({"ok": False, "error": "缺少连接"}, status_code=400)
        project, connection = conn.split("/", 1)
        try:
            columns = _json.loads(str(f.get("columns") or "[]"))
            rows = _json.loads(str(f.get("rows") or "[]"))
        except ValueError:
            return JSONResponse({"ok": False, "error": "columns/rows 不是合法 JSON"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.admin_import_rows(
                    project, connection, str(f.get("table") or ""), columns, rows,
                    _caller(req), schema=str(f.get("schema") or "") or None,
                    database=str(f.get("db") or "") or None))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/analysis/import", methods=["POST"])
    @guard
    async def _analysis_import(req: Request) -> JSONResponse:
        from .service import QueryRejected
        f = await req.form()
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            limit_raw = str(f.get("limit") or "").strip()
            out = await anyio.to_thread.run_sync(
                service.analysis_import,
                str(f.get("workspace") or ""), str(f.get("dataset") or ""),
                project, connection, str(f.get("sql") or ""), _caller(req),
                int(limit_raw) if limit_raw else None,
                str(f.get("schema") or "").strip() or None)
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/sql/history", methods=["GET"])
    @guard
    async def _sql_history(req: Request) -> JSONResponse:
        try:
            ws = _analysis_ws(req.query_params.get("conn", ""))
            if ws:
                items = await anyio.to_thread.run_sync(
                    service.admin_query_history, "analysis", ws)
                return JSONResponse({"ok": True, "items": items})
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            items = await anyio.to_thread.run_sync(
                service.admin_query_history, project, connection)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "items": items})

    @mcp.custom_route("/admin/sql/explain", methods=["POST"])
    @guard
    async def _sql_explain(req: Request) -> JSONResponse:
        from .service import QueryRejected
        f = await req.form()
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                service.admin_explain, project, connection, str(f.get("sql") or ""),
                _caller(req), schema, db)
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/sql/ddl", methods=["GET"])
    @guard
    async def _sql_ddl(req: Request) -> JSONResponse:
        schema = req.query_params.get("schema") or None
        ws = _analysis_ws(req.query_params.get("conn", ""))
        if ws:
            try:
                ddl = await anyio.to_thread.run_sync(
                    service.analysis.get_ddl, ws, req.query_params.get("table", ""))
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True, "ddl": ddl})
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            table = req.query_params.get("table", "")
            ddl = await anyio.to_thread.run_sync(
                service.get_table_ddl, project, connection, table, _caller(req), schema,
                req.query_params.get("db") or None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "ddl": ddl})

    @mcp.custom_route("/admin/sql/ai", methods=["POST"])
    @guard
    async def _sql_ai(req: Request) -> JSONResponse:
        """让命令行 AI 按表结构 + 需求生成一条 SQL。只返回文本、前端回填编辑器，不执行。"""
        from .service import QueryRejected
        if not service.get_settings().get("ai_enabled"):
            return JSONResponse({"ok": False, "error": "AI 辅助未开启"}, status_code=403)
        f = await req.form()
        question = str(f.get("question") or "")
        schema = str(f.get("schema") or "").strip() or None
        explain = str(f.get("explain") or "") in ("1", "on", "true")
        samples = str(f.get("include_samples") or "") in ("1", "on", "true")
        session_id = str(f.get("session_id") or "").strip() or None
        try:
            tables = json.loads(str(f.get("tables") or "[]"))
            tables = [str(t).strip() for t in tables if str(t).strip()] or None
        except (ValueError, TypeError):
            tables = None
        caller = _caller(req)
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                lambda: service.ai_generate_sql(
                    project, connection, question, caller, schema=schema, tables=tables,
                    explain=explain, include_samples=samples, session_id=session_id))
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        # 美化 SQL 再回给前端（AI 常吐一长条）；解析失败则原样返回
        engine = service.config.get_connection(project, connection).engine
        out["sql"] = _format_sql(out.get("sql") or "", engine)
        return JSONResponse({"ok": True, **out})

    # 异步查询任务：查询在服务端串行队列执行，页面切走/刷新不中断；前端凭 job_id
    # 轮询取结果（job_id 持久化在前端状态里，切回来自动续接）。结果保留 10 分钟。
    # 按连接串行（同一连接同时只跑一条 SQL，其余排队 FIFO；不同连接各自并行）——
    # queue_key=(project, connection)；workflow/画布这类多连接任务用独立 key（object()）
    # 各自并行、不参与串行。计时/排队位置/取消都由 JobManager 统一提供。
    from .jobs import JobManager

    _jobmgr = JobManager(ttl_s=600)

    def _solo_key() -> object:
        """给不参与串行的任务（workflow/画布 DAG）一个唯一 key，使其立即并行执行。"""
        return object()

    @mcp.custom_route("/admin/sql/run_async", methods=["POST"])
    @guard
    async def _sql_run_async(req: Request) -> JSONResponse:
        f = await req.form()
        sql = str(f.get("sql") or "")
        confirm = str(f.get("confirm") or "") in ("1", "on", "true")
        confirm_text = str(f.get("confirm_text") or "") or None
        expect_fp = str(f.get("expect_fingerprint") or "") or None
        try:
            page = max(int(str(f.get("page") or "0")), 0)
        except ValueError:
            page = 0
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        caller = _caller(req)
        from .jobs import Busy
        ws = _analysis_ws(str(f.get("conn") or ""))
        if ws:
            # 分析工作区：沙箱内任意 SQL 自由执行（不需确认流）。按工作区串行——忙时直接拒绝；
            # 不透传取消器（DuckDB 沙箱查询本地、无 KILL 路径）。
            def _work_ws(_register) -> dict:  # noqa: ANN001
                out = service.analysis_sql(ws, sql, caller)
                return {"kind": "read", "paginated": False, **out}

            try:
                job_id = _jobmgr.submit(("analysis", ws), _work_ws)
            except Busy:
                return JSONResponse({"ok": False,
                                     "error": f"工作区 {ws} 有查询正在执行，请等待其完成后再试。"})
            return JSONResponse({"ok": True, "job_id": job_id})
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})

        def _work(register) -> dict:  # noqa: ANN001
            try:
                return service.admin_run_sql(project, connection, sql, caller,
                                             confirm, page, None, schema, on_start=register,
                                             confirm_text=confirm_text, expect_fingerprint=expect_fp,
                                             database=db)
            except Exception as e:  # noqa: BLE001
                # 统一成已脱敏的文案；连接类错误打上 dbm_error_kind，JobManager 会把它
                # 透传到 /admin/sql/job 快照里，前端据此在结果区直接显示「重连」按钮。
                payload = error_payload(e)
                wrapped = RuntimeError(payload["error"])
                wrapped.dbm_error_kind = payload.get("error_kind", "")
                raise wrapped from e

        # 按连接串行只约束编辑器里手写的 SQL（同一连接同时只跑一条，忙时直接拒绝、前端明确提示）；
        # 双击表名打开的数据 tab（parallel=1）用独立 key 立即并行、不占用连接串行名额、也不互拒。
        # 取消器经 on_start 注册，运行中取消会对 DB 发 KILL QUERY / pg_cancel。
        parallel = str(f.get("parallel") or "") in ("1", "on", "true")
        queue_key = _solo_key() if parallel else (project, connection)
        try:
            job_id = _jobmgr.submit(queue_key, _work)
        except Busy:
            return JSONResponse({"ok": False,
                                 "error": f"连接 {project}/{connection} 有查询正在执行，"
                                          "请等待其完成，或点击「取消」中断后再试。"})
        return JSONResponse({"ok": True, "job_id": job_id})

    @mcp.custom_route("/admin/sql/job", methods=["GET"])
    @guard
    async def _sql_job(req: Request) -> JSONResponse:
        job_id = req.query_params.get("id", "")
        snap = _jobmgr.get(job_id)
        if snap is None:
            return JSONResponse({"ok": False, "error": "任务不存在或已过期（结果保留 10 分钟）"})
        out = {"ok": True, "status": snap["status"], "elapsed_ms": snap["elapsed_ms"]}
        if snap["status"] == "done":
            out["result"] = snap["result"]
        elif snap["status"] in ("error", "canceled"):
            out["error"] = snap["error"]
            out["error_kind"] = snap.get("error_kind") or ""
        return JSONResponse(out)

    @mcp.custom_route("/admin/sql/cancel", methods=["POST"])
    @guard
    async def _sql_cancel(req: Request) -> JSONResponse:
        """取消正在执行的任务：对 DB 发 KILL QUERY / pg_cancel_backend / interrupt。"""
        f = await req.form()
        job_id = str(f.get("id") or "")
        return JSONResponse({"ok": _jobmgr.cancel(job_id)})

    @mcp.custom_route("/admin/sql/health", methods=["GET"])
    @guard
    async def _sql_health(_req: Request) -> JSONResponse:
        """各连接的健康位快照，供查询台左树/连接选择器画状态灯。

        只返回**非健康**的连接（健康的不占带宽，前端把「查不到」当作正常）。
        retry_in_s 是距离下一次自动重试的秒数——前端据此显示「N 秒后自动重连」，
        让人知道系统在自愈、不必手忙脚乱地点重连。
        """
        import time as _time

        snap = service.health.snapshot()
        now = _time.monotonic()
        conns = {
            f"{proj}/{conn}": {
                "state": h.state,
                "fail_count": h.fail_count,
                "retry_in_s": max(0, int(h.next_retry_at - now)),
                "probing": h.probing,
                "last_error": h.last_error,
            }
            for (proj, conn), h in snap.items() if h.state != "ok"
        }
        return JSONResponse({"ok": True, "conns": conns})

    @mcp.custom_route("/admin/sql/reconnect", methods=["POST"])
    @guard
    async def _sql_reconnect(req: Request) -> JSONResponse:
        """人工重连：连接被判 unavailable/exhausted 后，从查询台点「重连」强制重建。

        绕过健康位（否则 exhausted 连接连测试都做不了），成功即恢复可用。
        """
        f = await req.form()
        raw = str(f.get("conn") or "")
        if _analysis_ws(raw):
            return JSONResponse({"ok": False, "error": "分析工作区为本地沙箱，无需重连"})
        try:
            project, connection = _resolve_conn(raw)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)})
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.reconnect_connection(project, connection, _caller(req)))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse(out)

    @mcp.custom_route("/admin/sql/run", methods=["POST"])
    @guard
    async def _sql_run(req: Request) -> JSONResponse:
        f = await req.form()
        sql = str(f.get("sql") or "")
        confirm = str(f.get("confirm") or "") in ("1", "on", "true")
        confirm_text = str(f.get("confirm_text") or "") or None
        expect_fp = str(f.get("expect_fingerprint") or "") or None
        try:
            page = max(int(str(f.get("page") or "0")), 0)
        except ValueError:
            page = 0
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        ws = _analysis_ws(str(f.get("conn") or ""))
        if ws:
            try:
                out = await anyio.to_thread.run_sync(
                    service.analysis_sql, ws, sql, _caller(req))
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"ok": False, "error": str(e)})
            # 沙箱 DDL/DML 无结果集时按 write 形态返回（批量 DROP 等复用前端逻辑）
            if not out["columns"]:
                return JSONResponse({"ok": True, "kind": "write", "affected_rows": 0,
                                     "duration_ms": 0})
            return JSONResponse({"ok": True, "kind": "read", "paginated": False, **out})
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            result = await anyio.to_thread.run_sync(
                lambda: service.admin_run_sql(
                    project, connection, sql, _caller(req), confirm, page, None, schema,
                    confirm_text=confirm_text, expect_fingerprint=expect_fp, database=db))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, **result})

    @mcp.custom_route("/admin/sql/format", methods=["POST"])
    @guard
    async def _sql_format(req: Request) -> JSONResponse:
        f = await req.form()
        sql = str(f.get("sql") or "")
        engine = ""
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            engine = service.config.get_connection(project, connection).engine
        except Exception:
            pass
        return JSONResponse({"ok": True, "sql": _format_sql(sql, engine)})

    @mcp.custom_route("/admin/sql/export", methods=["POST"])
    @guard
    async def _sql_export(req: Request) -> Response:
        from .export import ExportError
        from .service import QueryRejected
        f = await req.form()
        sql = str(f.get("sql") or "")
        fmt = str(f.get("format") or "csv")
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        ws = _analysis_ws(str(f.get("conn") or ""))
        try:
            if ws:
                from .export import export_result
                out = await anyio.to_thread.run_sync(
                    service.analysis_sql, ws, sql, _caller(req), 100_000)
                data, media_type, ext = export_result(out["columns"], out["rows"], fmt)
                project, connection = "analysis", ws
            else:
                project, connection = _resolve_conn(str(f.get("conn") or ""))
                data, media_type, ext = await anyio.to_thread.run_sync(
                    service.admin_export, project, connection, sql, fmt, _caller(req), schema, db)
        except (QueryRejected, KeyError, ValueError, ExportError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)
        import re
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{project}-{connection}")
        fname = f"{slug}-{stamp}.{ext}"
        return Response(data, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # ---------- SQL 片段库 ----------

    @mcp.custom_route("/admin/sql/snippets", methods=["GET"])
    @guard
    async def _snippets_list(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "snippets": service.list_snippets()})

    @mcp.custom_route("/admin/sql/snippets/save", methods=["POST"])
    @guard
    async def _snippets_save(req: Request) -> JSONResponse:
        from .snippets import SnippetError
        f = await req.form()
        sid_raw = str(f.get("id") or "").strip()
        try:
            snippet = service.save_snippet(
                title=str(f.get("title") or ""),
                sql=str(f.get("sql") or ""),
                note=str(f.get("note") or ""),
                connection=str(f.get("connection") or ""),
                snippet_id=int(sid_raw) if sid_raw else None,
            )
        except (SnippetError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "snippet": snippet})

    @mcp.custom_route("/admin/sql/snippets/delete", methods=["POST"])
    @guard
    async def _snippets_delete(req: Request) -> JSONResponse:
        from .snippets import SnippetError
        f = await req.form()
        try:
            service.delete_snippet(int(str(f.get("id") or "0")))
        except (SnippetError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})
