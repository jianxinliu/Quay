"""核心服务层：与 MCP 传输解耦，便于单元测试。

所有会触达数据库的操作都必须落审计记录（成功 / 拒绝 / 出错），
拒绝路径同样入库——这正是要给人看的部分。
"""

from __future__ import annotations

import hmac
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .approvals import ApprovalError, ApprovalStore
from .audit.classify import classify, fingerprint
from .audit.log import AuditRecord, AuditStore
from .audit.redis_rules import classify_command, command_fingerprint, parse_command
from .audit.risk import assess
from .config import AppConfig, ConnectionConfig
from .health import ConnectionUnavailable, HealthMonitor, is_connection_error
from .masking import apply_mask
from .metadata import MetadataCache
from .notify import NoopNotifier, Notifier
from . import engines, redis_engine

if TYPE_CHECKING:
    from .snippets import SnippetStore

logger = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL_S = 60
DEFAULT_RETENTION_DAYS = 30
ADMIN_PAGE_SIZE = 100  # 查询台每页行数（上限受连接 max_rows 约束）
DEFAULT_AGENT_MAX_RESULT_CHARS = 40000  # agent 结果字符预算的最终兜底（settings 未启用时）


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


def _rows_to_text(columns: list[str], rows: list[list], max_rows: int = 5) -> str:
    """把样本行拼成紧凑 TSV（喂 AI 用）：首行列名，其余数据行，制表符分隔，None→\\N。"""
    def cell(v: object) -> str:
        if v is None:
            return "\\N"
        return str(v).replace("\t", " ").replace("\n", " ")
    lines = ["\t".join(columns)]
    for row in rows[:max_rows]:
        lines.append("\t".join(cell(v) for v in row))
    return "\n".join(lines)


def _ai_api_cfg(s: dict) -> dict:
    """从设置里取 provider=api 的连接配置（base/format/key_env）。"""
    return {"base": str(s.get("ai_api_base") or ""),
            "format": str(s.get("ai_api_format") or "anthropic"),
            "key_env": str(s.get("ai_api_key_env") or "")}


