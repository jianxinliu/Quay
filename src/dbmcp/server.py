"""MCP interface layer: registers DbmService as FastMCP tools.

Only core tool descriptions are listed at connect time. Optional capabilities keep
their full descriptions behind the on-demand catalog.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from functools import partial
from typing import Annotated, Literal

import anyio.to_thread
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from mcp.types import TextContent
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from .agent_format import render_agent_result
from .approvals import STATUS_APPROVED, STATUS_CONSUMED, STATUS_PENDING, ApprovalError
from .errors import translate_db_error
from .budget import ResultBudgetExceeded, usage_note
from .guide import FIRST_CALL_GUIDE, USAGE_GUIDE
from .health import ConnectionUnavailable
from .i18n import use_locale
from .service import CallerInfo, DbmService, QueryRejected, change_status_payload
from .sync import SyncSpec

# Waiting on human approval: re-check the approval ticket's status every 1s. We poll
# instead of using an in-process condition variable because the decision can also come
# from a different process (the `dbm approve` CLI), which a condition variable can't
# reach; a 1s delay is imperceptible to a human clicking "approve".
logger = logging.getLogger(__name__)

_WAIT_POLL_S = 1.0
_WAIT_HEARTBEAT_S = 10.0   # report progress every 10s so clients don't time out the long call
_WAIT_MAX_S = 3600

# A small stable surface is shown to the model at connect time. The remaining tools
# remain callable through call_capability (and directly by existing MCP clients).
_CORE_TOOLS = frozenset({
    "list_projects", "list_connections", "begin_session", "query", "execute",
    "wait_for_change", "transaction", "list_capabilities", "capability_detail",
    "call_capability",
})
_IN_CAPABILITY_CALL: ContextVar[bool] = ContextVar("dbm_in_capability_call", default=False)


class _CoreToolList(Middleware):
    async def on_list_tools(self, context, call_next):  # noqa: ANN001, ANN201
        return [tool for tool in await call_next(context) if tool.name in _CORE_TOOLS]

# The elicitation dialog must fit on one screen and be clickable: the client renders the
# message verbatim in a terminal, and overly long content (typically sync_table's full
# plan with a CREATE TABLE) pushes the Accept/Decline buttons off screen, leaving the
# human with nothing to click. So we only show a summary here; the full statement/plan
# is on the approval page.
_ELICIT_MAX_LINES = 8
_ELICIT_MAX_CHARS = 480
_ELICIT_MAX_LINE_CHARS = 100


class ApprovalDecision(BaseModel):
    """The elicitation response schema.

    **Every field must have a default**: a field with no default ends up in the JSON
    Schema's `required` list, and clients (e.g. Claude Code) then force the human to fill
    it in before Accept is even clickable — the dialog shows a red "Value: not set / This
    field is required", one extra step that also looks like an error. With a default,
    Accept is clickable right away and means "approve"; the field is still there for
    someone who wants to explicitly choose deny.
    """

    decision: Literal["approve", "deny"] = Field(
        "approve", description="approve=批准并立即执行；deny=驳回（与 Decline 等价）"
    )


def _clip_block(
    text: str,
    *,
    max_lines: int = _ELICIT_MAX_LINES,
    max_chars: int = _ELICIT_MAX_CHARS,
    max_line_chars: int = _ELICIT_MAX_LINE_CHARS,
) -> tuple[str, bool]:
    """把多行文本裁到弹窗放得下的体量，返回 (裁剪后文本, 是否被裁)。

    行数、单行长度、总字符三条都卡：终端里长行会折行，只卡行数挡不住撑爆屏幕。
    """
    # 空行与纯分隔行（同步计划里的 `--`）在弹窗里只占地方，不带信息
    lines = [ln.rstrip() for ln in text.strip().splitlines()
             if ln.strip() and set(ln.strip()) != {"-"}]
    clipped = len(lines) > max_lines
    kept: list[str] = []
    used = 0
    for line in lines[:max_lines]:
        if len(line) > max_line_chars:
            line = line[: max_line_chars - 1] + "…"
            clipped = True
        if kept and used + len(line) > max_chars:
            clipped = True
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept), clipped


def build_elicit_message(
    *,
    change_id: int,
    risk_level: str,
    reasons: list[str],
    project: str,
    connection: str,
    environment: str,
    statement: str,
    approval_url: str = "",
) -> str:
    """拼会话内确认弹窗的正文：摘要 + 截断的语句 + 审批页链接，保证一屏放得下。"""
    body, clipped = _clip_block(statement)
    lines = [
        f"审批单 #{change_id} · 风险 {risk_level} · {project}/{connection}（{environment}）",
        body,
    ]
    if clipped:
        lines.append("…（内容已截断，完整语句见审批页）")
    reason_text, _ = _clip_block("; ".join(reasons[:3]), max_lines=1, max_chars=120,
                                 max_line_chars=120)
    if reason_text:
        lines.append(f"判定: {reason_text}")
    if approval_url:
        lines.append(f"详情: {approval_url}")
    lines.append("Accept = 批准并立即执行；Decline = 驳回。")
    return "\n".join(lines)


def _tool_error_from_unavailable(e: ConnectionUnavailable) -> ToolError:
    """Turn ConnectionUnavailable into a clear ToolError message for the agent.

    The message includes the state (unavailable/exhausted) and a suggested retry delay,
    so the agent can decide whether to wait and retry or tell the user to check the
    backend, instead of the agent receiving a raw error like pymysql 2013.
    """
    if e.state == "exhausted":
        return ToolError(f"[connection_exhausted] {e}")
    hint = f" (retry in about {e.retry_after_s}s)" if e.retry_after_s else ""
    return ToolError(f"[connection_unavailable] {e}{hint}")


def agent_error(e: BaseException) -> ToolError:
    """**The single error exit point on the agent side**: every exception is translated here
    into a categorized, sanitized ToolError.

    Error handling must be contained inside the service — driver exceptions (which can
    embed account passwords in a DSN, bound parameters, SQLAlchemy tracebacks and
    background-info links) must never bubble up into the agent's context, and must never
    turn into a transport-layer 500. The category prefix lets the agent tell at a glance
    what to do next:

    - `[connection_unavailable]` / `[connection_exhausted]`: a connection problem — retry
      later / needs human attention
    - `[sql_syntax_error]` and other DB error categories (see errors.py): fix the SQL,
      don't resend it as-is
    - `[result_budget_exceeded]`: this session has pulled back too much data — go ask the
      user whether to continue
    - Everything else (approval/read-only restriction/bad argument rejections): pass
      through the human-readable text the service layer already produced

    A ToolError is passed straight through (its message is already well-formed upstream).
    """
    if isinstance(e, ToolError):
        return e
    if isinstance(e, ConnectionUnavailable):
        return _tool_error_from_unavailable(e)
    if isinstance(e, ResultBudgetExceeded):
        # The quota is a governance rule, not a database error — don't let it fall into
        # translate_db_error's fallback category.
        return ToolError(f"[result_budget_exceeded] {e}")
    if isinstance(e, (QueryRejected, ValueError)):
        return ToolError(str(e))
    if isinstance(e, KeyError):
        # KeyError's str() is a quoted repr; the raw message is more readable
        return ToolError(str(e.args[0]) if e.args else "Requested resource not found")
    return ToolError(translate_db_error(e).as_text())


def _caller_from_ctx(ctx: Context | None) -> CallerInfo:
    """从 MCP 会话尽力提取 agent 身份，取不到时记 unknown。"""
    if ctx is None:
        return CallerInfo()
    agent = "unknown"
    session_id = ""
    try:
        session_id = ctx.session_id or ""
        client_params = getattr(ctx.session, "client_params", None)
        client_info = getattr(client_params, "clientInfo", None)
        if client_info is not None:
            agent = f"{client_info.name}/{getattr(client_info, 'version', '')}".rstrip("/")
    except Exception:
        pass
    return CallerInfo(agent=agent, session_id=session_id)


def _decision_of(answer: object) -> str:
    """从 elicitation 回执里取出决策词，兼容客户端回填的几种形态。

    正常是 ApprovalDecision 实例；宽松客户端可能直接回 dict 或裸字符串。取不到就按
    「Accept 即批准」处理（返回空串），因为 decision 有默认值、人点 Accept 就是批准。
    """
    data = getattr(answer, "data", None)
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        value = data.get("decision", data.get("value", ""))
        return value if isinstance(value, str) else ""
    value = getattr(data, "decision", "")
    return value if isinstance(value, str) else ""


# PG cross-database parameter. A PG connection is bound to exactly one database (the
# server has no cross-database references), so operating on a different one requires a
# separate connection — this parameter tells the service which database to connect to
# this time. The existing `database` parameter keeps meaning schema for PG, unchanged, so
# already-integrated agents don't see a behavior change.
PgDatabase = Annotated[
    str | None,
    Field(description="PostgreSQL only: which database on the server to operate on (a PG "
                      "connection is bound to one database; use this to switch); "
                      "omit to use the connection's bound database, see "
                      "list_server_databases for the options. "
                      "Note: the `database` parameter means schema for PG"),
]
_SCHEMA_DESC = "Database/schema (MySQL/ClickHouse: database, PostgreSQL: schema); omit to use the connection's default database"


async def _maybe_elicit_approval(
    service: DbmService,
    ctx: Context | None,
    project: str,
    connection: str,
    statement: str,
    caller: CallerInfo,
    result: dict,
    resubmit: Callable[[int], dict],
) -> dict:
    """elicitation 快捷审批：策略允许且客户端支持时，会话内确认即批准并执行。

    审批单已在 result 中创建（审计完整）；elicitation 只是把"去后台点批准"这一步
    搬进会话。客户端不支持或出错时原样返回 approval_required，自然回退审批单流程。
    """
    if result.get("status") != "approval_required" or ctx is None:
        return result
    cfg = service.config.get_connection(project, connection)
    if not cfg.elicitation_enabled:
        return result

    cid = result["change_id"]
    risk = result.get("risk", {})
    pg_db = result.get("pg_database")
    message = build_elicit_message(
        change_id=cid,
        risk_level=str(risk.get("level", "?")),
        reasons=list(risk.get("reasons", [])),
        project=project,
        connection=f"{connection}（库 {pg_db}）" if pg_db else connection,
        environment=cfg.environment,
        statement=statement,
        approval_url=result.get("approval_url", ""),
    )
    try:
        answer = await ctx.elicit(message, response_type=ApprovalDecision)
    except Exception:
        return result  # 客户端不支持 elicitation → 审批单流程兜底

    decided_by = f"elicitation:{caller.agent}"
    try:
        if getattr(answer, "action", None) == "accept" and _decision_of(answer) != "deny":
            service.approve_change(cid, decided_by=decided_by, note="confirmed in-session")
            return await anyio.to_thread.run_sync(resubmit, cid)
        service.reject_change(cid, decided_by=decided_by, note="declined in-session")
        return {"status": "rejected", "change_id": cid, "reason": "The user declined this operation in-session"}
    except ApprovalError as e:
        # 竞态（如后台已同时决策）：把最新状态告知 agent
        return {"status": "rejected", "change_id": cid, "reason": str(e)}


async def _wait_for_decision(
    service: DbmService, change_id: int, timeout_s: float, ctx: Context | None
) -> dict:
    """等待审批单离开 pending（人在后台或 CLI 上决策），或等到超时。

    异步等待：只在每次复查时借一下线程跑 SQLite 读，不长期占用 MCP 线程池名额，
    也不阻塞事件循环——同一进程上的管理后台在此期间照常响应（人要在那里点批准）。
    返回值同 change_status_payload；超时返回时 status 仍是 pending 且带 timed_out=True。
    """
    timeout_s = max(0.0, min(float(timeout_s), _WAIT_MAX_S))
    start = anyio.current_time()
    deadline = start + timeout_s
    next_beat = start + _WAIT_HEARTBEAT_S
    while True:
        change = await anyio.to_thread.run_sync(service.get_change, change_id)
        payload = change_status_payload(change)
        if payload["status"] != STATUS_PENDING:
            return payload
        now = anyio.current_time()
        if now >= deadline:
            payload["timed_out"] = True
            return payload
        if ctx is not None and now >= next_beat:
            next_beat = now + _WAIT_HEARTBEAT_S
            try:
                await ctx.report_progress(
                    progress=now - start, total=timeout_s,
                    message=f"Waiting for human approval (change #{change_id})",
                )
            except Exception:  # noqa: BLE001 - 客户端不支持进度通知，不影响等待
                pass
        await anyio.sleep(min(_WAIT_POLL_S, deadline - now))


async def _wait_then_execute(
    service: DbmService,
    result: dict,
    wait_seconds: float,
    ctx: Context | None,
    resubmit: Callable[[int], dict],
) -> dict:
    """审批单已生成 → 等人决策 → 批准即自动带 change_id 重提执行。

    把「人回 CLI 说一句已批准、agent 再重提一次」这两步去掉：人在后台点完批准，
    这里的等待即返回，随后自动核销执行。红线不变——执行的仍是审批单里存储的 SQL。
    """
    cid = result["change_id"]
    decision = await _wait_for_decision(service, cid, wait_seconds, ctx)
    status = decision["status"]
    if status == STATUS_APPROVED:
        return await anyio.to_thread.run_sync(resubmit, cid)
    if status == STATUS_CONSUMED:
        # The approver clicked "approve and execute now" on the backend: the change has
        # already landed; just relay the result to the agent (resubmitting would be rejected)
        executed = decision.get("exec_result") or {}
        return {"status": "executed", "change_id": cid, **executed,
                "message": "Approved and executed directly by the approver on the admin backend"}
    if status == STATUS_PENDING:  # wait timed out, the approval ticket is still valid
        return {**result, "waited_seconds": wait_seconds,
                "message": f"{result.get('message', '')} Waited {int(wait_seconds)}s with no "
                           f"decision yet; call wait_for_change({cid}) again to keep waiting."}
    reason = decision.get("decision_note") or f"Change ticket is currently in status {status}"
    return {"status": "rejected", "change_id": cid, "reason": reason}


class _AgentLocale(Middleware):
    """MCP 工具调用一律用英文文案（见 i18n.py）：agent 的读者是模型，不是中文后台的人。

    contextvar 在 call_next 期间生效，工具函数里的 anyio.to_thread 调用也继承它。
    """

    async def on_call_tool(self, context, call_next):  # noqa: ANN001, ANN201
        with use_locale("en"):
            return await call_next(context)


class _FirstCallGuide(Middleware):
    """会话内第一次成功调用工具时，把使用说明随结果一起送出去。

    为什么挂在中间件而不是逐个工具里：谁是「第一次」事先不知道，且这与工具本身的职责无关。
    做法是给结果**多加一个文本块**（不动 structured_content），所以按结构化返回值消费的
    客户端完全不受影响，只有读文本的模型会看到它。

    只在**成功**的调用后记账：首次调用就报错时不发说明也不算数，留给下一次成功的调用——
    把一大段说明拼在错误消息前面反而会盖住「下一步该怎么办」。
    """

    def __init__(self, guide: str, enabled: Callable[[], bool], max_sessions: int = 512):
        self._guide = guide
        self._enabled = enabled
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max = max_sessions
        self._lock = threading.Lock()

    def _claim(self, session_id: str) -> bool:
        """本会话是否「还没发过说明」；是则就地标记为已发（同一会话并发调用只会发一次）。"""
        with self._lock:
            if session_id in self._seen:
                return False
            self._seen[session_id] = None
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)  # 有界 FIFO：常驻进程不能无限攒会话 id
            return True

    async def on_call_tool(self, context, call_next):  # noqa: ANN001, ANN201
        result = await call_next(context)
        try:
            if _IN_CAPABILITY_CALL.get():
                return result
            if not self._enabled():
                return result
            ctx = context.fastmcp_context
            # 无会话 id 的客户端（部分 stdio 实现）退化成「本进程发一次」
            session_id = (getattr(ctx, "session_id", "") or "-") if ctx else "-"
            if self._claim(session_id):
                result.content = [TextContent(type="text", text=self._guide), *result.content]
        except Exception:  # noqa: BLE001
            logger.debug("附带使用说明失败，忽略", exc_info=True)
        return result


def _budget_gate(service: DbmService, caller: CallerInfo):  # noqa: ANN201
    """取数前查会话配额，返回一个「记账并按需追加提醒」的收尾函数。

    check 在**执行前**：超额就不该再去打数据库。charge 在拿到最终文本之后——计的是
    agent 真正收进上下文的那串字符，而不是行数或库里的原始体积。
    """
    budget = service.result_budget()
    budget.check(caller.session_id)

    def charge(text: str) -> str:
        usage = budget.charge(caller.session_id, text)
        note = usage_note(usage)
        return f"{text}\n{note}" if note else text

    return charge


def build_mcp(service: DbmService) -> FastMCP:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_server: FastMCP):
        # Apply concurrency/pool settings to the runtime at startup (the thread-pool cap
        # can only be set from inside the event loop)
        service.apply_runtime_settings()
        yield {}

    mcp = FastMCP(
        name="Quay",
        lifespan=_lifespan,
        instructions=(
            "Quay database access: call begin_session to label your work; use query for "
            "reads, execute or transaction for changes requiring human approval. "
            "Only common tools are listed. Discover optional tools with "
            "list_capabilities, inspect capability_detail, then call_capability. "
            "Use wait_for_change after an approval wait times out."
        ),
    )

    mcp.add_middleware(_AgentLocale())
    mcp.add_middleware(_CoreToolList())
    mcp.add_middleware(_FirstCallGuide(
        FIRST_CALL_GUIDE, enabled=lambda: service.guide_on_first_call()))

    async def _capabilities():
        return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}

    @mcp.tool
    async def list_capabilities() -> list[dict]:
        """Discover the optional database, audit, analysis and workflow capabilities."""
        tools = await _capabilities()
        return [{"name": name, "summary": (tool.description or "").strip().split("\n")[0]}
                for name, tool in sorted(tools.items()) if name not in _CORE_TOOLS]

    @mcp.tool
    async def capability_detail(name: str) -> dict:
        """Get the full description and input schema for an optional capability."""
        tool = (await _capabilities()).get(name)
        if tool is None or name in _CORE_TOOLS:
            raise ToolError(f"Unknown capability: {name}")
        return {"name": name, "title": tool.title,
                "description": tool.description or "",
                "input_schema": tool.parameters,
                "output_schema": tool.output_schema}

    @mcp.tool
    async def call_capability(name: str, arguments: dict | None = None) -> object:
        """Invoke an optional capability using the arguments shown by capability_detail."""
        tool = (await _capabilities()).get(name)
        if tool is None:
            raise ToolError(f"Unknown capability: {name}")
        if name in _CORE_TOOLS:
            raise ToolError(f"Capability {name} cannot be called through this gateway")
        marker = _IN_CAPABILITY_CALL.set(True)
        try:
            result = await mcp.call_tool(name, arguments or {})
        finally:
            _IN_CAPABILITY_CALL.reset(marker)
        if result.is_error:
            raise ToolError("; ".join(getattr(part, "text", "") for part in result.content))
        if result.structured_content is not None:
            if set(result.structured_content) == {"result"}:
                return result.structured_content["result"]
            return result.structured_content
        return "\n".join(getattr(part, "text", "") for part in result.content)

    @mcp.tool
    async def transaction(
        action: Literal["begin", "add", "preview", "commit", "rollback"],
        project: str,
        connection: str,
        transaction_id: str | None = None,
        sql: str | None = None,
        reason: str = "",
        rollback_note: str = "",
        wait_seconds: int | None = None,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> dict:
        """Stage SQL changes across calls, then approve and execute the frozen batch atomically.

        begin creates a one-hour draft bound to this session and connection; add appends
        SQL without touching the database; preview shows its count/fingerprint; commit
        submits the whole batch for one human approval and waits as execute does; rollback
        discards the draft. Read-only queries run separately through query. MySQL drafts
        accept DML only because MySQL DDL implicitly commits. Use wait_for_change after a
        commit approval wait times out.
        """
        caller = _caller_from_ctx(ctx)
        try:
            if action == "begin":
                return service.begin_transaction(project, connection, caller,
                                                 database=pg_database)
            if not transaction_id:
                raise QueryRejected("transaction_id is required for this action")
            if action == "add":
                return service.add_transaction_sql(transaction_id, sql or "", caller)
            if action == "preview":
                return service.preview_transaction(transaction_id, caller)
            if action == "rollback":
                return service.rollback_transaction(transaction_id, caller)
            preview = service.preview_transaction(transaction_id, caller)
            if (project, connection) != (preview["project"], preview["connection"]):
                raise QueryRejected("Transaction connection does not match the draft")
            result = await anyio.to_thread.run_sync(
                lambda: service.commit_transaction(
                    transaction_id, caller, reason=reason, rollback_note=rollback_note))
            if result.get("status") == "approval_required":
                ticket_sql = service.approvals.get(result["change_id"]).sql
                run = lambda cid: service.execute(  # noqa: E731
                    project, connection, ticket_sql, caller, change_id=cid,
                    database=preview["database"])
                result = await _maybe_elicit_approval(
                    service, ctx, project, connection, ticket_sql, caller, result,
                    resubmit=run)
                wait_s = service.approval_wait_seconds() if wait_seconds is None else wait_seconds
                if result.get("status") == "approval_required" and wait_s > 0:
                    result = await _wait_then_execute(service, result, wait_s, ctx, resubmit=run)
            return result
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def allow_more_results(
        reason: Annotated[
            str, Field(description="Explanation that the user confirmed continuing, e.g. "
                                   "\"User confirmed: still needs to reconcile 3 more "
                                   "tables\". Shown on the admin dashboard so a human can "
                                   "verify the user was actually asked")
        ],
        ctx: Context | None = None,
    ) -> dict:
        """After this session's result quota is used up, call this **only after the user has
        confirmed** to get one more allowance.

        Usage: a data fetch is rejected by the quota -> stop and ask the user "do you want
        to continue these token-costly queries", explaining what you still plan to query
        and roughly how much -> once they agree, call this tool (write their confirmation
        into reason) -> continue querying.
        **Do not call this to work around the limit without having asked the user** —
        grants show up on the admin dashboard.

        Before asking the user, consider a cheaper approach first: aggregate to only get
        the conclusion, dump to a file with export_table, or push the computation into the
        local sandbox with analysis_*.
        """
        caller = _caller_from_ctx(ctx)
        usage = service.result_budget().grant(caller.session_id, reason)
        logger.info("Session %s granted an extra result allowance (grant #%d): %s",
                    caller.session_id or "-", usage["grants"], reason)
        return {
            "granted": True,
            "used_chars": usage["used_chars"],
            "allowance_chars": usage["allowance_chars"],
            "grants": usage["grants"],
            "note": "One more allowance has been granted; you may continue fetching data. "
                    "Keep using aggregation/narrowing conditions to control the size of "
                    "each result.",
        }

    @mcp.tool
    def usage_guide() -> str:
        """The full usage guide and best practices for this service: which tool or
        combination fits which scenario, and where the boundaries are.

        The first call only carries a short hint. Load this optional guide when you
        need a scenario-specific workflow or want to review a boundary.
        """
        return USAGE_GUIDE

    @mcp.custom_route("/exports/{token:str}/{filename:str}", methods=["GET"])
    async def _download_export(req: Request) -> Response:
        """Short-lived random-token download; the file path can only be created by export_table inside a dedicated directory."""
        token = req.path_params["token"]
        filename = req.path_params["filename"]
        path = service.resolve_mcp_export(token, filename)
        if path is None:
            return Response("export not found or expired", status_code=404)
        return FileResponse(
            path,
            filename=filename,
            headers={"Cache-Control": "no-store"},
        )

    @mcp.tool
    def list_projects() -> list[dict]:
        """List every project and the database connection names available under it."""
        try:
            return service.list_projects()
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def list_connections(project: str) -> list[dict]:
        """List the database connections under the given project (engine, environment, database name, and other metadata — no account credentials)."""
        try:
            return service.list_connections(project)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def begin_session(
        title: Annotated[str, Field(description="This session's name, e.g. \"Investigating duplicate order charges\"")],
        note: Annotated[str, Field(description="Session background/description, optional, e.g. \"Reproducing issue #123, read-only investigation\"")] = "",
        ctx: Context | None = None,
    ) -> dict:
        """Declare this working session's name and description (recommended before you start running SQL).

        Once registered, every SQL statement this session runs (query/execute/sample_rows,
        etc.) will be grouped under this session on the admin backend, making it easy for a
        human to trace back "what did this session do". The same session can call this
        again to update the name. Not calling it still works, but the backend will only see
        an opaque session id.
        """
        try:
            return service.begin_session(_caller_from_ctx(ctx), title, note)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def list_sessions(
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum number of sessions to list")] = 20,
        since: Annotated[
            str, Field(description="Start time, `YYYY-MM-DD` or ISO time; interpreted as local time when no timezone is given")
        ] = "",
        until: Annotated[
            str, Field(description="End time, `YYYY-MM-DD` (inclusive) or ISO time")
        ] = "",
        keyword: Annotated[
            str, Field(description="Fuzzy-matches the session title/note, or any SQL "
                                   "statement the session ran (e.g. a table name like `orders`)")
        ] = "",
        project: Annotated[str, Field(description="Only sessions that operated on this project")] = "",
        connection: Annotated[str, Field(description="Only sessions that operated on this connection")] = "",
        status: Annotated[
            str, Field(description="Filter by outcome: ok=executed successfully / "
                                   "rejected=blocked, never landed / "
                                   "error=execution failed; empty=no filter. "
                                   "Combine with writes_only=True for "
                                   "\"sessions that actually changed data\"")
        ] = "",
        writes_only: Annotated[
            bool, Field(description="Only list sessions that ran a write operation (changed data)")
        ] = False,
        all_agents: Annotated[
            bool, Field(description="By default lists only the current agent's own sessions; True lists every agent's")
        ] = False,
        ctx: Context | None = None,
    ) -> list[dict]:
        """List your own past working sessions (most recently active first), for tracing back "what did I do before".

        Each entry gives session_id, the title/note declared via begin_session, the
        operation count ops, the write-operation count writes, and the first/last
        timestamp; the entry with current=true is the current session. Filter by time
        window (since/until), keyword (session title/note/any SQL it ran, e.g. a table
        name), project/connection, or result status:
        `writes_only=True, status="ok"` gives "sessions that actually changed data".
        Once you have a session_id, use session_history to see that session's operations.
        Sessions that never called begin_session are still listed, just with an empty title.

        When project/connection/status is given, ops/writes/first-last-timestamp only
        count the operations matching those filters.
        """
        try:
            return service.list_agent_sessions(
                _caller_from_ctx(ctx), limit, all_agents,
                since=since, until=until, keyword=keyword,
                project=project, connection=connection, status=status,
                writes_only=writes_only,
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def session_history(
        session_id: Annotated[
            str, Field(description="The session id to trace (from list_sessions); omit for the current session")
        ] = "",
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum number of operations to return")] = 50,
        writes_only: Annotated[
            bool, Field(description="Only show write operations (execute / sync_write) — cheaper on tokens when investigating a change")
        ] = False,
        status: Annotated[
            str, Field(description="Filter by result: ok=approved and actually executed "
                                   "successfully / "
                                   "rejected=blocked, never landed (e.g. the first "
                                   "submission generated an approval ticket, or it was "
                                   "denied/expired) / "
                                   "error=execution failed; empty=everything. "
                                   "To see \"what did the last change actually do\", use "
                                   "writes_only=True + status=ok")
        ] = "",
        fields: Annotated[
            str, Field(description="Which columns to return, comma-separated. Omit for "
                                   "the compact set "
                                   "(ts,tool,connection,status,row_count,change_id,"
                                   "approval_status,rollback_note); "
                                   "**the raw SQL and error details are not returned by "
                                   "default** — ask for `sql` / `detail` explicitly; "
                                   "`all` = every column. "
                                   "Options: sql,detail,duration_ms,environment,agent,fingerprint")
        ] = "",
        ctx: Context | None = None,
    ) -> dict:
        """Trace the operations a session ran (most recent first); **write operations carry their approval ticket id and rollback note**.

        Answers "what did that last batch of changes actually change, and can it still be
        rolled back": each write operation returns change_id, approval_status, and the
        "value before the change / how to roll back" written into rollback_note at
        submission time (the agent decides at the time whether it's worth writing one, so
        it may be empty). You can build a rollback statement from it and submit it via
        execute again (a rollback also requires human approval).

        **To see only changes that actually landed, use `writes_only=True,
        status="ok"`** — the record from the first submission that generated the approval
        ticket is rejected (never landed); without the status filter it will be included
        too. The returned status_counts gives the count for each outcome, so you can tell
        whether anything was still blocked or errored.

        Returns a compact column set by default to save context — **the raw SQL and error
        details are not returned by default**; scan the default columns first to find the
        entries you want, then narrow with `limit` and ask for `fields="sql,detail"` to get
        the full text (very long SQL is truncated; the full text is viewable on the
        approval page).
        """
        try:
            return service.session_history(
                _caller_from_ctx(ctx), session_id, limit, writes_only, status, fields
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def query(
        project: str,
        connection: str,
        sql: Annotated[str, Field(description="A single read-only statement (SELECT/SHOW/DESCRIBE/EXPLAIN)")],
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> str:
        """Run a read-only SQL statement on the given connection, returning compact TSV text (saves tokens vs. JSON).

        Output format: a top `#` metadata line (`shown=N truncated=bool reason=...
        elapsed_ms=...`) + a `# types:` column-types line; then a header row with column
        names, then data rows, **tab-separated**, `\\N` means NULL, `\\ \\t \\n` in values
        are backslash-escaped. **Big integers are returned as strings** (safe beyond 2^53
        precision).

        Results are subject to two hard caps: (1) row count (connection max_rows, default
        1000); (2) a character budget (agent_max_result_chars, default 40000 ≈ 12k
        tokens). `truncated=true` means you didn't get everything — **don't re-fetch the
        full set**, narrow it with WHERE/LIMIT/aggregation, or push the computation into
        the analysis workbench (analysis_*).

        Non-read-only statements (including multiple statements, DML hidden in a CTE,
        SELECT FOR UPDATE, side-effecting functions like SLEEP) are rejected.

        There's also a **session-level** quota: once this session's cumulative returned
        volume exceeds the cap, further fetches are rejected — at that point ask the user
        whether to continue, and call allow_more_results once they agree.
        """
        caller = _caller_from_ctx(ctx)
        try:
            _charge = _budget_gate(service, caller)
            result = service.query(project, connection, sql, caller, database=pg_database)
            budget = service.agent_result_budget(project, connection)
            return _charge(render_agent_result(result, budget))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def execute(
        project: str,
        connection: str,
        sql: Annotated[str, Field(description="The write SQL to execute (INSERT/UPDATE/DELETE/DDL); "
                                  "supports a multi-statement batch (semicolon-separated, "
                                  "e.g. an ALTER plus a backfill UPDATE migration) — one "
                                  "approval covers the whole batch, executed statement by "
                                  "statement in the same transaction")],
        reason: Annotated[str, Field(description="Reason for the change, for the approver's reference")] = "",
        rollback_note: Annotated[
            str, Field(description="Rollback reference: what these rows/columns were before "
                                   "the change and how to revert it. Use query first to "
                                   "read the old values and write them here, e.g. "
                                   "`order 1001 status before=2; rollback UPDATE orders SET "
                                   "status=2 WHERE id=1001`. The approver can see it, and "
                                   "it can be retrieved later via session_history. **Decide "
                                   "for yourself whether it's needed** — leave it blank for "
                                   "changes with no rollback value")
        ] = "",
        change_id: Annotated[
            int | None, Field(description="An already-approved change ticket id; resubmit the same SQL with it to execute")
        ] = None,
        wait_seconds: Annotated[
            int | None,
            Field(description="How many seconds the server should wait for a human decision "
                              "after the first submission generates an approval ticket; "
                              "0=don't wait, return the ticket id immediately, omit=use "
                              "the system-setting default"),
        ] = None,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> dict:
        """Execute a data-changing operation (requires human authorization).

        First submission (no change_id): the system assesses the risk and generates an
        approval ticket, then **the server waits for the human decision** (default wait
        duration is in system settings, overridable with wait_seconds). While waiting,
        give the returned approval_url to the user so they can open the approval page;
        once they approve, this same call auto-executes and returns status=executed —
        the user doesn't need to come back to the session and say "approved", and you
        don't need to resubmit.
        If the client supports in-session confirmation (elicitation) and the connection
        policy allows it, a confirmation dialog pops up directly and executes on approval.

        Returns status=approval_required if the wait timed out while the ticket is still
        pending: remind the user of the approval_url again, then call
        wait_for_change(change_id) to keep waiting (the ticket is valid for 60 minutes).
        Returns status=rejected with reason explaining why (denied/expired/SQL mismatch) —
        adjust accordingly. Read-only statements are executed directly.

        **When you judge this change might need a rollback, first use query to read the
        old values, then write them into rollback_note**: it's stored with the approval
        ticket, visible to the approver on the approval page, and retrievable later
        (by you or an agent in another session) via session_history to recover "what it
        was before, how to revert it". Whether to write it is your call — leave it blank
        for changes with no rollback value (e.g. adding a log entry, adding an index).
        """
        caller = _caller_from_ctx(ctx)
        run = partial(service.execute, project, connection, sql, caller, reason=reason,
                      rollback_note=rollback_note, database=pg_database)
        # 首提、等待期间的批准后执行都在同一个 try 内：批准后真正落库时才暴露的 DB 错误
        # （锁超时、约束冲突等）同样必须走 agent_error 翻译，不能裸奔到传输层。
        try:
            result = await anyio.to_thread.run_sync(partial(run, change_id=change_id))
            if change_id is None:
                result = await _maybe_elicit_approval(
                    service, ctx, project, connection, sql, caller, result,
                    resubmit=lambda cid: run(change_id=cid),
                )
                wait_s = service.approval_wait_seconds() if wait_seconds is None else wait_seconds
                if result.get("status") == "approval_required" and wait_s > 0:
                    result = await _wait_then_execute(
                        service, result, wait_s, ctx, resubmit=lambda cid: run(change_id=cid),
                    )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e
        return result

    @mcp.tool
    async def sync_table_ddl(
        source_project: str,
        source_connection: Annotated[str, Field(description="Source connection (structure is read from here)")],
        target_project: str,
        target_connection: Annotated[
            str, Field(description="Target connection (tables are created here); cannot be a prod-environment connection")
        ],
        tables: Annotated[
            str, Field(description="Table names to sync the structure of, comma-separated, e.g. `orders,order_item,users`")
        ],
        source_database: Annotated[
            str | None, Field(description="Source database/schema (schema for PG); omit to use the connection's default database")
        ] = None,
        target_database: Annotated[
            str | None, Field(description="Target database/schema (schema for PG); omit to use the connection's default database")
        ] = None,
        source_pg_database: Annotated[
            str | None, Field(description="PostgreSQL source only: which database to read from (source_database means schema for PG)")
        ] = None,
        target_pg_database: Annotated[
            str | None, Field(description="PostgreSQL target only: which database to create the table in (target_database means schema for PG)")
        ] = None,
        ddl: Annotated[
            Literal["create_if_missing", "recreate"],
            Field(description="create_if_missing=only create if the target table doesn't "
                              "exist (skip if it does); "
                              "recreate=DROP the target table then rebuild it (destructive "
                              "— discards any existing data in the target table)"),
        ] = "create_if_missing",
        reason: Annotated[str, Field(description="Reason for the sync, for the approver's reference")] = "",
        dry_run: Annotated[
            bool, Field(description="Only return the creation plan for each table without actually creating them")
        ] = False,
        ctx: Context | None = None,
    ) -> dict:
        """Sync table structure in bulk (no data at all), for rebuilding a set of empty tables locally that mirror production.

        Equivalent to calling `sync_table(ddl=..., data="none")` once per table, just
        without writing it out one by one. Same-engine syncs use the source table's raw
        CREATE TABLE text (faithful); cross-engine syncs rewrite it into an **approximate
        DDL** via sqlglot and list the dropped dialect-specific pieces in warnings
        (ENGINE/CHARSET/secondary indexes, etc.) — the resulting table is usable but is not
        a byte-for-byte copy of the source.

        Executes directly when the target is a local/dev connection; a staging target
        generates one approval ticket per table (so use dry_run first to preview the
        plan). The target cannot be a prod environment. A failure on one table doesn't
        affect the others — the return value gives a per-table status, with error on the
        ones that failed.

        To also pull a small sample of data, use sync_table; to get the data into a file
        instead of context, use export_table.
        """
        names = [t.strip() for t in tables.split(",") if t.strip()]
        try:
            return await anyio.to_thread.run_sync(partial(
                service.sync_table_ddls,
                source_project, source_connection, target_project, target_connection,
                names, _caller_from_ctx(ctx),
                source_database=source_database, target_database=target_database,
                source_pg_database=source_pg_database, target_pg_database=target_pg_database,
                ddl=ddl, reason=reason, dry_run=dry_run,
            ))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def sync_table(
        source_project: str,
        source_connection: str,
        source_table: Annotated[str, Field(description="Source table name")],
        target_project: str,
        target_connection: Annotated[
            str, Field(description="Target connection (the writer); cannot be a prod-environment connection")
        ],
        target_table: Annotated[
            str | None, Field(description="Target table name, defaults to the same name as the source table")
        ] = None,
        source_database: Annotated[
            str | None, Field(description="Source database/schema (schema for PG); omit to use the connection's default database")
        ] = None,
        target_database: Annotated[
            str | None, Field(description="Target database/schema (schema for PG); omit to use the connection's default database")
        ] = None,
        source_pg_database: Annotated[
            str | None, Field(description="PostgreSQL source only: which database to read from (source_database means schema for PG)")
        ] = None,
        target_pg_database: Annotated[
            str | None, Field(description="PostgreSQL target only: which database to write to (target_database means schema for PG)")
        ] = None,
        ddl: Annotated[
            Literal["skip", "create_if_missing", "recreate"],
            Field(description="Structure sync: skip=don't create the table (target must "
                              "already exist); "
                              "create_if_missing=create it from the source structure only "
                              "if the target doesn't exist; "
                              "recreate=DROP the target table then rebuild it (destructive)"),
        ] = "create_if_missing",
        data: Annotated[
            Literal["none", "append", "replace"],
            Field(description="Data sync: none=structure only; append=insert on top of "
                              "existing rows; "
                              "replace=clear the target table before writing (destructive)"),
        ] = "append",
        where: Annotated[
            str, Field(description="WHERE condition for fetching from the source (without "
                                   "the WHERE keyword), e.g. `created_at >= '2026-01-01'`; "
                                   "strongly recommended to narrow the data volume")
        ] = "",
        order_by: Annotated[
            str, Field(description="ORDER BY for fetching from the source (without the "
                                   "keyword), e.g. `id DESC`; combine with limit to get "
                                   "\"the latest N rows\"")
        ] = "",
        limit: Annotated[
            int, Field(ge=1, description="Maximum rows to sync (default 1000), clamped to "
                                         "the system-setting sync_max_rows cap")
        ] = 1000,
        reason: Annotated[str, Field(description="Reason for the sync, for the approver's reference")] = "",
        dry_run: Annotated[
            bool, Field(description="Only return the sync plan (create-table statement/columns/row cap/warnings), don't generate an approval ticket")
        ] = False,
        change_id: Annotated[
            int | None, Field(description="An already-approved sync change ticket id; "
                                          "resubmit with it, keeping every other parameter "
                                          "identical to the original submission, to execute")
        ] = None,
        wait_seconds: Annotated[
            int | None, Field(description="How many seconds the server should wait for a "
                                          "human decision after generating the approval "
                                          "ticket; 0=don't wait, omit=use the system-setting default"),
        ] = None,
        ctx: Context | None = None,
    ) -> dict:
        """Sync a table from one connection to another (typical scenario: production -> local).

        Can sync **structure** (built from the source table; same-engine syncs use the
        source's raw CREATE TABLE text, cross-engine syncs rewrite it into an approximate
        DDL via sqlglot and list what was dropped) and **data** (a sample taken via
        where/order_by/limit, written in parameterized batches). **The data volume has a
        hard cap** (default 1000 rows, cap configurable via the sync_max_rows system
        setting) — it's for pulling a sample you can run locally, not a full-migration
        tool; for bulk data use export/import instead.

        **No approval is needed when the target is a local/dev-environment connection** —
        it executes directly and returns status=executed (it's not touching production
        data; execution is still audited as usual and traceable on the backend). Only a
        staging target goes through approval: same flow as execute — an approval ticket is
        generated and the server waits for a human decision; give the returned
        approval_url to the user to open and approve; once approved this same call
        auto-executes; if the wait times out, status=approval_required is returned — use
        wait_for_change(change_id) to keep waiting, or resubmit with change_id (every other
        parameter must match the original submission exactly, or the fingerprint check
        will reject it). Use dry_run=True first to preview the plan and let the user
        confirm what will be synced.

        Constraints: the target connection cannot be a prod environment (writing into
        production is refused); the target must have a writer account configured;
        ClickHouse can only be a source, Redis does not participate. The data synced is
        the **real value** (not masked), so when syncing from production be aware the
        target will hold a copy of production data.
        """
        caller = _caller_from_ctx(ctx)
        spec = SyncSpec(
            source_project=source_project,
            source_connection=source_connection,
            source_table=source_table,
            target_project=target_project,
            target_connection=target_connection,
            target_table=target_table or source_table,
            ddl=ddl,
            data=data,
            where=where,
            order_by=order_by,
            limit=limit,
            source_database=source_database,
            target_database=target_database,
            source_pg_database=(source_pg_database or "").strip() or None,
            target_pg_database=(target_pg_database or "").strip() or None,
        )
        run = partial(service.sync_table, spec, caller, reason=reason)
        try:
            result = await anyio.to_thread.run_sync(
                partial(run, dry_run=dry_run, change_id=change_id)
            )
            if change_id is None and not dry_run:
                result = await _maybe_elicit_approval(
                    service, ctx, target_project, target_connection,
                    result.get("plan", ""), caller, result,
                    resubmit=lambda cid: run(change_id=cid),
                )
                wait_s = service.approval_wait_seconds() if wait_seconds is None else wait_seconds
                if result.get("status") == "approval_required" and wait_s > 0:
                    result = await _wait_then_execute(
                        service, result, wait_s, ctx, resubmit=lambda cid: run(change_id=cid),
                    )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e
        return result

    # Redis is deliberately not exposed as an MCP tool: the agent cannot reach Redis.
    # Redis is only for a human to operate through the logged-in admin backend at
    # /admin/redis (modeled after Medis's standalone console).

    @mcp.tool
    def get_change_status(change_id: int) -> dict:
        """Look up the current status of an approval ticket (pending / approved / rejected / consumed / expired); returns immediately.

        If you just want to wait until there's a result, use wait_for_change instead of writing your own polling loop around this tool.
        """
        try:
            change = service.get_change(change_id)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e
        return change_status_payload(change)

    @mcp.tool
    async def wait_for_change(
        change_id: int,
        timeout_seconds: Annotated[
            int | None, Field(description="Maximum seconds to wait; omit to use the system-setting default")
        ] = None,
        ctx: Context | None = None,
    ) -> dict:
        """Wait for an approval ticket to be decided by a human, blocking until there's a result or it times out (don't poll get_change_status yourself).

        Use this after execute's wait times out: remind the user of the approval_url once
        more, then call this tool to keep waiting.
        Returns status=approved when you can resubmit the same SQL with change_id to
        execute it; status=consumed means the approver clicked "approve and execute now" on
        the backend and the change has already landed (exec_result has the affected row
        count) — **do not resubmit**; see decision_note for status=rejected/expired;
        status=pending with timed_out=true means this wait timed out and the ticket is
        still valid, call it again to keep waiting.
        """
        wait_s = service.approval_wait_seconds() if timeout_seconds is None else timeout_seconds
        try:
            service.get_change(change_id)  # confirm the ticket exists first — error immediately instead of waiting for nothing
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e
        return await _wait_for_decision(service, change_id, wait_s, ctx)

    @mcp.tool
    def list_server_databases(
        project: str, connection: str, ctx: Context | None = None
    ) -> list[str]:
        """List the databases available on the server for this connection.

        PostgreSQL has two layers — database and schema — and a connection can only query
        one database at a time: use this tool first to see what databases exist, then pass
        the database name as the pg_database parameter to other tools to operate on that
        database (list_databases lists the schemas *inside* a given database). MySQL/
        ClickHouse have no such layer, so the result is the same as list_databases.
        """
        try:
            return service.list_server_databases(project, connection, _caller_from_ctx(ctx))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def list_databases(
        project: str, connection: str, pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> list[str]:
        """List the databases/schemas available for this connection; MySQL/ClickHouse return databases, PostgreSQL returns schemas.

        For PostgreSQL, this lists the schemas inside pg_database (or the connection's
        bound database if not given); use list_server_databases to see what databases
        exist on the server.
        """
        try:
            db = service.resolve_pg_database(project, connection, pg_database)
            return service.list_databases(project, connection, _caller_from_ctx(ctx), database=db)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def list_tables(
        project: str,
        connection: str,
        database: Annotated[str | None, Field(description=_SCHEMA_DESC)] = None,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> list[str]:
        """List every table in the given database/schema."""
        try:
            db = service.resolve_pg_database(project, connection, pg_database)
            return service.list_tables(
                project, connection, _caller_from_ctx(ctx), schema=database, database=db
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def describe_table(
        project: str,
        connection: str,
        table: str,
        database: Annotated[str | None, Field(description=_SCHEMA_DESC)] = None,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> dict:
        """Show a table's structure: columns (type/nullable/default/comment), indexes, primary key."""
        try:
            db = service.resolve_pg_database(project, connection, pg_database)
            return service.describe_table(
                project, connection, table, _caller_from_ctx(ctx), schema=database, database=db
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def db_checkup(
        project: str,
        connection: str,
        database: Annotated[
            str | None,
            Field(description="The database/schema to check (MySQL/ClickHouse: database, "
                              "PostgreSQL: schema); omit to use the connection's default database"),
        ] = None,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> dict:
        """Database health check: get a structured diagnostic report in one call, no need for multiple rounds of SQL to probe "is this database healthy".

        Provides a set of read-only diagnostic checks per engine (each check degrades
        gracefully — one that can't be measured is simply marked unknown with a reason,
        without failing the whole report):

        - **MySQL** (16 checks): connection usage (including connections rejected by
          max_connections), active threads, InnoDB buffer pool hit rate and pressure,
          buffer pool vs. data size, slow queries, full-table-scan JOINs, temp tables
          spilling to disk, abnormal disconnects, deadlocks, long-running queries, row lock
          waits, tables without a primary key, large transactions spilling to disk,
          replication lag, top-5 largest tables
        - **PostgreSQL** (16 checks): connection usage, idle transactions, long-running
          queries, wait events, cache hit rate, temp files spilling to disk, deadlocks,
          dead-tuple bloat, stale statistics, unused indexes, replication lag, replication
          slot health (wal_status), WAL archiving failures, transaction ID wraparound risk,
          database and largest-table sizes
        - **ClickHouse** (8 checks): disk health (is_broken/read-only/free space), core
          metrics, failed queries, replica sync queue (expired sessions/log_pointer lag),
          active part count, unfinished mutations, top-5 largest tables
        - **SQLite**: integrity check, free-page fragmentation, journal mode, table row counts

        Each item has a status (ok / info / warn / critical / unknown), the dimension it
        belongs to, a human-readable value, and interpretation guidance; overall is the
        most severe status among them. **status=unknown means "not measured", not
        "healthy"** — the views were chosen to avoid most permission gates where possible
        (MySQL long-query/lock-wait checks use performance_schema, no PROCESS privilege
        needed; PG connection-usage/replication-slot/statistics views are visible to
        read-only accounts); checks that genuinely lack permission are summarized in the
        report's privileges field as ready-to-copy GRANT statements (e.g. `GRANT
        pg_monitor TO ...`).
        """
        try:
            db = service.resolve_pg_database(project, connection, pg_database)
            return service.db_checkup(
                project, connection, _caller_from_ctx(ctx), schema=database, database=db
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def db_checkup_all(
        project: str,
        connection: str,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> dict:
        """Instance-level health check: sweep **every user database** on the server this connection points at, merged into one diagnostic report.

        db_checkup only checks one database/schema; but "is this database healthy"
        shouldn't only look at the current one — slow queries, large tables, index bloat,
        and tables without a primary key can all happen in any database. This tool
        checks each database and merges the results:

        - Instance-level metrics (connection usage, cache hit rate, long-running queries,
          locks, replication lag, ...) are the same value across databases, so only one
          copy is kept;
        - Database-level metrics (top-5 largest tables, tables without a primary key,
          bloat, unused indexes, ...) take the **most severe** entry, with the title
          prefixed by `[database name]`.

        So the report is about the same length as db_checkup's but covers every
        database. A single-database instance (like SQLite) automatically falls back to
        db_checkup. **Use this tool when you want to know the health of the whole
        instance, rather than stitching together per-database db_checkup calls.**
        """
        try:
            db = service.resolve_pg_database(project, connection, pg_database)
            return service.db_checkup_all(
                project, connection, _caller_from_ctx(ctx), database=db
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def table_ddl(
        project: str,
        connection: str,
        table: Annotated[
            str, Field(description="Table name; comma-separate several, e.g. `orders,order_item`")
        ],
        database: Annotated[str | None, Field(description=_SCHEMA_DESC)] = None,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> str:
        """Show the CREATE TABLE statement (DDL). Gives more raw detail than describe_table — index definitions, charset, engine, partitioning, etc.

        MySQL / ClickHouse return the raw `SHOW CREATE TABLE` text; engines without such a
        statement (PostgreSQL / SQLite, etc.) return an **approximate DDL** built from
        reflecting the table structure (a leading comment says so), useful for
        understanding the structure but not to be treated as a script you can run as-is
        to create the database.

        Use describe_table when you only need column names and types — it's cheaper on
        context; use this one when you need to see how indexes are built, whether it's
        partitioned, or the raw defaults/comments from table creation.
        """
        names = [t.strip() for t in table.split(",") if t.strip()]
        caller = _caller_from_ctx(ctx)
        try:
            # Single table takes the original path: a misspelled name or missing
            # permission should raise a real error to the agent, not return a comment
            # saying "failed to fetch" that looks like success. Only the batch path
            # needs per-table fault tolerance.
            db = service.resolve_pg_database(project, connection, pg_database)
            if len(names) == 1:
                return service.get_table_ddl(project, connection, names[0], caller,
                                             schema=database, database=db)
            items = service.get_table_ddls(project, connection, names, caller,
                                           schema=database, database=db)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e
        # Multiple tables: separate each block with a table-name comment, and honestly
        # mark the ones that failed to fetch (don't silently skip them)
        return "\n\n".join(
            f"-- {it['table']}\n" + (it["ddl"] if "ddl" in it
                                     else f"-- Failed to fetch DDL: {it['error']}")
            for it in items
        )

    @mcp.tool
    def sample_rows(
        project: str,
        connection: str,
        table: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> str:
        """Sample a table's data (default 10 rows, up to 100). Returns compact TSV text (same format as query).

        Shares the same session-level result quota with query.
        """
        caller = _caller_from_ctx(ctx)
        try:
            _charge = _budget_gate(service, caller)
            result = service.sample_rows(project, connection, table, limit, caller,
                                         database=pg_database)
            budget = service.agent_result_budget(project, connection)
            return _charge(render_agent_result(result, budget))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def export_table(
        project: str,
        connection: str,
        table: Annotated[str, Field(description="Table name to export")],
        limit: Annotated[
            int, Field(ge=1, description="Maximum rows to export; cannot exceed the connection policy's max_rows")
        ],
        fields: Annotated[
            list[str] | None,
            Field(description="List of column names to export; omit or pass an empty list for all columns"),
        ] = None,
        format: Annotated[  # noqa: A002
            Literal["csv", "json", "markdown", "xlsx"],
            Field(description="Export format: csv / json / markdown / xlsx"),
        ] = "csv",
        database: Annotated[
            str | None,
            Field(description="Database/schema to export from (schema for PG); omit if the connection already has a default database bound"),
        ] = None,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> dict:
        """Export data by database, table, columns, and row count, returning a short-lived download link.

        Does not accept arbitrary SQL; the table and columns are validated and safely
        quoted first. Export uses the read-only account, a read-only query, and is
        audited, subject to the connection's max_rows limit, and follows the same
        sensitive-column masking policy as the agent. The file is kept on the server for
        one hour; the tool result only contains metadata and the download URL — the file
        content never enters the agent's context.
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.export_table(
                    project, connection, table, fields, limit, format, caller, database,
                    pg_database=pg_database,
                )
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def analysis_workspaces() -> dict:
        """List analysis workspaces (with their datasets) and saved workflows (a DuckDB sandbox for cross-source data analysis).

        Use it when: cross-connection JOINs, large-result aggregation, multi-step
        analysis — snapshot the data into a workspace, then analyze it freely with
        analysis_sql and bring back only the small result. For a simple single-table
        query, use query directly instead.
        """
        try:
            return {"workspaces": service.analysis_overview(),
                    "workflows": [{"name": w["name"], "workspace": w["workspace"],
                                   "kind": "graph" if w.get("graph") else "script"}
                                  for w in service.workflow_list()]}
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def analysis_import(
        workspace: Annotated[str, Field(description="Workspace name (created automatically if it doesn't exist)")],
        dataset: Annotated[str, Field(description="Name of the dataset (table) to import into")],
        project: str,
        connection: str,
        sql: Annotated[str, Field(description="Read-only fetch SQL, e.g. SELECT * FROM t or an aggregation query")],
        limit: Annotated[int | None, Field(description="Snapshot row cap (default 200,000, hard cap 500,000)")] = None,
        schema: Annotated[str | None, Field(description="Execution schema (required for a connection with no bound database)")] = None,
        pg_database: PgDatabase = None,
        ctx: Context | None = None,
    ) -> dict:
        """Snapshot a query's result from a connection into the analysis workspace (fetched via the read-only account, fully audited, with a row cap).

        Step one of cross-source analysis: import each source's table/query result as a
        workspace dataset, then JOIN them with analysis_sql. A dataset with the same name
        is replaced (friendly to re-running).
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.analysis_import(workspace, dataset, project, connection,
                                                sql, caller, limit, schema,
                                                database=pg_database))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def analysis_sql(
        workspace: str,
        sql: Annotated[str, Field(description="Any SQL inside the workspace: JOIN/aggregation/CREATE VIEW/DDL are all fine (local sandbox, never touches production)")],
        max_rows: Annotated[int, Field(ge=1, le=5000, description="Row cap for the returned result")] = 200,
        ctx: Context | None = None,
    ) -> dict:
        """Run SQL inside the analysis workspace (DuckDB dialect, full support for JOIN/window functions/CTEs).

        A workspace is a local sandbox: creating views, building intermediate tables, and
        modifying data all need no approval — none of it touches any production database.
        Store intermediate results as a VIEW/TABLE so a multi-step analysis only needs to
        carry the final small result in context.
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.analysis_sql(workspace, sql, caller, max_rows))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def run_workflow(
        name: Annotated[str, Field(description="Workflow name (a saved analysis flow, from a human or an agent)")],
        ctx: Context | None = None,
    ) -> dict:
        """Re-run a saved analysis workflow with one call: re-pull the source data -> run each step -> return each step's status
        and a preview of the final output. Both kinds of workflow are supported: script-style
        (a multi-statement SQL script) and visual DAG (a fetch/filter/JOIN/aggregate flow
        laid out on the admin-backend canvas, executed in topological order).
        An agent can re-run an analysis a human saved and interpret the result as needed.
        See the analysis_workspaces tool, or ask the user, for the list of available workflows.
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.workflow_run(name, caller))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def save_workflow(
        name: Annotated[str, Field(description="Workflow name (an existing script-style workflow with the same name will be overwritten)")],
        workspace: Annotated[str, Field(description="Analysis workspace name (the workspace the datasets live in)")],
        script: Annotated[str, Field(description="A multi-statement SQL script (semicolon-separated, "
                                                 "DuckDB dialect) referencing the workspace's datasets; the last SELECT is the output")],
        ctx: Context | None = None,
    ) -> dict:
        """Save the current analysis as a re-runnable workflow: the script plus the fetch recipe for each dataset in the workspace (collected automatically).

        Use analysis_import to import data into the workspace and analysis_sql to verify
        the script works, then save it; afterwards a human or an agent can re-run it with
        one call via run_workflow (which re-pulls the latest source data).
        Cannot overwrite an admin-backend canvas (DAG) workflow with the same name.
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.workflow_save(name, workspace, script, caller,
                                              allow_replace_graph=False))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def test_connection(project: str, connection: str, ctx: Context | None = None) -> dict:
        """Test connectivity for a connection (runs SELECT 1)."""
        try:
            return service.test_connection(project, connection, _caller_from_ctx(ctx))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    return mcp
