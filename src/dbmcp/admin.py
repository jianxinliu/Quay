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
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

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


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · db-manage-mcp</title>
{_FAVICON_LINK}
<style>
 :root{{
  --ink:#14181f; --ink-2:#1c222c; --paper:#f4f5f7; --surface:#fff;
  --border:#e6e8ec; --line:#eef0f3; --text:#1a1f28; --muted:#6b7280; --faint:#9aa1ac;
  --accent:#0d9488; --accent-ink:#0b7268; --accent-soft:#e6faf6;
  --mono:ui-monospace,'SF Mono',Menlo,Monaco,'Cascadia Code',monospace;
  --sans:-apple-system,'SF Pro Text',system-ui,'PingFang SC',sans-serif;
 }}
 *{{box-sizing:border-box}}
 body{{font-family:var(--sans);margin:0;background:var(--paper);color:var(--text);
   -webkit-font-smoothing:antialiased;font-size:14px;line-height:1.5}}
 code,pre,.mono{{font-family:var(--mono)}}
 a{{color:var(--accent-ink);text-decoration:none}} a:hover{{text-decoration:underline}}
 .shell{{display:grid;grid-template-columns:236px 1fr;min-height:100vh}}
 /* 侧栏 */
 .side{{background:var(--ink);color:#c7cdd6;display:flex;flex-direction:column;padding:20px 14px;
   position:sticky;top:0;height:100vh}}
 .side .brand{{display:flex;align-items:center;gap:10px;padding:4px 8px 20px;color:#fff}}
 .side .brand svg{{width:30px;height:30px;border-radius:8px;flex:none}}
 .side .brand b{{font-size:15px;font-weight:600;letter-spacing:.2px}}
 .side .brand span{{display:block;font-family:var(--mono);font-size:10px;color:var(--faint);letter-spacing:1px;text-transform:uppercase}}
 .side nav{{display:flex;flex-direction:column;gap:2px;margin-top:6px}}
 .side nav a{{display:flex;align-items:center;gap:10px;color:#aeb6c2;padding:9px 11px;border-radius:8px;
   font-size:14px;transition:background .12s,color .12s}}
 .side nav a:hover{{background:var(--ink-2);color:#fff;text-decoration:none}}
 .side nav a.active{{background:var(--accent);color:#fff;font-weight:500}}
 .side nav a .dot{{width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.5;flex:none}}
 .side nav a.active .dot{{opacity:1}}
 .side .foot{{margin-top:auto;border-top:1px solid #262d38;padding-top:12px}}
 .side .foot a{{color:var(--faint);font-size:13px;padding:6px 11px;display:block;border-radius:6px}}
 .side .foot a:hover{{color:#fff;background:var(--ink-2);text-decoration:none}}
 /* 工作区 */
 main{{padding:32px 40px;max-width:1160px}}
 h1,h2,h3{{color:var(--text);font-weight:600;letter-spacing:-.01em}}
 h2{{font-size:19px;margin:0 0 2px}} h3{{font-size:15px;margin:0 0 8px}}
 .eyebrow{{font-family:var(--mono);font-size:11px;letter-spacing:1.5px;text-transform:uppercase;
   color:var(--accent-ink);margin-bottom:6px}}
 .pagehead{{margin-bottom:22px}}
 .card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 22px;margin-bottom:18px}}
 .card h2{{margin-bottom:14px}}
 /* 表格 */
 table{{width:100%;border-collapse:collapse;font-size:13.5px}}
 th,td{{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}}
 th{{font-family:var(--mono);font-weight:600;font-size:11px;letter-spacing:.6px;text-transform:uppercase;
   color:var(--faint);border-bottom:1px solid var(--border)}}
 tr:last-child td{{border-bottom:none}}
 tbody tr{{transition:background .1s}} tbody tr:hover{{background:#fafbfc}}
 td code{{font-size:12.5px;color:#334155;background:#f6f7f9;padding:1px 6px;border-radius:5px}}
 .cell-sql{{display:inline-block;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle}}
 .cell-detail{{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;margin-top:2px}}
 /* 徽章 / pill / tag */
 .badge{{display:inline-block;padding:2px 9px;border-radius:6px;color:#fff;font-size:11.5px;font-weight:600;
   font-family:var(--mono);letter-spacing:.3px}}
 .pill{{display:inline-block;padding:1px 10px;border-radius:6px;font-size:12px;font-weight:600;font-family:var(--mono)}}
 .pill-yes{{background:#dcfce7;color:#166534}} .pill-no{{background:#f1f5f9;color:#475569}}
 .pill-na{{background:#fef3c7;color:#92400e}}
 .tag{{display:inline-block;background:var(--accent-soft);color:var(--accent-ink);padding:1px 8px;
   border-radius:5px;font-size:12px;font-family:var(--mono);margin-right:4px}}
 /* 代码 / 计划 */
 pre{{background:var(--ink);color:#e2e8f0;padding:14px 16px;border-radius:10px;overflow-x:auto;
   font-size:12.5px;line-height:1.55;border:1px solid #262d38}}
 /* 按钮 */
 .btn{{display:inline-block;padding:9px 17px;border-radius:8px;border:1px solid transparent;
   font-size:13.5px;font-weight:500;cursor:pointer;text-decoration:none;transition:filter .12s,transform .04s;
   background:var(--ink);color:#fff;font-family:var(--sans)}}
 .btn:hover{{filter:brightness(1.15);text-decoration:none}} .btn:active{{transform:translateY(1px)}}
 .btn-primary{{background:var(--accent)}} .btn-approve{{background:#16794f}} .btn-reject{{background:#c0392b}}
 .btn-ghost{{background:#fff;color:var(--text);border-color:var(--border)}}
 .btn-ghost:hover{{background:#f6f7f9;filter:none}}
 /* 表单 */
 input,textarea,select{{font-family:var(--sans);font-size:13.5px;padding:9px 11px;border:1px solid var(--border);
   border-radius:8px;background:#fff;transition:border-color .12s,box-shadow .12s;color:var(--text)}}
 input:focus,textarea:focus,select:focus{{outline:none;border-color:var(--accent);
   box-shadow:0 0 0 3px rgba(13,148,136,.14)}}
 input::placeholder{{color:var(--faint)}}
 label{{font-size:12.5px;color:var(--muted);display:block;margin:12px 0 5px;font-weight:500}}
 .errbar{{background:#fef2f2;border:1px solid #fca5a5;color:#b91c1c;padding:11px 15px;border-radius:9px;
   margin-bottom:16px;font-size:13.5px}}
 /* 通用 */
 .muted{{color:var(--muted);font-size:13px}} .row{{display:flex;gap:22px;flex-wrap:wrap}}
 .filters{{margin-bottom:14px;display:flex;gap:4px;flex-wrap:wrap;align-items:center;color:var(--muted);font-size:13px}}
 .filters a{{padding:3px 10px;border-radius:6px;color:var(--muted)}}
 .filters a:hover{{background:#eceef1;text-decoration:none;color:var(--text)}}
 .kv{{display:grid;grid-template-columns:auto 1fr;gap:8px 18px;margin:4px 0;align-items:center;font-size:13.5px}}
 .kv dt{{color:var(--muted)}} .kv dd{{margin:0;color:var(--text)}}
 .sec-title{{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.8px;
   text-transform:uppercase;margin:18px 0 7px}}
 .card h3:first-child,.card .sec-title:first-child{{margin-top:0}}
 @media (max-width:720px){{
  .shell{{grid-template-columns:1fr}}
  .side{{position:static;height:auto;flex-direction:row;align-items:center;padding:12px 16px;gap:8px;overflow-x:auto}}
  .side .brand{{padding:0 8px 0 0}} .side .brand span{{display:none}}
  .side nav{{flex-direction:row;margin:0}} .side .foot{{margin:0 0 0 auto;border:none;padding:0}}
  .side nav a .dot{{display:none}} main{{padding:22px 18px}}
 }}
 @media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
</style></head><body>
<div class="shell">
 <aside class="side">
  <div class="brand">{_FAVICON_SVG}<div><b>db-manage-mcp</b><span>gatekeeper</span></div></div>
  <nav>
   <a href="/admin/approvals"><span class="dot"></span>审批中心</a>
   <a href="/admin/audit"><span class="dot"></span>操作审计</a>
   <a href="/admin/connections"><span class="dot"></span>连接管理</a>
  </nav>
  <div class="foot"><a href="/admin/logout">退出登录</a></div>
 </aside>
 <main>{body}</main>
</div>
<script>
 (function(){{var p=location.pathname;document.querySelectorAll('.side nav a').forEach(function(a){{
   if(p.indexOf(a.getAttribute('href'))===0)a.classList.add('active');}});}})();
</script>
</body></html>"""


def _login_page(error: str = "") -> str:
    err = (f"<div style='background:#fef2f2;border:1px solid #fca5a5;color:#b91c1c;"
           f"padding:9px 13px;border-radius:8px;font-size:13px;margin-bottom:14px'>{_esc(error)}</div>"
           if error else "")
    mono = "ui-monospace,'SF Mono',Menlo,monospace"
    sans = "-apple-system,'SF Pro Text',system-ui,'PingFang SC',sans-serif"
    body = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>登录 · db-manage-mcp</title>
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
 <div class="brand">{_FAVICON_SVG}<div><b>db-manage-mcp</b><span>gatekeeper</span></div></div>
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
 <label style="margin-top:12px"><input type="checkbox" name="force_privileged" value="1" style="width:auto;margin-right:6px">强制使用高权限账号（该账号是 root/超级用户或拥有写权限，我确认知晓风险）</label>
 <div id="conn-test-result" style="display:none;margin:12px 0"></div>
 <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
  <button class="btn btn-primary" type="submit">{'保存修改' if is_edit else '创建连接'}</button>
  <button class="btn btn-ghost" type="button" id="btn-test">测试连接</button>
  <button class="btn btn-ghost" type="button" id="btn-test-ssh">测试 SSH 隧道</button>
  {"<a href='/admin/connections' style='margin-left:4px'>取消编辑</a>" if is_edit else ""}
 </div>
</form>
<script>
(function(){{
  var form = document.getElementById('conn-form');
  var err = document.getElementById('conn-err');
  if (!form) return;

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
      if (data.ok) {{ window.location = '/admin/connections'; return; }}
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
            _pagehead("Approvals", "审批中心", "数据变更操作在此人工授权；批准后 agent 带 change_id 重提执行")
            + f"<div class='card'><h2>待审批 <span class='muted'>({len(pending)})</span></h2>"
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
{_pagehead("Change #" + str(c.id), f"审批单 #{c.id}")}
<div class='card'>
 <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">{_badge(st, _STATUS_COLOR)} {_badge(c.risk_level, _LEVEL_COLOR)} <span class="tag">{_esc(c.engine)}</span></div>
 <dl class="kv">
  <dt>连接</dt><dd><code>{_esc(c.project)}/{_esc(c.connection)}</code> · {_env_badge(c.environment)}</dd>
  <dt>提交 agent</dt><dd>{_esc(c.agent)}</dd>
  <dt>提交时间</dt><dd>{_esc(c.created_at)} · 有效期至 {_esc(c.expires_at)}</dd>
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
        qp = req.query_params
        try:
            limit = min(max(int(qp.get("limit", "200")), 1), 1000)
            offset = max(int(qp.get("offset", "0")), 0)
        except ValueError:
            limit, offset = 200, 0
        # 服务端筛选（下推到 SQL）
        filters = {k: qp.get(k) for k in ("project", "connection", "agent", "status") if qp.get(k)}
        total = service.store.count(filters)
        rows = service.store.recent(limit, offset, filters)

        trs = []
        for r in rows:
            sql = _esc((r["sql"] or "")[:90])
            # 结果列：有行数/耗时才显示；detail 截断 + 完整值放 title 悬浮
            stat = []
            if r["row_count"] is not None:
                stat.append(f"{r['row_count']} 行")
            if r["duration_ms"] is not None:
                stat.append(f"{r['duration_ms']}ms")
            statline = f"<span class='mono'>{' · '.join(stat)}</span>" if stat else ""
            detail = r["detail"] or ""
            dline = (f"<div class='cell-detail' title='{_esc(detail)}'>{_esc(detail[:70])}"
                     f"{'…' if len(detail) > 70 else ''}</div>") if detail else ""
            sqlcell = (f"<code class='cell-sql' title='{_esc(r['sql'] or '')}'>{sql}"
                       f"{'…' if r['sql'] and len(r['sql']) > 90 else ''}</code>") if r["sql"] else "<span class='muted'>—</span>"
            trs.append(
                f"<tr><td class='muted mono' style='white-space:nowrap'>{_esc(r['ts'])}</td>"
                f"<td class='mono'>{_esc(r['agent'])}</td>"
                f"<td><code>{_esc(r['project'])}/{_esc(r['connection'])}</code>"
                f"{(' ' + _env_badge(r['environment'])) if r['environment'] else ''}</td>"
                f"<td class='mono muted'>{_esc(r['tool'])}</td>"
                f"<td>{sqlcell}</td>"
                f"<td>{_badge(r['status'], _STATUS_COLOR)}</td>"
                f"<td class='muted'>{statline}{dline}</td></tr>"
            )
        table_rows = "".join(trs) or '<tr><td colspan="7" class="muted">（无匹配记录）</td></tr>'

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
            + "<a href='/admin/audit' style='margin-left:4px'>清除</a>"
            + "<label style='margin:0 0 0 auto;display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted)'>"
            "<input type='checkbox' id='auto-refresh' style='width:auto'>自动刷新（5s）</label>"
            "</form>"
        )

        # 分页保留筛选参数
        def _url(off: int) -> str:
            parts = [f"limit={limit}", f"offset={off}"] + [f"{k}={_esc(v)}" for k, v in filters.items()]
            return "/admin/audit?" + "&".join(parts)
        pager_parts = []
        if offset > 0:
            pager_parts.append(f"<a href='{_url(max(offset - limit, 0))}'>← 较新</a>")
        if offset + limit < total:
            pager_parts.append(f"<a href='{_url(offset + limit)}'>较旧 →</a>")
        shown = f"第 {offset + 1}–{min(offset + limit, total)} 条" if total else "无记录"
        pager = (f"<div class='filters' style='margin-top:12px'>共 {total} 条 · {shown} "
                 + " · ".join(pager_parts) + "</div>")

        body = (
            _pagehead("Audit Log", "操作审计", "每次数据库操作的完整留痕：谁、何时、在哪个库、跑了什么、结果如何")
            + f"<div class='card'>{filter_bar}"
            f"<table class='audit'><tr><th>时间</th><th>agent</th><th>连接</th><th>工具</th>"
            f"<th>SQL</th><th>状态</th><th>结果</th></tr>{table_rows}</table>{pager}</div>"
            "<script>(function(){"
            "var box=document.getElementById('auto-refresh');if(!box)return;"
            "var on=localStorage.getItem('dbm-audit-refresh')==='1';box.checked=on;"
            "var t=on?setTimeout(function(){location.reload();},5000):null;"
            "box.addEventListener('change',function(){"
            "localStorage.setItem('dbm-audit-refresh',box.checked?'1':'0');"
            "if(box.checked)location.reload();else if(t)clearTimeout(t);});"
            "})();</script>"
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
                stripe = _ENV_COLOR.get(c.environment, "#64748b")
                db = f"<code>{_esc(c.database)}</code>" if c.database else "<span class='muted'>—</span>"
                rows.append(
                    f"<tr><td style='border-left:3px solid {stripe};padding-left:13px'>"
                    f"<code>{_esc(pname)}/{_esc(cname)}</code></td><td class='mono muted'>{_esc(c.engine)}</td>"
                    f"<td>{_env_badge(c.environment)}</td><td><code>{_esc(c.host)}:{_esc(c.port)}</code></td>"
                    f"<td>{db}</td><td class='muted mono'>{_esc(jump)}</td>"
                    f"<td style='white-space:nowrap'><a href='/admin/connections?edit={_esc(pname)}/{_esc(cname)}'>编辑</a> · "
                    f"<form method='post' action='/admin/connections/delete' style='display:inline' "
                    f"onsubmit='return confirm(\"删除连接 {_esc(pname)}/{_esc(cname)}？\")'>"
                    f"<input type='hidden' name='project' value='{_esc(pname)}'>"
                    f"<input type='hidden' name='connection' value='{_esc(cname)}'>"
                    f"<button class='btn btn-reject' style='padding:3px 11px;font-size:12.5px'>删除</button></form></td></tr>"
                )
        table = "".join(rows) or '<tr><td colspan="7" class="muted">（无连接）</td></tr>'

        keyring_note = "" if _keyring_available() else (
            "<p style='color:#b00020'>⚠️ 未安装 keyring，无法安全存储密码。"
            "请 <code>pip install 'db-manage-mcp[keyring]'</code> 后重启。</p>"
        )
        form = _connection_form(e_project, e_conn, edit_cfg)
        body = (
            _pagehead("Connections", "连接管理", "主账号应为只读账号；保存时自动校验权限，密码写入系统钥匙串")
            + f"<div class='card'><h2>连接列表</h2>"
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
                force_privileged=str(f.get("force_privileged") or "") in ("1", "on", "true"),
            )
        except (ConnectionAdminError, QueryRejected, ValueError) as e:
            # 前端用 fetch 提交（Accept: json）→ 返回 JSON，页面 inline 提示、不清空表单
            if "application/json" in req.headers.get("accept", ""):
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            body = f"<div class='card'><h2>保存失败</h2><p style='color:#b00020'>{_esc(e)}</p>" \
                   f"<a href='/admin/connections'>← 返回</a></div>"
            return HTMLResponse(_page("保存失败", body), status_code=400)
        if "application/json" in req.headers.get("accept", ""):
            return JSONResponse({"ok": True})
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
            "jump_hosts": [x.strip() for x in str(f.get("jump_hosts") or "").split(",") if x.strip()],
            "ssh_options": (
                (["-i", str(f.get("ssh_key_path")).strip()] if str(f.get("ssh_key_path") or "").strip() else [])
                + [x for x in str(f.get("ssh_options_extra") or "").split(" ") if x]
            ),
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
