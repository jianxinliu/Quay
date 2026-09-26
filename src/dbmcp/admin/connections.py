"""连接管理与 SSH 配置的保存 / 删除 / 测试。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..drivers import (
    UnsupportedEngineError,
    connectable_engines,
    engine_default_port,
    get_driver,
)

if TYPE_CHECKING:

    from ..service import DbmService

from .common import _ENV_COLOR, _env_badge, _esc, _page, _set_row, _set_section
from .context import AdminContext

# redis 有独立适配器（redis_engine.py，不走 SQLAlchemy），不在驱动注册表里；
# 把它单列出来，是因为连接表单/默认端口/图标仍然要用到它。
_NON_DRIVER_ENGINES = ("redis",)


def _connectable_engines() -> tuple[str, ...]:
    """连接表单下拉的引擎清单 = 已注册且可连接的驱动 + 独立适配器（redis）。

    新增一种可连数据库时这里自动出现——只需在 drivers/ 加驱动并注册。
    """
    return tuple(connectable_engines()) + _NON_DRIVER_ENGINES


# 非注册表引擎的图标（redis）；驱动自带的图标见各驱动的 icon 属性
_EXTRA_ENGINE_ICONS = {"redis": "redis"}

# 引擎 → 连接表单的默认端口（空串 = 不预填）。来自驱动的 default_port；redis 单列。
def _engine_default_ports() -> dict[str, str]:
    out: dict[str, str] = {}
    for e in _connectable_engines():
        # redis 等非注册表引擎没有驱动，端口单列在下面
        port = engine_default_port(e)
        out[e] = str(port) if port else ""
    out["redis"] = "6379"
    return out


# 「看起来仍是自动填的」端口集合：用户没定制时才允许引擎切换覆盖它。
# 表单每次渲染都现算——晚注册的驱动（如测试替身）也能进下拉。
def _default_ports_js() -> str:
    return "{" + ",".join(f"{e}:'{v}'" for e, v in _engine_default_ports().items()) + "}"


def _auto_ports_js() -> str:
    return json.dumps(sorted({""} | set(_engine_default_ports().values())))


def _engine_icon_file(engine: str) -> str | None:
    """品牌 logo 文件名（devicon，vendored 到 static/db-icons/）；没有就 None。"""
    try:
        f = get_driver(engine).icon
    except UnsupportedEngineError:
        f = None
    return f or _EXTRA_ENGINE_ICONS.get(engine)


def _engine_icon(engine: str) -> str:
    """连接列表里引擎名前的品牌 logo（无对应图标则空串）。文件名来自驱动的 icon 属性。"""
    f = _engine_icon_file(engine)
    if not f:
        return ""
    return (f"<img src='/admin/static/db-icons/{f}.svg' alt='' title='{_esc(engine)}' "
            f"style='width:15px;height:15px;vertical-align:middle;margin-right:6px'>")


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
        for e in _connectable_engines()
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
    var DEFAULT_PORTS = {_default_ports_js()};
    var AUTO_PORTS = {_auto_ports_js()};
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
                    f"<td><div class='conn-line'>"
                    f"<a class='conn-name' href='{edit_url}'>{_esc(cname)}</a>"
                    f"<span class='conn-where mono muted' title='{where}'>{where}</span>"
                    f"</div></td>"
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
    from ..connections import identity_referers

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


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _caller = ctx._caller

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
        from ..connections import ConnectionAdminError
        from ..service import QueryRejected
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
        from ..connections import ConnectionAdminError
        from ..service import QueryRejected
        f = await req.form()
        try:
            service.delete_connection(str(f.get("project")), str(f.get("connection")), _caller(req))
        except (ConnectionAdminError, QueryRejected) as e:
            return HTMLResponse(_page("删除失败", f"<div class='card'>{_esc(e)}</div>"), status_code=400)
        return RedirectResponse(url="/admin/settings?tab=connections", status_code=303)

    @mcp.custom_route("/admin/ssh-identities/save", methods=["POST"])
    @guard
    async def _ssh_identity_save(req: Request) -> Response:
        from ..connections import ConnectionAdminError
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
        from ..connections import ConnectionAdminError
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
