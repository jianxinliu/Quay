"""管理后台公共原语：认证/本机来源校验、HTML 转义与徽章、页面外壳、错误分类。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import os
from typing import TYPE_CHECKING

from starlette.requests import Request

from ..drivers import (
    engine_dialect,
)

_COOKIE_NAME = "dbm_admin"


def _engine_dialect(engine: str) -> str | None:
    """sqlglot 方言名（用于 SQL 美化与编辑器 lint）。无方言/未注册 → None。"""
    return engine_dialect(engine)


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
    from ..errors import error_label, translate_db_error
    from ..health import ConnectionUnavailable, is_connection_error
    from ..service import QueryRejected

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
    dialect = _engine_dialect(engine)
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


_SETTINGS_TABS = [("general", "整体设置"), ("db", "DB"), ("redis", "Redis"),
                  ("ai", "AI 助手"), ("notify", "通知"),
                  ("connections", "连接管理"), ("ssh", "SSH 配置"), ("info", "系统信息")]

# 分区按「做什么用」分组，而不是平铺八个 tab——组本身就是信息：
# 偏好=看着舒服，能力=接外部服务，资源=连库与凭证，系统=只读信息。
_SETTINGS_GROUPS = [
    ("偏好", [("general", "整体"), ("db", "查询与 Agent"), ("redis", "Redis")]),
    ("能力", [("ai", "AI 助手"), ("notify", "通知")]),
    ("资源", [("connections", "连接管理"), ("ssh", "SSH 配置")]),
    ("", [("drivers", "驱动"), ("info", "系统信息")]),
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
    from ..settings import DEFAULTS  # noqa: PLC0415
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


def _text_setting(label: str, name: str, s: dict, default: str, hint: str,
                  wide: bool = False, more: str = "") -> str:
    val = _esc(str(s.get(name, default)))
    ctl = f"<input type='text' name='{name}' value='{val}' placeholder='{_esc(str(default))}'>"
    return _set_row(name, label, hint, ctl, s, wide=wide, more=more)
