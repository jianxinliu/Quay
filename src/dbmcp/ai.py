"""AI 辅助生成 SQL：把表结构 + 自然语言需求丢给命令行 AI，拿回一条可执行 SQL（可选带解释）。

只生成、不执行——产物回填到编辑器光标处，由人审阅后再走既有的写确认/审批闭环。AI 进程
不被授予任何工具（禁 MCP、只读沙箱），纯文本进、纯文本出，碰不到数据库。

Provider 可插拔，上层 service 不感知差异：
- claude:  `claude -p <prompt> --output-format json`，从 result 字段取正文
- codex:   `codex exec ... -o <file> <prompt>`，从 --output-last-message 文件取正文
- api:     直连 HTTP（Anthropic Messages / OpenAI Chat），密钥从 keyring/env 读；追问用内存会话

prompt 拼装（build_sql_prompt）与调用（run_ai）拆开，前者是纯函数、可单测；后者用假进程/
假 httpx monkeypatch 测。
"""

from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass

# 会话续接需要稳定的工作目录，且两家 CLI 各用独立目录（共用会让 codex 在 claude 写下的
# 目录里启动异常变慢/超时）：
#   claude 的 --resume <id> 按「项目目录」定位会话存档；
#   codex 的 `exec resume` 默认按 cwd 过滤会话（见 --all），故也要固定 cwd 才能命中。
# 用固定空目录（无 CLAUDE.md，省上下文）。
_CLAUDE_CWD = os.path.join(tempfile.gettempdir(), "dbm-ai-cwd-claude")
_CODEX_CWD = os.path.join(tempfile.gettempdir(), "dbm-ai-cwd-codex")

# 默认系统提示词，用户可在系统设置里覆盖（settings.ai_sql_prompt）。约束对齐 MCP instructions
# 里那套「写 SQL 的约定」，让 AI 产出的 SQL 一上来就带 LIMIT、走索引、不踩时区坑。
DEFAULT_SQL_PROMPT = """你是一位严谨的数据库 SQL 专家。根据给定的表结构和用户需求，生成一条可直接执行的 SQL。

硬性要求：
1. 大表查询必须带 LIMIT 或 WHERE 收窄，绝不无条件全表拉取；不确定量级时默认加 LIMIT 1000。
2. 尽量命中索引：WHERE / JOIN / ORDER BY 的过滤列优先用表结构里已有的索引列。
3. 不要对索引列套函数或做运算（如 DATE(ts)、FROM_UNIXTIME(ts)、ts+1 放在 WHERE 左侧）——会使索引失效；应对常量侧做转换，用范围比较（如 ts >= 起 AND ts < 止）。
4. 涉及时区函数（FROM_UNIXTIME / NOW / CURDATE 等）时，不要臆断会话时区，也不要重复叠加时区偏移。
5. 只用给定表结构里真实存在的表和列，不要臆造字段。
6. 只生成一条语句（除非需求明确要求多条）。"""

# 输出契约（附在 prompt 末尾，随「是否解释」切换）。
_CONTRACT_PLAIN = "只输出一条可执行 SQL，不要任何解释、不要 markdown 代码围栏。"
_CONTRACT_EXPLAIN = (
    '输出一个 JSON 对象：{"sql": "<一条可执行 SQL>", "explanation": "<精简说明>"}。'
    "explanation 用中文、尽量精简，只讲实现关键（如何命中索引、为何这样写、注意点），"
    "不要复述需求本身、不要长篇大论。只输出该 JSON，不要 markdown 代码围栏。"
)

# 各 provider 的默认二进制名（cli_path 为空时用）。
_DEFAULT_CLI = {"claude": "claude", "codex": "codex"}


class AIError(Exception):
    """AI 生成失败，message 面向使用者。"""


@dataclass
class AIResult:
    sql: str
    explanation: str  # 空串 = 无解释
    session_id: str = ""  # AI 会话 id，追问时带回以续接同一会话


def _api_kwargs(api: dict | None) -> dict:
    """把 {base, format, key_env} 归一为 run_ai 的 api_* 关键字（provider≠api 时无副作用）。"""
    api = api or {}
    return {"api_base": api.get("base", ""), "api_format": api.get("format", "anthropic"),
            "api_key_env": api.get("key_env", "")}


