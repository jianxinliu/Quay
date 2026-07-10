"""核心服务层：与 MCP 传输解耦，便于单元测试。

所有会触达数据库的操作都必须落审计记录（成功 / 拒绝 / 出错），
拒绝路径同样入库——这正是要给人看的部分。
"""

from __future__ import annotations

from dataclasses import dataclass

from .audit.classify import classify, fingerprint
from .audit.log import AuditRecord, AuditStore
from .config import AppConfig, ConnectionConfig
from . import engines


class QueryRejected(Exception):
    """SQL 被审计规则拒绝。message 面向 agent，说明原因与下一步动作。"""


@dataclass
class CallerInfo:
    agent: str = "unknown"
    session_id: str = ""


class DbmService:
    def __init__(self, config: AppConfig, store: AuditStore):
        self.config = config
        self.store = store
        self.pool = engines.EnginePool()

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
