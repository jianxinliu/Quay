"""核心服务层：与 MCP 传输解耦，便于单元测试。

所有会触达数据库的操作都必须落审计记录（成功 / 拒绝 / 出错），
拒绝路径同样入库——这正是要给人看的部分。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .approvals import ApprovalError, ApprovalStore
from .audit.classify import classify, fingerprint
from .audit.log import AuditRecord, AuditStore
from .audit.redis_rules import classify_command, command_fingerprint, parse_command
from .audit.risk import assess
from .config import AppConfig, ConnectionConfig
from .masking import apply_mask
from .metadata import MetadataCache
from . import engines, redis_engine

logger = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL_S = 60
DEFAULT_RETENTION_DAYS = 30


class QueryRejected(Exception):
    """SQL 被审计规则拒绝。message 面向 agent，说明原因与下一步动作。"""


def _is_no_database_error(e: Exception) -> bool:
    """识别"未选定数据库"类错误：MySQL 1046 / PG no schema / 未限定表名。"""
    msg = str(e).lower()
    return (
        "1046" in msg
        or "no database selected" in msg
        or "no schema has been selected" in msg
    )


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
        config_path: str | None = None,
        snippets: "SnippetStore | None" = None,
    ):
        self.config = config
        self.store = store
        self.pool = engines.EnginePool()
        self.redis_pool = redis_engine.RedisPool()
        self.approvals = approvals
        self.metadata = metadata
        self.config_path = config_path
        self.snippets = snippets
        self._housekeeping_stop: threading.Event | None = None

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
                # 无默认库时提示 agent 用全限定表名
                **({"note": "此连接未绑定默认库，查询/schema 操作请用「库名.表名」全限定，"
                            "list_tables/describe_table 需先用 SHOW DATABASES 选定库"}
                   if c.engine in ("mysql", "postgres") and not c.database else {}),
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
            result = engines.run_query(engine, sql, cfg.policy.max_rows,
                                        max_cell_chars=cfg.policy.max_cell_chars)
        except QueryRejected:
            raise
        except Exception as e:
            # 无默认库时用非限定表名 → MySQL 1046 / PG 未指定 schema，给 agent 友好指引
            if not cfg.database and _is_no_database_error(e):
                rec.status = "error"
                rec.detail = "未选定数据库"
                self.store.record(rec)
                raise QueryRejected(
                    "该连接未绑定默认库。请用「库名.表名」全限定表名查询"
                    "（如 SELECT * FROM mydb.users），或先执行 SHOW DATABASES 查看可用库。"
                ) from e
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise

        rec.status = "ok"
        rec.row_count = result.row_count
        rec.duration_ms = result.duration_ms
        self.store.record(rec)
        rows, masked = apply_mask(result.columns, result.rows, cfg.policy)
        out = {
            "columns": result.columns,
            "rows": rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
            "statement_kind": verdict.statement_kind,
        }
        if masked:
            out["masked_columns"] = masked
        if result.truncated:
            out["hint"] = (
                f"结果已截断到 {cfg.policy.max_rows} 行（连接策略 max_rows）。"
                "如需后续数据，请在 SQL 中用 LIMIT/OFFSET（或 WHERE 条件缩小范围）自行分页。"
            )
        return out

    # ---------- 管理后台查询台（人已认证，写操作二次确认后直接执行）----------

    def admin_run_sql(
        self, project: str, connection: str, sql: str, caller: CallerInfo, confirm: bool = False
    ) -> dict:
        """管理后台查询台专用入口。**只挂在已认证的后台路由上，agent 无法触达。**

        - 只读语句：直接跑 reader 出结果（等价 query，含脱敏/截断策略）。
        - 写语句 + confirm=False：评估风险并返回风险报告，**不执行**。
        - 写语句 + confirm=True：经人工二次确认，直接用 writer 账号执行并落审计。
          这是后台专属旁路（不进审批单）；红线「拒绝—重提」只约束 agent 的 execute。
        """
        cfg = self.config.get_connection(project, connection)
        verdict = classify(sql, cfg.engine)
        if verdict.readonly:
            return {"kind": "read", **self.query(project, connection, sql, caller)}

        if not confirm:
            report = assess(sql, cfg.engine, self._meta_provider(project, connection, cfg))
            report_dict = report.to_dict()
            plan = self._try_explain(project, connection, cfg, sql)
            if plan:
                report_dict["explain"] = plan
            return {"kind": "confirm", "risk": report_dict,
                    "statement_kind": verdict.statement_kind}

        rec = self._base_record(project, connection, cfg, "admin_execute", sql, caller)
        try:
            engine = self.pool.get(project, connection, cfg, role="writer")
            result = engines.run_write(engine, sql)
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        rec.detail = "后台查询台直接执行（已二次确认）"
        rec.row_count = result.row_count
        rec.duration_ms = result.duration_ms
        self.store.record(rec)
        return {"kind": "write", "affected_rows": result.row_count,
                "duration_ms": result.duration_ms}

    def admin_export(
        self, project: str, connection: str, sql: str, fmt: str, caller: CallerInfo
    ) -> tuple[bytes, str, str]:
        """导出只读查询结果为文件，返回 (字节, media_type, 扩展名)。仅限只读语句。"""
        from .export import export_result

        cfg = self.config.get_connection(project, connection)
        if not classify(sql, cfg.engine).readonly:
            raise QueryRejected("导出仅支持只读查询（SELECT/SHOW/...）的结果")
        result = self.query(project, connection, sql, caller)
        return export_result(result["columns"], result["rows"], fmt)

    # ---------- SQL 片段库（查询台保存/加载）----------

    def _require_snippets(self) -> "SnippetStore":
        if self.snippets is None:
            from .snippets import SnippetError
            raise SnippetError("片段库未启用")
        return self.snippets

    def list_snippets(self) -> list[dict]:
        if self.snippets is None:
            return []
        return [s.to_dict() for s in self.snippets.list()]

    def save_snippet(
        self, title: str, sql: str, note: str = "", connection: str = "",
        snippet_id: int | None = None,
    ) -> dict:
        store = self._require_snippets()
        if snippet_id is not None:
            return store.update(snippet_id, title, sql, note, connection).to_dict()
        return store.create(title, sql, note, connection).to_dict()

    def delete_snippet(self, snippet_id: int) -> None:
        self._require_snippets().delete(snippet_id)

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
        report_dict = report.to_dict()
        plan = self._try_explain(project, connection, cfg, sql)
        if plan:
            report_dict["explain"] = plan
        change = self.approvals.create(
            project=project,
            connection=connection,
            environment=cfg.environment,
            engine=cfg.engine,
            sql=sql,
            fingerprint=fingerprint(sql, cfg.engine),
            reason=reason,
            risk_level=report.level,
            risk_report=report_dict,
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
            "risk": report_dict,
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

    def _try_explain(
        self, project: str, connection: str, cfg: ConnectionConfig, sql: str
    ) -> str | None:
        """对写语句取执行计划（不带 ANALYZE，不执行）供审批人参考。

        reader 会话可能因只读事务拒绝 EXPLAIN DML（PG 会），失败则退回 writer；
        全部失败返回 None，不阻断审批单生成。计划文本截断到 4000 字符。
        """
        for role in ("reader", "writer"):
            if role == "writer" and cfg.writer is None:
                break
            try:
                engine = self.pool.get(project, connection, cfg, role=role)
            except Exception:
                continue
            plan = engines.explain(engine, sql, cfg.engine)
            if plan:
                return plan[:4000]
        return None

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

    # ---------- Redis（同一套拒绝—重提审批流）----------

    def redis_execute(
        self,
        project: str,
        connection: str,
        command: str,
        caller: CallerInfo,
        reason: str = "",
        change_id: int | None = None,
    ) -> dict:
        """Redis 命令统一入口：读命令直通；写命令走审批（与 SQL 相同的流程）。"""
        cfg = self.config.get_connection(project, connection)
        if cfg.engine != "redis":
            raise QueryRejected(f"连接 {project}/{connection} 引擎为 {cfg.engine}，请用 query/execute")
        if self.approvals is None:
            raise QueryRejected("审批子系统未启用，无法执行 Redis 写命令")

        verdict = classify_command(command)
        rec = self._base_record(project, connection, cfg, "redis_command", command, caller)
        rec.fingerprint = command_fingerprint(command)

        if verdict.readonly:
            try:
                client = self.redis_pool.get(project, connection, cfg)
                result = redis_engine.run_command(client, parse_command(command),
                                                  max_cell_chars=cfg.policy.max_cell_chars)
            except Exception as e:
                rec.status = "error"
                rec.detail = f"{type(e).__name__}: {e}"
                self.store.record(rec)
                raise
            rec.status = "ok"
            rec.duration_ms = result.duration_ms
            self.store.record(rec)
            return {"status": "executed", "readonly": True, "value": result.value,
                    "duration_ms": result.duration_ms}

        if change_id is not None:
            return self._redis_execute_approved(project, connection, cfg, command, change_id, caller)

        # 生成审批单并拒绝
        report = {
            "level": verdict.level,
            "statement_kind": f"Redis:{verdict.command}",
            "tables": [],
            "reasons": [verdict.reason],
            "warnings": [],
        }
        change = self.approvals.create(
            project=project, connection=connection, environment=cfg.environment,
            engine="redis", sql=command, fingerprint=command_fingerprint(command),
            reason=reason, risk_level=verdict.level, risk_report=report,
            agent=caller.agent, session_id=caller.session_id,
        )
        rec.status = "rejected"
        rec.detail = f"需人工授权，已生成审批单 #{change.id}（风险 {verdict.level}）"
        self.store.record(rec)
        return {
            "status": "approval_required",
            "change_id": change.id,
            "risk": report,
            "message": (
                f"Redis 写命令需人工授权（风险 {verdict.level}）。已生成审批单 #{change.id}，"
                f"请通知用户审批；批准后带 change_id={change.id} 重提相同命令。30 分钟内有效。"
            ),
        }

    def _redis_execute_approved(
        self, project: str, connection: str, cfg: ConnectionConfig,
        command: str, change_id: int, caller: CallerInfo,
    ) -> dict:
        rec = self._base_record(project, connection, cfg, "redis_command", command, caller)
        rec.fingerprint = command_fingerprint(command)
        try:
            change = self.approvals.consume(
                change_id, command_fingerprint(command), (project, connection)
            )
        except ApprovalError as e:
            rec.status = "rejected"
            rec.detail = str(e)
            self.store.record(rec)
            return {"status": "rejected", "change_id": change_id, "reason": str(e)}

        try:
            # 执行审批单存储的命令；配置了 writer（Redis ACL）则用 writer
            role = "writer" if cfg.writer is not None else "reader"
            client = self.redis_pool.get(project, connection, cfg, role=role)
            result = redis_engine.run_command(client, parse_command(change.sql),
                                              max_cell_chars=cfg.policy.max_cell_chars)
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        rec.detail = f"审批单 #{change_id} 已核销（审批人 {change.decided_by}）"
        rec.duration_ms = result.duration_ms
        self.store.record(rec)
        return {"status": "executed", "change_id": change_id, "value": result.value,
                "duration_ms": result.duration_ms}

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
            result = engines.sample_rows(engine, table, limit,
                                         max_cell_chars=cfg.policy.max_cell_chars)
            rows, masked = apply_mask(result.columns, result.rows, cfg.policy)
            out = {
                "columns": result.columns,
                "rows": rows,
                "row_count": result.row_count,
                "duration_ms": result.duration_ms,
            }
            if masked:
                out["masked_columns"] = masked
            return out

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

    # ---------- 连接管理（管理后台，需已配置 config_path）----------

    def _require_config_path(self) -> str:
        if not self.config_path:
            raise QueryRejected("未设置配置文件路径，无法在线管理连接")
        return self.config_path

    def upsert_connection(self, project: str, connection: str, caller: CallerInfo, **fields) -> None:
        from .connections import ConnectionManager

        mgr = ConnectionManager(self.config, self._require_config_path())
        mgr.upsert(project, connection, **fields)
        self._after_connection_change(project, connection, caller, "upsert_connection",
                                      f"引擎 {fields.get('engine')}")

    def probe_connection_fields(self, fields: dict, existing_password: str | None = None):
        """用表单值临时探测连通性与账号权限（测试按钮）。不保存、不入池。"""
        from .config import ConnectionConfig, Policy
        from .probe import probe_connection

        password = fields.get("password") or None
        eff_pw = f"plain://{password}" if password else existing_password
        if eff_pw is None and fields.get("engine") not in ("sqlite",):
            from .probe import ProbeResult
            return ProbeResult(ok=False, message="请填写密码后再测试")
        cfg = ConnectionConfig(
            engine=fields["engine"], environment=fields.get("environment", "dev"),
            host=fields.get("host") or None, port=fields.get("port"),
            database=fields.get("database") or None, user=fields.get("user") or None,
            password=eff_pw or "plain://", jump_hosts=fields.get("jump_hosts", []),
            ssh_options=fields.get("ssh_options", []),
            policy=Policy(max_rows=fields.get("max_rows", 500)),
        )
        return probe_connection(cfg, None)

    def probe_ssh_fields(self, fields: dict):
        """用表单值只测 SSH 跳板链是否可建隧道。"""
        from .config import ConnectionConfig
        from .probe import probe_ssh

        # SSH 测试只用 host/port/跳板，user/password 填占位满足校验
        cfg = ConnectionConfig(
            engine=fields["engine"], environment=fields.get("environment", "dev"),
            host=fields.get("host") or "127.0.0.1", port=fields.get("port"),
            database=fields.get("database") or None, user="_probe",
            password="plain://_", jump_hosts=fields.get("jump_hosts", []),
            ssh_options=fields.get("ssh_options", []),
        )
        return probe_ssh(cfg)

    def delete_connection(self, project: str, connection: str, caller: CallerInfo) -> None:
        from .connections import ConnectionManager

        mgr = ConnectionManager(self.config, self._require_config_path())
        mgr.delete(project, connection)
        self._after_connection_change(project, connection, caller, "delete_connection", "已删除")

    def _after_connection_change(
        self, project: str, connection: str, caller: CallerInfo, tool: str, detail: str
    ) -> None:
        # 回收旧引擎/隧道，下次访问用新配置重建
        self.pool.dispose_connection(project, connection)
        self.redis_pool.dispose_connection(project, connection)
        rec = AuditRecord(project=project, connection=connection, tool=tool, status="ok",
                          agent=caller.agent, session_id=caller.session_id, detail=detail)
        self.store.record(rec)

    # ---------- 后台维护（serve 时启动）----------

    def start_housekeeping(
        self,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        interval_s: int = HOUSEKEEPING_INTERVAL_S,
    ) -> None:
        """周期任务：空闲引擎/隧道回收 + 审计与终态审批单按保留期清理。"""
        if self._housekeeping_stop is not None:
            return
        stop = threading.Event()
        self._housekeeping_stop = stop

        def _loop() -> None:
            while not stop.wait(interval_s):
                self.housekeep_once(retention_days)

        threading.Thread(target=_loop, name="dbm-housekeeping", daemon=True).start()

    def housekeep_once(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
        """执行一轮维护，返回统计（供测试与日志）。单项失败不影响其他项。"""
        stats = {"engines_reaped": 0, "redis_reaped": 0, "audit_purged": 0, "changes_purged": 0}
        for key, fn in (
            ("engines_reaped", self.pool.reap_idle),
            ("redis_reaped", self.redis_pool.reap_idle),
            ("audit_purged", lambda: self.store.purge_old(retention_days)),
            ("changes_purged",
             (lambda: self.approvals.purge_old(retention_days)) if self.approvals else (lambda: 0)),
        ):
            try:
                stats[key] = fn()
            except Exception:
                logger.exception("housekeeping %s 失败", key)
        if any(stats.values()):
            logger.info("housekeeping: %s", stats)
        return stats

    def close(self) -> None:
        if self._housekeeping_stop is not None:
            self._housekeeping_stop.set()
            self._housekeeping_stop = None
        self.pool.dispose()
        self.redis_pool.dispose()
        self.store.close()
        if self.approvals is not None:
            self.approvals.close()
        if self.metadata is not None:
            self.metadata.close()
        if self.snippets is not None:
            self.snippets.close()