def build_sql_prompt(
    system_prompt: str,
    dialect: str,
    ddls: list[tuple[str, str]],
    question: str,
    *,
    explain: bool,
    samples: dict[str, str] | None = None,
) -> str:
    """拼出给 AI 的完整 prompt（纯函数）。

    system_prompt: 用户可配的系统提示词（persona + 约束）
    dialect:       目标方言（mysql / postgres / sqlite …）
    ddls:          [(表名, 建表语句), ...]
    samples:       {表名: 样本行文本} 或 None（不喂样本）
    """
    parts = [(system_prompt or DEFAULT_SQL_PROMPT).strip(), ""]
    parts.append(f"目标数据库方言：{dialect}")
    parts.append("")
    parts.append("=== 相关表结构 ===")
    if ddls:
        for name, ddl in ddls:
            parts.append((ddl or "").strip())
            parts.append("")
    else:
        parts.append("（未提供表结构）")
        parts.append("")
    if samples:
        parts.append("=== 样本数据（仅供理解数据形态，勿据此臆测全量）===")
        for name, text in samples.items():
            parts.append(f"-- {name}")
            parts.append((text or "").strip())
            parts.append("")
    parts.append("=== 需求 ===")
    parts.append((question or "").strip())
    parts.append("")
    parts.append(_CONTRACT_EXPLAIN if explain else _CONTRACT_PLAIN)
    return "\n".join(parts)


def build_followup_prompt(question: str, *, explain: bool) -> str:
    """追问 prompt（续接已有会话）：不重发表结构，只带新的调整要求 + 输出契约。"""
    parts = ["在上一条 SQL 的基础上按下面的要求调整，仍然遵守之前的所有约束：",
             "", (question or "").strip(), "",
             _CONTRACT_EXPLAIN if explain else _CONTRACT_PLAIN]
    return "\n".join(parts)


def generate_sql(
    *,
    system_prompt: str,
    dialect: str,
    ddls: list[tuple[str, str]],
    question: str,
    explain: bool,
    samples: dict[str, str] | None,
    provider: str,
    model: str,
    timeout: int,
    cli_path: str = "",
    session_id: str | None = None,
    api: dict | None = None,
) -> AIResult:
    """拼 prompt → 调 AI → 解析出 {sql, explanation, session_id}。失败抛 AIError。

    session_id 非空 = 追问：续接同一会话、只发调整要求（不重发表结构）。
    api = provider=api 时的 {base, format, key_env}（可选）。
    """
    if session_id:
        prompt = build_followup_prompt(question, explain=explain)
    else:
        prompt = build_sql_prompt(system_prompt, dialect, ddls, question,
                                  explain=explain, samples=samples)
    raw, new_sid = run_ai(prompt, provider=provider, model=model, timeout=timeout,
                          cli_path=cli_path, session_id=session_id, **_api_kwargs(api))
    result = parse_ai_output(raw, explain=explain)
    result.session_id = new_sid
    return result


# ---------- workflow（DAG 画布）生成 ----------

# 教 AI 画布 DAG 的 JSON 结构与节点契约（对齐 workflows.compile_graph）。
WORKFLOW_FORMAT_DOC = """\
Workflow 是一张有向无环图（DAG），在 DuckDB 分析工作区里从各数据库连接取数、逐步加工。
输出一个 JSON 对象：{"nodes":[...],"edges":[...]}。

节点 node = {"id":"唯一短id","type":"类型","name":"节点名","cfg":{...}}
- name 必须字母开头、仅含字母/数字/下划线（会作为中间视图名，下游用 name 引用它）
- 不要写 x/y 坐标（由系统自动排版）
type 及其 cfg：
- source   取数（无输入）      cfg:{"conn":"project/connection","sql":"SELECT ...","limit":可选整数}
- filter   过滤（1 输入 in）    cfg:{"where":"amount > 0"}         → SELECT * FROM 上游 WHERE ...
- join     连接（2 输入 left/right）cfg:{"kind":"INNER|LEFT|RIGHT|FULL","on":"l.id = r.uid","select":"l.*, r.name"}  左表别名 l、右表别名 r
- aggregate 聚合（1 输入 in）   cfg:{"group":"city","aggs":"count(*) AS n, sum(amount) AS total"}
- sql      自由 SQL（任意输入） cfg:{"sql":"SELECT ... FROM 上游节点name ..."}   直接用上游节点的 name 当表名
- output   输出（1 输入 in，最多一个，终点）cfg:{"order_by":"total DESC","limit":100}

边 edge = {"from":"上游node.id","to":"下游node.id","port":"in|left|right"}
- 普通节点用 port "in"；join 的两个输入分别用 "left" 和 "right"
- from/to 用节点的 id（不是 name）；下游 SQL 里引用上游时用 name

规则：source 的 conn 必须是给定「可用连接」里的某个值；只用给定表结构里真实存在的表和列；图必须无环。

示例（把一个库的订单按用户聚合再排序）：
{"nodes":[
  {"id":"a","type":"source","name":"orders","cfg":{"conn":"demo/main","sql":"SELECT user_id, amount FROM orders","limit":100000}},
  {"id":"b","type":"aggregate","name":"by_user","cfg":{"group":"user_id","aggs":"sum(amount) AS total"}},
  {"id":"c","type":"output","name":"result","cfg":{"order_by":"total DESC","limit":50}}
 ],
 "edges":[{"from":"a","to":"b","port":"in"},{"from":"b","to":"c","port":"in"}]}"""

