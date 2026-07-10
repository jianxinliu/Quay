"""MCP 接口层：把 DbmService 注册为 FastMCP 工具。

工具描述会直接进入 agent 的上下文，写清楚约束能减少 agent 撞墙。
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .service import CallerInfo, DbmService, QueryRejected


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


def build_mcp(service: DbmService) -> FastMCP:
    mcp = FastMCP(
        name="db-manage-mcp",
        instructions=(
            "统一的数据库访问服务。先用 list_projects / list_connections 找到目标连接，"
            "用 list_tables / describe_table / sample_rows 探索 schema。"
            "只读查询用 query（仅接受 SELECT/SHOW/DESCRIBE/EXPLAIN）。"
            "数据变更（INSERT/UPDATE/DELETE/DDL）用 execute：首次提交会生成审批单并返回 change_id，"
            "需人工在管理后台审批；批准后带 change_id 重提相同 SQL 才执行。"
            "所有操作都会被审计记录。"
        ),
    )

    @mcp.tool
    def list_projects() -> list[dict]:
        """列出所有项目及其下可用的数据库连接名。"""
        return service.list_projects()

    @mcp.tool
    def list_connections(project: str) -> list[dict]:
        """列出指定项目下的数据库连接（引擎、环境、库名等元信息，不含账号密码）。"""
        try:
            return service.list_connections(project)
        except KeyError as e:
            raise ToolError(str(e)) from e

    @mcp.tool
    def query(
        project: str,
        connection: str,
        sql: Annotated[str, Field(description="单条只读 SQL（SELECT/SHOW/DESCRIBE/EXPLAIN）")],
        ctx: Context | None = None,
    ) -> dict:
        """在指定连接上执行只读 SQL。结果默认截断到连接策略的 max_rows（默认 1000 行）。

        非只读语句（含多语句、CTE 中夹带 DML、SELECT FOR UPDATE 等）会被拒绝并记录审计。
        """
        try:
            return service.query(project, connection, sql, _caller_from_ctx(ctx))
        except (QueryRejected, KeyError, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool
    def execute(
        project: str,
        connection: str,
        sql: Annotated[str, Field(description="要执行的写 SQL（INSERT/UPDATE/DELETE/DDL）")],
        reason: Annotated[str, Field(description="变更原因，供审批人参考")] = "",
        change_id: Annotated[
            int | None, Field(description="已获批审批单号；批准后带上它重提相同 SQL 即可执行")
        ] = None,
        ctx: Context | None = None,
    ) -> dict:
        """执行数据变更操作（需人工授权）。

        首次提交（不带 change_id）：系统评估风险并生成审批单，返回 status=approval_required
        与 change_id；请把审批单号告知用户，让其在管理后台审批。
        审批通过后：带上 change_id 重新提交**完全相同的 SQL**，返回 status=executed。
        若返回 status=rejected，reason 会说明原因（未审批/已过期/被驳回/SQL 不一致），据此调整。
        只读语句会被直接执行。
        """
        try:
            return service.execute(
                project, connection, sql, _caller_from_ctx(ctx), reason=reason, change_id=change_id
            )
        except (QueryRejected, KeyError, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool
    def get_change_status(change_id: int) -> dict:
        """查询审批单状态（pending / approved / rejected / consumed / expired）及风险报告。"""
        try:
            change = service.get_change(change_id)
        except Exception as e:
            raise ToolError(str(e)) from e
        return {
            "change_id": change.id,
            "status": change.effective_status(),
            "risk_level": change.risk_level,
            "project": change.project,
            "connection": change.connection,
            "decided_by": change.decided_by,
            "decision_note": change.decision_note,
            "expires_at": change.expires_at,
        }

    @mcp.tool
    def list_tables(project: str, connection: str, ctx: Context | None = None) -> list[str]:
        """列出连接对应数据库中的所有表。"""
        try:
            return service.list_tables(project, connection, _caller_from_ctx(ctx))
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool
    def describe_table(
        project: str, connection: str, table: str, ctx: Context | None = None
    ) -> dict:
        """查看表结构：字段（类型/可空/默认值/注释）、索引、主键。"""
        try:
            return service.describe_table(project, connection, table, _caller_from_ctx(ctx))
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool
    def sample_rows(
        project: str,
        connection: str,
        table: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        ctx: Context | None = None,
    ) -> dict:
        """抽样查看表数据（默认 10 行，上限 100 行）。"""
        try:
            return service.sample_rows(project, connection, table, limit, _caller_from_ctx(ctx))
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool
    def test_connection(project: str, connection: str, ctx: Context | None = None) -> dict:
        """测试连接连通性（执行 SELECT 1）。"""
        try:
            return service.test_connection(project, connection, _caller_from_ctx(ctx))
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from e

    return mcp
