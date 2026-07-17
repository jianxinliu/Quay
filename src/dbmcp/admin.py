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
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

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


def _format_sql(sql: str, engine: str) -> str:
    """用 sqlglot 美化 SQL（缩进/关键字对齐）；解析失败则原样返回。"""
    if not sql:
        return ""
    dialect = {"mysql": "mysql", "postgres": "postgres", "sqlite": "sqlite"}.get(engine)
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
          font_size: int | None = None) -> str:
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
<link rel="stylesheet" href="/admin/static/admin-chrome.css">{doc_css}{font_css}</head><body>
<div class="shell">
 <aside class="side">
  <div class="brand">{_FAVICON_SVG}<div><b>Quay</b><span>gatekeeper</span></div></div>
  <nav>
   <a href="/admin/sql"><span class="nico nico-sql"></span>查询台</a>
   <a href="/admin/redis"><span class="nico nico-redis"></span>Redis</a>
   <a href="/admin/approvals"><span class="nico nico-approve"></span>审批中心{nav_badge}</a>
   <a href="/admin/audit"><span class="nico nico-audit"></span>操作审计</a>
   <a href="/admin/settings"><span class="nico nico-settings"></span>系统设置</a>
  </nav>
  <div class="foot"><a href="/admin/logout">退出登录</a></div>
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


def _env_badge(env: str) -> str:
    color = _ENV_COLOR.get(env, "#64748b")
    return f'<span class="badge" style="background:{color}">{_esc(env or "—")}</span>'


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