DEFAULT_WORKFLOW_PROMPT = (
    "你是数据分析流程设计专家。根据可用连接、表结构和用户需求，设计一张 Workflow DAG。"
    "尽量用少而清晰的节点表达需求；取数节点的 SQL 只取需要的列并带合理 LIMIT。"
)

_WF_CONTRACT = "只输出该 JSON 对象，不要 markdown 代码围栏、不要额外解释。"


def build_workflow_prompt(
    system_prompt: str,
    dialect: str,
    connections: list[str],
    ddls: list[tuple[str, str]],
    question: str,
) -> str:
    """拼出让 AI 生成 workflow DAG 的完整 prompt（纯函数）。"""
    parts = [(system_prompt or DEFAULT_WORKFLOW_PROMPT).strip(), "", WORKFLOW_FORMAT_DOC, ""]
    parts.append(f"取数节点里 SQL 的数据库方言：{dialect}")
    parts.append("可用连接（source 的 conn 只能从中选）：")
    parts.append("、".join(connections) if connections else "（无）")
    parts.append("")
    parts.append("=== 相关表结构 ===")
    for name, ddl in ddls:
        parts.append((ddl or "").strip())
        parts.append("")
    parts.append("=== 需求 ===")
    parts.append((question or "").strip())
    parts.append("")
    parts.append(_WF_CONTRACT)
    return "\n".join(parts)


def build_workflow_repair_prompt(error: str) -> str:
    """重修 prompt（续接会话）：把编译错误回喂给 AI，让它改好重出完整 JSON。"""
    return ("上一版流程图校验没通过，错误：\n" + (error or "").strip()
            + "\n\n请修正后重新输出**完整**的 JSON 对象（nodes+edges）。" + _WF_CONTRACT)


def generate_workflow(
    *,
    system_prompt: str,
    dialect: str,
    connections: list[str],
    ddls: list[tuple[str, str]],
    question: str,
    provider: str,
    model: str,
    timeout: int,
    cli_path: str = "",
    repair_error: str | None = None,
    session_id: str | None = None,
    api: dict | None = None,
) -> tuple[dict, str]:
    """拼 prompt → 调 AI → 解析出 workflow graph dict。返回 (graph, session_id)。失败抛 AIError。

    repair_error 非空 = 重修：续接会话、只回喂编译错误（不重发表结构）。
    """
    if repair_error and session_id:
        prompt = build_workflow_repair_prompt(repair_error)
    else:
        prompt = build_workflow_prompt(system_prompt, dialect, connections, ddls, question)
    raw, new_sid = run_ai(prompt, provider=provider, model=model, timeout=timeout,
                          cli_path=cli_path, session_id=session_id, **_api_kwargs(api))
    graph = _parse_workflow_output(raw)
    if not isinstance(graph, dict) or "nodes" not in graph:
        raise AIError(f"AI 未按格式输出流程图：{raw.strip()[:200]}")
    graph.setdefault("edges", [])
    return graph, new_sid


def _parse_workflow_output(text: str) -> object:
    return _try_json(_strip_fences((text or "").strip()))


# ---------- provider 调用 ----------


# provider=api 的会话存 messages（内存、有界）；追问带回 session_id 续接同一对话。
_API_SESSIONS: "collections.OrderedDict[str, list[dict]]" = collections.OrderedDict()
_API_SESSIONS_MAX = 100

AI_API_KEY_ACCOUNT = "ai_api_key"  # keyring 里存 API key 的 account（service=db-manage-mcp）