def _layout_graph(graph: dict) -> None:
    """给 AI 生成的节点按拓扑层级赋 x/y（AI 不给坐标），使画布排版可读。原地修改。"""
    nodes = graph.get("nodes") or []
    ids = {n.get("id") for n in nodes}
    preds: dict = {n.get("id"): [] for n in nodes}
    for e in graph.get("edges") or []:
        if e.get("from") in ids and e.get("to") in ids:
            preds[e["to"]].append(e["from"])
    level: dict = {}

    def _lvl(nid: str, seen: frozenset) -> int:
        if nid in level:
            return level[nid]
        ps = [p for p in preds.get(nid, []) if p not in seen]
        level[nid] = 0 if not ps else 1 + max(_lvl(p, seen | {nid}) for p in ps)
        return level[nid]

    for n in nodes:
        _lvl(n.get("id"), frozenset())
    per_level: dict = {}
    for n in nodes:
        lv = level.get(n.get("id"), 0)
        row = per_level.get(lv, 0)
        per_level[lv] = row + 1
        n["x"] = 30 + lv * 200
        n["y"] = 30 + row * 100


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
        notifier: Notifier | None = None,
    ):
        self.config = config
        self.store = store
        self.pool = engines.EnginePool()
        self.redis_pool = redis_engine.RedisPool()
        # 让引擎池能解析每跳的 SSH 证书引用（同一 dict，连接管理原地增删即时可见）
        self.pool.identities = self.config.ssh_identities
        self.redis_pool.identities = self.config.ssh_identities
        self.approvals = approvals
        # 通知抽象：默认 Noop（safe default，测试与库使用都不会真发通知）；
        # serve 入口显式注入 build_default_notifier() 得到 macOS 系统通知。
        self.notifier = notifier if notifier is not None else NoopNotifier()
        # 健康监控：exhausted 时发通知（同一连接短时间去重）
        self.health = HealthMonitor(
            probe=self._health_probe,
            on_exhausted=self._on_connection_exhausted,
        )
        self.metadata = metadata
        self.config_path = config_path
        self.snippets = snippets
        self.settings = None   # SettingsStore（serve 时注入）
        self.analysis = None   # AnalysisStore（serve 时注入；未启用则分析功能不可用）
        self.workflows = None  # WorkflowStore（serve 时注入）
        self._housekeeping_stop: threading.Event | None = None

    # ---------- 元信息 ----------

    def list_projects(self) -> list[dict]:
        # 对 agent 隐藏 Redis 连接（Redis 只供人通过 /admin/redis 操作）；
        # 只剩 Redis 连接的项目也不出现
        out = []
        for name, proj in sorted(self.config.projects.items()):
            conns = sorted(n for n, c in proj.connections.items() if c.engine != "redis")
            if conns:
                out.append({"project": name, "connections": conns})
        return out

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
                   if c.engine in ("mysql", "postgres", "clickhouse") and not c.database else {}),
                # 有意不返回 user/password/writer 等账号信息
            }
            # Redis 有意不返回：agent 碰不到 Redis
            for name, c in sorted(proj.connections.items()) if c.engine != "redis"
        ]

    # ---------- 查询 ----------

    def _read(
        self, project: str, connection: str, cfg: ConnectionConfig, sql: str,
        caller: CallerInfo, max_rows: int, schema: str | None = None,
        on_start=None,  # noqa: ANN001
        max_cell_chars: int | None = None, mask: bool = True,
    ) -> dict:
        """执行一条已判定只读的 SQL：跑 reader、落审计、脱敏，返回结果 dict。

        max_rows 由调用方决定（query 用连接策略；查询台分页用 page_size+1 以探测下一页），
        与 truncated 检测解耦，便于复用。schema 为查询台的执行 schema 上下文。
        max_cell_chars 缺省用连接策略（查询台可传系统设置的 sql_max_cell_chars 覆盖）。
        mask=True 对敏感列脱敏（agent 路径的红线：密码不出现在工具返回值中）；已认证的后台
        查询台/导出传 mask=False——人就是要看真实数据，脱敏反而碍事。
        """
        rec = self._base_record(project, connection, cfg, "query", sql, caller)
        if schema:
            rec.detail = f"schema={schema}"

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, schema=schema)
            return engines.run_query(
                engine, sql, max_rows,
                max_cell_chars=max_cell_chars or cfg.policy.max_cell_chars,
                on_start=on_start,
            )

        try:
            result = self._run_touching_db(project, connection, _do)
        except ConnectionUnavailable as e:
            rec.status = "error"
            rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
            self.store.record(rec)
            raise
        except QueryRejected:
            raise
        except Exception as e:
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
        rows, masked = apply_mask(result.columns, result.rows, cfg.policy) if mask else (result.rows, [])
        out = {
            "columns": result.columns,
            "rows": rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
            "column_types": result.column_types,
        }
        if masked:
            out["masked_columns"] = masked
        return out

    def query(self, project: str, connection: str, sql: str, caller: CallerInfo) -> dict:
        cfg = self.config.get_connection(project, connection)
        verdict = classify(sql, cfg.engine)
        if not verdict.readonly:
            rec = self._base_record(project, connection, cfg, "query", sql, caller)
            rec.status = "rejected"
            rec.detail = verdict.reason
            self.store.record(rec)
            raise QueryRejected(
                f"已拒绝：{verdict.reason}。query 工具仅允许只读语句；"
                "数据变更操作需人工授权的 execute 流程（M3 上线后提供）。"
            )

        # 兜底：缺 LIMIT 的 SELECT 注入 LIMIT max_rows+1，防大表全量缓冲把 DB/进程拖挂
        run_sql, _, _ = engines.paginate_sql(sql, cfg.engine, cfg.policy.max_rows + 1, 0)
        out = self._read(project, connection, cfg, run_sql, caller, cfg.policy.max_rows)
        out["statement_kind"] = verdict.statement_kind
        if out["truncated"]:
            out["hint"] = (
                f"结果已截断到 {cfg.policy.max_rows} 行（连接策略 max_rows）。"
                "如需后续数据，请在 SQL 中用 LIMIT/OFFSET（或 WHERE 条件缩小范围）自行分页。"
            )
        return out

    # ---------- 管理后台查询台（人已认证，写操作二次确认后直接执行）----------

    def admin_run_sql(
        self, project: str, connection: str, sql: str, caller: CallerInfo, confirm: bool = False,
        page: int = 0, page_size: int | None = None, schema: str | None = None,
        on_start=None,  # noqa: ANN001
        confirm_text: str | None = None, expect_fingerprint: str | None = None,
    ) -> dict:
        """管理后台查询台专用入口。**只挂在已认证的后台路由上，agent 无法触达。**

        - 只读语句：跑 reader 出结果，自动分页（缺 LIMIT 的 SELECT 注入 LIMIT/OFFSET
          兜底，防大表拉挂 DB）；用户自带 LIMIT 则尊重不改。
        - 写语句 + confirm=False：评估风险并返回风险报告（含 fingerprint / prod / expect_text），
          **不执行**。
        - 写语句 + confirm=True：经人工二次确认，直接用 writer 账号执行并落审计。
          这是后台专属旁路（不进审批单）；红线「拒绝—重提」只约束 agent 的 execute。
          二次闸门 H1 指纹绑定：确认时若带回 expect_fingerprint，须与当前 SQL 的指纹一致，
          否则拒绝——防「看 A 批 B」（确认前后 SQL 被改）。
          注：prod 写操作只需人工二次确认（不再要求输入连接名），便利优先；红框/红条视觉警示仍在。
        - schema：执行 schema 上下文（右上角选择），未限定表名的 SQL 在该库下执行。
        """
        cfg = self.config.get_connection(project, connection)
        verdict = classify(sql, cfg.engine)
        # 语法错误：明确报语法错，不走"确认写操作"流程（默认拒绝仍成立——不执行）
        if verdict.statement_kind == "ParseError":
            rec = self._base_record(project, connection, cfg, "query", sql, caller)
            rec.status = "rejected"
            rec.detail = verdict.reason
            self.store.record(rec)
            return {"kind": "error", "error": f"SQL 语法错误：{verdict.reason.replace('SQL 解析失败: ', '')}"}
        if verdict.readonly:
            page = max(page, 0)
            default_size = int(self._setting("sql_page_size") or ADMIN_PAGE_SIZE)
            eff_cell = int(self._setting("sql_max_cell_chars") or cfg.policy.max_cell_chars)
            # 分页每页行数仍受连接策略上限（连接可显式限行）；单元格上限用系统设置
            size = min(page_size or default_size, cfg.policy.max_rows)
            paged_sql, paginated, ordered = engines.paginate_sql(
                sql, cfg.engine, size + 1, page * size)
            if paginated:
                # 取 size+1 行探测是否有下一页；不受连接 max_rows 二次截断影响
                out = self._read(project, connection, cfg, paged_sql, caller, size + 1,
                                 schema=schema, on_start=on_start, max_cell_chars=eff_cell, mask=False)
                rows = out["rows"]
                out["has_next"] = len(rows) > size
                out["rows"] = rows[:size]
                out["row_count"] = len(out["rows"])
                out.update(paginated=True, page=page, page_size=size, ordered=ordered)
                out.pop("truncated", None)
                return {"kind": "read", **out}
            # 自带 LIMIT / 非 SELECT：不分页，受系统设置的结果行上限 sql_max_rows 兜底
            eff_max_rows = int(self._setting("sql_max_rows") or cfg.policy.max_rows)
            out = self._read(project, connection, cfg, sql, caller, eff_max_rows,
                             schema=schema, on_start=on_start, max_cell_chars=eff_cell, mask=False)
            out["paginated"] = False
            return {"kind": "read", **out}

        is_prod = (cfg.environment or "").lower() == "prod"
        fp = fingerprint(sql, cfg.engine)
        if not confirm:
            report = assess(sql, cfg.engine, self._meta_provider(project, connection, cfg))
            report_dict = report.to_dict()
            plan = self._try_explain(project, connection, cfg, sql, schema=schema)
            if plan:
                report_dict["explain"] = plan
            return {"kind": "confirm", "risk": report_dict,
                    "statement_kind": verdict.statement_kind,
                    "fingerprint": fp, "prod": is_prod,
                    "expect_text": connection if is_prod else None}

        # H1：确认必须绑定到刚才被评估/展示的那条 SQL（指纹一致），否则拒绝执行
        if expect_fingerprint is not None and not hmac.compare_digest(expect_fingerprint, fp):
            rec = self._base_record(project, connection, cfg, "admin_execute", sql, caller)
            rec.status = "rejected"
            rec.detail = "确认指纹与提交 SQL 不一致，已拒绝执行（H1）"
            self.store.record(rec)
            raise QueryRejected("SQL 在确认前后发生了变化（指纹不一致），已拒绝执行，请重新确认。")

        rec = self._base_record(project, connection, cfg, "admin_execute", sql, caller)
        if schema:
            rec.detail = f"schema={schema}"

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, role="writer", schema=schema)
            return engines.run_write(engine, sql, on_start=on_start)

        try:
            result = self._run_touching_db(project, connection, _do)
        except ConnectionUnavailable as e:
            rec.status = "error"
            rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
            self.store.record(rec)
            raise
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        rec.detail = "后台查询台直接执行（已二次确认）" + (f" schema={schema}" if schema else "")
        rec.row_count = result.row_count
        rec.duration_ms = result.duration_ms
        self.store.record(rec)
        return {"kind": "write", "affected_rows": result.row_count,
                "duration_ms": result.duration_ms}

    MAX_IMPORT_ROWS = 50_000

    def admin_import_rows(
        self, project: str, connection: str, table: str, columns: list[str],
        rows: list[list], caller: CallerInfo, schema: str | None = None,
    ) -> dict:
        """后台数据导入（CSV/粘贴）：参数化批量 INSERT，writer 单事务执行并审计。

        安全：列名必须存在于目标表结构（防拼接注入）；值全部走绑定参数；
        行数上限 MAX_IMPORT_ROWS。仅挂在已认证的后台路由上，agent 无法触达。
        """
        cfg = self.config.get_connection(project, connection)
        if not rows:
            raise ValueError("没有可导入的行")
        if len(rows) > self.MAX_IMPORT_ROWS:
            raise ValueError(f"单次导入上限 {self.MAX_IMPORT_ROWS} 行，实际 {len(rows)} 行")
        if not columns:
            raise ValueError("缺少列映射")
        info = self.describe_table(project, connection, table, caller, schema=schema)
        valid = {c["name"] for c in info["columns"]}
        bad = [c for c in columns if c not in valid]
        if bad:
            raise ValueError(f"列不存在于表 {table}: {', '.join(bad)}（表列: {', '.join(sorted(valid))}）")
        rec = self._base_record(project, connection, cfg, "admin_import",
                                f"IMPORT INTO {table} ({', '.join(columns)}) — {len(rows)} 行",
                                caller)

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, role="writer", schema=schema)
            return engines.insert_rows(engine, table, columns, rows, schema=schema)

        try:
            result = self._run_touching_db(project, connection, _do)
        except ConnectionUnavailable as e:
            rec.status = "error"
            rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
            self.store.record(rec)
            raise
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        rec.row_count = result.row_count
        rec.duration_ms = result.duration_ms
        rec.detail = "后台导入（已确认，单事务）" + (f" schema={schema}" if schema else "")
        self.store.record(rec)
        return {"inserted": result.row_count, "duration_ms": result.duration_ms}

    def admin_export(
        self, project: str, connection: str, sql: str, fmt: str, caller: CallerInfo,
        schema: str | None = None,
    ) -> tuple[bytes, str, str]:
        """导出只读查询结果为文件，返回 (字节, media_type, 扩展名)。仅限只读语句。"""
        from .export import export_result

        cfg = self.config.get_connection(project, connection)
        if not classify(sql, cfg.engine).readonly:
            raise QueryRejected("导出仅支持只读查询（SELECT/SHOW/...）的结果")
        run_sql, _, _ = engines.paginate_sql(sql, cfg.engine, cfg.policy.max_rows + 1, 0)
        result = self._read(project, connection, cfg, run_sql, caller,
                            cfg.policy.max_rows, schema=schema, mask=False)
        return export_result(result["columns"], result["rows"], fmt)

    def admin_query_history(self, project: str, connection: str, limit: int = 30) -> list[dict]:
        """查询台历史面板：从审计取该连接最近执行过的 SQL，按文本去重保留最新。"""
        rows = self.store.recent(limit=300, filters={"project": project, "connection": connection})
        seen: set[str] = set()
        out: list[dict] = []
        for r in rows:
            sql = (r["sql"] or "").strip()
            if not sql or r["tool"] not in ("query", "execute", "admin_execute"):
                continue
            key = " ".join(sql.split()).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"sql": sql, "ts": r["ts"], "status": r["status"], "tool": r["tool"]})
            if len(out) >= limit:
                break
        return out

    _EXPLAIN_PREFIX = {
        "mysql": "EXPLAIN FORMAT=JSON ",
        "postgres": "EXPLAIN (FORMAT JSON) ",
        "sqlite": "EXPLAIN QUERY PLAN ",
    }

    def admin_explain(
        self, project: str, connection: str, sql: str, caller: CallerInfo,
        schema: str | None = None,
    ) -> dict:
        """查询台 EXPLAIN：按引擎方言取执行计划（MySQL/PG 为 JSON，SQLite 为行）。

        纯 EXPLAIN 不执行语句（不带 ANALYZE），对写语句也安全。多语句拒绝。
        """
        import re as _re
        cfg = self.config.get_connection(project, connection)
        stmt = _re.sub(r"^\s*explain\s+", "", sql, flags=_re.IGNORECASE).strip().rstrip(";")
        if not stmt:
            raise QueryRejected("请先在编辑器写一条 SQL")
        verdict = classify(stmt, cfg.engine)
        if "多语句" in verdict.reason:
            raise QueryRejected("EXPLAIN 只支持单条语句")
        prefix = self._EXPLAIN_PREFIX.get(cfg.engine)
        if prefix is None:
            raise QueryRejected(f"引擎 {cfg.engine} 不支持 EXPLAIN")
        fmt = "rows" if cfg.engine == "sqlite" else "json"
        # 写语句（DELETE/UPDATE/INSERT/DDL）的 EXPLAIN 需要对应表的写权限：reader（只读账号）
        # 会被 DB 以权限不足拒绝（MySQL 1142）。EXPLAIN 不带 ANALYZE 不真正执行，改用 writer
        # 账号取计划是安全的；无独立 writer 时退回 reader（sqlite 等无账号概念场景）。
        role = "writer" if (not verdict.readonly and cfg.writer is not None) else "reader"

        def _run() -> dict:
            engine = self.pool.get(project, connection, cfg, role=role, schema=schema)
            # JSON 计划可能很长，放开单元格截断
            res = engines.run_query(engine, prefix + stmt, max_rows=500, max_cell_chars=1_000_000)
            return {"format": fmt, "columns": res.columns, "rows": res.rows}

        return self._audited(project, connection, cfg, "explain", stmt, caller, _run)

    # ---------- 分析工作台（DuckDB 沙箱，设计见 ANALYSIS.md）----------
    # 边界：工作区内任意 SQL 自由执行（本地草稿纸，不需审批）；
    # 从源库取数走 _read（reader 只读 + 审计 + 行数上限），生产红线不动。

    def _require_analysis(self):
        if self.analysis is None:
            raise QueryRejected("分析工作台未启用（需 serve 模式运行）")
        return self.analysis

    def _analysis_record(self, workspace: str, tool: str, sql: str, caller: CallerInfo) -> AuditRecord:
        return AuditRecord(project="analysis", connection=workspace, tool=tool, status="",
                           agent=caller.agent, session_id=caller.session_id,
                           environment="local", engine="duckdb", sql=sql)

    def analysis_overview(self) -> list[dict]:
        """工作区列表（含数据集摘要）。"""
        store = self._require_analysis()
        out = []
        for ws in store.list_workspaces():
            try:
                ws["datasets"] = store.list_datasets(ws["workspace"])
            except Exception:
                ws["datasets"] = []
            out.append(ws)
        return out

    def analysis_import(
        self, workspace: str, dataset: str, project: str, connection: str, source_sql: str,
        caller: CallerInfo, limit: int | None = None, schema: str | None = None,
    ) -> dict:
        """从某连接把查询结果快照进工作区（source_sql 也可为 `SELECT * FROM 表`）。

        只读校验 + 注入 LIMIT 上限 + reader 拉数（全程审计），随后落成 DuckDB 表。
        """
        from .analysis import DEFAULT_SNAPSHOT_ROWS, MAX_SNAPSHOT_ROWS
        store = self._require_analysis()
        cfg = self.config.get_connection(project, connection)
        if not classify(source_sql, cfg.engine).readonly:
            raise QueryRejected("快照导入仅支持只读查询（SELECT/SHOW/...）")
        n = min(limit or DEFAULT_SNAPSHOT_ROWS, MAX_SNAPSHOT_ROWS)
        run_sql, _, _ = engines.paginate_sql(source_sql, cfg.engine, n, 0)
        result = self._read(project, connection, cfg, run_sql, caller, n, schema=schema)
        spec = {"kind": "connection", "project": project, "connection": connection,
                "sql": source_sql, "limit": n, "schema": schema}
        imported = store.import_rows(workspace, dataset, result["columns"], result["rows"],
                                     spec=spec)
        rec = self._analysis_record(workspace, "analysis_import", source_sql, caller)
        rec.status = "ok"
        rec.detail = f"{project}/{connection} → {workspace}.{dataset}"
        rec.row_count = imported
        self.store.record(rec)
        return {"workspace": workspace, "dataset": dataset, "rows": imported,
                "truncated_to_limit": imported >= n}

    def analysis_import_file(
        self, workspace: str, dataset: str, path: str, caller: CallerInfo
    ) -> dict:
        store = self._require_analysis()
        rec = self._analysis_record(workspace, "analysis_import_file", path, caller)
        try:
            n = store.import_file(workspace, dataset, path)
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        rec.row_count = n
        self.store.record(rec)
        return {"workspace": workspace, "dataset": dataset, "rows": n}

    def analysis_sql(
        self, workspace: str, sql: str, caller: CallerInfo, max_rows: int | None = None
    ) -> dict:
        """在工作区执行任意 SQL（沙箱，自由写）。审计留痕。"""
        from .analysis import MAX_RESULT_ROWS
        store = self._require_analysis()
        rec = self._analysis_record(workspace, "analysis_sql", sql, caller)
        try:
            out = store.run_sql(workspace, sql, max_rows or MAX_RESULT_ROWS)
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        rec.row_count = out["row_count"]
        self.store.record(rec)
        return out

    # ---------- 分析 workflow（保存取数配方 + 脚本，一键重跑）----------

    def _require_workflows(self):
        if self.workflows is None:
            from .workflows import WorkflowError
            raise WorkflowError("workflow 存储未启用（需 serve 模式运行）")
        return self.workflows

    def workflow_save(self, name: str, workspace: str, script: str, caller: CallerInfo,
                      chart: dict | None = None, graph: dict | None = None,
                      allow_replace_graph: bool = True) -> dict:
        """保存 workflow：脚本/DAG + 取数配方 + 图表配置。

        DAG workflow 的取数配方在图的 source 节点里（编译时校验图合法）；
        纯脚本 workflow 从工作区 provenance 自动收集。
        allow_replace_graph=False（agent 侧）：同名 workflow 若是人画的 DAG，
        拒绝覆盖——agent 只允许创建/迭代脚本式 workflow。
        """
        from .workflows import compile_graph
        store = self._require_workflows()
        if not allow_replace_graph:
            existing = next((w for w in self.workflow_list() if w["name"] == name.strip()), None)
            if existing and existing.get("graph"):
                raise ValueError(
                    f"workflow {name!r} 是管理后台画布创建的 DAG，不允许覆盖；"
                    "请换一个名字，或让用户在后台修改")
        if graph:
            sources = compile_graph(graph)["sources"]  # 校验 + 配方以图为准
        else:
            sources = self._require_analysis().get_provenance(workspace)
        wf = store.save(name, workspace, script, sources, chart, graph)
        rec = self._analysis_record(workspace, "workflow_save", (script or "graph")[:500], caller)
        rec.status = "ok"
        rec.detail = f"workflow={name} sources={len(sources)} graph={bool(graph)}"
        self.store.record(rec)
        return wf.to_dict()

    def workflow_list(self) -> list[dict]:
        if self.workflows is None:
            return []
        return [w.to_dict() for w in self.workflows.list()]

    def workflow_delete(self, name: str) -> None:
        self._require_workflows().delete(name)

    def workflow_run(self, name: str, caller: CallerInfo) -> dict:
        """一键重跑：按 sources 重拉数据 → 逐条执行（脚本语句或 DAG 编译结果）→ 输出。

        任一步失败即停，标注在哪一步。全程审计（取数走 _read，脚本走 analysis_sql）。
        """
        from .workflows import compile_graph, split_statements
        wf = self._require_workflows().get(name)
        if wf.graph:
            plan = compile_graph(wf.graph)
            out = self._run_plan(wf.workspace, plan["sources"], plan["steps"], caller)
        else:
            stmts = [{"node": None, "name": f"步骤 {i}", "sql": s}
                     for i, s in enumerate(split_statements(wf.script), 1)]
            out = self._run_plan(wf.workspace, wf.sources, stmts, caller)
        return {"workflow": name, **out}

    def workflow_run_graph(self, workspace: str, graph: dict, caller: CallerInfo) -> dict:
        """直接运行画布上的 DAG（未保存也能跑）。编译失败作为第一步错误返回。"""
        from .workflows import WorkflowError, compile_graph
        try:
            plan = compile_graph(graph)
        except WorkflowError as e:
            return {"workflow": None, "ok": False, "output": None,
                    "steps": [{"step": "编译流程", "ok": False, "error": str(e)}]}
        return {"workflow": None, **self._run_plan(workspace, plan["sources"], plan["steps"], caller)}

    def _run_plan(self, workspace: str, sources: list[dict], steps: list[dict],
                  caller: CallerInfo) -> dict:
        """执行计划：重拉 sources → 顺序执行 steps（带 node id 供画布标注状态）。"""
        done: list[dict] = []
        for src in sources:
            label = f"导入 {src.get('dataset')}"
            node = src.get("node")
            try:
                if src.get("kind") == "file":
                    out = self.analysis_import_file(workspace, src["dataset"], src["path"], caller)
                else:
                    out = self.analysis_import(workspace, src["dataset"], src["project"],
                                               src["connection"], src["sql"], caller,
                                               limit=src.get("limit"), schema=src.get("schema"))
                done.append({"step": label, "node": node, "ok": True, "rows": out["rows"]})
            except Exception as e:  # noqa: BLE001
                done.append({"step": label, "node": node, "ok": False, "error": str(e)})
                return {"steps": done, "output": None, "ok": False}
        output = None
        for st in steps:
            label = f"{st['name']}: {st['sql'][:60]}"
            try:
                res = self.analysis_sql(workspace, st["sql"], caller)
                done.append({"step": label, "node": st.get("node"), "ok": True,
                             "rows": res["row_count"]})
                if res["columns"]:
                    output = res
            except Exception as e:  # noqa: BLE001
                done.append({"step": label, "node": st.get("node"), "ok": False, "error": str(e)})
                return {"steps": done, "output": output, "ok": False}
        return {"steps": done, "output": output, "ok": True}

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

        # 带 change_id：一律走审批单核销（指纹校验 + 原子核销），不看重新分类结果——
        # 否则可构造「首提判写→生成审批单、重提判读→走 query() 绕开 consume 的指纹与核销」（H5）。
        if change_id is not None:
            return self._execute_approved(project, connection, cfg, sql, change_id, caller)

        verdict = classify(sql, cfg.engine)
        if verdict.readonly:
            return {"status": "executed", "readonly": True, **self.query(project, connection, sql, caller)}
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
        # 需要人为介入 → 主动发通知（安静即正常：不通知的话可能长时间没人看到）
        try:
            sql_preview = " ".join(sql.split())[:120]
            self.notifier.send(
                title=f"新审批单 #{change.id} · {project}/{connection}",
                body=f"风险 {report.level} · agent={caller.agent or 'unknown'}\nSQL: {sql_preview}",
                meta={"kind": "approval_created", "change_id": change.id,
                      "project": project, "connection": connection,
                      "risk_level": report.level},
            )
        except Exception:  # noqa: BLE001
            logger.exception("notify approval_created failed")
        return {
            "status": "approval_required",
            "change_id": change.id,
            "risk": report_dict,
            "message": (
                f"该操作被评估为需人工授权（风险等级 {report.level}）。"
                f"已生成审批单 #{change.id}，请通知用户在管理后台审批；"
                f"批准后带上 change_id={change.id} 重新提交相同 SQL 即可执行。"
                f"审批单 60 分钟内有效。"
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

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, role="writer")
            return engines.run_write(engine, change.sql)

        try:
            result = self._run_touching_db(project, connection, _do)
        except ConnectionUnavailable as e:
            rec.status = "error"
            rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
            self.store.record(rec)
            raise
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
        self, project: str, connection: str, cfg: ConnectionConfig, sql: str,
        schema: str | None = None,
    ) -> str | None:
        """对写语句取执行计划（不带 ANALYZE，不执行）供审批人参考。

        reader 会话可能因只读事务拒绝 EXPLAIN DML（PG 会），失败则退回 writer；
        全部失败返回 None，不阻断审批单生成。计划文本截断到 4000 字符。
        """
        for role in ("reader", "writer"):
            if role == "writer" and cfg.writer is None:
                break
            try:
                engine = self.pool.get(project, connection, cfg, role=role, schema=schema)
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

    # ---------- 系统设置（后台界面偏好）----------

    def get_settings(self) -> dict:
        from .settings import DEFAULTS
        return self.settings.get_all() if self.settings is not None else dict(DEFAULTS)

    def save_settings(self, updates: dict) -> dict:
        if self.settings is None:
            raise QueryRejected("设置子系统未启用")
        return self.settings.save(updates)

    def _setting(self, key: str):
        return self.get_settings().get(key)

    def agent_result_budget(self, project: str, connection: str) -> int:
        """解析给 agent 的结果字符预算：连接级 Policy 优先，否则全局设置兜底。"""
        cfg = self.config.get_connection(project, connection)
        if cfg.policy.agent_max_result_chars:
            return int(cfg.policy.agent_max_result_chars)
        return int(self._setting("agent_max_result_chars") or DEFAULT_AGENT_MAX_RESULT_CHARS)

    # ---------- Redis 浏览 / 命令窗口（管理后台，对标 Medis）----------

    def _redis_cfg(self, project: str, connection: str) -> ConnectionConfig:
        cfg = self.config.get_connection(project, connection)
        if cfg.engine != "redis":
            raise QueryRejected(f"连接 {project}/{connection} 引擎为 {cfg.engine}，不是 Redis")
        return cfg

    def redis_databases(self, project: str, connection: str, caller: CallerInfo) -> list[dict]:
        """列出全部逻辑库（db0..N-1），有数据的带键数。对标 Medis 底部库切换器。"""
        cfg = self._redis_cfg(project, connection)
        min_dbs = int(self._setting("redis_min_dbs") or redis_engine.MIN_DBS_SHOWN)
        return self._audited(
            project, connection, cfg, "redis_keyspace", "", caller,
            lambda: redis_engine.keyspace_dbs(self.redis_pool.get(project, connection, cfg),
                                              min_dbs=min_dbs))

    def redis_keys(
        self, project: str, connection: str, caller: CallerInfo,
        db: int | None = None, pattern: str = "*", max_keys: int | None = None,
    ) -> dict:
        cfg = self._redis_cfg(project, connection)
        limit = max_keys if max_keys is not None else int(self._setting("redis_key_limit"))
        scan_count = int(self._setting("redis_scan_count") or 500)
        detail = f"db={db if db is not None else ''} match={pattern}"
        return self._audited(
            project, connection, cfg, "redis_scan", detail, caller,
            lambda: redis_engine.scan_keys(
                self.redis_pool.get(project, connection, cfg, db=db),
                pattern=pattern or "*", max_keys=limit, scan_count=scan_count))

    def redis_value(
        self, project: str, connection: str, key: str, caller: CallerInfo,
        db: int | None = None,
    ) -> dict:
        cfg = self._redis_cfg(project, connection)
        redis_engine.set_msgpack_decode(bool(self._setting("redis_msgpack_decode")))
        detail = f"db={db if db is not None else ''} key={key}"
        return self._audited(
            project, connection, cfg, "redis_read", detail, caller,
            lambda: redis_engine.read_value(
                self.redis_pool.get(project, connection, cfg, db=db), key,
                max_cell_chars=cfg.policy.max_cell_chars))

    def admin_redis_run(
        self, project: str, connection: str, command: str, caller: CallerInfo,
        confirm: bool = False, db: int | None = None, confirm_text: str | None = None,
    ) -> dict:
        """后台命令窗口专用入口（对标 admin_run_sql 的 Redis 版）。

        - 读命令：直通 reader 出结果。
        - 写命令 + confirm=False：返回风险报告，不执行。
        - 写命令 + confirm=True：writer（无 writer 则 reader）直接执行并审计（tool=admin_execute）。
          Redis 只供人通过后台操作（不暴露给 agent），故无 agent 侧审批流。
        - **生产环境写命令**：单次确认不够，须额外输入连接名（confirm_text）匹配才放行，
          防误清线上库（对齐 SQL 侧 prod 强管控）。
        """
        cfg = self._redis_cfg(project, connection)
        is_prod = (cfg.environment or "").lower() == "prod"
        verdict = classify_command(command)
        parts = parse_command(command)
        # 命令原文脱敏后再入审计（密码永不进审计记录）；执行仍用未脱敏的 parts
        safe_command = redis_engine.redact_command_text(command, parts)

        if verdict.readonly:
            rec = self._base_record(project, connection, cfg, "redis_command", safe_command, caller)
            rec.fingerprint = command_fingerprint(safe_command)
            if db is not None:
                rec.detail = f"db={db}"

            def _do_read():
                client = self.redis_pool.get(project, connection, cfg, db=db)
                return redis_engine.run_command(client, parts,
                                                max_cell_chars=cfg.policy.max_cell_chars)

            try:
                result = self._run_touching_db(project, connection, _do_read)
            except ConnectionUnavailable as e:
                rec.status = "error"
                rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
                self.store.record(rec)
                raise
            except Exception as e:
                rec.status = "error"
                rec.detail = f"{type(e).__name__}: {e}"
                self.store.record(rec)
                raise
            rec.status = "ok"
            rec.duration_ms = result.duration_ms
            self.store.record(rec)
            return {"kind": "read", "readonly": True, "command": verdict.command,
                    "value": result.value, "duration_ms": result.duration_ms}

        if not confirm:
            return {"kind": "confirm", "statement_kind": f"Redis:{verdict.command}",
                    "prod": is_prod, "expect_text": connection if is_prod else None,
                    "risk": {"level": verdict.level, "statement_kind": f"Redis:{verdict.command}",
                             "tables": [], "reasons": [verdict.reason], "warnings": []}}

        # 生产环境：确认之外还须输入连接名匹配，否则拒绝执行
        if is_prod and (confirm_text or "").strip() != connection:
            raise QueryRejected(
                f"生产环境写命令需输入连接名「{connection}」确认后才执行")

        rec = self._base_record(project, connection, cfg, "admin_execute", safe_command, caller)
        rec.fingerprint = command_fingerprint(safe_command)
        role = "writer" if cfg.writer is not None else "reader"

        def _do_write():
            client = self.redis_pool.get(project, connection, cfg, role=role, db=db)
            return redis_engine.run_command(client, parts,
                                            max_cell_chars=cfg.policy.max_cell_chars)

        try:
            result = self._run_touching_db(project, connection, _do_write)
        except ConnectionUnavailable as e:
            rec.status = "error"
            rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
            self.store.record(rec)
            raise
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        rec.detail = "后台命令窗口直接执行（已二次确认）" + (f" db={db}" if db is not None else "")
        rec.duration_ms = result.duration_ms
        self.store.record(rec)
        return {"kind": "write", "command": verdict.command,
                "value": result.value, "duration_ms": result.duration_ms}

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

    def list_databases(self, project: str, connection: str, caller: CallerInfo) -> list[str]:
        """列出连接可选的库/schema（MySQL 数据库 / PG schema）。sqlite 无此概念返回 []。"""
        cfg = self.config.get_connection(project, connection)
        if cfg.engine not in ("mysql", "postgres", "clickhouse"):
            return []
        engine = self.pool.get(project, connection, cfg)
        return self._audited(project, connection, cfg, "list_databases", "", caller,
                             lambda: engines.list_databases(engine))

    def list_tables(
        self, project: str, connection: str, caller: CallerInfo, schema: str | None = None
    ) -> list[str]:
        cfg = self.config.get_connection(project, connection)
        # 未绑定默认库时，先让用户选库（库→表→列 三级树）。MySQL/PG 不带 schema 反射会崩
        # （默认 schema 为 None）；ClickHouse 不会崩但会落到 default 库、看不到别的库 → 一并引导
        if schema is None and not cfg.database and cfg.engine in ("mysql", "postgres", "clickhouse"):
            raise ValueError("此连接未绑定默认库，请先选择一个库（schema）再列表")
        engine = self.pool.get(project, connection, cfg)
        return self._audited(project, connection, cfg, "list_tables", schema or "", caller,
                             lambda: engines.list_tables(engine, schema))

    def describe_table(
        self, project: str, connection: str, table: str, caller: CallerInfo,
        schema: str | None = None,
    ) -> dict:
        cfg = self.config.get_connection(project, connection)
        # 未绑定默认库时的两道防线（否则 SQLAlchemy 反射取默认库为 None → NoneType.replace 崩）：
        # ① 从「库.表」限定名拆出 schema；② 仍无 schema 且无默认库 → 明确报错引导，而非让它崩
        if schema is None and "." in table:
            schema, table = table.split(".", 1)
        if schema is None and not cfg.database and cfg.engine in ("mysql", "postgres", "clickhouse"):
            raise ValueError("此连接未绑定默认库，请用「库名.表名」指定表，或先选择一个库（schema）")
        engine = self.pool.get(project, connection, cfg)
        detail = f"{schema}.{table}" if schema else table
        return self._audited(project, connection, cfg, "describe_table", detail, caller,
                             lambda: engines.describe_table(engine, table, schema))

    def admin_search_tables(
        self, project: str, connection: str, q: str, caller: CallerInfo,
    ) -> list[dict]:
        """查询台全局表名搜索（⌘P）：跨库 LIKE 匹配，最多 50 条。"""
        q = (q or "").strip()
        if not q:
            return []
        cfg = self.config.get_connection(project, connection)
        engine = self.pool.get(project, connection, cfg)
        return self._audited(project, connection, cfg, "search_tables", q, caller,
                             lambda: engines.search_tables(engine, cfg.engine, q))

    def admin_table_sizes(
        self, project: str, connection: str, caller: CallerInfo, schema: str | None = None
    ) -> dict[str, int]:
        """查询台树右侧的表容量（字节）。取不到返回空 dict，不阻断列表。"""
        cfg = self.config.get_connection(project, connection)
        engine = self.pool.get(project, connection, cfg)
        return self._audited(project, connection, cfg, "table_sizes", schema or "", caller,
                             lambda: engines.table_sizes(engine, cfg.engine, schema))

    def get_table_ddl(
        self, project: str, connection: str, table: str, caller: CallerInfo,
        schema: str | None = None,
    ) -> str:
        """取建表语句（查询台「查看 DDL」）。"""
        cfg = self.config.get_connection(project, connection)
        engine = self.pool.get(project, connection, cfg)
        detail = f"{schema}.{table}" if schema else table
        return self._audited(project, connection, cfg, "table_ddl", detail, caller,
                             lambda: engines.get_table_ddl(engine, cfg.engine, table, schema))

    def ai_generate_sql(
        self, project: str, connection: str, question: str, caller: CallerInfo,
        *, schema: str | None = None, tables: list[str] | None = None,
        explain: bool = False, include_samples: bool = False,
        session_id: str | None = None,
    ) -> dict:
        """让命令行 AI 按表结构 + 自然语言需求生成一条 SQL。只生成、不执行。

        tables 为空 = 「整库」模式：列出该库的表（超 ai_max_tables 报错要求收窄）。
        include_samples 时附少量样本行帮助 AI 理解数据形态。
        session_id 非空 = 追问：续接同一会话、不重发表结构。返回 {sql, explanation, session_id}。
        """
        from . import ai

        s = self.get_settings()
        if not s.get("ai_enabled"):
            raise QueryRejected("AI 辅助未开启，请在系统设置中开启")
        question = (question or "").strip()
        if not question:
            raise QueryRejected("请填写你想查什么")
        cfg = self.config.get_connection(project, connection)
        if cfg.engine not in ("mysql", "postgres", "sqlite"):
            raise QueryRejected(f"连接引擎 {cfg.engine} 暂不支持 AI 生成 SQL")
        engine = self.pool.get(project, connection, cfg)
        max_tables = int(s.get("ai_max_tables") or 40)

        def _run() -> dict:
            ddls: list[tuple[str, str]] = []
            samples: dict[str, str] | None = None
            if not session_id:  # 首轮才收集表结构；追问续接会话、上下文已在 AI 侧
                names = list(tables or [])
                if not names:  # 整库：列出全部表
                    names = engines.list_tables(engine, schema)
                if not names:
                    raise QueryRejected("该库没有可用的表")
                if len(names) > max_tables:
                    raise QueryRejected(
                        f"待发送的表有 {len(names)} 张，超过上限 {max_tables}；请勾选具体的表，"
                        "或在系统设置调大「最大表数」")
                for t in names:
                    tbl_schema, tbl = (t.split(".", 1) if "." in t else (schema, t))
                    ddls.append((tbl, engines.get_table_ddl(engine, cfg.engine, tbl, tbl_schema)))
                if include_samples:
                    samples = {}
                    for t in names:
                        tbl_schema, tbl = (t.split(".", 1) if "." in t else (schema, t))
                        try:
                            r = engines.sample_rows(engine, tbl, 5,
                                                    max_cell_chars=cfg.policy.max_cell_chars)
                            samples[tbl] = _rows_to_text(r.columns, r.rows)
                        except Exception:  # 样本拿不到不阻断生成
                            continue
            result = ai.generate_sql(
                system_prompt=str(s.get("ai_sql_prompt") or ai.DEFAULT_SQL_PROMPT),
                dialect=cfg.engine, ddls=ddls, question=question,
                explain=explain, samples=samples,
                provider=str(s.get("ai_provider") or "claude"),
                model=str(s.get("ai_model") or ""),
                timeout=int(s.get("ai_timeout_s") or 60),
                cli_path=str(s.get("ai_cli_path") or ""),
                session_id=session_id, api=_ai_api_cfg(s))
            if not result.sql.strip():
                raise QueryRejected("AI 未能生成 SQL，请补充需求描述后重试")
            return {"sql": result.sql, "explanation": result.explanation,
                    "session_id": result.session_id}

        tool = "ai_followup_sql" if session_id else "ai_generate_sql"
        try:
            return self._audited(project, connection, cfg, tool,
                                 question[:2000], caller, _run)
        except ai.AIError as e:
            raise QueryRejected(str(e)) from e

    def ai_generate_workflow(
        self, project: str, connection: str, question: str, caller: CallerInfo,
        *, schema: str | None = None, tables: list[str] | None = None,
    ) -> dict:
        """让命令行 AI 按连接/表结构 + 需求设计一张 workflow DAG（画布图）。

        产物用 compile_graph 校验，编译失败把错误回喂给 AI 重修一次；仍失败则报错。
        返回 {graph:{nodes,edges}}（节点已排版赋 x/y），前端载到画布待人审阅、不自动执行。
        """
        from . import ai
        from .workflows import WorkflowError, compile_graph

        s = self.get_settings()
        if not s.get("ai_enabled"):
            raise QueryRejected("AI 辅助未开启，请在系统设置中开启")
        question = (question or "").strip()
        if not question:
            raise QueryRejected("请描述你想做的分析流程")
        cfg = self.config.get_connection(project, connection)
        if cfg.engine not in ("mysql", "postgres", "sqlite"):
            raise QueryRejected(f"连接引擎 {cfg.engine} 暂不支持 AI 生成流程")
        engine = self.pool.get(project, connection, cfg)
        max_tables = int(s.get("ai_max_tables") or 40)
        # 可用连接（供 source 节点选，排除 redis）
        conns = [f"{p}/{c}" for p, proj in sorted(self.config.projects.items())
                 for c, cc in sorted(proj.connections.items()) if cc.engine != "redis"]

        def _run() -> dict:
            names = list(tables or [])
            if not names:
                names = engines.list_tables(engine, schema)
            if len(names) > max_tables:
                raise QueryRejected(
                    f"待发送的表有 {len(names)} 张，超过上限 {max_tables}；请勾选具体的表")
            ddls: list[tuple[str, str]] = []
            for t in names:
                tbl_schema, tbl = (t.split(".", 1) if "." in t else (schema, t))
                ddls.append((tbl, engines.get_table_ddl(engine, cfg.engine, tbl, tbl_schema)))
            kw = dict(system_prompt=str(s.get("ai_workflow_prompt") or ai.DEFAULT_WORKFLOW_PROMPT),
                      dialect=cfg.engine, connections=conns, ddls=ddls, question=question,
                      provider=str(s.get("ai_provider") or "claude"),
                      model=str(s.get("ai_model") or ""),
                      timeout=int(s.get("ai_timeout_s") or 60),
                      cli_path=str(s.get("ai_cli_path") or ""), api=_ai_api_cfg(s))
            graph, sid = ai.generate_workflow(**kw)
            try:
                compile_graph(graph)
            except WorkflowError as e:  # 回喂错误、续接会话重修一次
                graph, sid = ai.generate_workflow(**kw, repair_error=str(e), session_id=sid)
                try:
                    compile_graph(graph)
                except WorkflowError as e2:
                    raise QueryRejected(f"AI 生成的流程仍不合法：{e2}") from e2
            _layout_graph(graph)
            return {"graph": graph}

        try:
            return self._audited(project, connection, cfg, "ai_generate_workflow",
                                 question[:2000], caller, _run)
        except ai.AIError as e:
            raise QueryRejected(str(e)) from e

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
            result = self._run_touching_db(project, connection, fn)
        except ConnectionUnavailable as e:
            rec.status = "error"
            rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
            self.store.record(rec)
            raise
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        self.store.record(rec)
        return result

    def _run_touching_db(self, project: str, connection: str, fn):  # noqa: ANN001
        """任何"会触达 DB/隧道"的动作都过这里：入口先查健康位、出错时按类别打标。

        - 若健康位为 unavailable/exhausted：直接抛 ConnectionUnavailable（不碰 DB）
        - 执行成功：清健康标记（如果之前挂过）
        - 失败：判断是不是"连接级"异常，是就打标 + 启后台重连，然后原样再抛
          （非连接级异常如 SQL 语法/权限拒/审批拒不打标，重连也没用）
        """
        self.health.check(project, connection)
        try:
            result = fn()
        except ConnectionUnavailable:
            raise
        except Exception as e:
            if is_connection_error(e):
                # 池里对应的引擎/隧道大概率也坏了：回收让重连时用新连接
                try:
                    self.pool.dispose_connection(project, connection)
                    self.redis_pool.dispose_connection(project, connection)
                except Exception:  # noqa: BLE001
                    pass
                self.health.mark_failed(project, connection, f"{type(e).__name__}: {e}")
            raise
        self.health.mark_ok(project, connection)
        return result

    def _health_probe(self, project: str, connection: str) -> None:
        """健康监控的探测回调：走 reader 建/借连接做 SELECT 1（Redis 用 PING）。

        失败原样抛出，让 HealthMonitor 记退避；成功即视作连接已恢复。
        """
        try:
            cfg = self.config.get_connection(project, connection)
        except KeyError:
            # 连接被删了：视作已恢复（后续不会再有请求走它）
            return
        # 每次探测前先回收旧引擎/隧道，避免复用坏连接
        try:
            self.pool.dispose_connection(project, connection)
            self.redis_pool.dispose_connection(project, connection)
        except Exception:  # noqa: BLE001
            pass
        if cfg.engine == "redis":
            client = self.redis_pool.get(project, connection, cfg)
            client.ping()
            return
        engine = self.pool.get(project, connection, cfg)
        engines.run_query(engine, "SELECT 1", max_rows=1)

    def _on_connection_exhausted(self, project: str, connection: str, error: str) -> None:
        """连接 exhausted 时发一条通知（人为介入信号）。"""
        try:
            self.notifier.send(
                title=f"连接不可用 · {project}/{connection}",
                body=f"重连多次仍失败，请到管理后台检查连接配置。最近错误：{error}",
                meta={"kind": "connection_exhausted",
                      "project": project, "connection": connection},
            )
        except Exception:  # noqa: BLE001
            logger.exception("notify exhausted failed")

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

    # ---------- SSH 证书库 ----------

    def list_ssh_identities(self) -> dict:
        return dict(self.config.ssh_identities)

    def upsert_ssh_identity(
        self, name: str, key_path: str, known_hosts_path: str | None, caller: CallerInfo,
        host: str | None = None, user: str | None = None, port: str | int | None = None,
    ) -> None:
        from .connections import ConnectionManager

        mgr = ConnectionManager(self.config, self._require_config_path())
        referers = mgr.identity_referers(name)
        mgr.upsert_identity(name, key_path, known_hosts_path, host=host, user=user, port=port)
        # 证书变更影响引用它的连接的隧道：回收让下次用新证书重建
        for ref in referers:
            proj, conn = ref.split("/", 1)
            self.pool.dispose_connection(proj, conn)
            self.redis_pool.dispose_connection(proj, conn)
        self.store.record(AuditRecord(
            project="admin", connection=name, tool="upsert_ssh_identity", status="ok",
            agent=caller.agent, session_id=caller.session_id, detail="已保存 SSH 配置"))

    def delete_ssh_identity(self, name: str, caller: CallerInfo) -> None:
        from .connections import ConnectionManager

        mgr = ConnectionManager(self.config, self._require_config_path())
        mgr.delete_identity(name)
        self.store.record(AuditRecord(
            project="admin", connection=name, tool="delete_ssh_identity", status="ok",
            agent=caller.agent, session_id=caller.session_id, detail="已删除 SSH 配置"))

    def probe_connection_fields(self, fields: dict, existing_password: str | None = None):
        """用表单值临时探测连通性与账号权限（测试按钮）。不保存、不入池。"""
        from .config import ConnectionConfig, Policy
        from .probe import probe_connection

        password = fields.get("password") or None
        eff_pw = f"plain://{password}" if password else existing_password
        # sqlite 无账号；redis 允许无认证（本地无 auth 实例）——都不强制填密码
        if eff_pw is None and fields.get("engine") not in ("sqlite", "redis"):
            from .probe import ProbeResult
            return ProbeResult(ok=False, message="请填写密码后再测试")
        cfg = ConnectionConfig(
            engine=fields["engine"], environment=fields.get("environment", "dev"),
            host=fields.get("host") or None, port=fields.get("port"),
            database=fields.get("database") or None, user=fields.get("user") or None,
            password=eff_pw, jump_hosts=fields.get("jump_hosts", []),
            ssh_options=fields.get("ssh_options", []),
            policy=Policy(max_rows=fields.get("max_rows", 500)),
        )
        return probe_connection(cfg, None, self.config.ssh_identities)

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
        return probe_ssh(cfg, self.config.ssh_identities)

    def delete_connection(self, project: str, connection: str, caller: CallerInfo) -> None:
        from .connections import ConnectionManager

        mgr = ConnectionManager(self.config, self._require_config_path())
        mgr.delete(project, connection)
        self._after_connection_change(project, connection, caller, "delete_connection", "已删除")

    def _after_connection_change(
        self, project: str, connection: str, caller: CallerInfo, tool: str, detail: str
    ) -> None:
        # 回收旧引擎/隧道，下次访问用新配置重建；同步清健康位（新配置视作全新开始）
        self.pool.dispose_connection(project, connection)
        self.redis_pool.dispose_connection(project, connection)
        self.health.force_clear(project, connection)
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
        self.health.stop()
        self.pool.dispose()
        self.redis_pool.dispose()
        self.store.close()
        if self.approvals is not None:
            self.approvals.close()
        if self.metadata is not None:
            self.metadata.close()
        if self.snippets is not None:
            self.snippets.close()
        if self.settings is not None:
            self.settings.close()
