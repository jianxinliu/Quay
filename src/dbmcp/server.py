"""MCP 接口层：把 DbmService 注册为 FastMCP 工具。

工具描述会直接进入 agent 的上下文，写清楚约束能减少 agent 撞墙。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Annotated, Literal

import anyio.to_thread
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from .agent_format import render_agent_result
from .approvals import STATUS_APPROVED, STATUS_CONSUMED, STATUS_PENDING, ApprovalError
from .errors import translate_db_error
from .health import ConnectionUnavailable
from .service import CallerInfo, DbmService, QueryRejected, change_status_payload

# 等待人工审批：每 1s 复查一次审批单状态。用轮询而非进程内条件变量，是因为决策也可能
# 来自别的进程（`dbm approve` CLI），条件变量覆盖不到；1s 延迟对「人点批准」无感。
_WAIT_POLL_S = 1.0
_WAIT_HEARTBEAT_S = 10.0   # 每 10s 报一次 progress，防客户端把长调用判超时
_WAIT_MAX_S = 3600


def _tool_error_from_unavailable(e: ConnectionUnavailable) -> ToolError:
    """把 ConnectionUnavailable 转成给 agent 的清晰 ToolError 文案。

    文案里包含 state（unavailable/exhausted）与建议重试秒数，agent 据此决定
    是稍等重试还是提示用户去后台处理，而不是把 pymysql 2013 之类的原始错误抛给 agent。
    """
    if e.state == "exhausted":
        return ToolError(f"[connection_exhausted] {e}")
    hint = f"（建议 {e.retry_after_s} 秒后重试）" if e.retry_after_s else ""
    return ToolError(f"[connection_unavailable] {e}{hint}")


def agent_error(e: BaseException) -> ToolError:
    """**agent 侧唯一的错误出口**：任何异常在这里被翻译成分类化、已脱敏的 ToolError。

    错误控制必须收在服务内部——不能让驱动异常（含 DSN 里的账号密码、绑定参数、
    SQLAlchemy 的 traceback 与背景链接）冒泡到 agent 上下文，也不能变成传输层 500。
    分类前缀让 agent 一眼知道下一步：

    - `[connection_unavailable]` / `[connection_exhausted]`：连接问题，稍后重试 / 需人介入
    - `[sql_syntax_error]` 等 DB 错误分类（见 errors.py）：改 SQL，别原样重发
    - 其余业务拒绝（审批/只读限制/参数错）：原样透传服务层已经写好的人话

    ToolError 本身直接放行（上游已经组织好文案）。
    """
    if isinstance(e, ToolError):
        return e
    if isinstance(e, ConnectionUnavailable):
        return _tool_error_from_unavailable(e)
    if isinstance(e, (QueryRejected, ValueError)):
        return ToolError(str(e))
    if isinstance(e, KeyError):
        # KeyError 的 str() 是带引号的 repr，取原始消息更可读
        return ToolError(str(e.args[0]) if e.args else "未找到指定资源")
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
    message = (
        f"Agent 请求执行数据变更（审批单 #{cid}，风险等级 {risk.get('level', '?')}）\n"
        f"连接: {project}/{connection}（环境 {cfg.environment}）\n"
        f"语句: {statement}\n"
        f"判定: {'; '.join(risk.get('reasons', [])) or '—'}\n"
        f"选择 approve 批准并立即执行；deny 或关闭则驳回。"
    )
    try:
        answer = await ctx.elicit(message, response_type=["approve", "deny"])
    except Exception:
        return result  # 客户端不支持 elicitation → 审批单流程兜底

    decided_by = f"elicitation:{caller.agent}"
    try:
        if getattr(answer, "action", None) == "accept" and getattr(answer, "data", None) == "approve":
            service.approve_change(cid, decided_by=decided_by, note="会话内确认")
            return await anyio.to_thread.run_sync(resubmit, cid)
        service.reject_change(cid, decided_by=decided_by, note="会话内拒绝")
        return {"status": "rejected", "change_id": cid, "reason": "用户在会话内拒绝了该操作"}
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
                    message=f"等待人工审批（审批单 #{change_id}）",
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
        # 后台按了「批准并立即执行」：变更已落地，这里只把结果转达给 agent（再重提会被拒）
        executed = decision.get("exec_result") or {}
        return {"status": "executed", "change_id": cid, **executed,
                "message": "已由审批人在管理后台批准并直接执行"}
    if status == STATUS_PENDING:  # 等待超时，审批单仍有效
        return {**result, "waited_seconds": wait_seconds,
                "message": f"{result.get('message', '')} 已等待 {int(wait_seconds)}s 仍无人决策，"
                           f"可再调 wait_for_change({cid}) 继续等待。"}
    reason = decision.get("decision_note") or f"审批单当前状态为 {status}"
    return {"status": "rejected", "change_id": cid, "reason": reason}


def build_mcp(service: DbmService) -> FastMCP:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_server: FastMCP):
        # 启动即把并发/连接池设置应用到运行时（线程池上限需在事件循环内才设得上）
        service.apply_runtime_settings()
        yield {}

    mcp = FastMCP(
        name="db-manage-mcp",
        lifespan=_lifespan,
        instructions=(
            "统一的数据库访问服务。开始跑 SQL 前，建议先调 begin_session(title, note) 声明本次"
            "会话的名字和背景，之后本会话跑过的 SQL 会在后台按此会话归类、方便人回溯。"
            "先用 list_projects / list_connections 找到目标连接，"
            "用 list_tables / describe_table / sample_rows 探索 schema。"
            "按库、表、字段、行数导出文件用 export_table（支持 CSV/JSON/Markdown/XLSX）。"
            "必要时可用程序把 export_table 返回的 download_url 直接下载到目标位置，"
            "不要读取或把文件内容放入模型上下文。"
            "只读查询用 query（仅接受 SELECT/SHOW/DESCRIBE/EXPLAIN）。"
            "数据变更（INSERT/UPDATE/DELETE/DDL）用 execute：首次提交会生成审批单，"
            "并在服务端等待人工决策——把返回的 approval_url 贴给用户让其点开审批，"
            "用户一批准本次调用就自动执行并返回 status=executed，不必让用户回来说「已批准」。"
            "若等待超时返回 status=approval_required，提醒用户后调 wait_for_change(change_id) "
            "继续等（别自己循环调 get_change_status 轮询）。"
            "跨源 JOIN、大结果集聚合、多步分析请用分析工作台（DuckDB 本地沙箱）："
            "analysis_import 把各源查询结果快照为工作区数据集（reader 拉取、带行数上限），"
            "analysis_sql 在工作区自由 JOIN/聚合/建 VIEW（不需审批），只把小结果带回上下文。"
            "做完的分析可用 save_workflow 沉淀为可重跑流程，"
            "人或 agent 沉淀的流程（多语句脚本或后台画布 DAG）用 run_workflow 一键重跑："
            "自动重拉源数据 → 逐步执行 → 返回每步状态与输出，"
            "可用列表见 analysis_workspaces。所有操作都会被审计记录。"
            "\n【写 SQL 的约定，务必遵守】"
            "① 时区：本服务不固定数据库会话时区，@@session.time_zone 继承各库设置"
            "（可能是 UTC+8，也可能是 UTC 或其它）。凡用 FROM_UNIXTIME / UNIX_TIMESTAMP / "
            "NOW / CURDATE / DATE 等依赖会话时区的函数，先 `SELECT @@session.time_zone` 确认，"
            "切勿重复叠加时区偏移——典型坑：会话已是 UTC+8 却又手动 +28800，等于 +16h，"
            "按天分组会把傍晚的数据串到第二天、同一天裂成两行。"
            "② epoch 秒列按天分组：优先纯算术 `FLOOR((ts+偏移)/86400)` 得 day_idx，"
            "日期由 day_idx 反推 `DATE_ADD('1970-01-01', INTERVAL day_idx DAY)`，绕开隐式时区，"
            "day_idx 唯一决定日期、不必把日期再放进 GROUP BY。"
            "③ 大表 SELECT 必须带 LIMIT 或 WHERE 收窄，别全表拉取。"
            "④ 尽量走索引：WHERE / JOIN / ORDER BY 的过滤列尽量命中索引，先用 describe_table "
            "看有哪些索引，不确定就用 query 跑 EXPLAIN（access_type=ALL/table 即全表扫描）。"
            "别对索引列套函数或运算（如 `DATE(ts)`、`FROM_UNIXTIME(ts)`、`ts+1` 放在 WHERE 左侧）"
            "——会使索引失效；应改成对常量侧做转换、用范围比较（如 `ts >= 起 AND ts < 止`）。"
            "⑤ execute 支持多语句批量（分号分隔，如 ALTER + 回填 UPDATE 的迁移），"
            "整批一次审批、按语句拆开在同一事务逐条执行。"
            "⑥ query/sample_rows 返回**紧凑 TSV 文本**（非 JSON，省 token）：顶部 `#` 元信息行 + "
            "`# types:` 列类型，随后首行列名、其余数据行，制表符分隔，`\\N`=NULL，大整数为字符串。"
            "结果有**两级硬上限**：行数（默认 1000）+ 字符预算（默认 ≈12k token）；元信息里 "
            "`truncated=true` 即没给全——**别重复拉全量**，用 WHERE/LIMIT/聚合收窄，或用分析工作台下推计算。"
            "\n【错误怎么读】所有错误都带一个方括号分类前缀，照着它决定下一步，别盲目重发同一条 SQL："
            "`[sql_syntax_error]` = 语法错（发到 DB 前就被拦下并由 DB 复核确认），必须改写 SQL；"
            "`[table_not_found]`/`[column_not_found]` = 先用 list_tables / describe_table 核对名字；"
            "`[permission_denied]`/`[readonly_violation]` = 只读账号不能写，数据变更走 execute 审批流；"
            "`[query_timeout]` = 收窄范围或改用聚合/分析工作台；"
            "`[connection_unavailable]` = 连接暂时断开、后台正在自动重连，按提示的秒数稍后重试即可；"
            "`[connection_exhausted]` = 连续重连失败（仍在自动重试），提醒用户去后台看一眼连接。"
        ),
    )

    @mcp.custom_route("/exports/{token:str}/{filename:str}", methods=["GET"])
    async def _download_export(req: Request) -> Response:
        """短期随机 token 下载；文件路径只能由 export_table 在专用目录内创建。"""
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
        """列出所有项目及其下可用的数据库连接名。"""
        try:
            return service.list_projects()
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def list_connections(project: str) -> list[dict]:
        """列出指定项目下的数据库连接（引擎、环境、库名等元信息，不含账号密码）。"""
        try:
            return service.list_connections(project)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def begin_session(
        title: Annotated[str, Field(description="本次会话的名字，如「排查订单重复扣款」")],
        note: Annotated[str, Field(description="会话简介/背景，可选，如「复现 issue #123，只读排查」")] = "",
        ctx: Context | None = None,
    ) -> dict:
        """声明本次工作会话的名字和简介（建议在开始跑 SQL 前调用一次）。

        登记后，本次会话里跑过的所有 SQL（query/execute/sample_rows 等）都会在管理后台
        按这个会话归类，方便人回溯「这个会话都做了哪些操作」。同一会话可重复调用以更新名字。
        不调用也能工作，但后台只能看到一串没有语义的会话 id。
        """
        try:
            return service.begin_session(_caller_from_ctx(ctx), title, note)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def query(
        project: str,
        connection: str,
        sql: Annotated[str, Field(description="单条只读 SQL（SELECT/SHOW/DESCRIBE/EXPLAIN）")],
        ctx: Context | None = None,
    ) -> str:
        """在指定连接上执行只读 SQL，返回紧凑 TSV 文本（比 JSON 省 token）。

        输出格式：顶部 `#` 元信息行（`shown=N truncated=bool reason=... elapsed_ms=...`）+
        `# types:` 列类型行；随后首行是列名、其余是数据行，**制表符分隔**，`\\N` 表示 NULL，
        值里的 `\\ \\t \\n` 做反斜杠转义。**大整数以字符串返回**（超 2^53 精度安全）。

        结果受两级硬上限：① 行数（连接 max_rows，默认 1000）；② 字符预算
        （agent_max_result_chars，默认 40000≈12k token）。`truncated=true` 表示没给全——
        **不要重复拉全量**，改用 WHERE/LIMIT/聚合收窄，或用分析工作台（analysis_*）下推计算。

        非只读语句（含多语句、CTE 中夹带 DML、SELECT FOR UPDATE、SLEEP 等有副作用函数）会被拒绝。
        """
        try:
            result = service.query(project, connection, sql, _caller_from_ctx(ctx))
            budget = service.agent_result_budget(project, connection)
            return render_agent_result(result, budget)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def execute(
        project: str,
        connection: str,
        sql: Annotated[str, Field(description="要执行的写 SQL（INSERT/UPDATE/DELETE/DDL）；"
                                  "支持多语句批量（分号分隔，如 ALTER + 回填 UPDATE 的迁移），"
                                  "整批一次审批、按语句拆开在同一事务逐条执行")],
        reason: Annotated[str, Field(description="变更原因，供审批人参考")] = "",
        change_id: Annotated[
            int | None, Field(description="已获批审批单号；批准后带上它重提相同 SQL 即可执行")
        ] = None,
        wait_seconds: Annotated[
            int | None,
            Field(description="首次提交生成审批单后，服务端等待人工决策的秒数；"
                              "0=不等待立即返回审批单号，省略=用系统设置的默认值"),
        ] = None,
        ctx: Context | None = None,
    ) -> dict:
        """执行数据变更操作（需人工授权）。

        首次提交（不带 change_id）：系统评估风险并生成审批单，并**在服务端等待人工决策**
        （默认等待时长见系统设置，可用 wait_seconds 覆盖）。等待期间请把返回的 approval_url
        贴给用户，让其点开审批页处理；用户一批准，本次调用就会自动执行并返回
        status=executed，无需用户回到会话里说「已批准」，也无需你再重提一次。
        若客户端支持会话内确认（elicitation）且连接策略允许，则直接弹确认框、批准即执行。

        返回 status=approval_required 表示等待超时而审批单仍挂着：把 approval_url 再提醒
        用户一次，然后调 wait_for_change(change_id) 继续等即可（审批单 60 分钟内有效）。
        返回 status=rejected 时 reason 说明原因（被驳回/已过期/SQL 不一致），据此调整。
        只读语句会被直接执行。
        """
        caller = _caller_from_ctx(ctx)
        run = partial(service.execute, project, connection, sql, caller, reason=reason)
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

    # Redis 有意不暴露为 MCP 工具：agent 碰不到 Redis。Redis 仅供人通过已登录的
    # 管理后台 /admin/redis 操作（对标 Medis 的独立控制台）。

    @mcp.tool
    def get_change_status(change_id: int) -> dict:
        """查询审批单当前状态（pending / approved / rejected / consumed / expired），立即返回。

        只想「等到有结果为止」用 wait_for_change，别自己写循环反复调本工具。
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
            int | None, Field(description="最长等待秒数，省略则用系统设置的默认值")
        ] = None,
        ctx: Context | None = None,
    ) -> dict:
        """等待审批单被人决策，一直阻塞到有结果或超时（不要自己轮询 get_change_status）。

        用在 execute 的等待超时之后：把 approval_url 再提醒用户一次，然后调本工具继续等。
        返回 status=approved 表示可以带 change_id 重提相同 SQL 执行；
        status=consumed 表示审批人在后台点了「批准并立即执行」，变更已落地（exec_result 里有
        影响行数），**不要再重提**；status=rejected/expired 见 decision_note；
        status=pending 且 timed_out=true 表示本次等待超时、审批单仍有效，可再调一次继续等。
        """
        wait_s = service.approval_wait_seconds() if timeout_seconds is None else timeout_seconds
        try:
            service.get_change(change_id)  # 先确认审批单存在，不存在直接报错而不是空等
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e
        return await _wait_for_decision(service, change_id, wait_s, ctx)

    @mcp.tool
    def list_databases(
        project: str, connection: str, ctx: Context | None = None
    ) -> list[str]:
        """列出连接可选择的库/schema；MySQL/ClickHouse 返回数据库，PostgreSQL 返回 schema。"""
        try:
            return service.list_databases(project, connection, _caller_from_ctx(ctx))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def list_tables(
        project: str,
        connection: str,
        database: Annotated[
            str | None, Field(description="库/schema；不传时使用连接默认库")
        ] = None,
        ctx: Context | None = None,
    ) -> list[str]:
        """列出指定库/schema 中的所有表。"""
        try:
            return service.list_tables(
                project, connection, _caller_from_ctx(ctx), schema=database
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def describe_table(
        project: str,
        connection: str,
        table: str,
        database: Annotated[
            str | None, Field(description="库/schema；不传时使用连接默认库")
        ] = None,
        ctx: Context | None = None,
    ) -> dict:
        """查看表结构：字段（类型/可空/默认值/注释）、索引、主键。"""
        try:
            return service.describe_table(
                project, connection, table, _caller_from_ctx(ctx), schema=database
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def sample_rows(
        project: str,
        connection: str,
        table: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        ctx: Context | None = None,
    ) -> str:
        """抽样查看表数据（默认 10 行，上限 100 行）。返回紧凑 TSV 文本（格式同 query）。"""
        try:
            result = service.sample_rows(project, connection, table, limit, _caller_from_ctx(ctx))
            budget = service.agent_result_budget(project, connection)
            return render_agent_result(result, budget)
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def export_table(
        project: str,
        connection: str,
        table: Annotated[str, Field(description="要导出的表名")],
        limit: Annotated[
            int, Field(ge=1, description="最多导出的行数，不能超过连接策略 max_rows")
        ],
        fields: Annotated[
            list[str] | None,
            Field(description="要导出的字段名列表；不传或空列表表示全部字段"),
        ] = None,
        format: Annotated[  # noqa: A002
            Literal["csv", "json", "markdown", "xlsx"],
            Field(description="导出格式：csv / json / markdown / xlsx"),
        ] = "csv",
        database: Annotated[
            str | None,
            Field(description="要导出的库/schema；连接已绑定默认库时可不传"),
        ] = None,
        ctx: Context | None = None,
    ) -> dict:
        """按库、表、字段和行数导出数据，返回短期下载链接。

        不接受任意 SQL；表和字段会先校验并安全引用。导出使用 reader 账号、只读查询与审计，
        受连接 max_rows 限制，并沿用 agent 敏感字段脱敏策略。文件保存在服务端一小时，
        tool result 仅含元信息和下载 URL，文件内容不会进入 agent 上下文。
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.export_table(
                    project, connection, table, fields, limit, format, caller, database
                )
            )
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    def analysis_workspaces() -> dict:
        """列出分析工作区（含数据集）与已保存的 workflow（DuckDB 沙箱，跨源数据分析用）。

        适用场景：跨连接 JOIN、大结果集聚合、多步分析——把数据快照进工作区后
        用 analysis_sql 自由分析，只把小结果带回上下文。简单单表查询请直接用 query。
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
        workspace: Annotated[str, Field(description="工作区名（不存在则自动创建）")],
        dataset: Annotated[str, Field(description="导入后的数据集（表）名")],
        project: str,
        connection: str,
        sql: Annotated[str, Field(description="只读取数 SQL，如 SELECT * FROM t 或聚合查询")],
        limit: Annotated[int | None, Field(description="快照行数上限（默认 20 万，硬上限 50 万）")] = None,
        schema: Annotated[str | None, Field(description="执行 schema（未绑库连接需指定）")] = None,
        ctx: Context | None = None,
    ) -> dict:
        """从某个连接把查询结果快照进分析工作区（reader 只读拉取，全程审计，带行数上限）。

        跨源分析第一步：把各源的表/查询结果导成工作区数据集，再用 analysis_sql JOIN。
        同名数据集会被替换（重跑友好）。
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.analysis_import(workspace, dataset, project, connection,
                                                sql, caller, limit, schema))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def analysis_sql(
        workspace: str,
        sql: Annotated[str, Field(description="工作区内任意 SQL：JOIN/聚合/建 VIEW/DDL 均可（本地沙箱，不碰生产）")],
        max_rows: Annotated[int, Field(ge=1, le=5000, description="返回行数上限")] = 200,
        ctx: Context | None = None,
    ) -> dict:
        """在分析工作区执行 SQL（DuckDB 方言，完整支持 JOIN/窗口函数/CTE）。

        工作区是本地沙箱：建视图、建中间表、改数据都不需要审批——它不影响任何
        生产库。把中间结果存成 VIEW/TABLE，多步分析时上下文只需携带最终小结果。
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.analysis_sql(workspace, sql, caller, max_rows))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def run_workflow(
        name: Annotated[str, Field(description="workflow 名称（人或 agent 之前保存的分析流程）")],
        ctx: Context | None = None,
    ) -> dict:
        """一键重跑已保存的分析 workflow：重新拉取源数据 → 逐步执行 → 返回每步状态
        与最终输出预览。两类 workflow 均支持：脚本式（多语句 SQL）与可视化 DAG
        （管理后台画布编排的取数/过滤/JOIN/聚合流程，按拓扑序执行）。
        人沉淀的分析，agent 可按需重跑并解读结果。
        可用 workflow 列表见 analysis_workspaces 工具或询问用户。
        """
        caller = _caller_from_ctx(ctx)
        try:
            return await anyio.to_thread.run_sync(
                lambda: service.workflow_run(name, caller))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    @mcp.tool
    async def save_workflow(
        name: Annotated[str, Field(description="workflow 名称（已存在的脚本式同名会被覆盖更新）")],
        workspace: Annotated[str, Field(description="分析工作区名（数据集所在的工作区）")],
        script: Annotated[str, Field(description="多语句 SQL 脚本（分号分隔，DuckDB 方言），"
                                                 "引用工作区里的数据集；最后一条 SELECT 作为输出")],
        ctx: Context | None = None,
    ) -> dict:
        """把当前分析沉淀为可重跑的 workflow：脚本 + 工作区各数据集的取数配方（自动收集）。

        先用 analysis_import 把数据导入工作区、analysis_sql 验证脚本可行，再保存；
        之后人或 agent 都可用 run_workflow 一键重跑（自动重拉最新源数据）。
        同名的管理后台画布（DAG）workflow 不允许覆盖。
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
        """测试连接连通性（执行 SELECT 1）。"""
        try:
            return service.test_connection(project, connection, _caller_from_ctx(ctx))
        except Exception as e:  # noqa: BLE001
            raise agent_error(e) from e

    return mcp
