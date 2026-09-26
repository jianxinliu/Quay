"""系统设置页与保存接口。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

if TYPE_CHECKING:

    from ..service import DbmService

from .common import (
    _bool_setting,
    _esc,
    _num_setting,
    _plain_settings_page,
    _select_setting,
    _set_row,
    _set_section,
    _settings_layout,
    _text_setting,
)
from .connections import _connections_body, _engine_icon, _ssh_identities_body
from .context import AdminContext


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
                         "单个连接可在连接管理里覆盖。旁边的 token 数是按英文密度粗估的，"
                         "中文结果的实际 token 会明显更多。",
                         unit="字符", read="tokens")
            + _num_setting("会话累计配额", "agent_session_budget_chars", s, 400000,
                           "一个会话累计返回多少字符后停止取数。撞到上限时 agent 必须先问你，"
                           "你同意后它才能追加额度。填 0 = 不限制。",
                           unit="字符", read="tokens",
                           more="<b>配额以字符为单位强制</b>——字符数是确定、可复现的，不依赖任何模型的分词器；"
                                "旁边那个 token 数只是粗估的注解（按英文密度算，中文会更多）。"
                                "看板上「已用多少 token」按实际返回文本的字符类别分别估算，比这里准。<br>"
                                "单次预算管不住「一直查」——一次 1 万字符查两百次照样烧掉几十万 token，"
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


def _ai_api_key_present() -> bool:
    """当前 keyring 里是否已存 AI API key（只判有无，不取值）。"""
    try:
        import keyring  # noqa: PLC0415

        from ..ai import AI_API_KEY_ACCOUNT  # noqa: PLC0415
        from ..secrets import KEYRING_SERVICE  # noqa: PLC0415
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
    from ..notify import is_macos  # noqa: PLC0415
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


def _settings_drivers_body(service: "DbmService") -> str:
    """驱动 tab：已注册的数据库驱动清单 + 客户端库装没装 + 怎么加一种新库支持。

    驱动是**代码模块**（src/dbmcp/drivers/，import 即完成注册），不是配置项——所以
    这里是只读页：告诉使用者「现在支持哪些库、各自什么能力、客户端库装全没有」，
    以及加一种新库支持只需要加一个驱动文件。使用者在配置里选引擎时看到的下拉、
    图标、默认端口全部来自这张表。
    """
    import importlib.util

    from ..drivers import DRIVERS

    def lib_cell(drv) -> str:
        if not drv.client_lib:
            return "<span class='lib std'>Python 标准库 sqlite3</span>"
        mod, pip = drv.client_lib
        ok = importlib.util.find_spec(mod) is not None
        cls = "ok" if ok else "miss"
        word = "已安装" if ok else "未安装"
        hint = ("缺这个库，该引擎的连接会建不上；装一下："
                if not ok else "")
        cmd = f"uv sync                      # 或 pip install {pip}"
        return (f"<span class='lib {cls}'>● {word}</span>"
                f"<code class='pip'>{_esc(pip)}</code>"
                + (f"<span class='lib-hint'>{hint}<code>{cmd}</code></span>" if not ok else ""))

    def cap(name: str, on: bool, desc: str) -> str:
        cls = "on" if on else "off"
        return (f"<span class='cap {cls}' title='{_esc(desc)}'>{_esc(name)}</span>")

    rows = []
    for name in sorted(DRIVERS):
        drv = DRIVERS[name]
        caps = []
        if not drv.connectable:
            caps.append(cap("进程内", True, "不占连接：分析工作台的本地 DuckDB 引擎，仅元数据参与注册"))
        else:
            caps.append(cap("可连接", True, "能配置成一条连接"))
        caps.append(cap("需选库层", drv.needs_database_layer,
                        "PG 的 database/schema 是两层：一条连接只绑一个库，浏览别的库要另建连接"))
        caps.append(cap("可同步目标", drv.sync_target, "能作为表同步的目标（要能 CREATE TABLE + INSERT）"))
        caps.append(cap("AI 生成 SQL", drv.ai_sql, "AI 生成 SQL / 流程时按本引擎方言产出并转写"))
        rows.append(
            "<tr>"
            f"<td class='eng'>{_engine_icon(name)}<b>{_esc(name)}</b></td>"
            f"<td>{lib_cell(drv)}</td>"
            f"<td class='dim'>{_esc(drv.dialect or '—')}</td>"
            f"<td class='port'>{_esc(str(drv.default_port or '—'))}</td>"
            f"<td class='caps'>{''.join(caps)}</td>"
            "</tr>"
        )

    css = ("<style>"
           ".drv-tbl{width:100%;border-collapse:collapse}"
           ".drv-tbl th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;"
           "padding:4px 10px;border-bottom:1px solid var(--border);white-space:nowrap}"
           ".drv-tbl td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}"
           ".drv-tbl td.eng{white-space:nowrap}"
           ".drv-tbl td.eng b{font-family:var(--mono);font-size:12.5px}"
           ".drv-tbl td.dim,.drv-tbl td.port{font-family:var(--mono);font-size:12px;color:var(--muted)}"
           ".drv-tbl .lib{font-size:12px;white-space:nowrap}"
           ".drv-tbl .lib.ok{color:#166534} .drv-tbl .lib.miss{color:#c02a26;font-weight:600}"
           ".drv-tbl .lib.std{color:var(--faint)}"
           ".drv-tbl .pip{margin-left:7px;background:var(--paper);padding:1px 6px;border-radius:5px;"
           "font-family:var(--mono);font-size:11.5px}"
           ".drv-tbl .lib-hint{display:block;margin-top:4px;color:var(--muted);font-size:11.5px}"
           ".drv-tbl .lib-hint code{background:var(--ink);color:#d7dde6;padding:2px 6px;border-radius:5px;"
           "font-family:var(--mono);font-size:11px}"
           ".drv-tbl .caps{display:flex;flex-wrap:wrap;gap:5px;min-width:220px}"
           ".drv-tbl .cap{font-size:11px;padding:1px 8px;border-radius:9px;white-space:nowrap;"
           "border:1px solid var(--border);color:var(--muted);cursor:help}"
           ".drv-tbl .cap.on{border-color:#9ec0a4;color:#166534;background:#f0f7f1}"
           "pre.cmd{background:var(--ink);color:#d7dde6;border-radius:8px;padding:10px 12px;overflow-x:auto;"
           "font-family:var(--mono);font-size:12.5px;margin:0}"
           "</style>")

    table = ("<table class='drv-tbl'><thead><tr>"
             "<th>引擎</th><th>客户端库</th><th>sqlglot 方言</th><th>默认端口</th><th>能力</th>"
             "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    howto = (
        "<div class='card'><h3>新增一种数据库支持</h3>"
        "<p class='muted'>加一种库 = 加一个驱动文件，不必改别处（引擎名已在配置层放宽为字符串，"
        "连接表单的引擎下拉、默认端口、图标都从驱动注册表来）。</p>"
        "<ol style='margin:10px 0 4px;padding-left:22px;color:var(--text);font-size:13px;line-height:1.9'>"
        "<li>在 <code>src/dbmcp/drivers/</code> 加一个模块（如 <code>mssql.py</code>），"
        "继承 <code>DbDriver</code>，填 <code>name</code> / <code>dialect</code> / "
        "<code>default_port</code>，按需覆盖 <code>build_engine</code> 等方法。</li>"
        "<li>关系库基本零代码：SQLAlchemy 的 dialect 已覆盖 MySQL / PostgreSQL / SQLite / "
        "MSSQL / Oracle / DB2…，<code>build_engine</code> 就是拼 URL；没有现成方言的库"
        "（Redis 是先例）自己实现接口的几个方法即可。</li>"
        "<li>在 <code>src/dbmcp/drivers/__init__.py</code> 加一行 <code>import</code> 完成注册。</li>"
        "<li>重启服务（<code>bash scripts/install-launchd.sh</code> 或重启 <code>dbm serve</code>），"
        "新建连接时引擎下拉就会自动出现它。</li>"
        "</ol>"
        "<p class='muted' style='margin-top:10px'>基类方法都给了能用的默认实现（取消为空操作、"
        "容量/行数估算返回空、DDL 走反射拼近似、语法复核标不支持），新驱动只覆盖真正支持的部分，"
        "缺的能力会优雅降级而不是报错。</p>"
        "<p class='muted' style='margin-top:8px'>Redis 有独立适配器（<code>redis_engine.py</code>，"
        "不走 SQLAlchemy），不在本表内，同样可用——它专属于 Redis 控制台。</p>"
        "</div>"
    )
    return (css
            + f"<div class='card'><h3>已注册驱动（{len(DRIVERS)} 种）</h3>"
            + "<p class='muted'>驱动在哪里：<code>src/dbmcp/drivers/</code>。import 即注册，"
              "重复注册以最后一次为准（便于测试替换）。</p>"
            + table + "</div>" + howto)


def _settings_info_body(service: "DbmService", req: "Request") -> str:
    """系统信息 tab：只读展示项目/数据/日志路径、运行时信息、登录 token 获取与更新指引。"""

    from ..secrets import KEYRING_SERVICE
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


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _shell = ctx._shell

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
        from ..settings import DEFAULTS as _SETTING_DEFAULTS
        updates = {key: str(f.get(key)) for key in _SETTING_DEFAULTS if key in f}
        # AI API key 单独处理：存钥匙串、绝不入设置库；勾选清除则删除
        from ..ai import AI_API_KEY_ACCOUNT
        from ..secrets import (
            KEYRING_SERVICE,
            SecretResolveError,
            delete_keyring_secret,
            store_keyring_secret,
        )
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
            "drivers": lambda: _settings_drivers_body(service),
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