def run_ai(prompt: str, *, provider: str, model: str, timeout: int,
           cli_path: str = "", session_id: str | None = None,
           api_base: str = "", api_format: str = "anthropic", api_key_env: str = "") -> tuple[str, str]:
    """按 provider 调 AI，返回 (原始正文, 会话 id)。session_id 非空 = 续接会话。失败抛 AIError。"""
    if provider == "api":
        return _run_api(prompt, model=model, timeout=timeout, session_id=session_id,
                        base=api_base, fmt=api_format, key_env=api_key_env)
    cli = cli_path.strip() or _DEFAULT_CLI.get(provider, "")
    if provider == "claude":
        return _run_claude(prompt, model=model, timeout=timeout, cli=cli, session_id=session_id)
    if provider == "codex":
        return _run_codex(prompt, model=model, timeout=timeout, cli=cli, session_id=session_id)
    raise AIError(f"不支持的 AI provider：{provider!r}（当前支持 claude / codex / api）")


def _resolve_api_key(key_env: str) -> str:
    """取 API key：优先系统钥匙串（后台页面存的），回退环境变量。绝不打印/落库。"""
    try:
        import keyring  # noqa: PLC0415
        from .secrets import KEYRING_SERVICE  # noqa: PLC0415
        v = keyring.get_password(KEYRING_SERVICE, AI_API_KEY_ACCOUNT)
        if v:
            return v
    except Exception:  # 无 keyring 后端/未安装 → 回退 env
        pass
    return os.environ.get((key_env or "DBM_AI_API_KEY").strip(), "")


def _run_api(prompt: str, *, model: str, timeout: int, session_id: str | None,
             base: str, fmt: str, key_env: str) -> tuple[str, str]:
    """直连 HTTP API（Anthropic Messages / OpenAI Chat）。密钥从 keyring/env 读，绝不落库/日志。

    追问续接：内存里按 session_id 存整段对话 messages，续接时追加新一轮再整体发过去。
    """
    import httpx  # 惰性导入

    key = _resolve_api_key(key_env)
    if not key:
        raise AIError(f"未配置 API key：请在后台系统设置填写并保存，或设置环境变量 {key_env or 'DBM_AI_API_KEY'}")
    if session_id and session_id in _API_SESSIONS:
        messages = _API_SESSIONS[session_id]
    else:
        messages, session_id = [], uuid.uuid4().hex
    messages = list(messages) + [{"role": "user", "content": prompt}]

    base = (base or "https://api.anthropic.com").rstrip("/")
    try:
        if fmt == "openai":
            url = base + "/v1/chat/completions"
            headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
            body = {"model": model or "gpt-4o", "messages": messages}
        else:  # anthropic
            url = base + "/v1/messages"
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            body = {"model": model or "claude-sonnet-5", "max_tokens": 4096, "messages": messages}
        resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise AIError(f"调用 API 失败：{e}") from e
    if resp.status_code != 200:
        raise AIError(f"API 返回 {resp.status_code}：{resp.text[:300]}")
    data = resp.json()
    if fmt == "openai":
        text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    else:
        text = "".join(b.get("text", "") for b in (data.get("content") or [])
                       if b.get("type") == "text").strip()
    if not text:
        raise AIError(f"API 未返回文本：{json.dumps(data)[:300]}")
    # 存回会话（新建列表，不改动已发出的 messages）+ 移到末尾 + 限量淘汰
    _API_SESSIONS[session_id] = [*messages, {"role": "assistant", "content": text}]
    _API_SESSIONS.move_to_end(session_id)
    while len(_API_SESSIONS) > _API_SESSIONS_MAX:
        _API_SESSIONS.popitem(last=False)
    return text, session_id


def _run_subprocess(cmd: list[str], *, timeout: int, cli: str, cwd: str) -> subprocess.CompletedProcess:
    os.makedirs(cwd, exist_ok=True)
    try:
        # stdin=DEVNULL 关键：非 TTY（守护进程/subprocess）下 codex 见到「管道 stdin」会等着读
        # 输入 → 挂起到超时；给它立即 EOF 的 /dev/null 才不会卡。claude 无此问题但一并给上无害。
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise AIError(f"找不到 {cli} CLI，请确认已安装并在 PATH 中，或在系统设置里指定路径") from None
    except subprocess.TimeoutExpired:
        raise AIError(f"AI 生成超时（{timeout}s），可在系统设置调大超时或简化需求") from None