def _explain_html(risk: dict) -> str:
    plan = risk.get("explain")
    if not plan:
        return ""
    # MySQL 对单行主键更新等语句的无信息量输出，转成人话
    if "not executable by iterator executor" in plan:
        return ('<div class="sec-title">执行计划</div>'
                '<div class="muted">该语句为点查/单行定位更新，优化器无需生成可展示的查询计划。</div>')
    return f'<div class="sec-title">执行计划（EXPLAIN）</div><pre>{_esc(plan)}</pre>'


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
    ssh_extra = " ".join(cfg.ssh_options) if cfg and cfg.ssh_options else ""
    existing_hops = list(cfg.jump_hosts) if cfg else []
    hop_rows = "".join(_hop_row(h, identities) for h in existing_hops)
    empty_hop = _hop_row(None, identities)
    masks = ", ".join(cfg.policy.mask_columns) if cfg else ""
    pw_ph = "留空表示不修改" if is_edit else "写入系统 keyring，配置只存引用"
    writer_user = cfg.writer.user if cfg and cfg.writer else ""
    is_edit_js = "true" if is_edit else "false"
    return f"""<div id="conn-err" class="errbar" style="display:none"></div>
<form id="conn-form" method="post" action="/admin/connections/save">
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
  <div>{_field("database（可留空）", "database", cfg.database if cfg else "")}</div>
 </div>
 <div class="muted" style="margin:-2px 0 6px">database 留空：MySQL/PG 连到实例但不绑定默认库，查询需用「库名.表名」全限定；SQLite 必填（文件路径）；Redis 为 db 编号（默认 0）。</div>
 <div class="row">
  <div>{_field("只读账号 user", "user", cfg.user if cfg else "")}</div>
  <div>{_field("密码", "password", "", ph=pw_ph, typ="password")}</div>
 </div>
 <div class="muted" style="margin:-2px 0 6px">主账号应为<b>最小权限的只读账号</b>；保存时会自动校验，检测到写权限/超级用户会被拦截。写操作用下方 writer 账号。</div>
 <div class="row">
  <div>{_field("writer user（可选，写操作用）", "writer_user", writer_user)}</div>
  <div>{_field("writer password", "writer_password", "", ph=pw_ph, typ="password")}</div>
 </div>
 <hr style="border:none;border-top:1px solid #eee;margin:12px 0">
 <label>SSH 跳板链（按序，最后一跳落地转发到数据库）</label>
 <div class="muted" style="margin:2px 0 8px">每跳可引用一条已保存的 SSH 配置（主机/用户/私钥都从配置来，跳板处可留空覆盖），或填内联主机+私钥；不同跳板可用不同配置。无跳板＝直连。
 配置在<a href="/admin/settings?tab=ssh">SSH 配置</a>页维护。</div>
 <div id="hops">{hop_rows}</div>
 <template id="hop-tpl">{empty_hop}</template>
 <button type="button" id="add-hop" class="btn btn-ghost" style="margin:2px 0 10px">＋ 加一跳</button>
 <div class="row">
  <div>{_field("其它 ssh 选项（空格分隔，作用于最终目标）", "ssh_options_extra", ssh_extra, ph="-o ConnectTimeout=5", width="360px")}</div>
 </div>
 <div class="row">
  <div>{_field("max_rows", "max_rows", cfg.policy.max_rows if cfg else 500, typ="number", width="120px")}</div>
  <div>{_field("读超时(秒)", "statement_timeout_s", cfg.policy.statement_timeout_s if cfg else 30, typ="number", width="120px")}</div>
  <div>{_field("写超时(秒)", "write_timeout_s", cfg.policy.write_timeout_s if cfg else 600, typ="number", width="120px")}</div>
 </div>
 <div class="muted" style="margin:-2px 0 6px">读超时限只读查询（SELECT）；写超时给 writer 账号的大 DELETE/UPDATE 留足时间，避免 socket 提前断开报 2013。跑飞的写可在查询台点「取消」KILL。</div>
 <div class="row">
  <div>{_field("脱敏列（逗号分隔）", "mask_columns", masks, ph="email, phone")}</div>
 </div>
 <label style="margin-top:12px"><input type="checkbox" name="force_privileged" value="1" style="width:auto;margin-right:6px">强制使用高权限账号（该账号是 root/超级用户或拥有写权限，我确认知晓风险）</label>
 <div id="conn-test-result" style="display:none;margin:12px 0"></div>
 <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
  <button class="btn btn-primary" type="submit">{'保存修改' if is_edit else '创建连接'}</button>
  <button class="btn btn-ghost" type="button" id="btn-test">测试连接</button>
  <button class="btn btn-ghost" type="button" id="btn-test-ssh">测试 SSH 隧道</button>
  {"<a href='/admin/settings?tab=connections' style='margin-left:4px'>取消编辑</a>" if is_edit else ""}
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

    if not sql.strip():
        return []
    try:
        sqlglot.parse(sql, read=dialect)
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
        '<script src="/admin/static/redis.js"></script>'
    )


_SETTINGS_TABS = [("general", "整体设置"), ("db", "DB"), ("redis", "Redis"),
                  ("ai", "AI 助手"),
                  ("connections", "连接管理"), ("ssh", "SSH 配置"), ("info", "系统信息")]

_SETTINGS_SUBMIT_JS = """<script>
document.querySelectorAll('form.settings-form').forEach(function(f){
  f.addEventListener('submit', async function(e){
    e.preventDefault();
    var msg=f.querySelector('.settings-msg'); if(msg)msg.textContent='保存中…';
    try{ var r=await fetch('/admin/settings/save',{method:'POST',body:new FormData(f)});
      var d=await r.json();
      if(msg)msg.textContent=d.ok?'✓ 已保存（重新打开查询台/Redis 生效）':'保存失败：'+d.error;
    }catch(err){ if(msg)msg.textContent='保存失败：'+err; }
  });
});
</script>"""


def _settings_tabs(active: str) -> str:
    items = "".join(
        f"<a class='stab{' active' if active == k else ''}' "
        f"href='/admin/settings?tab={k}'>{_esc(label)}</a>"
        for k, label in _SETTINGS_TABS
    )
    return f"<div class='stabs'>{items}</div>"


def _settings_form(inner: str) -> str:
    return (f"<div class='card' style='max-width:560px'><form class='settings-form'>{inner}"
            "<button class='btn btn-primary' type='submit'>保存</button>"
            "<span class='settings-msg muted' style='margin-left:12px'></span>"
            f"</form></div>{_SETTINGS_SUBMIT_JS}")


def _num_setting(label: str, name: str, s: dict, default: object, hint: str) -> str:
    return ("<div style='margin-bottom:16px'>"
            + _field(label, name, s.get(name, default), ph=str(default), typ="number", width="200px")
            + f"<div class='muted' style='margin-top:4px'>{hint}</div></div>")


def _bool_setting(label: str, name: str, s: dict, default: bool,
                  on_text: str, off_text: str, hint: str) -> str:
    on = bool(s.get(name, default))
    opts = (f"<option value='true'{' selected' if on else ''}>{_esc(on_text)}</option>"
            f"<option value='false'{'' if on else ' selected'}>{_esc(off_text)}</option>")
    return (f"<div style='margin-bottom:16px'><label>{_esc(label)}</label>"
            f"<select name='{name}' style='width:200px'>{opts}</select>"
            f"<div class='muted' style='margin-top:4px'>{hint}</div></div>")


def _settings_general_body(s: dict) -> str:
    def sel(v: str) -> str:
        return " selected" if s.get("theme") == v else ""
    return _settings_form(
        "<div style='margin-bottom:16px'><label>界面主题</label>"
        f"<select name='theme' style='width:200px'>"
        f"<option value='dark'{sel('dark')}>深色（默认）</option>"
        f"<option value='light'{sel('light')}>浅色</option></select>"
        "<div class='muted' style='margin-top:4px'>作用于查询台与 Redis 控制台的深色 IDE 界面。</div></div>"
        + _num_setting("后台整体字号（px）", "ui_font_size", s, 14, "后台各页面的基础字号，10–20 之间。")
        + _bool_setting("审计页默认自动刷新", "audit_auto_refresh", s, False,
                        "开启（每 5s）", "关闭（默认）", "打开操作审计页时是否默认每 5 秒自动刷新。")
        + _bool_setting("审计页默认隐藏 admin-ui", "audit_hide_admin_ui", s, True,
                        "隐藏（默认）", "显示", "默认是否隐藏 agent=admin-ui（查询台自身操作）的审计记录。"))


def _settings_db_body(s: dict) -> str:
    return _settings_form(
        _num_setting("查询台结果每页行数", "sql_page_size", s, 100, "查询台（DB）结果分页大小。")
        + _bool_setting("编辑器 minimap（代码缩略图）", "sql_minimap", s, True,
                        "显示（默认）", "隐藏", "编辑器右侧代码缩略图，隐藏可让出更多编辑宽度。")
        + _num_setting("编辑器字号（px）", "sql_font_size", s, 13, "查询台 SQL 编辑器字号，10–24 之间。")
        + _bool_setting("编辑器自动换行", "sql_word_wrap", s, False,
                        "开启", "关闭（默认）", "超出宽度的长 SQL 是否自动折行。")
        + _num_setting("结果默认行上限", "sql_max_rows", s, 1000,
                       "缺 LIMIT 的查询自动兜底的行上限、非分页读取的截断上限。")
        + _num_setting("单元格最大字符数", "sql_max_cell_chars", s, 4096,
                       "超长 TEXT/BLOB 单元格截断的字符数。")
        + _num_setting("Agent 结果字符预算", "agent_max_result_chars", s, 40000,
                       "给 agent（MCP query/sample_rows）的 TSV 结果字符上限（≈token×4，默认 40000≈12k token）。"
                       "连接级 Policy 可单独覆盖。"))


def _text_setting(label: str, name: str, s: dict, default: str, hint: str,
                  width: str = "260px") -> str:
    return ("<div style='margin-bottom:16px'>"
            + _field(label, name, s.get(name, default), ph=str(default), width=width)
            + f"<div class='muted' style='margin-top:4px'>{hint}</div></div>")


def _settings_ai_body(s: dict) -> str:
    provider = s.get("ai_provider", "claude")

    def psel(v: str) -> str:
        return " selected" if provider == v else ""

    prompt = _esc(s.get("ai_sql_prompt", ""))
    return _settings_form(
        _bool_setting("启用 AI 辅助写 SQL", "ai_enabled", s, False,
                      "启用", "关闭（默认）",
                      "开启后查询台会出现「✨ AI」按钮；产物只回填编辑器、绝不自动执行。")
        + "<div style='margin-bottom:16px'><label>AI 后端</label>"
        + "<select name='ai_provider' style='width:200px'>"
        + f"<option value='claude'{psel('claude')}>Claude CLI（claude -p）</option>"
        + f"<option value='codex'{psel('codex')}>CodeX CLI（codex exec）</option></select>"
        + "<div class='muted' style='margin-top:4px'>调用本机命令行 AI（需已安装并登录）。</div></div>"
        + _text_setting("CLI 路径", "ai_cli_path", s, "",
                        "留空则用后端默认二进制名（claude / codex）；如不在 PATH 中可填绝对路径。")
        + _text_setting("模型", "ai_model", s, "claude-sonnet-5",
                        "Claude 用 claude-*（如 claude-sonnet-5）；CodeX 用其账号支持的模型名。")
        + _num_setting("生成超时（秒）", "ai_timeout_s", s, 60,
                       "单次生成的最长等待时间，10–600 之间。")
        + _num_setting("最大表数", "ai_max_tables", s, 40,
                       "「整库」模式下最多把多少张表的结构发给 AI，超出会要求你勾选具体表（1–200）。")
        + "<div style='margin-bottom:16px'><label>SQL 生成系统提示词</label>"
        + f"<textarea name='ai_sql_prompt' rows='10' style='width:100%'>{prompt}</textarea>"
        + "<div class='muted' style='margin-top:4px'>生成 SQL 的系统提示（角色设定 + SQL 约束）。"
        + "清空并保存即恢复默认。</div></div>"
        + "<div style='margin-bottom:16px'><label>流程生成系统提示词</label>"
        + f"<textarea name='ai_workflow_prompt' rows='6' style='width:100%'>{_esc(s.get('ai_workflow_prompt', ''))}</textarea>"
        + "<div class='muted' style='margin-top:4px'>生成可视化流程（DAG 画布）的系统提示。"
        + "清空并保存即恢复默认。</div></div>")


def _settings_redis_body(s: dict) -> str:
    return _settings_form(
        _num_setting("Redis 结果每页行数", "redis_page_size", s, 100,
                     "Redis 键详情（hash/list/set/zset）与命令结果分页大小。")
        + _num_setting("Redis 键列表加载上限", "redis_key_limit", s, 1000,
                       "左侧键树 SCAN 一次最多加载的键数量。")
        + _num_setting("SCAN 每批 COUNT", "redis_scan_count", s, 500,
                       "SCAN 每轮的批大小，越大越快但单次更阻塞（50–10000）。")
        + _bool_setting("非 UTF-8 值 msgpack 解码", "redis_msgpack_decode", s, True,
                        "开启（默认）", "关闭", "非 UTF-8 的值是否尝试用 msgpack 解码为结构展示。")
        + _num_setting("库切换器最少展示库数", "redis_min_dbs", s, 16,
                       "底部数据库切换器至少列出多少个逻辑库（1–256）。"))


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
    """连接管理（并入系统设置的『连接管理』tab）：连接列表 + 新增/编辑弹窗表单。"""
    edit_cfg = None
    e_project = e_conn = ""
    if editing and "/" in editing:
        e_project, e_conn = editing.split("/", 1)
        proj = service.config.projects.get(e_project)
        edit_cfg = proj.connections.get(e_conn) if proj else None

    rows = []
    for pname, proj in sorted(service.config.projects.items()):
        for cname, c in sorted(proj.connections.items()):
            jump = " → ".join(h.label() for h in c.jump_hosts) if c.jump_hosts else "—"
            stripe = _ENV_COLOR.get(c.environment, "#64748b")
            db = f"<code>{_esc(c.database)}</code>" if c.database else "<span class='muted'>—</span>"
            rows.append(
                f"<tr><td style='border-left:3px solid {stripe};padding-left:13px'>"
                f"<code>{_esc(pname)}/{_esc(cname)}</code></td><td class='mono muted'>{_esc(c.engine)}</td>"
                f"<td>{_env_badge(c.environment)}</td><td><code>{_esc(c.host)}:{_esc(c.port)}</code></td>"
                f"<td>{db}</td><td class='muted mono'>{_esc(jump)}</td>"
                f"<td style='white-space:nowrap'>"
                f"<a href='/admin/settings?tab=connections&edit={_esc(pname)}/{_esc(cname)}'>编辑</a> · "
                f"<form method='post' action='/admin/connections/delete' style='display:inline' "
                f"onsubmit='return dbmConfirm(this)'>"
                f"<input type='hidden' name='project' value='{_esc(pname)}'>"
                f"<input type='hidden' name='connection' value='{_esc(cname)}'>"
                f"<button class='btn btn-reject' style='padding:3px 11px;font-size:12.5px'>删除</button></form></td></tr>"
            )
    table = "".join(rows) or '<tr><td colspan="7" class="muted">（无连接）</td></tr>'
    keyring_note = "" if _keyring_available() else (
        "<p style='color:#b00020'>⚠️ 未安装 keyring，无法安全存储密码。"
        "请 <code>pip install 'db-manage-mcp[keyring]'</code> 后重启。</p>"
    )
    form = _connection_form(e_project, e_conn, edit_cfg, sorted(service.config.ssh_identities))
    back = "/admin/settings?tab=connections"
    auto_open = "document.getElementById('conn-modal').classList.add('open');" if edit_cfg else ""
    return (
        "<div class='card'><div style='display:flex;align-items:center;margin-bottom:14px'>"
        "<h2 style='margin:0'>连接列表</h2>"
        "<button class='btn btn-primary' style='margin-left:auto' "
        "onclick=\"document.getElementById('conn-modal').classList.add('open')\">＋ 新增连接</button></div>"
        f"<div class='tablewrap'><table><tr><th>连接</th><th>引擎</th><th>环境</th><th>地址</th><th>库</th>"
        f"<th>跳板</th><th>操作</th></tr>{table}</table></div></div>"
        f"<div class='modalbg' id='conn-modal'><div class='modalbox'>"
        f"<button class='mclose' onclick=\"document.getElementById('conn-modal').classList.remove('open');"
        f"if(location.search.indexOf('edit=')>=0)location.href='{back}'\">✕</button>"
        f"<h2>{'编辑连接' if edit_cfg else '新增连接'}</h2>{keyring_note}{form}</div></div>"
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


def mount_admin(mcp: "FastMCP", service: "DbmService", admin_token: str) -> None:
    expected_cookie = _session_value(admin_token)

    def guard(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        """未认证访问受保护路由 → 重定向登录页；非本机来源（Host/Origin 不符）→ 403。"""
        @wraps(handler)
        async def _wrapped(req: Request) -> Response:
            if not _local_request_ok(req):
                if _wants_json(req):
                    return JSONResponse(
                        {"ok": False, "error": "请求被拒绝：管理后台只能从本机访问"},
                        status_code=403)
                return Response("forbidden: request must originate from localhost",
                                status_code=403)
            if not _authed(req, expected_cookie):
                # 前端 fetch 期望 JSON → 返回 401 JSON（前端据此提示重新登录）；
                # 浏览器页面导航 → 仍 303 到登录页。
                if _wants_json(req):
                    return JSONResponse(
                        {"ok": False, "error": "登录已过期，请刷新页面重新登录"},
                        status_code=401)
                return RedirectResponse(url="/admin/login", status_code=303)
            return await handler(req)
        return _wrapped

    def _shell(title: str, body: str, doc: bool = True) -> HTMLResponse:
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
        return HTMLResponse(_page(title, body, pending=pending, doc=doc, font_size=fs))

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
                f"@ {_esc(_fmt_ts(c.decided_at))}<br>备注: {_esc(c.decision_note) or '—'}</div>"
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
 </dl>
 <div class="sec-title">SQL</div><pre>{_esc(c.sql)}</pre>
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
        qp = req.query_params
        try:
            limit = min(max(int(qp.get("limit", "200")), 1), 1000)
            offset = max(int(qp.get("offset", "0")), 0)
        except ValueError:
            limit, offset = 200, 0
        # 服务端筛选（下推到 SQL）
        filters = {k: qp.get(k) for k in ("project", "connection", "agent", "status") if qp.get(k)}
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
                                 f"<td colspan='8'><pre>{_esc(detail)}</pre></td></tr>")
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
                              f"<td colspan='8'><pre>{_esc(_format_sql(raw_sql, r['engine'] or ''))}</pre></td></tr>")
            else:
                sqlcell = "<span class='muted'>—</span>"
                expand_row = ""
            trs.append(
                f"<tr><td style='white-space:nowrap'><code>{_esc(r['project'])}/{_esc(r['connection'])}</code></td>"
                f"<td>{_env_badge(r['environment']) if r['environment'] else '<span class=muted>—</span>'}</td>"
                f"<td class='mono'>{_esc(r['agent'])}</td>"
                f"<td class='mono muted' style='white-space:nowrap'>{_esc(r['tool'])}</td>"
                f"<td>{sqlcell}</td>"
                f"<td>{_badge(r['status'], _STATUS_COLOR)}</td>"
                f"<td class='muted mono' style='white-space:nowrap'>{_esc(_fmt_ts(r['ts']))}</td>"
                f"<td class='muted'>{statline}{dline}</td></tr>"
                f"{expand_row}{detail_expand}"
            )
        table_rows = "".join(trs) or '<tr><td colspan="8" class="muted">（无匹配记录）</td></tr>'

        # 筛选下拉
        def _sel(name: str, label: str, values: list[str]) -> str:
            cur = filters.get(name, "")
            opts = "<option value=''>全部" + _esc(label) + "</option>" + "".join(
                f"<option value='{_esc(v)}'{' selected' if v == cur else ''}>{_esc(v)}</option>"
                for v in values)
            return f"<select name='{name}' onchange='this.form.submit()'>{opts}</select>"

        status_opts = ["ok", "rejected", "error"]
        filter_bar = (
            "<form method='get' class='filters' style='gap:8px'>"
            + _sel("project", "项目", service.store.distinct_values("project"))
            + _sel("connection", "连接", service.store.distinct_values("connection"))
            + _sel("agent", "agent", service.store.distinct_values("agent"))
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

        body = (
            # 审计表信息密度高：本页放开 main 宽度限制，表格占满可用宽度、列宽自适应
            "<style>main{max-width:none}</style>"
            + _pagehead("Audit Log", "操作审计", "每次数据库操作的完整留痕：谁、何时、在哪个库、跑了什么、结果如何")
            + f"<div class='card'>{filter_bar}"
            f"<div class='tablewrap'><table class='audit'><tr>"
            f"<th>连接</th><th>环境</th><th>agent</th><th>工具</th>"
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
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            dbs = await anyio.to_thread.run_sync(
                service.list_databases, project, connection, _caller(req))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
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
                service.list_tables, project, connection, _caller(req), schema)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        try:  # 表容量（树右侧分级展示）：拿不到不阻断表列表
            sizes = await anyio.to_thread.run_sync(
                service.admin_table_sizes, project, connection, _caller(req), schema)
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
                service.describe_table, project, connection, table, _caller(req), schema)
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
        try:
            settings = service.save_settings(updates)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "settings": settings})

    @mcp.custom_route("/admin/settings", methods=["GET"])
    @guard
    async def _settings_page(req: Request) -> HTMLResponse:
        tab = req.query_params.get("tab") or "general"
        s = service.get_settings()
        if tab == "connections":
            content = _connections_body(service, req.query_params.get("edit"))
        elif tab == "ssh":
            content = _ssh_identities_body(service)
        elif tab == "db":
            content = _settings_db_body(s)
        elif tab == "redis":
            content = _settings_redis_body(s)
        elif tab == "ai":
            content = _settings_ai_body(s)
        elif tab == "info":
            content = _settings_info_body(service, req)
        else:
            tab = "general"
            content = _settings_general_body(s)
        body = (_pagehead("Settings", "系统设置", "界面偏好 + 连接管理，服务端保存")
                + _settings_tabs(tab) + content)
        return _shell("系统设置", body)

    @mcp.custom_route("/admin/workflows", methods=["GET"])
    @guard
    async def _wf_list(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "workflows": service.workflow_list()})

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
        """让 AI 按连接/表结构 + 需求生成一张 workflow DAG（compile 校验+重修）。回前端载到画布，不执行。"""
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
        caller = _caller(req)
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                lambda: service.ai_generate_workflow(
                    project, connection, question, caller, schema=schema, tables=tables))
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
                lambda: service.admin_search_tables(project, connection, q, _caller(req)))
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
        if dialect not in ("mysql", "postgres", "sqlite", "duckdb"):
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
                    _caller(req), schema=str(f.get("schema") or "") or None))
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
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                service.admin_explain, project, connection, str(f.get("sql") or ""),
                _caller(req), schema)
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
                service.get_table_ddl, project, connection, table, _caller(req), schema)
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
        from .service import QueryRejected
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
                                             confirm_text=confirm_text, expect_fingerprint=expect_fp)
            except (QueryRejected, KeyError, ValueError) as e:
                raise RuntimeError(str(e)) from e

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
        return JSONResponse(out)

    @mcp.custom_route("/admin/sql/cancel", methods=["POST"])
    @guard
    async def _sql_cancel(req: Request) -> JSONResponse:
        """取消正在执行的任务：对 DB 发 KILL QUERY / pg_cancel_backend / interrupt。"""
        f = await req.form()
        job_id = str(f.get("id") or "")
        return JSONResponse({"ok": _jobmgr.cancel(job_id)})

    @mcp.custom_route("/admin/sql/run", methods=["POST"])
    @guard
    async def _sql_run(req: Request) -> JSONResponse:
        from .service import QueryRejected
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
                    confirm_text=confirm_text, expect_fingerprint=expect_fp))
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
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
                    service.admin_export, project, connection, sql, fmt, _caller(req), schema)
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
