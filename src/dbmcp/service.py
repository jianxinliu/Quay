"""核心服务层：与 MCP 传输解耦，便于单元测试。

所有会触达数据库的操作都必须落审计记录（成功 / 拒绝 / 出错），
拒绝路径同样入库——这正是要给人看的部分。
"""

from __future__ import annotations

from dataclasses import dataclass

from .approvals import ApprovalError, ApprovalStore
from .audit.classify import classify, fingerprint
from .audit.log import AuditRecord, AuditStore
from .audit.risk import assess
from .config import AppConfig, ConnectionConfig
from .metadata import MetadataCache
from . import engines


class QueryRejected(Exception):
    """SQL 被审计规则拒绝。message 面向 agent，说明原因与下一步动作。"""


@dataclass
class CallerInfo:
    agent: str = "unknown"
    session_id: str = ""


class DbmService:
    def __init__(
        self,
        config: AppConfig,
        store: AuditStore,
        approvals: ApprovalStore | None = None,
        metadata: MetadataCache | None = None,
    ):
        self.config = config
        self.store = store
        self.pool = engines.EnginePool()
        self.approvals = approvals
        self.metadata = metadata

    # ---------- 元信息 ----------

    def list_projects(self) -> list[dict]:
        return [
            {"project": name, "connections": sorted(proj.connections)}
            for name, proj in sorted(self.config.projects.items())
        ]

    def list_connections(self, project: str) -> list[dict]:
        proj = self.config.projects.get(project)
        if proj is None:
            raise KeyError(f"项目 {project!r} 不存在")
        return [
            {
                "connection": name,
                "engine": c.engine,
                "environment": c.environment,
                "database": c.database,
                "host": c.host,
                # 有意不返回 user/password/writer 等账号信息
            }
            for name, c in sorted(proj.connections.items())
        ]

    # ---------- 查询 ----------

    def query(self, project: str, connection: str, sql: str, caller: CallerInfo) -> dict:
        cfg = self.config.get_connection(project, connection)
        rec = self._base_record(project, connection, cfg, "query", sql, caller)

        verdict = classify(sql, cfg.engine)
        if not verdict.readonly:
            rec.status = "rejected"
            rec.detail = verdict.reason
            self.store.record(rec)
            raise QueryRejected(
                f"已拒绝：{verdict.reason}。query 工具仅允许只读语句；"
                "数据变更操作需人工授权的 execute 流程（M3 上线后提供）。"
            )

        try:
            engine = self.pool.get(project, connection, cfg)
            result = engines.run_query(engine, sql, cfg.policy.max_rows)
        except QueryRejected:
            raise
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise

        rec.status = "ok"
        rec.row_count = result.row_count
        rec.duration_ms = result.duration_ms
        self.store.record(rec)
        return {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
            "statement_kind": verdict.statement_kind,
        }

    # ---------- 写操作（拒绝—重提 + change_id 放行）----------

    def execute(
        self,
        project: str,
        connection: str,
        sql: str,
        caller: CallerInfo,
        reason: str = "",
        change_id: int | None = None,
    ) -> dict:
        """写操作统一入口。

        - 只读语句：直接执行（等价于 query）；
        - 写操作 + 无 change_id：评估风险、生成审批单、拒绝并返回 change_id；
        - 写操作 + 有 change_id：校验审批单后执行**审批单里存储的 SQL**。
        """
        cfg = self.config.get_connection(project, connection)
        if self.approvals is None:
            raise QueryRejected("审批子系统未启用，无法执行写操作")

        verdict = classify(sql, cfg.engine)
        if verdict.readonly:
            return {"status": "executed", "readonly": True, **self.query(project, connection, sql, caller)}

        if change_id is not None:
            return self._execute_approved(project, connection, cfg, sql, change_id, caller)
        return self._request_approval(project, connection, cfg, sql, reason, caller)

    def _request_approval(
        self,
        project: str,
        connection: str,
        cfg: ConnectionConfig,
        sql: str,
        reason: str,
        caller: CallerInfo,
    ) -> dict:
        report = assess(sql, cfg.engine, self._meta_provider(project, connection, cfg))
        change = self.approvals.create(
            project=project,
            connection=connection,
            environment=cfg.environment,
            engine=cfg.engine,
            sql=sql,
            fingerprint=fingerprint(sql, cfg.engine),
            reason=reason,
            risk_level=report.level,
            risk_report=report.to_dict(),
            agent=caller.agent,
            session_id=caller.session_id,
        )
        rec = self._base_record(project, connection, cfg, "execute", sql, caller)
        rec.status = "rejected"
        rec.detail = f"需人工授权，已生成审批单 #{change.id}（风险 {report.level}）"
        self.store.record(rec)
        return {
            "status": "approval_required",
            "change_id": change.id,
            "risk": report.to_dict(),
            "message": (
                f"该操作被评估为需人工授权（风险等级 {report.level}）。"
                f"已生成审批单 #{change.id}，请通知用户在管理后台审批；"
                f"批准后带上 change_id={change.id} 重新提交相同 SQL 即可执行。"
                f"审批单 30 分钟内有效。"
            ),
        }

    def _execute_approved(
        self,
        project: str,
        connection: str,
        cfg: ConnectionConfig,
        sql: str,
        change_id: int,
        caller: CallerInfo,
    ) -> dict:
        rec = self._base_record(project, connection, cfg, "execute", sql, caller)
        try:
            change = self.approvals.consume(
                change_id, fingerprint(sql, cfg.engine), (project, connection)
            )
        except ApprovalError as e:
            rec.status = "rejected"
            rec.detail = str(e)
            self.store.record(rec)
            return {"status": "rejected", "change_id": change_id, "reason": str(e)}

        # 执行审批单里存储的 SQL（不是 agent 重提的文本），用 writer 账号
        try:
            engine = self.pool.get(project, connection, cfg, role="writer")
            result = engines.run_write(engine, change.sql)
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise

        rec.status = "ok"
        rec.detail = f"审批单 #{change_id} 已核销（审批人 {change.decided_by}）"
        rec.row_count = result.row_count
        rec.duration_ms = result.duration_ms
        self.store.record(rec)
        return {
            "status": "executed",
            "change_id": change_id,
            "affected_rows": result.row_count,
            "duration_ms": result.duration_ms,
        }

    def _meta_provider(self, project: str, connection: str, cfg: ConnectionConfig):
        """给风险引擎注入"按表取元数据"的能力；无缓存或取不到时返回 None。"""
        if self.metadata is None:
            return lambda _table: None

        def provider(table: str):
            try:
                return self.metadata.get(project, connection, cfg, table)
            except Exception:
                return None

        return provider

    # ---------- 审批决策（管理后台 / elicitation 调用）----------

    def approve_change(self, change_id: int, decided_by: str, note: str = ""):
        if self.approvals is None:
            raise QueryRejected("审批子系统未启用")
        return self.approvals.approve(change_id, decided_by, note)

    def reject_change(self, change_id: int, decided_by: str, note: str = ""):
        if self.approvals is None:
            raise QueryRejected("审批子系统未启用")
        return self.approvals.reject(change_id, decided_by, note)

    def get_change(self, change_id: int):
        if self.approvals is None:
            raise QueryRejected("审批子系统未启用")
        return self.approvals.get(change_id)

    def list_changes(self, status: str | None = None):
        if self.approvals is None:
            return []
        return self.approvals.list_by_status(status)

    # ---------- schema 探索 ----------

    def list_tables(self, project: str, connection: str, caller: CallerInfo) -> list[str]:
        cfg = self.config.get_connection(project, connection)
        engine = self.pool.get(project, connection, cfg)
        return self._audited(project, connection, cfg, "list_tables", "", caller,
                             lambda: engines.list_tables(engine))

    def describe_table(self, project: str, connection: str, table: str, caller: CallerInfo) -> dict:
        cfg = self.config.get_connection(project, connection)
        engine = self.pool.get(project, connection, cfg)
        return self._audited(project, connection, cfg, "describe_table", table, caller,
                             lambda: engines.describe_table(engine, table))

    def sample_rows(self, project: str, connection: str, table: str, limit: int, caller: CallerInfo) -> dict:
        cfg = self.config.get_connection(project, connection)
        limit = min(limit, cfg.policy.max_rows)
        engine = self.pool.get(project, connection, cfg)

        def _run() -> dict:
            result = engines.sample_rows(engine, table, limit)
            return {
                "columns": result.columns,
                "rows": result.rows,
                "row_count": result.row_count,
                "duration_ms": result.duration_ms,
            }

        return self._audited(project, connection, cfg, "sample_rows", table, caller, _run)

    def test_connection(self, project: str, connection: str, caller: CallerInfo) -> dict:
        cfg = self.config.get_connection(project, connection)

        def _run() -> dict:
            engine = self.pool.get(project, connection, cfg)
            result = engines.run_query(engine, "SELECT 1", max_rows=1)
            return {"ok": True, "engine": cfg.engine, "duration_ms": result.duration_ms}

        return self._audited(project, connection, cfg, "test_connection", "", caller, _run)

    # ---------- 内部 ----------

    def _base_record(
        self,
        project: str,
        connection: str,
        cfg: ConnectionConfig,
        tool: str,
        sql: str,
        caller: CallerInfo,
    ) -> AuditRecord:
        return AuditRecord(
            project=project,
            connection=connection,
            tool=tool,
            status="",
            agent=caller.agent,
            session_id=caller.session_id,
            environment=cfg.environment,
            engine=cfg.engine,
            sql=sql,
            fingerprint=fingerprint(sql, cfg.engine) if sql else "",
        )

    def _audited(self, project, connection, cfg, tool, detail_sql, caller, fn):  # noqa: ANN001
        rec = self._base_record(project, connection, cfg, tool, detail_sql, caller)
        try:
            result = fn()
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        self.store.record(rec)
        return result

    def close(self) -> None:
        self.pool.dispose()
        self.store.close()
        if self.approvals is not None:
            self.approvals.close()
        if self.metadata is not None:
            self.metadata.close()