def _run_claude(prompt: str, *, model: str, timeout: int, cli: str,
                session_id: str | None = None) -> tuple[str, str]:
    # 稳定空目录（支持 --resume）+ 一组省 token/加固 flags（实测 input token 从 ~25k 降到 ~260）：
    #   --system-prompt 极简  → 替换掉 Claude Code 自身庞大的 agent 系统提示（规则仍在 prompt 里）
    #   --setting-sources ''  → 不加载任何 CLAUDE.md / settings（省上下文、行为可预期）
    #   --strict-mcp-config + 空 mcp   → 不加载任何 MCP 工具
    #   --disallowedTools '*' → 禁用全部工具（纯文本生成、碰不到文件/库，安全兜底）
    cmd = [cli, "-p", prompt, "--output-format", "json",
           "--system-prompt", "只输出被要求的内容，不要多余说明。",
           "--setting-sources", "",
           "--disallowedTools", "*",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    if session_id:
        cmd += ["--resume", session_id]
    if model:
        cmd += ["--model", model]
    proc = _run_subprocess(cmd, timeout=timeout, cli=cli, cwd=_CLAUDE_CWD)
    if not (proc.stdout or "").strip():
        raise AIError(f"claude 调用失败：{(proc.stderr or '').strip()[:500] or '无输出'}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AIError(f"无法解析 claude 输出：{proc.stdout.strip()[:300]}")
    if data.get("is_error"):
        raise AIError(f"claude 返回错误：{data.get('result') or data.get('subtype') or '未知'}")
    return (data.get("result") or "").strip(), str(data.get("session_id") or "")


def _run_codex(prompt: str, *, model: str, timeout: int, cli: str,
               session_id: str | None = None) -> tuple[str, str]:
    # 最终答案写到 -o 文件（免解析 JSONL）。会话持久化（不加 --ephemeral）以支持 resume；
    # 追问用 `codex exec resume <id>`（注意：resume 子命令不接受 --sandbox，只保留通用项）。
    # 低推理档省 token/更快，实测不影响 SQL 质量。
    out_file = os.path.join(_CODEX_CWD, "codex_last.txt")
    common = ["--skip-git-repo-check", "-c", "model_reasoning_effort=low", "-o", out_file]
    if model:
        common += ["-m", model]
    if session_id:  # 续接：resume 不接受 --sandbox
        cmd = [cli, "exec", "resume", session_id, *common, prompt]
    else:  # 首轮：只读沙箱
        cmd = [cli, "exec", "--sandbox", "read-only", *common, prompt]
    try:
        os.remove(out_file)
    except OSError:
        pass
    proc = _run_subprocess(cmd, timeout=timeout, cli=cli, cwd=_CODEX_CWD)
    try:
        with open(out_file, encoding="utf-8") as f:
            out = f.read().strip()
    except OSError:
        out = ""
    if not out:  # -o 文件仅在成功时写；空 = 失败，把 stderr 尾部（含 ERROR: {...}）带出来
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-500:]
        raise AIError(f"codex 未产出结果：{tail or '无输出'}")
    m = re.search(r"session id:\s*([0-9a-fA-F-]+)", (proc.stderr or "") + (proc.stdout or ""))
    new_sid = m.group(1) if m else (session_id or "")
    return out, new_sid


# ---------- 输出解析 ----------


def parse_ai_output(text: str, *, explain: bool) -> AIResult:
    """把 AI 原始输出解析为 {sql, explanation}。容错：剥 markdown 围栏、explain 时尽力取 JSON。"""
    text = _strip_fences((text or "").strip())
    if explain:
        obj = _try_json(text)
        if isinstance(obj, dict) and obj.get("sql"):
            return AIResult(sql=str(obj["sql"]).strip(),
                            explanation=str(obj.get("explanation") or "").strip())
        # JSON 没取到就整块当 SQL（无解释）——宁可少解释，也不把非 SQL 塞进编辑器
    return AIResult(sql=text, explanation="")


def _strip_fences(text: str) -> str:
    """剥掉 ```lang ... ``` 代码围栏（若整段被围栏包裹）。"""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if len(lines) >= 2:
        lines = lines[1:]  # 去掉起始 ```lang
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return t


def _try_json(text: str) -> object | None:
    """尽力从文本中抽出第一个 JSON 对象。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None
