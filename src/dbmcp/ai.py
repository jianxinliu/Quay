"""AI 辅助生成 SQL：把表结构 + 自然语言需求丢给命令行 AI，拿回一条可执行 SQL（可选带解释）。

只生成、不执行——产物回填到编辑器光标处，由人审阅后再走既有的写确认/审批闭环。AI 进程
不被授予任何工具（禁 MCP、只读沙箱），纯文本进、纯文本出，碰不到数据库。

Provider 可插拔，当前支持两种命令行 AI，上层 service 不感知差异：
- claude:  `claude -p <prompt> --output-format json`，从 result 字段取正文
- codex:   `codex exec ... -o <file> <prompt>`，从 --output-last-message 文件取正文
后期可再加 api provider（直接 HTTP 调），run_ai 的签名不变。

prompt 拼装（build_sql_prompt）与进程调用（run_ai）拆开，前者是纯函数、可单测；后者
用假进程 monkeypatch 测。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
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
    session_id: str = ""  # 命令行 AI 的会话 id，追问时带回以续接同一会话


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
) -> AIResult:
    """拼 prompt → 调 AI → 解析出 {sql, explanation, session_id}。失败抛 AIError。

    session_id 非空 = 追问：续接同一会话、只发调整要求（不重发表结构）。
    """
    if session_id:
        prompt = build_followup_prompt(question, explain=explain)
    else:
        prompt = build_sql_prompt(system_prompt, dialect, ddls, question,
                                  explain=explain, samples=samples)
    raw, new_sid = run_ai(prompt, provider=provider, model=model, timeout=timeout,
                          cli_path=cli_path, session_id=session_id)
    result = parse_ai_output(raw, explain=explain)
    result.session_id = new_sid
    return result


# ---------- provider 调用 ----------


def run_ai(prompt: str, *, provider: str, model: str, timeout: int,
           cli_path: str = "", session_id: str | None = None) -> tuple[str, str]:
    """按 provider 调命令行 AI，返回 (原始正文, 会话 id)。session_id 非空 = 续接会话。失败抛 AIError。"""
    cli = cli_path.strip() or _DEFAULT_CLI.get(provider, "")
    if provider == "claude":
        return _run_claude(prompt, model=model, timeout=timeout, cli=cli, session_id=session_id)
    if provider == "codex":
        return _run_codex(prompt, model=model, timeout=timeout, cli=cli, session_id=session_id)
    raise AIError(f"不支持的 AI provider：{provider!r}（当前支持 claude / codex）")


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
