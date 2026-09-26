"""核心服务层：与 MCP 传输解耦，便于单元测试。

所有会触达数据库的操作都必须落审计记录（成功 / 拒绝 / 出错），
拒绝路径同样入库——这正是要给人看的部分。
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .approvals import KIND_SYNC, ApprovalError, ApprovalStore
from .audit.classify import classify, fingerprint
from .audit.log import AuditRecord, AuditStore, parse_time_filter
from .audit.redis_rules import classify_command, command_fingerprint, parse_command
from .audit.risk import assess
from .config import AppConfig, ConnectionConfig
from .health import ConnectionUnavailable, HealthMonitor, is_connection_error
from .masking import apply_mask, resolve_default_patterns
from .metadata import MetadataCache
from .budget import SessionBudget
from .metrics import LiveOps
from .notify import NoopNotifier, Notifier
from . import checkup, engines, privileges, redis_engine, sync
from .drivers import get_driver as _get_driver


def _driver_of(cfg: ConnectionConfig):  # noqa: ANN001, ANN201
    """连接对应的驱动（引擎特有能力从这里取，不再散落 if engine == ... 清单）。"""
    return _get_driver(cfg.engine)


def _has_schema_layer(cfg: ConnectionConfig) -> bool:  # noqa: ANN001
    """该连接的表是否位于 schema/database 之下（sqlite 一个文件就是一个库，不是）。"""
    return _driver_of(cfg).has_schema_layer

if TYPE_CHECKING:
    from .snippets import SnippetStore

logger = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL_S = 60
DEFAULT_RETENTION_DAYS = 30
ADMIN_PAGE_SIZE = 100  # 查询台每页行数（上限受连接 max_rows 约束）
DEFAULT_AGENT_MAX_RESULT_CHARS = 40000  # agent 结果字符预算的最终兜底（settings 未启用时）
DEFAULT_APPROVAL_WAIT_S = 120  # execute 等待人工审批的默认秒数（settings 未启用时）
DEFAULT_SYNC_MAX_ROWS = 10_000  # 表同步单次行数上限的兜底（settings 未启用时）
DEFAULT_SYNC_MAX_BYTES = 64 * 1024 * 1024  # 表同步单次体积上限的兜底
DEFAULT_SESSION_BUDGET_CHARS = 400_000  # 单会话累计返回字符上限的兜底


class QueryRejected(Exception):
    """SQL 被审计规则拒绝。message 面向 agent，说明原因与下一步动作。"""


class SqlSyntaxError(QueryRejected):
    """SQL 语法错误——sqlglot 初筛失败**且**目标 DB 只解析不执行地复核也报语法错。

    与普通 QueryRejected 分开，是为了让 agent 一眼分辨「改 SQL」与「走审批/换工具」：
    语法错重发相同 SQL 永远不会成功，也不该浪费一次人工审批。
    """


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


def _patch_source_schema(graph: dict, default_conn: str, schema: str | None) -> None:
    """AI 生成的 source 节点用了 default_conn 但漏写 cfg.schema 时兜底填 schema（原地修改）。

    schema 为空则什么都不做（连接自身有默认库，不需要 schema）。
    """
    if not schema:
        return
    for n in graph.get("nodes") or []:
        if n.get("type") != "source":
            continue
        cfg = n.setdefault("cfg", {})
        if (cfg.get("conn") or "").strip() == default_conn and not (cfg.get("schema") or "").strip():
            cfg["schema"] = schema


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


def _plan_node_name(plan: dict, node_id: str, graph: dict) -> str | None:
    """从编译后的 plan 找目标节点的名字（=工作区里的表/视图名）。

    plan 里 sources 有 node/dataset，steps 有 node/name；这些都是 compile_graph 输出。
    output 节点是虚 output_sql 无独立 view，此时回退取 graph.nodes[node_id].name。
    """
    for src in plan.get("sources") or []:
        if src.get("node") == node_id:
            return src.get("dataset")
    for st in plan.get("steps") or []:
        if st.get("node") == node_id:
            return st.get("name")
    for n in graph.get("nodes") or []:
        if n.get("id") == node_id:
            return (n.get("name") or "").strip() or None
    return None


def _preview_target_for(graph: dict, node_id: str) -> tuple[str, str] | None:
    """预览节点输出时应该看哪张 view/table。

    普通节点（source/file/filter/join/aggregate/sql）compile 时会 CREATE OR REPLACE
    VIEW <节点名>，DESCRIBE 节点名即可拿列。
    **output 节点例外**：SQL 是 `SELECT * FROM 上游 ORDER BY .. LIMIT ..`，不物化
    成 view；查询 output.name 会报 "Table … does not exist"。预览它=预览它的上游。

    返回 (target_view_name, source_node_id_to_materialize)：
    - 普通节点：(name, node_id)
    - output 节点：(上游节点的 name, 上游 node_id)
    找不到节点或 output 无上游连线 → None。
    """
    nodes = {n.get("id"): n for n in (graph.get("nodes") or [])}
    node = nodes.get(node_id)
    if not node:
        return None
    if node.get("type") != "output":
        name = (node.get("name") or "").strip()
        return (name, node_id) if name else None
    # output 节点：找 in 边指向它的那个上游
    for e in graph.get("edges") or []:
        if e.get("to") == node_id:
            up_id = e.get("from")
            up = nodes.get(up_id)
            if up:
                up_name = (up.get("name") or "").strip()
                if up_name:
                    return (up_name, up_id)
    return None


def _plan_prefix_for(plan: dict, target_name: str) -> dict:
    """从 plan 里挑出「构建 target_name 所必需的」sources + steps（按 plan 顺序，保拓扑序）。

    简单实现：走 SQL 里的 FROM/JOIN 关联反向追依赖太脆；这里保守起见——
    截到 target 在 steps 中出现的位置为止（含），sources 全带上（不多也不害事）。
    """
    sources = list(plan.get("sources") or [])
    steps: list[dict] = []
    for st in plan.get("steps") or []:
        steps.append(st)
        if st.get("name") == target_name:
            break
    return {"sources": sources, "steps": steps}


def _dataset_exists(store, workspace: str, name: str) -> bool:
    """工作区中是否已存在同名 table 或 view。"""
    try:
        con = store._connect(workspace, must_exist=False)
    except Exception:  # noqa: BLE001
        return False
    try:
        row = con.execute(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_schema='main' AND table_name = ? LIMIT 1",
            [name]).fetchone()
        return row is not None
    finally:
        con.close()


def _describe_columns(store, workspace: str, name: str) -> list[dict]:
    """DuckDB DESCRIBE 拿列名/类型。"""
    con = store._connect(workspace)
    try:
        rows = con.execute(f'DESCRIBE "{name}"').fetchall()
    finally:
        con.close()
    # DESCRIBE 返回 (column_name, column_type, null, key, default, extra)
    return [{"name": r[0], "type": r[1]} for r in rows]


@dataclass
class CallerInfo:
    agent: str = "unknown"
    session_id: str = ""


# _audited 包着的工具若返回 dict（而非 QueryResult），用这个私有键把结果集体积捎给
# 审计层。_audited 记完流量后会 pop 掉，绝不出现在给 agent / 前端的返回值里。
AUDIT_BYTES_KEY = "__audit_result_bytes__"


def change_status_payload(change) -> dict:
    """审批单状态的统一回传结构（get_change_status / wait_for_change / execute 等待共用）。"""
    payload = {
        "change_id": change.id,
        "status": change.effective_status(),
        "risk_level": change.risk_level,
        "project": change.project,
        "connection": change.connection,
        "decided_by": change.decided_by,
        "decision_note": change.decision_note,
        "expires_at": change.expires_at,
    }
    if change.rollback_note:
        payload["rollback_note"] = change.rollback_note
    if change.exec_result:
        payload["exec_result"] = change.exec_result
    return payload


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
        # serve 入口注入 NotifierRouter（内推 + 用户配置的外部渠道，动态跟随设置）。
        self.notifier = notifier if notifier is not None else NoopNotifier()
        # 站内通知收件箱（serve 时注入 InboxStore）；SSE 铃铛与外部渠道之外的默认路径
        self.inbox = None
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
        self.schedules = None  # WorkflowScheduleStore（serve 时注入）
        self.runs = None       # WorkflowRunStore（serve 时注入）
        self._housekeeping_stop: threading.Event | None = None
        self._scheduler_stop: threading.Event | None = None
        # 记录本进程周期内已经为哪一分钟执行过（防止 30s tick 在同一分钟触发两次）
        self._sched_ticked_minute: set[tuple[str, str]] = set()  # {(name, "YYYY-MM-DD HH:MM")}
        self.data_dir = None   # serve 时注入，供 xlsx 产物落盘
        self.base_url = ""     # serve 时注入，如 http://127.0.0.1:8100（导出下载链接）
        # 在途操作登记簿：审计只在操作**结束后**落库，「此刻谁在查」只能靠它。
        # 由 _run_touching_db 统一登记/注销，看板 /admin/dashboard 读它。
        self.live = LiveOps()
        # 会话级结果配额（只约束 agent 取数；后台/人的路径不经过它）。
        # 上限跟随系统设置：每次取用前同步，改设置即时生效、不必重启。
        self.session_budget = SessionBudget(DEFAULT_SESSION_BUDGET_CHARS)
        self.started_at = time.time()
        # PG：连接上可切换的 database 清单缓存（(project, connection) → (取数时刻, 库名列表)），
        # 校验 agent 传入的 pg_database 用，避免每次调用都多打一条 pg_database 查询
        self._pg_db_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

    # ---------- 元信息 ----------

    def begin_session(self, caller: CallerInfo, title: str, note: str = "") -> dict:
        """登记当前 agent 会话的名字/简介，供后台按会话回溯其跑过的 SQL。

        之后同一 MCP 会话（session_id 相同）里跑的所有 SQL 都能在审计页按此会话归类。
        幂等：同一会话重复调用覆盖标题/简介。title 必填、非空。
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("Session title cannot be empty")
        note = (note or "").strip()
        sid = caller.session_id
        self.store.upsert_session(sid, caller.agent or "unknown", title, note)
        return {
            "session_id": sid,
            "title": title,
            # When session_id is empty (e.g. some stdio clients have no session id) it
            # can't be grouped by session — say so explicitly.
            "note": "" if sid else "The current client did not provide a session id; "
                                   "this session's SQL cannot be traced back by session",
        }

    # 审计记录的结果状态：ok=真正执行成功；rejected=被挡下（首提生成审批单、
    # 审批被驳回/过期、只读防线拒绝等，都没落库）；error=执行时出错。
    HISTORY_STATUSES = ("ok", "rejected", "error")

    # 一条操作可回传的列。默认只给「回溯所需的骨架」——SQL 原文与错误明细都很占
    # 上下文（一次会话几十条就能吃掉几千 token），agent 认准了哪几条再用 fields 取。
    HISTORY_DEFAULT_FIELDS = ("ts", "tool", "connection", "status", "row_count",
                              "change_id", "approval_status", "rollback_note")
    HISTORY_ALL_FIELDS = HISTORY_DEFAULT_FIELDS + (
        "sql", "detail", "duration_ms", "environment", "agent", "fingerprint")
    # 单条记录里 SQL 的回传上限：写操作 SQL 通常很短，长的多半是批量迁移脚本，
    # 截断后给出提示，需要全文可以去审批页/审计页看。
    HISTORY_SQL_MAX_CHARS = 2000

    def _history_status(self, status: str) -> str:
        """校验结果状态入参，非法值直接报错而不是静默返回空列表。"""
        st = (status or "").strip().lower()
        if st and st not in self.HISTORY_STATUSES:
            raise ValueError(
                f"Unsupported status {status!r}; options: {', '.join(self.HISTORY_STATUSES)}"
                " (ok=executed successfully, rejected=blocked without landing, error=execution failed)"
            )
        return st

    def _history_fields(self, fields: str) -> tuple[str, ...]:
        """Parse the fields argument: empty=default compact columns, all=every column, otherwise pick by name (validated against the allow-list)."""
        raw = (fields or "").strip()
        if not raw:
            return self.HISTORY_DEFAULT_FIELDS
        if raw.lower() == "all":
            return self.HISTORY_ALL_FIELDS
        wanted = [f.strip() for f in raw.split(",") if f.strip()]
        unknown = [f for f in wanted if f not in self.HISTORY_ALL_FIELDS]
        if unknown:
            raise ValueError(
                f"Unsupported field(s) {unknown}; options: {', '.join(self.HISTORY_ALL_FIELDS)} (or all)"
            )
        return tuple(wanted)

    def list_agent_sessions(
        self,
        caller: CallerInfo,
        limit: int = 20,
        all_agents: bool = False,
        since: str = "",
        until: str = "",
        keyword: str = "",
        project: str = "",
        connection: str = "",
        status: str = "",
        writes_only: bool = False,
    ) -> list[dict]:
        """列出跑过 SQL 的历史会话（默认只列当前 agent 自己的），最近活动在前。

        供 agent 回溯「我上次/前几次都做了什么」：先在这里按日期/关键词/连接找到目标会话，
        再用 session_history(session_id) 看那次会话的具体操作。
        since/until 支持 `YYYY-MM-DD` 或 ISO 时间，不带时区时按本地时区解释。
        status 只保留有该结果状态操作的会话（`writes_only=True, status="ok"` =
        「真正改成过数据的会话」）。
        """
        rows = self.store.list_sessions(
            limit=limit,
            agent=None if all_agents else (caller.agent or None),
            since=parse_time_filter(since),
            until=parse_time_filter(until, end_of_day=True),
            keyword=keyword or None,
            project=project or None,
            connection=connection or None,
            status=self._history_status(status) or None,
            only_with_writes=writes_only,
        )
        return [
            {
                "session_id": r["session_id"],
                # 没调过 begin_session 的会话没有名字，用空串而不是 None
                "title": r["title"] or "",
                "note": r["note"] or "",
                "agent": r["agent"] or "",
                "ops": r["ops"],
                "writes": r["writes"],
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
                "current": r["session_id"] == caller.session_id,
            }
            for r in rows
        ]

    def session_history(
        self, caller: CallerInfo, session_id: str = "", limit: int = 50,
        writes_only: bool = False, status: str = "", fields: str = "",
    ) -> dict:
        """回溯某个会话跑过的操作（最近在前），写操作带上其审批单与回滚备注。

        session_id 省略即当前会话。写操作（execute/sync_write）通过审计记录上的
        change_id 关联回审批单，返回审批状态与提交时写下的 rollback_note——
        「这次改动前是什么值、怎么回滚」就在那里。

        status 按结果筛选：`writes_only=True, status="ok"` 就是「审批通过并真正执行成功的
        改动」——排查「上次到底改成了哪些」时用它，被挡下的首提记录不会混进来。

        默认只回精简列（见 HISTORY_DEFAULT_FIELDS）：SQL 原文与错误明细不默认返回，
        免得几十条记录把上下文吃满；要看时用 fields 显式点名（如 "sql,detail" 或 "all"）。
        """
        sid = (session_id or caller.session_id or "").strip()
        if not sid:
            raise ValueError(
                "No session_id given, and the current client did not provide a session id; "
                "use list_sessions to find the target session first, then pass its session_id in"
            )
        want = self._history_fields(fields)
        st = self._history_status(status)
        filters = {"session_id": sid}
        if writes_only:
            filters["rw"] = "write"
        if st:
            filters["status"] = st
        rows = self.store.recent(limit=limit, filters=filters)
        # 关联审批单只在真要用到审批信息时查，省一次库
        changes = {}
        if self.approvals is not None and {"approval_status", "rollback_note"} & set(want):
            changes = self.approvals.get_many(r["change_id"] for r in rows)

        ops = []
        for r in rows:
            change = changes.get(r["change_id"])
            full = {
                "ts": r["ts"],
                "tool": r["tool"],
                "connection": f"{r['project']}/{r['connection']}",
                "environment": r["environment"] or "",
                "agent": r["agent"] or "",
                "status": r["status"],
                "row_count": r["row_count"],
                "duration_ms": r["duration_ms"],
                "detail": r["detail"] or "",
                "fingerprint": r["fingerprint"] or "",
                "sql": self._clip_history_sql(r["sql"] or ""),
                "change_id": r["change_id"],
                "approval_status": change.effective_status() if change else "",
                "rollback_note": change.rollback_note if change else "",
            }
            # 空值不占位（None/空串一律省掉），只留真正有内容的列
            ops.append({k: full[k] for k in want if full[k] not in (None, "")})

        status_counts: dict[str, int] = {}
        for r in rows:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

        meta = self.store.get_session(sid) or {}
        out = {
            "session_id": sid,
            "title": meta.get("title") or "",
            "note": meta.get("note") or "",
            "operations": ops,
            "count": len(ops),
            "status_counts": status_counts,
            "fields": list(want),
            "order": "most recent first",
        }
        if fields.strip().lower() not in ("all",) and "sql" not in want:
            out["hint"] = ('The raw SQL and error details are not returned by default; '
                           'ask for them separately with fields="sql,detail" (or all), '
                           'and consider narrowing with limit')
        return out

    def _clip_history_sql(self, sql: str) -> str:
        if len(sql) <= self.HISTORY_SQL_MAX_CHARS:
            return sql
        return sql[: self.HISTORY_SQL_MAX_CHARS] + "... (truncated)"

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
            raise KeyError(f"Project {project!r} does not exist")
        return [
            {
                "connection": name,
                "engine": c.engine,
                "environment": c.environment,
                "database": c.database,
                "host": c.host,
                # No default database: tell the agent to use fully-qualified table names
                **({"note": "This connection has no default database bound; qualify "
                            "queries/schema operations with \"database.table\", and pick "
                            "a database first with SHOW DATABASES before calling "
                            "list_tables/describe_table"}
                   if _get_driver(c.engine).has_schema_layer and not c.database else {}),
                # user/password/writer and other account info is deliberately not returned
            }
            # Redis is deliberately not returned: the agent cannot reach Redis
            for name, c in sorted(proj.connections.items()) if c.engine != "redis"
        ]

    # ---------- 查询 ----------

    PG_DB_LIST_TTL_S = 60.0

    def resolve_pg_database(self, project: str, connection: str,
                            database: str | None,
                            cfg: ConnectionConfig | None = None) -> str | None:
        """校验并规范化 agent 指定的 PG database（`pg_database` 参数）。

        - 未指定 / 空串 / 恰好是连接本来就连的库 → None（走默认引擎，不多建一条连接）；
        - 非 PG 连接指定了它 → 报错（MySQL/CH 的「库」走 database/schema 参数）；
        - 不在服务器可连接的库清单里 → 报错并列出可选项。

        必须先校验再建连接：连一个不存在的库，失败原文带 `connection to server`，
        一旦被当成断连就会把整条连接的健康位打坏（health 里另有兜底，这里是第一道）。
        """
        name = (database or "").strip()
        if not name:
            return None
        cfg = cfg or self.config.get_connection(project, connection)
        if cfg.engine != "postgres":
            raise ValueError(
                f"pg_database only applies to PostgreSQL connections; {project}/{connection} "
                f"is {cfg.engine}. For MySQL/ClickHouse, specify the database with the "
                "database parameter instead")
        if name == engines.pg_database_name(cfg):
            return None
        names = self._pg_server_databases(project, connection, cfg)
        if name not in names:
            raise ValueError(
                f"Database {name!r} is not among the databases {project}/{connection} can "
                f"connect to (options: {', '.join(names) or 'none'}). Call "
                "list_server_databases first to see what's available")
        return name

    def _pg_server_databases(self, project: str, connection: str,
                             cfg: ConnectionConfig) -> list[str]:
        key = (project, connection)
        hit = self._pg_db_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < self.PG_DB_LIST_TTL_S:
            return hit[1]

        def _do() -> list[str]:
            engine = self.pool.get(project, connection, cfg)
            return engines.list_server_databases(engine, cfg.engine)

        names = self._run_touching_db(project, connection, _do)
        self._pg_db_cache[key] = (time.monotonic(), names)
        return names

    def _read(
        self, project: str, connection: str, cfg: ConnectionConfig, sql: str,
        caller: CallerInfo, max_rows: int, schema: str | None = None,
        on_start=None,  # noqa: ANN001
        max_cell_chars: int | None = None, mask: bool = True,
        database: str | None = None,
    ) -> dict:
        """执行一条已判定只读的 SQL：跑 reader、落审计、脱敏，返回结果 dict。

        max_rows 由调用方决定（query 用连接策略；查询台分页用 page_size+1 以探测下一页），
        与 truncated 检测解耦，便于复用。schema 为查询台的执行 schema 上下文。
        max_cell_chars 缺省用连接策略（查询台可传系统设置的 sql_max_cell_chars 覆盖）。
        mask=True 对敏感列脱敏（agent 路径的红线：密码不出现在工具返回值中）；已认证的后台
        查询台/导出传 mask=False——人就是要看真实数据，脱敏反而碍事。
        """
        rec = self._base_record(project, connection, cfg, "query", sql, caller)
        rec.detail = " ".join(f"{k}={v}" for k, v in (("db", database), ("schema", schema)) if v)

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, schema=schema, database=database)
            return engines.run_query(
                engine, sql, max_rows,
                max_cell_chars=max_cell_chars or cfg.policy.max_cell_chars,
                on_start=on_start,
            )

        try:
            result = self._run_touching_db(project, connection, _do, rec)
        except ConnectionUnavailable as e:
            rec.status = "error"
            rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
            self.store.record(rec)
            raise
        except QueryRejected:
            raise
        except Exception as e:
            where = rec.detail
            if not cfg.database and _is_no_database_error(e):
                rec.status = "error"
                rec.detail = "No database selected"
                self.store.record(rec)
                raise QueryRejected(
                    "This connection has no default database bound. Query with a "
                    "fully-qualified table name (\"database.table\", e.g. SELECT * FROM "
                    "mydb.users), or run SHOW DATABASES first to see what's available."
                ) from e
            rec.status = "error"
            rec.detail = (f"{where} " if where else "") + f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise

        rec.status = "ok"
        rec.row_count = result.row_count
        rec.duration_ms = result.duration_ms
        rec.result_bytes = result.result_bytes
        self.store.record(rec)
        rows, masked = (
            apply_mask(result.columns, result.rows, cfg.policy, self.mask_default_patterns(cfg))
            if mask else (result.rows, [])
        )
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

    def query(self, project: str, connection: str, sql: str, caller: CallerInfo,
              database: str | None = None) -> dict:
        """database：仅 PG，查询在哪个 database 上执行（见 resolve_pg_database）。"""
        cfg = self.config.get_connection(project, connection)
        database = self.resolve_pg_database(project, connection, database, cfg)
        verdict = classify(sql, cfg.engine)
        if verdict.statement_kind == "ParseError":
            # 解析失败先走语法预检：DB 复核也报语法错就直接给出精确的语法错误，
            # 而不是让 agent 收到误导性的「仅允许只读语句」。
            self._syntax_precheck(project, connection, cfg, sql, caller, "query",
                                  database=database)
            # DB 认这条语法（或无法复核）：仍然不能放行——解析不了就判定不了只读性，
            # 按「默认拒绝」红线拒，但把真实原因说清楚。
            rec = self._base_record(project, connection, cfg, "query", sql, caller)
            rec.status = "rejected"
            rec.detail = verdict.reason
            self.store.record(rec)
            raise QueryRejected(
                f"Could not parse this SQL ({verdict.reason}), so its read-only status "
                "cannot be determined; rejected by default for safety. If it is really a "
                "read-only query, rewrite it in standard form and retry; if it is a data "
                "change, use the execute tool instead."
            )
        if not verdict.readonly:
            rec = self._base_record(project, connection, cfg, "query", sql, caller)
            rec.status = "rejected"
            rec.detail = verdict.reason
            self.store.record(rec)
            raise QueryRejected(
                f"Rejected: {verdict.reason}. The query tool only allows read-only "
                "statements; data changes require the human-authorized execute flow."
            )

        # 兜底：缺 LIMIT 的 SELECT 注入 LIMIT max_rows+1，防大表全量缓冲把 DB/进程拖挂
        run_sql, _, _ = engines.paginate_sql(sql, cfg.engine, cfg.policy.max_rows + 1, 0)
        out = self._read(project, connection, cfg, run_sql, caller, cfg.policy.max_rows,
                         database=database)
        out["statement_kind"] = verdict.statement_kind
        if out["truncated"]:
            out["hint"] = (
                f"The result was truncated to {cfg.policy.max_rows} rows (connection "
                "policy max_rows). For more data, paginate yourself with LIMIT/OFFSET in "
                "the SQL (or narrow the range with a WHERE condition)."
            )
        return out

    # ---------- 管理后台查询台（人已认证，写操作二次确认后直接执行）----------

    def admin_assess_batch(
        self, project: str, connection: str, statements: list[str], caller: CallerInfo,
        schema: str | None = None, database: str | None = None,
    ) -> dict:
        """查询台选中多条语句执行前的一次性评估：整批只让人确认一次。

        逐条分类，不执行任何语句：
        - 有语法错误 → kind=error，整批一条都不跑（免得前几条已经写进去才发现后面写错了）；
        - 全是只读 → kind=read，前端直接按顺序执行；
        - 含写操作 → kind=confirm，逐条给出风险与指纹。人确认一次后，前端逐条带上
          confirm 与**各自的**指纹执行，admin_run_sql 的指纹绑定（H1）仍逐条生效——
          执行的就是确认框里列出的那几条。
        """
        from .audit.risk import LEVELS  # noqa: PLC0415

        cfg = self.config.get_connection(project, connection)
        stmts = [x.strip() for x in statements if x and x.strip()]
        if not stmts:
            raise ValueError("没有可执行的语句")
        provider = self._meta_provider(project, connection, cfg, database=database,
                                       schema=schema)
        writes: list[dict] = []
        level = LEVELS[0]
        for i, sql in enumerate(stmts, 1):
            verdict = classify(sql, cfg.engine)
            if verdict.statement_kind == "ParseError":
                rec = self._base_record(project, connection, cfg, "query", sql, caller)
                rec.status = "rejected"
                rec.detail = verdict.reason
                self.store.record(rec)
                reason = verdict.reason.replace("SQL 解析失败: ", "")
                return {"kind": "error", "index": i,
                        "error": f"第 {i} 条 SQL 语法错误：{reason}（整批未执行）"}
            if verdict.readonly:
                continue
            report = assess(sql, cfg.engine, provider).to_dict()
            if LEVELS.index(report["level"]) > LEVELS.index(level):
                level = report["level"]
            writes.append({"index": i, "sql": sql, "statement_kind": verdict.statement_kind,
                           "fingerprint": fingerprint(sql, cfg.engine), "risk": report})
        if not writes:
            return {"kind": "read", "count": len(stmts)}
        # 只有一条写时执行计划才看得过来；多条逐一 EXPLAIN 会拖慢确认框（见 _try_explain）
        if len(writes) == 1:
            plan = self._try_explain(project, connection, cfg, writes[0]["sql"],
                                     schema=schema, database=database)
            if plan:
                writes[0]["risk"]["explain"] = plan
        return {"kind": "confirm", "count": len(stmts), "level": level, "writes": writes,
                "prod": (cfg.environment or "").lower() == "prod"}

    def admin_run_sql(
        self, project: str, connection: str, sql: str, caller: CallerInfo, confirm: bool = False,
        page: int = 0, page_size: int | None = None, schema: str | None = None,
        on_start=None,  # noqa: ANN001
        confirm_text: str | None = None, expect_fingerprint: str | None = None,
        database: str | None = None,
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
                                 schema=schema, on_start=on_start, max_cell_chars=eff_cell,
                                 mask=False, database=database)
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
                             schema=schema, on_start=on_start, max_cell_chars=eff_cell,
                             mask=False, database=database)
            out["paginated"] = False
            return {"kind": "read", **out}

        is_prod = (cfg.environment or "").lower() == "prod"
        fp = fingerprint(sql, cfg.engine)
        if not confirm:
            report = assess(sql, cfg.engine,
                            self._meta_provider(project, connection, cfg, database=database,
                                                schema=schema))
            report_dict = report.to_dict()
            plan = self._try_explain(project, connection, cfg, sql, schema=schema,
                                     database=database)
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
            engine = self.pool.get(project, connection, cfg, role="writer", schema=schema,
                                   database=database)
            return engines.run_write(engine, sql, on_start=on_start)

        try:
            result = self._run_touching_db(project, connection, _do, rec)
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

    # ------------------------------------------------------------ 用户与权限管理
    #
    # 目录查询与 DCL 都用 **writer 账号**：权限管理本身就是管理员动作，reader 往往
    # 既看不全 pg_class.relacl / mysql.user，也无权 GRANT。红线「日常查询走只读账号」
    # 约束的是数据查询路径，不是这里；页面上会明示用的是哪个账号。
    # 整组方法只挂在已认证的后台路由上，**不暴露为 MCP 工具**（红线 5：连接与密钥管理
    # agent 碰不到，账号权限管理同理）。

    def _privilege_role(self, cfg: ConnectionConfig) -> str:
        return "writer" if cfg.writer is not None else "reader"

    def _privilege_read(self, project: str, connection: str, cfg: ConnectionConfig,
                        sql: str, caller: CallerInfo, max_rows: int = 2000,
                        database: str | None = None) -> dict:
        """用管理账号跑一条权限目录查询并落审计（tool=admin_privileges）。

        database：PG 的授权与权限矩阵是**库级**的（ACL 存在各库自己的 pg_class 里），
        必须跟着查询台当前所在的库走，否则会把另一个库的 ACL 当成这个库的。
        账号本身是服务器级的（pg_roles 全局），列账号连哪个库都一样。
        """
        role = self._privilege_role(cfg)

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, role=role, database=database)
            return engines.run_query(engine, sql, max_rows)

        result = self._audited(project, connection, cfg, "admin_privileges", sql, caller, _do)
        return {"columns": result.columns, "rows": result.rows,
                "row_count": result.row_count, "duration_ms": result.duration_ms}

    def admin_list_db_users(self, project: str, connection: str, caller: CallerInfo,
                            database: str | None = None) -> dict:
        """列出目标库里的账号 / 角色。"""
        cfg = self.config.get_connection(project, connection)
        privileges.check_engine(cfg.engine)
        out = self._privilege_read(project, connection, cfg,
                                   privileges.list_users_sql(cfg.engine), caller,
                                   database=database)
        return {"engine": cfg.engine, "role": self._privilege_role(cfg),
                "environment": cfg.environment,
                "privileges": privileges.available_privileges(cfg.engine), **out}

    def admin_db_user_grants(self, project: str, connection: str, user: str,
                             caller: CallerInfo, host: str = "%",
                             database: str | None = None) -> dict:
        """某账号的授权明细。返回按层级分组的多张表（PG 有表/schema/库/属主/角色五组）。"""
        cfg = self.config.get_connection(project, connection)
        privileges.check_engine(cfg.engine)
        groups = []
        for title, sql in privileges.user_grants_sql(cfg.engine, user, host):
            out = self._privilege_read(project, connection, cfg, sql, caller, database=database)
            groups.append({"title": title, **out})
        return {"engine": cfg.engine, "user": user, "host": host, "groups": groups}

    def admin_privilege_matrix(self, project: str, connection: str, schema: str,
                               caller: CallerInfo, database: str | None = None) -> dict:
        """某 schema / 库下「表 × 账号 × 权限」明细，前端透视成矩阵。"""
        cfg = self.config.get_connection(project, connection)
        privileges.check_engine(cfg.engine)
        out = self._privilege_read(project, connection, cfg,
                                   privileges.privilege_matrix_sql(cfg.engine, schema), caller,
                                   database=database)
        return {"engine": cfg.engine, "schema": schema, **out}

    def admin_run_dcl(self, project: str, connection: str, action: str, params: dict,
                      caller: CallerInfo, confirm: bool = False,
                      confirm_text: str | None = None,
                      expect_fingerprint: str | None = None,
                      database: str | None = None) -> dict:
        """执行一次权限变更（CREATE USER / GRANT / REVOKE / …）。后台旁路，不进审批单。

        - confirm=False：只把服务端**构造好的**语句和影响回给页面做二次确认，不执行。
        - confirm=True：用 writer 执行并落审计（tool=admin_dcl）。

        三重闸门：
        - 语句由服务端按动作名 + 参数构造（`privileges.build`），页面传不进任意 SQL；
        - prod 环境须额外输入连接名（confirm_text）匹配才放行——DCL 在生产上不可逆
          （DROP USER / REVOKE 会当场掐断线上服务的访问），比普通写更需要一道人为减速带；
        - H1 指纹绑定：确认时带回的指纹须与当前参数构造出的语句一致，防「看 A 批 B」。

        审计落的是 `audit_sql`（密码已换成 ***）——红线 2：密码永不出现在审计记录里。
        """
        cfg = self.config.get_connection(project, connection)
        privileges.check_engine(cfg.engine)
        stmt = privileges.build(cfg.engine, action, params)
        is_prod = (cfg.environment or "").lower() == "prod"
        fp = fingerprint(stmt.audit_sql, cfg.engine)

        if not confirm:
            return {"kind": "confirm", "action": action, "sql": stmt.audit_sql,
                    "summary": stmt.summary, "has_secret": stmt.has_secret,
                    "prod": is_prod, "fingerprint": fp,
                    "expect_text": connection if is_prod else None}

        if is_prod and (confirm_text or "") != connection:
            raise QueryRejected(
                f"生产环境的权限变更需要输入连接名「{connection}」确认后才能执行。")
        if expect_fingerprint is not None and not hmac.compare_digest(expect_fingerprint, fp):
            raise QueryRejected("权限操作在确认前后发生了变化（指纹不一致），已拒绝执行，请重新确认。")
        if cfg.writer is None:
            raise QueryRejected(
                f"连接 {project}/{connection} 未配置 writer（管理）账号，无法执行权限变更。"
                "请先在系统设置的连接管理里补上。")

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, role="writer", database=database)
            return engines.run_write(engine, stmt.sql)

        # 审计记录里存脱敏后的语句：CREATE USER / ALTER USER 的原文带明文密码
        result = self._audited(project, connection, cfg, "admin_dcl", stmt.audit_sql, caller, _do)
        return {"kind": "done", "action": action, "sql": stmt.audit_sql,
                "summary": stmt.summary, "duration_ms": result.duration_ms}

    MAX_IMPORT_ROWS = 50_000

    def admin_import_rows(
        self, project: str, connection: str, table: str, columns: list[str],
        rows: list[list], caller: CallerInfo, schema: str | None = None,
        database: str | None = None,
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
        info = self.describe_table(project, connection, table, caller, schema=schema,
                                   database=database)
        valid = {c["name"] for c in info["columns"]}
        bad = [c for c in columns if c not in valid]
        if bad:
            raise ValueError(f"列不存在于表 {table}: {', '.join(bad)}（表列: {', '.join(sorted(valid))}）")
        rec = self._base_record(project, connection, cfg, "admin_import",
                                f"IMPORT INTO {table} ({', '.join(columns)}) — {len(rows)} 行",
                                caller)

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, role="writer", schema=schema,
                                   database=database)
            return engines.insert_rows(engine, table, columns, rows, schema=schema)

        try:
            result = self._run_touching_db(project, connection, _do, rec)
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

    _EXPORT_MAX_ROWS_DEFAULT = 1_000_000

    def export_max_rows(self) -> int:
        """导出的行数上限：独立于查询台的 max_rows，取系统设置 export_max_rows。

        查询台 max_rows（默认 1000）管的是「屏幕上一页看多少行」，导出要的是完整结果集，
        被它截断就没有导出的意义；这里另设一个较大的上限做防 OOM 硬护栏。
        """
        value = self._setting("export_max_rows")
        try:
            n = int(value) if value is not None else self._EXPORT_MAX_ROWS_DEFAULT
        except (TypeError, ValueError):
            n = self._EXPORT_MAX_ROWS_DEFAULT
        return n if n > 0 else self._EXPORT_MAX_ROWS_DEFAULT

    def admin_export(
        self, project: str, connection: str, sql: str, fmt: str, caller: CallerInfo,
        schema: str | None = None, database: str | None = None,
        max_rows: int | None = None, on_progress=None,  # noqa: ANN001
        on_start=None,  # noqa: ANN001
    ) -> dict:
        """导出只读查询的**完整**结果集为文件字节，流式写盘、不驻留全量行。

        返回 {data, media_type, ext, row_count, truncated, result_bytes, duration_ms}。

        - max_rows 缺省取 export_max_rows（远大于查询台 max_rows）；仍会注入 LIMIT 兜底，
          超过上限时 truncated 如实标注——导出「不该被截断」指的是不被查询上限截断，
          而不是无上限（无上限会把进程内存拖垮）。
        - on_progress(rows_written)：每导出一批（_STREAM_BATCH 行）回调一次累计行数，
          供前端弹框实时显示「已导出 N 行」。
        - on_start：拿到连接后回调一次并传入取消函数，导出任务经它注册 KILL QUERY。
        """
        import time

        from .export import SUPPORTED_FORMATS, stream_export
        from .metrics import estimate_cell_bytes

        cfg = self.config.get_connection(project, connection)
        if fmt not in SUPPORTED_FORMATS:
            from .export import ExportError
            raise ExportError(f"不支持的导出格式 {fmt!r}，可选：{', '.join(SUPPORTED_FORMATS)}")
        if not classify(sql, cfg.engine).readonly:
            raise QueryRejected("导出仅支持只读查询（SELECT/SHOW/...）的结果")
        limit = int(max_rows) if (max_rows or 0) > 0 else self.export_max_rows()
        # 注入 LIMIT limit+1 兜底：既防大表拖垮，又让 truncated 判得准（多取一行探测）
        run_sql, _, _ = engines.paginate_sql(sql, cfg.engine, limit + 1, 0)
        rec = self._base_record(project, connection, cfg, "query", sql, caller)
        rec.detail = " ".join(f"{k}={v}" for k, v in (("db", database), ("schema", schema)) if v)

        def _do() -> dict:
            engine = self.pool.get(project, connection, cfg, schema=schema, database=database)
            # writer 在 _on_meta 里按真实列名创建——stream_rows 保证 on_meta 在首个 yield
            # 之前触发，故 for 循环拿到第一行时 writer 一定已就绪。
            writer = None
            media_type = ext = ""
            columns: list[str] = []
            written = 0
            est_bytes = 0
            truncated = False
            start = time.monotonic()

            def _on_meta(cols, _cats):  # noqa: ANN001
                nonlocal writer, media_type, ext, columns
                columns = cols
                writer, media_type, ext = stream_export(cols, fmt)

            gen = engines.stream_rows(
                engine, run_sql, limit + 1,
                max_cell_chars=cfg.policy.max_cell_chars,
                on_start=on_start, on_meta=_on_meta,
            )
            try:
                for row in gen:
                    if written >= limit:          # 第 limit+1 行：只用来判定截断，不写盘
                        truncated = True
                        break
                    writer.write(row)
                    written += 1
                    # 结果体积估算只用于审计统计，没必要逐单元格精确算——
                    # 每行采样第一个单元格再按列数放大，量级足够（实测逐单元格算
                    # 会让百万行导出从几秒退化到两分钟）。
                    est_bytes += estimate_cell_bytes(row[0]) if row else 0
                    est_bytes += (len(row) - 1) * 16 if len(row) > 1 else 0
                    if on_progress is not None and written % engines._STREAM_BATCH == 0:
                        on_progress(written)
            finally:
                gen.close()
            if on_progress is not None:
                on_progress(written)
            data = writer.bytes()
            duration_ms = int((time.monotonic() - start) * 1000)
            est_bytes += sum(estimate_cell_bytes(c) for c in columns)
            return {
                "data": data,
                "media_type": media_type,
                "ext": ext,
                "columns": columns,
                "row_count": written,
                "truncated": truncated,
                "result_bytes": est_bytes,
                "duration_ms": duration_ms,
            }

        try:
            out = self._run_touching_db(project, connection, _do, rec)
        except ConnectionUnavailable as e:
            rec.status = "error"
            rec.detail = f"ConnectionUnavailable[{e.state}]: {e}"
            self.store.record(rec)
            raise
        except QueryRejected:
            raise
        except Exception as e:
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            raise
        rec.status = "ok"
        rec.row_count = out["row_count"]
        rec.duration_ms = out["duration_ms"]
        rec.result_bytes = out["result_bytes"]
        if out["truncated"]:
            rec.detail = (rec.detail + " " if rec.detail else "") + f"导出截断至 {limit} 行"
        self.store.record(rec)
        return out

    def export_table(
        self,
        project: str,
        connection: str,
        table: str,
        fields: list[str] | None,
        limit: int,
        fmt: str,
        caller: CallerInfo,
        database: str | None = None,
        pg_database: str | None = None,
    ) -> dict:
        """供 agent 导出单表数据，文件落服务端，仅返回下载链接及摘要。

        表、库和字段均先通过数据库反射校验，再由当前方言引用标识符，不接受任意 SQL，
        从而避免把导出入口变成查询分类器的旁路。agent 导出沿用敏感字段脱敏策略；
        文件内容不进入 MCP tool result，避免大文件占用模型上下文。
        """
        from .export import SUPPORTED_FORMATS, export_result

        cfg = self.config.get_connection(project, connection)
        pg_database = self.resolve_pg_database(project, connection, pg_database, cfg)
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported export format {fmt!r}, options: {', '.join(SUPPORTED_FORMATS)}")
        if limit < 1:
            raise ValueError("Export row count must be greater than 0")
        if limit > cfg.policy.max_rows:
            raise ValueError(
                f"Export row count {limit} exceeds the connection policy cap of "
                f"{cfg.policy.max_rows}; reduce the row count or have an administrator "
                "raise max_rows"
            )

        # Support table="database.table"; if database is also given, require them to
        # match, to avoid an ambiguous choice.
        if "." in table:
            table_database, plain_table = table.split(".", 1)
            if database is not None and database != table_database:
                raise ValueError(
                    f"The database {table_database!r} in the table name conflicts with database={database!r}"
                )
            database, table = table_database, plain_table
        if not table:
            raise ValueError("Table name cannot be empty")
        if database is None and not cfg.database and _has_schema_layer(cfg):
            raise ValueError("This connection has no default database bound; select the "
                             "database (schema) to export from with the database parameter")

        engine = self.pool.get(project, connection, cfg, schema=database, database=pg_database)
        info = engines.describe_table(engine, table, database)
        available = [str(c["name"]) for c in info["columns"]]
        selected = fields or available
        if not selected:
            raise ValueError(f"Table {table!r} has no columns to export")
        if len(selected) != len(set(selected)):
            raise ValueError("Export columns cannot contain duplicates")
        unknown = [name for name in selected if name not in available]
        if unknown:
            raise ValueError(
                f"Column(s) not found in table {table}: {', '.join(unknown)}"
                f" (available: {', '.join(available)})"
            )

        preparer = engine.dialect.identifier_preparer
        quoted_fields = ", ".join(preparer.quote(name) for name in selected)
        quoted_table = (
            f"{preparer.quote(database)}." if database else ""
        ) + preparer.quote(table)
        sql = f"SELECT {quoted_fields} FROM {quoted_table}"
        run_sql, _, _ = engines.paginate_sql(sql, cfg.engine, limit + 1, 0)
        result = self._read(
            project,
            connection,
            cfg,
            run_sql,
            caller,
            limit,
            schema=database,
            database=pg_database,
            mask=True,
        )
        data, media_type, ext = export_result(result["columns"], result["rows"], fmt)
        summary = {
            "project": project,
            "connection": connection,
            "database": database or cfg.database,
            "table": table,
            "fields": result["columns"],
            "row_count": result["row_count"],
            "requested_limit": limit,
            "truncated": result["truncated"],
            "format": fmt,
            "masked_columns": result.get("masked_columns", []),
        }
        return self._save_mcp_export(data, media_type, ext, summary)

    _MCP_EXPORT_TTL_S = 3600

    def _save_mcp_export(
        self, data: bytes, media_type: str, ext: str, summary: dict
    ) -> dict:
        """保存 MCP 导出产物并返回短期 bearer-token 下载链接。"""
        import re
        import secrets
        import json
        import time
        from datetime import UTC, datetime
        from pathlib import Path
        from urllib.parse import quote

        if not self.data_dir:
            raise QueryRejected("Export file storage is not enabled (the service has no data_dir configured)")
        root = Path(self.data_dir) / "mcp_exports"
        root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        self.purge_mcp_exports()

        token = secrets.token_hex(24)
        artifact_dir = root / token
        artifact_dir.mkdir(mode=0o700)
        raw_name = "_".join(
            str(part) for part in (
                summary.get("connection"), summary.get("database"), summary.get("table")
            ) if part
        ) or "quay_export"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._") or "quay_export"
        filename = f"{safe_name}.{ext}"
        path = artifact_dir / filename
        path.write_bytes(data)
        path.chmod(0o600)

        relative_url = f"/exports/{token}/{quote(filename)}"
        base = self.base_url.rstrip("/")
        result = {
            **summary,
            "token": token,
            "filename": filename,
            "media_type": media_type,
            "byte_size": len(data),
            "expires_at": int(now + self._MCP_EXPORT_TTL_S),
            "download_url": f"{base}{relative_url}" if base else relative_url,
            "agent_instruction": (
                "When needed, download download_url directly to the target location with "
                "code; do not read the file content or put it into the model's context."
            ),
        }
        metadata = {
            **result,
            "created_at": datetime.fromtimestamp(now, UTC).isoformat(),
        }
        metadata_path = artifact_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        metadata_path.chmod(0o600)
        return result

    def purge_mcp_exports(self) -> int:
        """删除超过 TTL 的临时导出目录，返回删除数量。"""
        import shutil
        import time
        from pathlib import Path

        if not self.data_dir:
            return 0
        root = Path(self.data_dir) / "mcp_exports"
        if not root.is_dir():
            return 0
        now = time.time()
        removed = 0
        for child in root.iterdir():
            try:
                if child.is_dir() and now - child.stat().st_mtime > self._MCP_EXPORT_TTL_S:
                    shutil.rmtree(child)
                    removed += 1
            except OSError:
                continue
        return removed

    def list_mcp_exports(self) -> list[dict]:
        """列出尚未过期的临时导出，供管理后台查看。"""
        import json
        import time
        from pathlib import Path

        self.purge_mcp_exports()
        if not self.data_dir:
            return []
        root = Path(self.data_dir) / "mcp_exports"
        if not root.is_dir():
            return []
        out = []
        for child in root.iterdir():
            try:
                meta = json.loads((child / "metadata.json").read_text(encoding="utf-8"))
                if int(meta.get("expires_at") or 0) <= int(time.time()):
                    continue
                # base_url 可能因重启/端口变化而改变，展示时用当前地址重新生成。
                filename = str(meta["filename"])
                relative = f"/exports/{child.name}/{filename}"
                meta["download_url"] = f"{self.base_url.rstrip('/')}{relative}" \
                    if self.base_url else relative
                out.append(meta)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(out, key=lambda item: item.get("created_at", ""), reverse=True)

    def delete_mcp_export(self, token: str) -> bool:
        """按随机 token 删除一条临时导出。"""
        import re
        import shutil
        from pathlib import Path

        if not self.data_dir or not re.fullmatch(r"[0-9a-f]{48}", token):
            return False
        path = Path(self.data_dir) / "mcp_exports" / token
        if not path.is_dir():
            return False
        shutil.rmtree(path)
        return True

    def preview_mcp_export(self, token: str, max_rows: int = 100) -> dict | None:
        """读取临时导出供人类后台预览；不会进入 MCP/agent 上下文。"""
        import csv
        import io
        import json
        import re
        from pathlib import Path

        if not self.data_dir or not re.fullmatch(r"[0-9a-f]{48}", token):
            return None
        artifact_dir = Path(self.data_dir) / "mcp_exports" / token
        try:
            meta = json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))
            filename = str(meta["filename"])
            path = self.resolve_mcp_export(token, filename)
            if path is None:
                return None
            fmt = str(meta.get("format") or "").lower()
            columns: list[str] = []
            rows: list[list] = []
            raw: str | None = None
            total = 0
            if fmt == "csv":
                parsed = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
                columns = parsed[0] if parsed else []
                total = max(0, len(parsed) - 1)
                rows = parsed[1:max_rows + 1]
            elif fmt == "json":
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    columns = list(data[0]) if isinstance(data[0], dict) else ["value"]
                    total = len(data)
                    rows = [
                        [item.get(c) for c in columns] if isinstance(item, dict) else [item]
                        for item in data[:max_rows]
                    ]
            elif fmt == "xlsx":
                from openpyxl import load_workbook

                wb = load_workbook(path, read_only=True, data_only=True)
                ws = wb.active
                iterator = ws.iter_rows(values_only=True)
                columns = [str(v or "") for v in next(iterator, ())]
                for row in iterator:
                    total += 1
                    if len(rows) < max_rows:
                        rows.append(list(row))
                wb.close()
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
                raw = text[:50_000]
                total = len(text.splitlines())
            return {
                "metadata": meta,
                "columns": columns,
                "rows": rows,
                "raw": raw,
                "total_rows": total,
                "truncated": total > max_rows or (raw is not None and len(raw) < path.stat().st_size),
            }
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def resolve_mcp_export(self, token: str, filename: str):
        """校验短期下载 token，返回产物路径；任何异常都视为不存在。"""
        import re
        import time
        from pathlib import Path

        if not self.data_dir or not re.fullmatch(r"[0-9a-f]{48}", token):
            return None
        if filename != Path(filename).name:
            return None
        artifact_dir = Path(self.data_dir) / "mcp_exports" / token
        path = artifact_dir / filename
        try:
            if (
                not path.is_file()
                or time.time() - artifact_dir.stat().st_mtime > self._MCP_EXPORT_TTL_S
            ):
                return None
        except OSError:
            return None
        return path

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

    def admin_explain(
        self, project: str, connection: str, sql: str, caller: CallerInfo,
        schema: str | None = None, database: str | None = None,
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
        # 计划前缀与输出格式由驱动决定（drivers/<engine>.py 的 explain_json_prefix/explain_format）
        from .drivers import get_driver

        drv = get_driver(cfg.engine)
        prefix = drv.explain_json_prefix
        if prefix is None:
            raise QueryRejected(f"引擎 {cfg.engine} 不支持 EXPLAIN")
        fmt = drv.explain_format
        # 写语句（DELETE/UPDATE/INSERT/DDL）的 EXPLAIN 需要对应表的写权限：reader（只读账号）
        # 会被 DB 以权限不足拒绝（MySQL 1142）。EXPLAIN 不带 ANALYZE 不真正执行，改用 writer
        # 账号取计划是安全的；无独立 writer 时退回 reader（sqlite 等无账号概念场景）。
        role = "writer" if (not verdict.readonly and cfg.writer is not None) else "reader"

        def _run() -> dict:
            engine = self.pool.get(project, connection, cfg, role=role, schema=schema,
                                   database=database)
            # JSON 计划可能很长，放开单元格截断
            res = engines.run_query(engine, prefix + stmt, max_rows=500, max_cell_chars=1_000_000)
            return {"format": fmt, "columns": res.columns, "rows": res.rows}

        return self._audited(project, connection, cfg, "explain", stmt, caller, _run)

    # ---------- 分析工作台（DuckDB 沙箱，设计见 ANALYSIS.md）----------
    # 边界：工作区内任意 SQL 自由执行（本地草稿纸，不需审批）；
    # 从源库取数走 _read（reader 只读 + 审计 + 行数上限），生产红线不动。

    def _require_analysis(self):
        if self.analysis is None:
            raise QueryRejected("The analysis workbench is not enabled (requires serve mode)")
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
        database: str | None = None,
    ) -> dict:
        """从某连接把查询结果快照进工作区（source_sql 也可为 `SELECT * FROM 表`）。

        只读校验 + 注入 LIMIT 上限 + reader 拉数（全程审计），随后落成 DuckDB 表。
        """
        from .analysis import DEFAULT_SNAPSHOT_ROWS, MAX_SNAPSHOT_ROWS
        store = self._require_analysis()
        cfg = self.config.get_connection(project, connection)
        database = self.resolve_pg_database(project, connection, database, cfg)
        if not classify(source_sql, cfg.engine).readonly:
            raise QueryRejected("Snapshot import only supports read-only queries (SELECT/SHOW/...)")
        n = min(limit or DEFAULT_SNAPSHOT_ROWS, MAX_SNAPSHOT_ROWS)
        run_sql, _, _ = engines.paginate_sql(source_sql, cfg.engine, n, 0)
        result = self._read(project, connection, cfg, run_sql, caller, n, schema=schema,
                            database=database)
        spec = {"kind": "connection", "project": project, "connection": connection,
                "sql": source_sql, "limit": n, "schema": schema}
        if database:  # 只在指定了才记，老 provenance 与非 PG 源的形状保持不变
            spec["database"] = database
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
            raise WorkflowError("Workflow storage is not enabled (requires serve mode)")
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
                    f"Workflow {name!r} is a DAG created on the admin-backend canvas and "
                    "cannot be overwritten; use a different name, or have the user edit "
                    "it on the backend")
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
            stmts = [{"node": None, "name": f"step {i}", "sql": s}
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

    def workflow_preview_columns(self, workspace: str, graph: dict, node_id: str,
                                 caller: CallerInfo, refresh: bool = False) -> dict:
        """拿目标节点输出的列 schema（供上游 schema 感知用）。

        懒建策略：目标节点在工作区里已有对应 view/table 就直接 DESCRIBE；
        否则递归确保它依赖的 sources 已导入 + 前置 steps 已建 view，再 DESCRIBE。
        refresh=True 时强制重建（用于用户点「刷新 schema」）。
        编译异常或上游取数失败 → 返回 {columns:[], error:...} 而非抛（让前端友好提示）。
        """
        from .workflows import WorkflowError, compile_graph
        try:
            plan = compile_graph(graph)
        except WorkflowError as e:
            return {"columns": [], "error": f"编译流程失败：{e}"}
        # output 节点没有物化 view，改预览它的上游（"预览这一步输出" 等价于预览上游）
        target = _preview_target_for(graph, node_id)
        if target is None:
            return {"columns": [], "error": f"节点 {node_id!r} 不在流程中或未连接上游"}
        target_name, _target_node = target
        store = self._require_analysis()
        # 懒模式：目标已存在直接 DESCRIBE
        if not refresh and _dataset_exists(store, workspace, target_name):
            return {"columns": _describe_columns(store, workspace, target_name)}
        # 递归物化：只做目标依赖链上的 sources/steps
        needed = _plan_prefix_for(plan, target_name)
        for src in needed["sources"]:
            dataset = src["dataset"]
            if not refresh and _dataset_exists(store, workspace, dataset):
                continue
            try:
                if src.get("kind") == "file":
                    self.analysis_import_file(workspace, dataset, src["path"], caller)
                else:
                    self.analysis_import(workspace, dataset, src["project"], src["connection"],
                                         src["sql"], caller,
                                         limit=src.get("limit"), schema=src.get("schema"),
                                         database=src.get("database"))
            except Exception as e:  # noqa: BLE001
                return {"columns": [], "error": f"上游节点「{dataset}」取数失败：{e}"}
        for st in needed["steps"]:
            step_name = st.get("name")
            if not step_name:
                continue
            if not refresh and _dataset_exists(store, workspace, step_name):
                continue
            try:
                self.analysis_sql(workspace, st["sql"], caller)
            except Exception as e:  # noqa: BLE001
                return {"columns": [], "error": f"节点「{step_name}」构建失败：{e}"}
        return {"columns": _describe_columns(store, workspace, target_name)}

    def workflow_preview_node(self, workspace: str, graph: dict, node_id: str,
                              caller: CallerInfo, limit: int = 100) -> dict:
        """预览目标节点的输出前 N 行（抽屉的「预览」标签用）。

        先确保依赖链已物化（复用 preview_columns 的懒模式），再 SELECT * LIMIT N。
        output 节点预览它的上游（output 不物化成 view）。
        返回 analysis_sql 的标准 dict（columns/rows/row_count）或 {error}。
        """
        cols_result = self.workflow_preview_columns(workspace, graph, node_id, caller)
        if cols_result.get("error"):
            return {"columns": [], "rows": [], "row_count": 0, "error": cols_result["error"]}
        target = _preview_target_for(graph, node_id)
        if target is None:
            return {"columns": [], "rows": [], "row_count": 0,
                    "error": f"节点 {node_id!r} 不在流程中或未连接上游"}
        target_name, _ = target
        try:
            n = max(1, min(int(limit or 100), 1000))
        except (TypeError, ValueError):
            n = 100
        return self.analysis_sql(workspace,
                                 f'SELECT * FROM "{target_name}" LIMIT {n}', caller)

    def _run_plan(self, workspace: str, sources: list[dict], steps: list[dict],
                  caller: CallerInfo) -> dict:
        """执行计划：重拉 sources → 顺序执行 steps（带 node id 供画布标注状态）。"""
        done: list[dict] = []
        for src in sources:
            label = f"Import {src.get('dataset')}"
            node = src.get("node")
            try:
                if src.get("kind") == "file":
                    out = self.analysis_import_file(workspace, src["dataset"], src["path"], caller)
                else:
                    out = self.analysis_import(workspace, src["dataset"], src["project"],
                                               src["connection"], src["sql"], caller,
                                               limit=src.get("limit"), schema=src.get("schema"),
                                               database=src.get("database"))
                done.append({"step": label, "node": node, "ok": True, "rows": out["rows"]})
            except Exception as e:  # noqa: BLE001
                done.append({"step": label, "node": node, "ok": False, "error": str(e)})
                return {"steps": done, "output": None, "ok": False}
        # output 保留旧语义（最后一个有 columns 的结果作主输出预览，向后兼容脚本式 workflow）
        # outputs 是新增的多 output 收集：只包含图上真正的 output 节点，供前端展示每个副产出
        output = None
        outputs: list[dict] = []
        for st in steps:
            label = f"{st['name']}: {st['sql'][:60]}"
            try:
                res = self.analysis_sql(workspace, st["sql"], caller)
                done.append({"step": label, "node": st.get("node"), "ok": True,
                             "rows": res["row_count"]})
                if res["columns"]:
                    output = res
                # 只把图里的 output 节点收集进 outputs（compile_graph 标了 is_output）
                if st.get("is_output") and res.get("columns"):
                    outputs.append({"node": st.get("node"), "name": st.get("name"), **res})
            except Exception as e:  # noqa: BLE001
                done.append({"step": label, "node": st.get("node"), "ok": False, "error": str(e)})
                return {"steps": done, "output": output, "outputs": outputs, "ok": False}
        return {"steps": done, "output": output, "outputs": outputs, "ok": True}

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
        rollback_note: str = "",
        database: str | None = None,
    ) -> dict:
        """写操作统一入口。database 仅 PG：在哪个 database 上执行（随审批单存下）。

        - 只读语句：直接执行（等价于 query）；
        - 写操作 + 无 change_id：评估风险、生成审批单、拒绝并返回 change_id；
        - 写操作 + 有 change_id：校验审批单后执行**审批单里存储的 SQL**。
        """
        cfg = self.config.get_connection(project, connection)
        if self.approvals is None:
            raise QueryRejected("The approval subsystem is not enabled; cannot execute write operations")

        # 带 change_id：一律走审批单核销（指纹校验 + 原子核销），不看重新分类结果——
        # 否则可构造「首提判写→生成审批单、重提判读→走 query() 绕开 consume 的指纹与核销」（H5）。
        resolved = self.resolve_pg_database(project, connection, database, cfg)
        if change_id is not None:
            # 明确指定了库（哪怕指定的就是默认库）才参与核对；没指定就按审批单记的库执行
            db_check = None if database is None else (resolved or "")
            return self._execute_approved(project, connection, cfg, sql, change_id, caller,
                                          database=db_check)
        database = resolved

        verdict = classify(sql, cfg.engine)
        if verdict.statement_kind == "ParseError":
            # 语法预检放在建审批单之前：真语法错的 SQL 不该浪费一次人工审批
            # （审批人批了也只会在 DB 上 1064）。DB 复核认这条语法（或无法复核）时
            # 静默返回，继续走原有的「默认拒绝 → 审批流」，保留方言兜底能力。
            self._syntax_precheck(project, connection, cfg, sql, caller, "execute",
                                  database=database)
        if verdict.readonly:
            return {"status": "executed", "readonly": True,
                    **self.query(project, connection, sql, caller, database=database)}
        return self._request_approval(project, connection, cfg, sql, reason, caller,
                                      rollback_note=rollback_note, database=database)

    def _request_approval(
        self,
        project: str,
        connection: str,
        cfg: ConnectionConfig,
        sql: str,
        reason: str,
        caller: CallerInfo,
        rollback_note: str = "",
        database: str | None = None,
    ) -> dict:
        report = assess(sql, cfg.engine,
                        self._meta_provider(project, connection, cfg, database=database))
        report_dict = report.to_dict()
        plan = self._try_explain(project, connection, cfg, sql, database=database)
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
            rollback_note=rollback_note,
            database=database,
        )
        rec = self._base_record(project, connection, cfg, "execute", sql, caller)
        rec.change_id = change.id
        rec.status = "rejected"
        rec.detail = f"需人工授权，已生成审批单 #{change.id}（风险 {report.level}）"
        if database:
            rec.detail += f" db={database}"
        self.store.record(rec)
        # 审批页直达链接：既随通知下发，也回给 agent（agent 把它贴进会话，人点一下就到
        # 审批页，省掉「自己去后台翻审批列表」这一步）
        from .notify import approval_deeplink  # noqa: PLC0415
        base_url = str(self._setting("admin_base_url") or "http://127.0.0.1:8100")
        approval_url = approval_deeplink(base_url, change.id)
        action_url = self._issue_action_link(change.id, base_url)
        # 需要人为介入 → 主动发通知（安静即正常：不通知的话可能长时间没人看到）
        # meta.deeplink 让各渠道适配跳转：Bark→url 字段、企微→markdown 链接、
        # 飞书→post 富文本 a 节点、macOS→body 附 URL 文本、站内 inbox→前端点击
        try:
            sql_preview = " ".join(sql.split())[:120]
            self.notifier.send(
                title=f"新审批单 #{change.id} · {project}/{connection}"
                      + (f"/{database}" if database else ""),
                body=f"风险 {report.level} · agent={caller.agent or 'unknown'}\nSQL: {sql_preview}",
                meta={"kind": "approval_created", "change_id": change.id,
                      "project": project, "connection": connection,
                      "risk_level": report.level,
                      "deeplink": approval_url,
                      **({"action_url": action_url} if action_url else {})},
            )
        except Exception:  # noqa: BLE001
            logger.exception("notify approval_created failed")
        return {
            "status": "approval_required",
            "change_id": change.id,
            **({"pg_database": database} if database else {}),
            "approval_url": approval_url,
            "risk": report_dict,
            "message": (
                f"This operation was assessed as requiring human authorization (risk level "
                f"{report.level}). Approval ticket #{change.id} has been generated — give "
                f"the approval link {approval_url} to the user, and use "
                f"wait_for_change({change.id}) to wait for the human decision (it will "
                f"auto-execute once approved). The ticket is valid for 60 minutes."
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
        database: str | None = None,
    ) -> dict:
        """database：重提时声明的执行库（None=未声明）。只用于与审批单核对，
        实际执行库永远取审批单里存的那个。"""
        rec = self._base_record(project, connection, cfg, "execute", sql, caller)
        rec.change_id = change_id
        # 同步型审批单存的是计划而非可执行 SQL，走这条路会把计划文本当 SQL 发给 DB
        if self.approvals.get(change_id).kind == KIND_SYNC:
            return {"status": "rejected", "change_id": change_id,
                    "reason": f"Change #{change_id} is a table-sync plan; use "
                              f"sync_table(change_id={change_id}, ...) to execute it"}
        try:
            change = self.approvals.consume(
                change_id, fingerprint(sql, cfg.engine), (project, connection),
                database=database,
            )
        except ApprovalError as e:
            rec.status = "rejected"
            rec.detail = str(e)
            self.store.record(rec)
            return {"status": "rejected", "change_id": change_id, "reason": str(e)}

        # 执行审批单里存储的 SQL（不是 agent 重提的文本），用 writer 账号

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(project, connection, cfg, role="writer",
                                   database=change.database or None)
            return engines.run_write(engine, change.sql)

        try:
            result = self._run_touching_db(project, connection, _do, rec)
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
        if change.database:
            rec.detail += f" db={change.database}"
        rec.row_count = result.row_count
        rec.duration_ms = result.duration_ms
        self.store.record(rec)
        payload = {
            "status": "executed",
            "change_id": change_id,
            "affected_rows": result.row_count,
            "duration_ms": result.duration_ms,
            "executed_by": caller.agent,
        }
        # 回填到审批单：等待中的 agent 与后台详情页据此知道「已落地、影响多少行」
        self.approvals.record_execution(change_id, payload)
        return payload

    def approve_and_execute_change(
        self, change_id: int, decided_by: str, note: str = ""
    ) -> dict:
        """后台「批准并立即执行」：批准后当场核销执行，人点一次即落地。

        与 agent 带 change_id 重提走的是同一条 `_execute_approved` 路径——执行的仍是审批单
        里存储的 SQL、仍是原子核销、审计照记，只是触发方从 agent 换成后台操作者。
        等待中的 agent 会从 exec_result 收到执行结果，不必再重提（重提会因已核销被拒）。
        """
        if self.approvals is None:
            raise QueryRejected("审批子系统未启用")
        change = self.approvals.approve(change_id, decided_by, note)
        cfg = self.config.get_connection(change.project, change.connection)
        caller = CallerInfo(agent="admin-ui", session_id=f"approve:{decided_by}")
        if change.kind == KIND_SYNC:
            return self._execute_sync_approved(
                change_id, change.fingerprint, (change.project, change.connection), caller)
        return self._execute_approved(
            change.project, change.connection, cfg, change.sql, change_id, caller
        )

    # ---------- 跨连接表同步（线上库 → 本地库）----------

    def result_budget(self) -> SessionBudget:
        """会话结果配额器，上限已同步为当前设置值（改设置即时生效）。"""
        value = self._setting("agent_session_budget_chars")
        try:
            self.session_budget.limit_chars = max(int(value), 0)
        except (TypeError, ValueError):
            self.session_budget.limit_chars = DEFAULT_SESSION_BUDGET_CHARS
        return self.session_budget

    def guide_on_first_call(self) -> bool:
        """会话首次调用工具时是否附带使用说明（系统设置，默认开）。"""
        value = self._setting("agent_guide_on_first_call")
        return True if value is None else bool(value)

    def sync_max_rows(self) -> int:
        """同步单次行数硬上限：系统设置优先，最终受 sync.MAX_SYNC_ROWS 封顶。"""
        value = self._setting("sync_max_rows")
        try:
            return min(int(value), sync.MAX_SYNC_ROWS)
        except (TypeError, ValueError):
            return min(DEFAULT_SYNC_MAX_ROWS, sync.MAX_SYNC_ROWS)

    def sync_max_bytes(self) -> int:
        """同步单次体积上限（估算字节）：行数管不住宽表，这道闸门管的是「搬多少数据」。"""
        value = self._setting("sync_max_bytes")
        try:
            return max(int(value), 1)
        except (TypeError, ValueError):
            return DEFAULT_SYNC_MAX_BYTES

    def _sync_endpoints(self, spec: "sync.SyncSpec") -> tuple[ConnectionConfig, ConnectionConfig]:
        """取源/目标连接配置并做「这条同步允不允许发生」的守卫。

        目标 prod 一律拒绝：本工具是批量灌数，与「一条被逐字审阅的 SQL」不是一个风险量级，
        真要往生产写请走 execute（同样审批，但人看到的是确切语句）。
        """
        src = self.config.get_connection(spec.source_project, spec.source_connection)
        dst = self.config.get_connection(spec.target_project, spec.target_connection)
        # 指定了 PG 库就先确认它真实存在（也挡住在非 PG 连接上误传），再去建连接
        self.resolve_pg_database(spec.source_project, spec.source_connection,
                                 spec.source_pg_database, src)
        self.resolve_pg_database(spec.target_project, spec.target_connection,
                                 spec.target_pg_database, dst)
        if src.engine not in sync.SOURCE_ENGINES:
            raise QueryRejected(f"Engine {src.engine} is not supported as a sync source "
                                f"(supported: {'/'.join(sync.SOURCE_ENGINES)})")
        if dst.engine not in sync.TARGET_ENGINES:
            raise QueryRejected(f"Engine {dst.engine} is not supported as a sync target "
                                f"(supported: {'/'.join(sync.TARGET_ENGINES)}; ClickHouse is "
                                f"read-only in this project, Redis does not participate "
                                f"in table sync)")
        if dst.environment == "prod":
            raise QueryRejected(
                f"Refusing to sync data to the production connection "
                f"{spec.target_project}/{spec.target_connection}. This tool is for "
                "syncing data to local/dev databases; to write to production, submit the "
                "specific SQL via execute instead."
            )
        if dst.engine != "sqlite" and dst.writer is None:
            raise QueryRejected(
                f"Target connection {spec.target_project}/{spec.target_connection} has no "
                "writer account configured, so it cannot be written to. Add a writer "
                "account for it in the admin backend first."
            )
        return src, dst

    def _build_sync_plan(self, spec: "sync.SyncSpec", caller: CallerInfo) -> dict:
        """构造同步计划：查源表结构/建表语句、查目标表是否存在、算出要复制的列。

        只做只读探查，不写任何东西。返回的 dict 既是 dry_run 的结果，也是审批单的 payload。
        """
        sync.validate_spec(spec)
        src, dst = self._sync_endpoints(spec)

        src_info = self.describe_table(spec.source_project, spec.source_connection,
                                       spec.source_table, caller, schema=spec.source_database,
                                       database=spec.source_pg_database)
        src_columns = [c["name"] for c in src_info["columns"]]

        target_tables = self.list_tables(spec.target_project, spec.target_connection, caller,
                                         schema=spec.target_database,
                                         database=spec.target_pg_database)
        target_exists = spec.target_table in target_tables
        if spec.ddl == sync.DDL_SKIP and not target_exists:
            raise QueryRejected(
                f"Target table {spec.target_table} does not exist, and ddl=skip does not "
                "create it. Use ddl=create_if_missing to have this tool create it from the "
                "source structure, or create it manually first."
            )

        warnings: list[str] = []
        ddl_sql = ""
        # recreate 一定要重建；create_if_missing 只在目标表不存在时才需要建表语句
        if spec.ddl == sync.DDL_RECREATE or (
            spec.ddl == sync.DDL_CREATE_IF_MISSING and not target_exists
        ):
            source_ddl = self.get_table_ddl(spec.source_project, spec.source_connection,
                                            spec.source_table, caller,
                                            schema=spec.source_database,
                                            database=spec.source_pg_database)
            ddl_sql, warnings = sync.rewrite_ddl(source_ddl, src.engine, dst.engine,
                                                 spec.source_table, spec.target_table)

        # 要复制的列：目标表重建时以源表为准；否则取源/目标列的交集
        columns = src_columns
        if spec.data != sync.DATA_NONE and target_exists and spec.ddl != sync.DDL_RECREATE:
            dst_info = self.describe_table(spec.target_project, spec.target_connection,
                                           spec.target_table, caller,
                                           schema=spec.target_database,
                                           database=spec.target_pg_database)
            dst_columns = {c["name"] for c in dst_info["columns"]}
            columns = [c for c in src_columns if c in dst_columns]
            missing = [c for c in src_columns if c not in dst_columns]
            if missing:
                warnings.append(f"Target table is missing these source columns, so they "
                                f"will not be synced: {', '.join(missing)}")
            if not columns:
                raise QueryRejected(
                    f"Source table {spec.source_table} and target table {spec.target_table} "
                    f"have no columns with matching names; data cannot be synced. "
                    f"Source columns: {', '.join(src_columns)}"
                )
        if spec.data == sync.DATA_NONE:
            columns = []

        # 行数量级只用于让审批人有个"全表多大 / 我取多少"的概念，取不到就不显示
        meta = self._meta_provider(spec.source_project, spec.source_connection, src,
                                   database=spec.source_pg_database,
                                   schema=spec.source_database)(spec.source_table)
        row_estimate = getattr(meta, "row_estimate", None) if meta is not None else None
        plan_text = sync.render_plan(
            spec, src.environment, src.engine, dst.environment, dst.engine,
            columns, ddl_sql, warnings, target_exists, row_estimate,
        )
        return {
            "spec": spec.to_dict(),
            "ddl_sql": ddl_sql,
            "columns": columns,
            "warnings": warnings,
            "target_exists": target_exists,
            "source_engine": src.engine,
            "target_engine": dst.engine,
            "plan_text": plan_text,
            "risk": sync.assess_plan(spec, dst.environment, src.environment),
        }

    def sync_table(
        self, spec: "sync.SyncSpec", caller: CallerInfo, reason: str = "",
        dry_run: bool = False, change_id: int | None = None,
    ) -> dict:
        """表同步统一入口，形状对齐 execute：计划 → 审批单 → 带 change_id 核销执行。

        - dry_run：只回计划，不生成审批单（agent 想先给用户看看要同步什么）；
        - 无 change_id：构造计划、生成审批单并返回 approval_url；
        - 有 change_id：核销审批单并执行**审批单里存的那份计划**（重提的 spec 只作指纹校验）。
        """
        if self.approvals is None:
            raise QueryRejected("The approval subsystem is not enabled; cannot execute table sync")
        # 行数上限由服务端定，且首提与重提用同一套夹取规则 → 指纹一致
        spec = replace(spec, limit=max(1, min(spec.limit, self.sync_max_rows())))
        if change_id is not None:
            return self._execute_sync_approved(
                change_id, sync.spec_fingerprint(spec),
                (spec.target_project, spec.target_connection), caller)
        plan = self._build_sync_plan(spec, caller)
        if dry_run:
            return {"status": "planned", "plan": plan["plan_text"],
                    "columns": plan["columns"], "warnings": plan["warnings"],
                    "target_exists": plan["target_exists"], "risk": plan["risk"]}
        dst = self.config.get_connection(spec.target_project, spec.target_connection)
        if not dst.sync_requires_approval:
            # 目标是本地/开发库：动的不是线上数据，直接执行（仍建审批单留痕 + 审计）
            return self._run_sync_without_approval(spec, plan, reason, caller)
        return self._request_sync_approval(spec, plan, reason, caller)

    # 一次结构同步最多多少张表：整库重建常有几十张，20 张太碎；再多就该分批，
    # 免得一次调用里跑上百条 DDL、中途失败时难以判断做到哪儿了。
    MAX_SYNC_DDL_TABLES = 50

    def sync_table_ddls(
        self, source_project: str, source_connection: str,
        target_project: str, target_connection: str,
        tables: list[str], caller: CallerInfo, *,
        source_database: str | None = None, target_database: str | None = None,
        source_pg_database: str | None = None, target_pg_database: str | None = None,
        ddl: str = sync.DDL_CREATE_IF_MISSING, reason: str = "", dry_run: bool = False,
    ) -> dict:
        """批量**只同步结构、不同步数据**（在本地重建线上库的表结构）。

        实现上就是对每张表跑一次 `sync_table(data=none)`——**刻意不另开一条执行路径**：
        建表语句的生成/转写、目标不能是 prod、审批与审计，全部沿用同一套，
        这里只负责「按表循环 + 汇总结果」。

        单张表失败不中断整批（表名写错、目标已存在同名视图等），逐表如实报状态；
        只有连接级故障才中断——那对后面每张表都一样，接着试没有意义。
        """
        if ddl not in (sync.DDL_CREATE_IF_MISSING, sync.DDL_RECREATE):
            raise ValueError(
                f"Structure sync's ddl must be {sync.DDL_CREATE_IF_MISSING} or "
                f"{sync.DDL_RECREATE} ({sync.DDL_RECREATE} will DROP the target table "
                f"first), got {ddl!r}")
        names = [t.strip() for t in tables if t and t.strip()]
        if not names:
            raise ValueError("Give at least one table name")
        if len(names) > self.MAX_SYNC_DDL_TABLES:
            raise ValueError(
                f"Can sync the structure of at most {self.MAX_SYNC_DDL_TABLES} tables per "
                f"call ({len(names)} given); split it into batches")

        results = []
        for name in names:
            spec = sync.SyncSpec(
                source_project=source_project, source_connection=source_connection,
                source_table=name,
                target_project=target_project, target_connection=target_connection,
                target_table=name,
                ddl=ddl, data=sync.DATA_NONE, limit=1,
                source_database=source_database, target_database=target_database,
                source_pg_database=(source_pg_database or "").strip() or None,
                target_pg_database=(target_pg_database or "").strip() or None,
            )
            try:
                out = self.sync_table(spec, caller, reason=reason, dry_run=dry_run)
                results.append({"table": name, **out})
            except ConnectionUnavailable:
                raise
            except Exception as e:  # noqa: BLE001
                results.append({"table": name, "status": "failed",
                                "error": f"{type(e).__name__}: {e}"})
        done = [r for r in results if r.get("status") in ("executed", "consumed", "planned")]
        return {
            "source": f"{source_project}/{source_connection}",
            "target": f"{target_project}/{target_connection}",
            "ddl": ddl,
            "requested": len(names),
            "succeeded": len(done),
            "tables": results,
        }

    def _create_sync_change(
        self, spec: "sync.SyncSpec", plan: dict, reason: str, caller: CallerInfo,
    ):  # noqa: ANN201 - ChangeRequest，避免为类型再引一次 approvals
        """把同步计划落成审批单记录（审批与免审批两条路都用它，保证留痕一致）。"""
        dst = self.config.get_connection(spec.target_project, spec.target_connection)
        return self.approvals.create(
            project=spec.target_project,
            connection=spec.target_connection,
            environment=dst.environment,
            engine=dst.engine,
            sql=plan["plan_text"],
            fingerprint=sync.spec_fingerprint(spec),
            reason=reason,
            risk_level=plan["risk"]["level"],
            risk_report=plan["risk"],
            agent=caller.agent,
            session_id=caller.session_id,
            kind=KIND_SYNC,
            payload=plan,
        )

    def _run_sync_without_approval(
        self, spec: "sync.SyncSpec", plan: dict, reason: str, caller: CallerInfo,
    ) -> dict:
        """本地/开发目标的同步：不打扰人，直接批准并执行。

        仍走审批单对象（自动批准 + 原子核销），这样执行的依然是「记录在案的那份计划」，
        审计、执行结果回填、后台可回溯都与人工审批路径完全一致，只是省掉了等人这一步。
        """
        change = self._create_sync_change(spec, plan, reason, caller)
        self.approvals.approve(change.id, decided_by="auto", note="Target is local/dev, approval skipped")
        change = self.approvals.consume(
            change.id, sync.spec_fingerprint(spec),
            (spec.target_project, spec.target_connection))
        result = self._run_sync_change(change, caller)
        result["warnings"] = plan["warnings"]
        result["auto_approved"] = True
        return result

    def _request_sync_approval(
        self, spec: "sync.SyncSpec", plan: dict, reason: str, caller: CallerInfo,
    ) -> dict:
        """为同步计划生成审批单。与 _request_approval 同构，只是审批的是计划而非 SQL。

        审批单挂在**目标连接**下（写发生在那里），sql 字段存人可读的计划文本、payload 存
        结构化计划——核销时执行 payload，与「执行的永远是审批单里存储的内容」一致。
        """
        dst = self.config.get_connection(spec.target_project, spec.target_connection)
        change = self._create_sync_change(spec, plan, reason, caller)
        rec = self._base_record(spec.target_project, spec.target_connection, dst,
                                "sync_write", plan["plan_text"], caller)
        rec.change_id = change.id
        rec.status = "rejected"
        rec.detail = f"需人工授权，已生成同步审批单 #{change.id}（风险 {plan['risk']['level']}）"
        self.store.record(rec)

        from .notify import approval_deeplink  # noqa: PLC0415
        base_url = str(self._setting("admin_base_url") or "http://127.0.0.1:8100")
        approval_url = approval_deeplink(base_url, change.id)
        action_url = self._issue_action_link(change.id, base_url)
        try:
            self.notifier.send(
                title=f"新同步审批单 #{change.id} · → {spec.target_project}/{spec.target_connection}",
                body=(f"{spec.source_project}/{spec.source_connection}.{spec.source_table} → "
                      f"{spec.target_table}\n最多 {spec.limit} 行 · 结构 {spec.ddl} · 数据 {spec.data}"),
                meta={"kind": "approval_created", "change_id": change.id,
                      "project": spec.target_project, "connection": spec.target_connection,
                      "risk_level": plan["risk"]["level"], "deeplink": approval_url,
                      **({"action_url": action_url} if action_url else {})},
            )
        except Exception:  # noqa: BLE001
            logger.exception("notify sync approval_created failed")
        return {
            "status": "approval_required",
            "change_id": change.id,
            "approval_url": approval_url,
            "plan": plan["plan_text"],
            "warnings": plan["warnings"],
            "risk": plan["risk"],
            "message": (
                f"This table sync requires human authorization (risk level "
                f"{plan['risk']['level']}). Approval ticket #{change.id} has been "
                f"generated — give the approval link {approval_url} to the user; it will "
                f"auto-execute the plan once approved. The ticket is valid for 60 minutes."
            ),
        }

    def _execute_sync_approved(
        self, change_id: int, resubmit_fingerprint: str,
        connection_key: tuple[str, str], caller: CallerInfo,
    ) -> dict:
        """核销同步审批单并执行：建表（可选）→ 从源库取数 → 写入目标表。

        执行的是审批单 payload 里存的计划，**不是**重提参数——重提只用于指纹校验。
        数据在这里才从源库取，所以拿到的是批准时刻的最新数据，而不是提交时的快照。
        """
        change = self.approvals.get(change_id)
        if change.kind != KIND_SYNC:
            raise QueryRejected(
                f"Change #{change_id} is not a table-sync plan; use "
                f"execute(change_id={change_id}) to execute it")
        try:
            change = self.approvals.consume(change_id, resubmit_fingerprint, connection_key)
        except ApprovalError as e:
            # 未批准 / 已核销 / 过期 / 计划被改过：和 SQL 审批流一样，回成可读的 rejected
            cfg = self.config.get_connection(change.project, change.connection)
            rec = self._base_record(change.project, change.connection, cfg,
                                    "sync_write", change.sql, caller)
            rec.status = "rejected"
            rec.detail = str(e)
            self.store.record(rec)
            return {"status": "rejected", "change_id": change_id, "reason": str(e)}
        return self._run_sync_change(change, caller)

    def _run_sync_change(self, change, caller: CallerInfo) -> dict:  # noqa: ANN001
        plan = change.payload or {}
        spec = sync.SyncSpec.from_dict(plan.get("spec") or {})
        src_cfg = self.config.get_connection(spec.source_project, spec.source_connection)
        dst_cfg = self.config.get_connection(spec.target_project, spec.target_connection)
        columns: list[str] = plan.get("columns") or []
        ddl_sql: str = plan.get("ddl_sql") or ""

        rec = self._base_record(spec.target_project, spec.target_connection, dst_cfg,
                                "sync_write", change.sql, caller)
        rec.change_id = change.id
        steps: list[dict] = []
        started = time.monotonic()
        try:
            if spec.ddl == sync.DDL_RECREATE:
                drop_sql = sync.build_drop_sql(dst_cfg.engine, spec.target_table)
                self._sync_write_ddl(spec, dst_cfg, drop_sql, caller)
                steps.append({"step": "drop", "sql": drop_sql})
            if ddl_sql:
                self._sync_write_ddl(spec, dst_cfg, ddl_sql, caller)
                steps.append({"step": "create", "sql": ddl_sql})

            copied = 0
            source_truncated = False
            if spec.data != sync.DATA_NONE:
                # 多取一行用于判断"源侧其实还有更多"（同分页那套 +1 探测），取回后再截掉
                select_sql = sync.build_select_sql(
                    src_cfg.engine, spec.source_table, columns, spec.limit + 1,
                    spec.where, spec.order_by, spec.source_database,
                )
                # 纵深防御：WHERE/ORDER BY 是自由文本，拼出来的整条 SQL 必须仍是单条只读语句
                verdict = classify(select_sql, src_cfg.engine)
                if not verdict.readonly:
                    raise QueryRejected(
                        f"The generated fetch statement was judged non-read-only "
                        f"({verdict.reason}); check whether where / order_by smuggled in "
                        "a semicolon or a write operation."
                    )
                rows, source_truncated = self._sync_fetch(spec, src_cfg, select_sql,
                                                          columns, caller)
                copied = self._sync_write_rows(spec, dst_cfg, columns, rows, caller)
                steps.append({"step": "copy", "rows": copied,
                              "select": select_sql, "truncated": source_truncated})
        except Exception as e:
            rec.status = "error"
            rec.detail = f"同步失败（审批单 #{change.id}）: {type(e).__name__}: {e}"
            self.store.record(rec)
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        rec.status = "ok"
        rec.row_count = copied
        rec.duration_ms = duration_ms
        rec.detail = f"同步审批单 #{change.id} 已核销（审批人 {change.decided_by}）"
        self.store.record(rec)
        payload = {
            "status": "executed",
            "change_id": change.id,
            "affected_rows": copied,
            "duration_ms": duration_ms,
            "executed_by": caller.agent,
            "steps": steps,
            "source_truncated": source_truncated,
        }
        if source_truncated:
            # Truncated at fewer than limit rows = hit the byte-size budget rather than
            # the row cap; the two cases need different fixes.
            payload["note"] = (
                f"Only the first {copied} rows were synced: the cumulative data volume "
                f"hit the byte-size cap (system setting sync_max_bytes, currently "
                f"{self.sync_max_bytes()} bytes). This table's rows are wide — narrow "
                "where, reduce limit, or select only the columns you need."
                if copied < spec.limit else
                f"The source table has more than {spec.limit} matching rows; only the "
                f"first {copied} were synced. For more, narrow where or raise limit "
                "(bounded by the system setting sync_max_rows) and resubmit."
            )
        self.approvals.record_execution(change.id, payload)
        return payload

    def _sync_write_ddl(self, spec: "sync.SyncSpec", dst_cfg: ConnectionConfig,
                        ddl: str, caller: CallerInfo) -> None:
        def _do() -> "engines.QueryResult":
            engine = self.pool.get(spec.target_project, spec.target_connection, dst_cfg,
                                   role="writer", schema=spec.target_database,
                                   database=spec.target_pg_database)
            return engines.run_write(engine, ddl)

        self._audited(spec.target_project, spec.target_connection, dst_cfg,
                      "sync_write", ddl, caller, _do)

    def _sync_fetch(self, spec: "sync.SyncSpec", src_cfg: ConnectionConfig, select_sql: str,
                    columns: list[str], caller: CallerInfo) -> tuple[list[list], bool]:
        """从源库读要复制的行（reader 账号 + 审计）。

        用 fetch_rows_for_copy 而不是 _read：复制要的是驱动原值，`_read` 的 JSON 化/单元格
        截断/脱敏都会让写进目标库的数据与源库不一致（见该函数 docstring）。
        """
        result: dict = {}

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(spec.source_project, spec.source_connection, src_cfg,
                                   schema=spec.source_database,
                                   database=spec.source_pg_database)
            cols, rows, truncated = engines.fetch_rows_for_copy(
                engine, select_sql, spec.limit, max_bytes=self.sync_max_bytes())
            result["rows"] = rows
            result["truncated"] = truncated
            result["columns"] = cols
            return engines.QueryResult(columns=[], rows=[], row_count=len(rows),
                                       truncated=truncated, duration_ms=0)

        self._audited(spec.source_project, spec.source_connection, src_cfg,
                      "sync_read", select_sql, caller, _do)
        return result.get("rows", []), bool(result.get("truncated"))

    def _sync_write_rows(self, spec: "sync.SyncSpec", dst_cfg: ConnectionConfig,
                         columns: list[str], rows: list[list], caller: CallerInfo) -> int:
        detail = (f"SYNC INSERT INTO {spec.target_table} ({', '.join(columns)}) — "
                  f"{len(rows)} 行" + ("（先清空）" if spec.data == sync.DATA_REPLACE else ""))

        def _do() -> "engines.QueryResult":
            engine = self.pool.get(spec.target_project, spec.target_connection, dst_cfg,
                                   role="writer", schema=spec.target_database,
                                   database=spec.target_pg_database)
            return engines.insert_rows(engine, spec.target_table, columns, rows,
                                       schema=spec.target_database,
                                       delete_first=spec.data == sync.DATA_REPLACE)

        result = self._audited(spec.target_project, spec.target_connection, dst_cfg,
                               "sync_write", detail, caller, _do)
        return result.row_count

    def _try_explain(
        self, project: str, connection: str, cfg: ConnectionConfig, sql: str,
        schema: str | None = None, database: str | None = None,
    ) -> dict | None:
        """对写语句取执行计划（不带 ANALYZE，不执行）供审批人参考。

        返回 {"columns": [...], "rows": [[...]]}（带列名，审批页据此渲染表头）。
        reader 会话可能因只读事务拒绝 EXPLAIN DML（PG 会），失败则退回 writer；
        全部失败返回 None，不阻断审批单生成。行数/单格长度上限见 engines.PLAN_MAX_*。
        """
        if not engines.explainable(sql, cfg.engine):
            return None
        for role in ("reader", "writer"):
            if role == "writer" and cfg.writer is None:
                break
            try:
                engine = self.pool.get(project, connection, cfg, role=role, schema=schema,
                                       database=database)
            except Exception:
                continue
            plan = engines.explain(engine, sql, cfg.engine)
            if plan:
                return plan
        return None

    def _meta_provider(self, project: str, connection: str, cfg: ConnectionConfig,
                       database: str | None = None, schema: str | None = None):
        """给风险引擎注入"按表取元数据"的能力；无缓存或取不到时返回 None。

        schema：执行上下文（查询台右上角选的 schema / 库）。语句里没写 schema 的表
        就在这里找——SQL 实际也是在它下面执行的。
        """
        if self.metadata is None:
            return lambda _table: None

        def provider(table: str):
            name = f"{schema}.{table}" if schema and "." not in table else table
            try:
                return self.metadata.get(project, connection, cfg, name, database=database)
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
        old_pool = int(self.get_settings().get("engine_pool_size")
                       or engines.DEFAULT_ENGINE_POOL_SIZE)
        saved = self.settings.save(updates)
        new_pool = int(saved.get("engine_pool_size") or engines.DEFAULT_ENGINE_POOL_SIZE)
        if new_pool != old_pool:
            self.pool.dispose()  # 池大小变了：回收旧引擎，下次按新大小重建
        self.apply_runtime_settings()
        return saved

    def apply_runtime_settings(self) -> None:
        """把「并发/连接池」进程级设置应用到运行时：引擎池大小 + anyio 线程池上限。

        由 serve 启动的 lifespan 与设置保存路由调用（都在事件循环内）——线程池限流器是
        per-loop 的，拿不到（非 serve 的单测/CLI）就静默跳过，只影响并发上限、不报错。
        """
        if self.settings is None:
            return
        s = self.get_settings()
        self.pool.engine_pool_size = int(s.get("engine_pool_size")
                                         or engines.DEFAULT_ENGINE_POOL_SIZE)
        try:
            import anyio.to_thread  # noqa: PLC0415
            anyio.to_thread.current_default_thread_limiter().total_tokens = int(
                s.get("mcp_max_concurrency") or 40)
        except Exception:  # noqa: BLE001 — 不在事件循环里就跳过
            pass

    def _setting(self, key: str):
        return self.get_settings().get(key)

    def _issue_action_link(self, change_id: int, base_url: str) -> str | None:
        """通知里的一次性审批链接（设置 notify_action_links 开且选了外部渠道才发）。

        令牌只随外部通知出去；后台铃铛/macOS 通知本就在已登录环境里，不需要它。
        """
        if not self._setting("notify_action_links"):
            return None
        if str(self._setting("notify_primary") or "none").lower() == "none":
            return None
        from .notify import build_admin_deeplink  # noqa: PLC0415
        token = self.approvals.issue_action_token(change_id)
        return build_admin_deeplink(base_url, f"/admin/approvals/{change_id}/act?t={token}")

    def approval_wait_seconds(self) -> int:
        """execute 首提被拒后，服务端默认等待人工决策的秒数（0 = 不等待）。"""
        value = self._setting("approval_wait_seconds")
        return DEFAULT_APPROVAL_WAIT_S if value is None else int(value)

    def mask_default_patterns(self, cfg: ConnectionConfig) -> bool:
        """agent 查询是否按内置模式自动脱敏：连接级 Policy 优先，None 则跟随全局设置。

        只用于 agent 路径；后台查询台/导出走 `_read(mask=False)`，压根不到这里。
        """
        return resolve_default_patterns(
            cfg.policy, bool(self._setting("mask_sensitive_columns") is not False))

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
                result = self._run_touching_db(project, connection, _do_read, rec)
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
            result = self._run_touching_db(project, connection, _do_write, rec)
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
            raise QueryRejected("The approval subsystem is not enabled")
        return self.approvals.approve(change_id, decided_by, note)

    def reject_change(self, change_id: int, decided_by: str, note: str = ""):
        if self.approvals is None:
            raise QueryRejected("The approval subsystem is not enabled")
        return self.approvals.reject(change_id, decided_by, note)

    def get_change(self, change_id: int):
        if self.approvals is None:
            raise QueryRejected("The approval subsystem is not enabled")
        return self.approvals.get(change_id)

    def list_changes(self, status: str | None = None):
        if self.approvals is None:
            return []
        return self.approvals.list_by_status(status)

    # ---------- schema 探索 ----------

    def list_databases(self, project: str, connection: str, caller: CallerInfo,
                       database: str | None = None) -> list[str]:
        """列出连接可选的库/schema（MySQL 数据库 / PG schema）。sqlite 无此概念返回 []。

        database：PG 专用——列**哪个 database 下**的 schema（查询台切库后要看新库的 schema）。
        """
        cfg = self.config.get_connection(project, connection)
        if not _has_schema_layer(cfg):
            return []
        engine = self.pool.get(project, connection, cfg, database=database)
        return self._audited(project, connection, cfg, "list_databases", database or "", caller,
                             lambda: engines.list_databases(engine))

    def list_server_databases(self, project: str, connection: str,
                              caller: CallerInfo) -> list[str]:
        """列出服务器上的 database（PG 查 pg_database；MySQL/CH 等价于 list_databases）。

        PG 的库与 schema 是两层：一条连接只绑一个 database，`get_schema_names()` 只看得到
        当前库里的 schema。查询台左树的「库」这一层用的就是这里。
        """
        cfg = self.config.get_connection(project, connection)
        if not _has_schema_layer(cfg):
            return []
        engine = self.pool.get(project, connection, cfg)
        return self._audited(project, connection, cfg, "list_databases", "server", caller,
                             lambda: engines.list_server_databases(engine, cfg.engine))

    def list_tables(
        self, project: str, connection: str, caller: CallerInfo, schema: str | None = None,
        database: str | None = None,
    ) -> list[str]:
        cfg = self.config.get_connection(project, connection)
        # 未绑定默认库时，先让用户选库（库→表→列 三级树）。MySQL/PG 不带 schema 反射会崩
        # （默认 schema 为 None）；ClickHouse 不会崩但会落到 default 库、看不到别的库 → 一并引导
        if schema is None and not cfg.database and _has_schema_layer(cfg):
            raise ValueError("This connection has no default database bound; select a database (schema) first, then list its tables")
        engine = self.pool.get(project, connection, cfg, database=database)
        return self._audited(project, connection, cfg, "list_tables", schema or "", caller,
                             lambda: engines.list_tables(engine, schema))

    def describe_table(
        self, project: str, connection: str, table: str, caller: CallerInfo,
        schema: str | None = None, database: str | None = None,
    ) -> dict:
        cfg = self.config.get_connection(project, connection)
        # 未绑定默认库时的两道防线（否则 SQLAlchemy 反射取默认库为 None → NoneType.replace 崩）：
        # ① 从「库.表」限定名拆出 schema；② 仍无 schema 且无默认库 → 明确报错引导，而非让它崩
        if schema is None and "." in table:
            schema, table = table.split(".", 1)
        if schema is None and not cfg.database and _has_schema_layer(cfg):
            raise ValueError("This connection has no default database bound; specify the table as \"database.table\", or select a database (schema) first")
        engine = self.pool.get(project, connection, cfg, database=database)
        detail = f"{schema}.{table}" if schema else table
        return self._audited(project, connection, cfg, "describe_table", detail, caller,
                             lambda: engines.describe_table(engine, table, schema))

    def admin_search_tables(
        self, project: str, connection: str, q: str, caller: CallerInfo,
        database: str | None = None,
    ) -> list[dict]:
        """查询台全局表名搜索（⌘P）：跨库 LIKE 匹配，最多 50 条。"""
        q = (q or "").strip()
        if not q:
            return []
        cfg = self.config.get_connection(project, connection)
        engine = self.pool.get(project, connection, cfg, database=database)
        return self._audited(project, connection, cfg, "search_tables", q, caller,
                             lambda: engines.search_tables(engine, cfg.engine, q))

    def db_checkup(
        self, project: str, connection: str, caller: CallerInfo,
        schema: str | None = None, database: str | None = None,
    ) -> dict:
        """数据库体检：一次调用拿到结构化诊断报告。

        覆盖连接占用、缓存命中率、长查询、锁等待、空闲事务、死锁、复制延迟、大表等
        常见健康指标（按引擎提供不同项），逐项容错——取不到数据的指标标 unknown
        并说明原因（如权限不足），不影响其它项。agent 不必再为此多轮 SQL 摸底。

        schema 为 MySQL/ClickHouse 的库名、PG 的 schema（不传用连接默认库）；
        database 仅 PG，指定在哪个 database 上体检。
        """
        cfg = self.config.get_connection(project, connection)
        if cfg.engine == "redis":
            raise ValueError(
                "Redis connections do not support health checks (Redis is not exposed to "
                "the agent; use the Redis console in the admin backend instead)")
        scope = schema or cfg.database
        return self._checkup_one(project, connection, cfg, caller, scope, database).to_dict()

    def _checkup_one(
        self, project: str, connection: str, cfg: ConnectionConfig, caller: CallerInfo,
        scope: str | None, database: str | None = None,
    ) -> "checkup.CheckupReport":
        """单库体检（db_checkup / db_checkup_all 共用）：建引擎 + 审计 + 跑检查项。

        database 只对 needs_database_layer 的引擎（PG）有意义——一条 PG 连接只绑一个
        库，换库必须另建连接；MySQL/ClickHouse 的 schema 就是库，靠 scope 指定即可。
        """
        engine = self.pool.get(project, connection, cfg, database=database)
        return self._audited(
            project, connection, cfg, "checkup", scope or "", caller,
            lambda: checkup.run_checkup(engine, cfg.engine, scope),
        )

    def db_checkup_all(
        self, project: str, connection: str, caller: CallerInfo,
        *, database: str | None = None,
    ) -> dict:
        """实例级体检：对该连接下的所有用户库逐个体检，合并成一份报告。

        普通体检只看一个库/schema；「这台 DB 健康吗」不该只看当前库——慢查询、大表、
        膨胀在哪个库都可能发生。枚举该实例上的用户库（PG 的统计视图是库级隔离的，
        逐库另建连接；MySQL/CH 的 schema 就是库，同一条连接按库查），逐库走
        _checkup_one，再 merge_reports 去重实例级指标、只留各库最严重的那条。

        database：PG 指定先连哪个库去列清单（不传用连接默认库）。
        单库实例（sqlite 一个文件就是一个库）或列库失败时回落到普通体检，保证可用。
        """
        cfg = self.config.get_connection(project, connection)
        if cfg.engine == "redis":
            raise ValueError(
                "Redis connections do not support health checks (Redis is not exposed to "
                "the agent; use the Redis console in the admin backend instead)")
        # PG 一条连接只绑一个库：换库必须另建引擎；MySQL/CH 的 schema 就是库，
        # 同一条连接按 schema 查即可，不必为每个库建引擎（实例上库可能很多）
        per_db_engine = _driver_of(cfg).needs_database_layer
        engine = self.pool.get(project, connection, cfg, database=database)
        try:
            if per_db_engine:
                # 先查 pg_database 拿到这台实例上的库清单
                dbs = engines.list_server_databases(engine, cfg.engine)
            else:
                dbs = engines.list_databases(engine)
        except Exception:  # noqa: BLE001
            dbs = []
        # 单库连接或列库失败：退化成普通体检（不抛错，保证按钮永远可用）
        if not dbs:
            return self.db_checkup(project, connection, caller, database=database)

        reports: list[tuple[str, checkup.CheckupReport]] = []
        last_err = ""
        for db in dbs:
            try:
                reports.append((db, self._checkup_one(
                    project, connection, cfg, caller, scope=db,
                    database=db if per_db_engine else None)))
            except Exception as e:  # noqa: BLE001 - 某个库失败不废掉整份报告
                last_err = f"{db}: {e}"
        if not reports:
            raise QueryRejected(f"Health check failed for every database (last error: {last_err})")
        return checkup.merge_reports(cfg.engine, reports).to_dict()

    def admin_table_sizes(
        self, project: str, connection: str, caller: CallerInfo, schema: str | None = None,
        database: str | None = None,
    ) -> dict[str, int]:
        """查询台树右侧的表容量（字节）。取不到返回空 dict，不阻断列表。"""
        cfg = self.config.get_connection(project, connection)
        engine = self.pool.get(project, connection, cfg, database=database)
        return self._audited(project, connection, cfg, "table_sizes", schema or "", caller,
                             lambda: engines.table_sizes(engine, cfg.engine, schema))

    def get_table_ddl(
        self, project: str, connection: str, table: str, caller: CallerInfo,
        schema: str | None = None, database: str | None = None,
    ) -> str:
        """取建表语句（查询台「查看 DDL」）。"""
        cfg = self.config.get_connection(project, connection)
        engine = self.pool.get(project, connection, cfg, database=database)
        detail = f"{schema}.{table}" if schema else table
        return self._audited(project, connection, cfg, "table_ddl", detail, caller,
                             lambda: engines.get_table_ddl(engine, cfg.engine, table, schema))

    # 一次批量取多少张表的 DDL：再多就该用 list_tables 先筛，而不是把整库结构灌进上下文
    MAX_DDL_TABLES = 20

    def get_table_ddls(
        self, project: str, connection: str, tables: list[str], caller: CallerInfo,
        schema: str | None = None, database: str | None = None,
    ) -> list[dict]:
        """批量取建表语句，按传入顺序返回 [{table, ddl} | {table, error}]。

        单张表取不到（表名写错、无权限）**不中断整批**：把这张表标成 error 继续取下一张，
        agent 一次调用就能看清「哪几张拿到了、哪几张没拿到、为什么」，不必逐张重试。
        """
        names = [t.strip() for t in tables if t and t.strip()]
        if not names:
            raise ValueError("Give at least one table name")
        if len(names) > self.MAX_DDL_TABLES:
            raise ValueError(
                f"Can fetch the DDL of at most {self.MAX_DDL_TABLES} tables per call "
                f"({len(names)} given); fetch them in batches, or narrow the scope with list_tables first")
        out = []
        for name in names:
            try:
                ddl = self.get_table_ddl(project, connection, name, caller,
                                         schema=schema, database=database)
                out.append({"table": name, "ddl": ddl})
            except ConnectionUnavailable:
                raise  # 连接级故障对后续每张表都一样，没必要接着试
            except Exception as e:  # noqa: BLE001
                out.append({"table": name, "error": f"{type(e).__name__}: {e}"})
        return out

    def ai_generate_sql(
        self, project: str, connection: str, question: str, caller: CallerInfo,
        *, schema: str | None = None, tables: list[str] | None = None,
        explain: bool = False, include_samples: bool = False,
        session_id: str | None = None, database: str | None = None,
    ) -> dict:
        """让命令行 AI 按表结构 + 自然语言需求生成一条 SQL。只生成、不执行。

        tables 为空 = 「整库」模式：列出该库的表（超 ai_max_tables 报错要求收窄）。
        include_samples 时附少量样本行帮助 AI 理解数据形态。
        session_id 非空 = 追问：续接同一会话、不重发表结构。返回 {sql, explanation, session_id}。
        database 仅 PostgreSQL 有效：表所在的库（PG 一条连接只绑一个库，跨库必须换引擎）。
        """
        from . import ai

        s = self.get_settings()
        if not s.get("ai_enabled"):
            raise QueryRejected("AI 辅助未开启，请在系统设置中开启")
        question = (question or "").strip()
        if not question:
            raise QueryRejected("请填写你想查什么")
        cfg = self.config.get_connection(project, connection)
        if not _driver_of(cfg).ai_sql:
            raise QueryRejected(f"连接引擎 {cfg.engine} 暂不支持 AI 生成 SQL")
        # PG 的库先校验再建引擎：不存在的库会让连接失败被当成断连、打坏健康位
        database = self.resolve_pg_database(project, connection, database, cfg)
        engine = self.pool.get(project, connection, cfg, database=database)
        max_tables = int(s.get("ai_max_tables") or 40)

        def _run() -> dict:
            ddls: list[tuple[str, str]] = []
            samples: dict[str, str] | None = None
            # 首轮收集全部表结构；追问续接会话、上下文已在 AI 侧，只补发本轮新增的表（tables 里传的即新增表）。
            if session_id:
                for t in list(tables or []):  # 追问新增表：补发这些表的建表语句
                    tbl_schema, tbl = (t.split(".", 1) if "." in t else (schema, t))
                    ddls.append((tbl, engines.get_table_ddl(engine, cfg.engine, tbl, tbl_schema)))
            else:
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
                            # schema 一起带上：PG 的表多在非默认 schema、MySQL 在非默认库，
                            # 不带 schema 反射不到，样本就被静默丢弃了
                            r = engines.sample_rows(engine, tbl, 5,
                                                    max_cell_chars=cfg.policy.max_cell_chars,
                                                    schema=tbl_schema)
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

    def ai_diagnose_checkup(
        self, project: str, connection: str, question: str, caller: CallerInfo,
        *,
        report: dict | None = None, schema: str | None = None,
        database: str | None = None, session_id: str | None = None,
        all_dbs: bool = False,
    ) -> dict:
        """让 AI 根据体检报告 + 连接信息给出诊断建议（只读分析、不执行任何 SQL）。

        report 为前端回传的体检报告 JSON（db_checkup 的返回结构）。为防止前端伪造
        上下文，**报告本身不作为权威数据**：服务端只用它判定「有过体检」，
        并把连接侧的非敏感信息（引擎/版本/环境/范围）补进来；报告内容原样透传给 AI
        （体检报告本身就是只读诊断结果，不存在被篡改后执行的风险）。
        report 缺失时服务端现场重跑一次体检（要求连接可用）。
        session_id 非空 = 追问：续接同一会话、不重发报告。返回 {diagnosis, session_id}。
        """
        from . import ai, checkup

        s = self.get_settings()
        if not s.get("ai_enabled"):
            raise QueryRejected("AI 辅助未开启，请在系统设置中开启")
        cfg = self.config.get_connection(project, connection)
        if cfg.engine == "redis":
            raise ValueError("Redis 连接不支持体检诊断（Redis 无体检报告）")

        # 报告缺失：现场重跑体检（自带健康检查与审计）。拿到了报告就不再碰 DB——
        # 诊断是纯文本分析，连接挂了也能基于已有报告给出建议。
        rep = report if isinstance(report, dict) and report.get("checks") else None
        if rep is None:
            # 连接级（未选具体库）时跑实例级体检：诊断针对全体库，而不是某一个
            rep = (self.db_checkup_all(project, connection, caller, database=database) if all_dbs
                   else self.db_checkup(project, connection, caller, schema=schema, database=database))

        db_info = {
            "engine": cfg.engine,
            "version": str(rep.get("version") or ""),
            "environment": cfg.environment,
            "scope": rep.get("scope") or schema or cfg.database or "",
        }
        report_md = checkup.report_to_markdown(rep)
        question = (question or "").strip()

        def _run() -> dict:
            text, new_sid = ai.generate_diagnosis(
                system_prompt=str(s.get("ai_diagnosis_prompt") or ai.DEFAULT_DIAGNOSIS_PROMPT),
                db_info=db_info, report_md=report_md, question=question,
                provider=str(s.get("ai_provider") or "claude"),
                model=str(s.get("ai_model") or ""),
                timeout=int(s.get("ai_timeout_s") or 60),
                cli_path=str(s.get("ai_cli_path") or ""),
                session_id=session_id, api=_ai_api_cfg(s))
            return {"diagnosis": text, "session_id": new_sid}

        tool = "ai_diagnose_followup" if session_id else "ai_diagnose_checkup"
        detail = (question or report_md)[:2000]
        try:
            return self._audited(project, connection, cfg, tool, detail, caller, _run,
                                 touch_db=False)
        except ai.AIError as e:
            raise QueryRejected(str(e)) from e

    def ai_generate_workflow(
        self, project: str, connection: str, question: str, caller: CallerInfo,
        *, schema: str | None = None, tables: list[str] | None = None,
        current_graph: dict | None = None,
    ) -> dict:
        """让命令行 AI 按连接/表结构 + 需求设计一张 workflow DAG（画布图）。

        current_graph 非空 = 修改模式：把当前流程 JSON 塞进 prompt，让 AI 在此基础上按需求修改。
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
        if not _driver_of(cfg).ai_sql:
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
            default_conn = f"{project}/{connection}"
            kw = dict(system_prompt=str(s.get("ai_workflow_prompt") or ai.DEFAULT_WORKFLOW_PROMPT),
                      dialect=cfg.engine, connections=conns, ddls=ddls, question=question,
                      provider=str(s.get("ai_provider") or "claude"),
                      model=str(s.get("ai_model") or ""),
                      timeout=int(s.get("ai_timeout_s") or 60),
                      cli_path=str(s.get("ai_cli_path") or ""), api=_ai_api_cfg(s),
                      schema=schema, default_conn=default_conn,
                      current_graph=current_graph)
            graph, sid = ai.generate_workflow(**kw)
            _patch_source_schema(graph, default_conn, schema)   # 兜底：AI 漏写就补上
            try:
                compile_graph(graph)
            except WorkflowError as e:  # 回喂错误、续接会话重修一次
                graph, sid = ai.generate_workflow(**kw, repair_error=str(e), session_id=sid)
                _patch_source_schema(graph, default_conn, schema)
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

    def sample_rows(self, project: str, connection: str, table: str, limit: int,
                    caller: CallerInfo, database: str | None = None) -> dict:
        cfg = self.config.get_connection(project, connection)
        database = self.resolve_pg_database(project, connection, database, cfg)
        limit = min(limit, cfg.policy.max_rows)
        engine = self.pool.get(project, connection, cfg, database=database)

        def _run() -> dict:
            result = engines.sample_rows(engine, table, limit,
                                         max_cell_chars=cfg.policy.max_cell_chars)
            rows, masked = apply_mask(result.columns, result.rows, cfg.policy,
                                       self.mask_default_patterns(cfg))
            out = {
                AUDIT_BYTES_KEY: result.result_bytes,
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

    def reconnect_connection(self, project: str, connection: str, caller: CallerInfo) -> dict:
        """人工触发的强制重连：清健康位 + 回收旧引擎/隧道，然后主动探测重建。

        与 test_connection 的关键区别：test_connection 走 `_run_touching_db`，连接处于
        unavailable/exhausted 时会被 `health.check()` 直接挡下、根本不会尝试重建；exhausted
        连接因此无法靠它自愈。这里**绕过健康位**，无条件重置后走 `_health_probe`（SELECT 1 /
        PING）建立新连接——供管理后台/查询台在收到「连接不可用」错误时点「重连」使用。

        探测成功：健康位已随成功清为 ok，返回 {ok, engine}。
        探测失败：记审计 + 若为连接级错误则 mark_failed（后台退避重连从头再来），原样抛出。
        """
        cfg = self.config.get_connection(project, connection)  # 连接不存在 → KeyError
        # 无条件清健康状态：exhausted/unavailable 都归零，让本次探测不被拦
        self.health.force_clear(project, connection)
        rec = self._base_record(project, connection, cfg, "reconnect", "", caller)
        try:
            # _health_probe 会先 dispose 旧引擎/隧道再探测，拿到的是全新连接
            self._health_probe(project, connection)
        except Exception as e:  # noqa: BLE001
            rec.status = "error"
            rec.detail = f"{type(e).__name__}: {e}"
            self.store.record(rec)
            if is_connection_error(e):
                # 仍连不上：重新进入后台退避重连（给它再一轮机会）
                self.health.mark_failed(project, connection, f"{type(e).__name__}: {e}")
            raise
        rec.status = "ok"
        rec.detail = "重连成功"
        self.store.record(rec)
        return {"ok": True, "engine": cfg.engine}

    # ---------- 内部 ----------

    # ------------------------------------------------------------------ 看板
    #
    # 「系统现在被怎么用了」由三份数据拼出来，各自答不同的问题：
    #   config + health + 池 stats  →  有几条连接、连得上吗、此刻占了多少物理连接
    #   self.live                   →  此刻谁在跑什么（审计只在结束后落库，答不了）
    #   audit_log 聚合              →  一段时间内跑了多少、传了多少数据、谁跑得最多
    # 这里只做汇总，不碰 DB——看板每几秒刷一次，绝不能顺带去 ping 真实数据库。

    DASHBOARD_WINDOWS = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}

    # 活跃会话列表**不跟随统计窗口**：窗口是给流量图用的（可能只有 1 小时，也可能 30 天），
    # 而「最近谁在用」问的是这几天的事——窗口选 1 小时就看不见昨天的会话，选 30 天又会
    # 翻出一个月前的陈年会话，两头都不对。固定成最近几天，列表本身可滚动。
    DASHBOARD_SESSION_DAYS = 3
    DASHBOARD_SESSION_LIMIT = 50

    def dashboard_snapshot(self, window: str = "24h", live_limit: int = 50) -> dict:
        """看板数据快照。window 为统计窗口（见 DASHBOARD_WINDOWS），非法值报错。"""
        hours = self.DASHBOARD_WINDOWS.get(window)
        if hours is None:
            raise ValueError(
                f"不支持的统计窗口: {window!r}（可选 {', '.join(self.DASHBOARD_WINDOWS)}）")
        now = datetime.now(UTC)
        since = (now - timedelta(hours=hours)).isoformat(timespec="milliseconds")
        # 粒度按窗口长短选：一律用小时的话，1 小时窗口只剩一两个点、30 天窗口有 720 根柱子
        bucket = "minute" if hours <= 1 else "hour" if hours <= 48 else "day"

        summary = self.store.traffic_summary(since)
        series = self.store.traffic_series(since, bucket=bucket)
        live_ops = self.live.snapshot()

        return {
            "generated_at": now.isoformat(timespec="milliseconds"),
            "window": window,
            "window_hours": hours,
            "bucket": bucket,
            "uptime_s": int(time.time() - self.started_at),
            "connections": self._dashboard_connections(),
            "live": {"count": len(live_ops), "ops": live_ops[:live_limit]},
            "traffic": summary,
            "series": series,
            "top": {
                "connections": self.store.top_groups("connection", since),
                "tools": self.store.top_groups("tool", since),
                "agents": self.store.top_groups("agent", since),
            },
            "session_days": self.DASHBOARD_SESSION_DAYS,
            "sessions": self.store.list_sessions(
                limit=self.DASHBOARD_SESSION_LIMIT,
                since=(now - timedelta(days=self.DASHBOARD_SESSION_DAYS)).isoformat(
                    timespec="milliseconds")),
            # 会话结果配额是进程内的实时用量，与 audit_log 里的历史会话是两回事：
            # 人要判断「哪个 agent 正在猛拉数据」看的是这个。
            "budgets": self.result_budget().snapshot()[:self.DASHBOARD_SESSION_LIMIT],
            "approvals": {"pending": self._pending_approvals_count()},
        }

    def _pending_approvals_count(self) -> int:
        if self.approvals is None:
            return 0
        try:
            # 惰性过期：存储态仍是 pending 但已过 TTL 的单不算「待处理」（与侧栏角标一致）
            return len([c for c in self.list_changes("pending")
                        if c.effective_status() == "pending"])
        except Exception:  # noqa: BLE001
            logger.debug("dashboard: 取待审批数失败", exc_info=True)
            return 0

    def _dashboard_connections(self) -> dict:
        """每条已配置连接的健康位与实时占用（配置 ∪ 引擎池 ∪ Redis 池）。

        以**配置**为准列出全部连接（哪怕从没连过，看板也该看到它存在），再把池里的
        引擎按 (project, connection) 归并上去——一条连接可能因 role/schema/database
        维度而有多个引擎，看板关心的是「这条连接一共占了几个引擎、几条物理连接」。
        """
        pooled = self.pool.stats() + self.redis_pool.stats()
        by_conn: dict[tuple[str, str], list[dict]] = {}
        for entry in pooled:
            by_conn.setdefault((entry["project"], entry["connection"]), []).append(entry)
        health = self.health.snapshot()
        now = time.monotonic()

        items, by_engine, by_env = [], {}, {}
        for project, proj in sorted(self.config.projects.items()):
            for name, cfg in sorted(proj.connections.items()):
                engs = by_conn.get((project, name), [])
                # checked_out 取不到的池类型（SQLite）记 None，不参与求和
                outs = [e["checked_out"] for e in engs if e["checked_out"] is not None]
                h = health.get((project, name))
                by_engine[cfg.engine] = by_engine.get(cfg.engine, 0) + 1
                env = cfg.environment or "—"
                by_env[env] = by_env.get(env, 0) + 1
                items.append({
                    "project": project,
                    "connection": name,
                    "engine": cfg.engine,
                    "environment": cfg.environment,
                    "host": cfg.host,
                    "database": cfg.database,
                    "has_writer": cfg.writer is not None,
                    # 从未触达过的连接是「未探测」，不是「正常」——健康位只在第一次
                    # 成功/失败后才有记录
                    "state": h.state if h else "unprobed",
                    "fail_count": h.fail_count if h else 0,
                    "last_error": h.last_error if h else "",
                    # 距下次自动重连还有多少秒（健康位用单调时钟存的绝对时刻）
                    "retry_in_s": (max(int(h.next_retry_at - now), 0)
                                   if h and h.state != "ok" else 0),
                    "engines": len(engs),
                    # None = 这类池根本不报这个数（SQLite 用 SingletonThreadPool，
                    # 没有 checkedout()）。记 0 会被读成「没占用连接」，那是另一回事。
                    "checked_out": sum(outs) if outs else (0 if not engs else None),
                    "tunnel": any(e["tunnel"] for e in engs),
                    "idle_s": min((e["idle_s"] for e in engs), default=None),
                })
        return {
            "configured": len(items),
            "by_engine": by_engine,
            "by_environment": by_env,
            "unhealthy": sum(1 for i in items if i["state"] not in ("ok", "unprobed")),
            "unprobed": sum(1 for i in items if i["state"] == "unprobed"),
            "pooled_engines": len(pooled),
            "checked_out": sum(i["checked_out"] for i in items),
            "items": items,
        }

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

    def _audited(self, project, connection, cfg, tool, detail_sql, caller, fn,  # noqa: ANN001
                 *, touch_db: bool = True):
        """落审计地执行 fn。

        touch_db=False：fn 不碰数据库（如纯文本的体检诊断），跳过健康位检查——
        否则连接一挂，连「根据已有报告让 AI 给建议」都做不了，而那恰恰是连接出问题时
        用户最想做的事。
        """
        rec = self._base_record(project, connection, cfg, tool, detail_sql, caller)
        try:
            result = (fn() if not touch_db
                      else self._run_touching_db(project, connection, fn, rec))
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
        # 结果集体积计入流量统计：返回 QueryResult 的直接取属性；返回 dict 的（如
        # sample_rows 已把行脱敏成前端结构）用 AUDIT_BYTES_KEY 捎带，取完就 pop 掉。
        # 返回结构性小数据的（list_tables/describe_table 等）体积可忽略，不计。
        if isinstance(result, dict) and AUDIT_BYTES_KEY in result:
            rec.result_bytes = result.pop(AUDIT_BYTES_KEY)
        else:
            rec.result_bytes = getattr(result, "result_bytes", None)
        self.store.record(rec)
        return result

    def _syntax_precheck(
        self, project: str, connection: str, cfg: ConnectionConfig, sql: str,
        caller: CallerInfo, tool: str, database: str | None = None,
    ) -> None:
        """agent 侧 SQL 的语法预检（仅在 sqlglot 解析失败后调用）。

        让目标 DB **只解析不执行**地复核一次（MySQL PREPARE / SQLite EXPLAIN / …）：
        - DB 明确报语法错 → 记审计并抛 SqlSyntaxError，不再往下走（不建审批单）；
        - DB 认这条语法，或该引擎/语句无法复核 → 静默返回，交回调用方原有的保守路径。

        为什么不直接信 sqlglot：它的方言覆盖不完整（MySQL 无括号的 DROP PARTITION 是合法
        SQL 但 sqlglot 解析不了），只信它会误拒合法语句。见 CLAUDE.md 同名教训。
        """
        if cfg.engine == "redis":
            return
        def _do() -> "engines.SyntaxCheck":
            engine = self.pool.get(project, connection, cfg, database=database)
            return engines.dry_run_syntax_check(engine, sql, cfg.engine)

        try:
            res = self._run_touching_db(project, connection, _do)
        except ConnectionUnavailable:
            raise
        except Exception:  # noqa: BLE001
            # 复核本身出错（引擎建不起来等）不该改变主流程的判定，退回保守路径
            logger.debug("syntax precheck failed for %s/%s", project, connection, exc_info=True)
            return
        if not res.supported or res.ok:
            return
        where = f" (statement #{res.stmt_index})" if res.stmt_index > 1 else ""
        rec = self._base_record(project, connection, cfg, tool, sql, caller)
        rec.status = "rejected"
        rec.detail = f"SQL 语法错误: {res.error}"
        self.store.record(rec)
        raise SqlSyntaxError(
            f"[sql_syntax_error] SQL syntax error{where}, confirmed by the target "
            f"database ({cfg.engine}): {res.error}. Fix the syntax and retry — don't "
            f"resend it as-is."
        )

    def _run_touching_db(self, project: str, connection: str, fn,  # noqa: ANN001
                         rec: AuditRecord | None = None):
        """任何"会触达 DB/隧道"的动作都过这里：入口先查健康位、出错时按类别打标。

        - 若健康位为 unavailable/exhausted：直接抛 ConnectionUnavailable（不碰 DB）
        - 执行成功：清健康标记（如果之前挂过）
        - 失败：判断是不是"连接级"异常，是就打标 + 启后台重连，然后原样再抛
          （非连接级异常如 SQL 语法/权限拒/审批拒不打标，重连也没用）

        rec：本次操作的审计记录骨架。给了就在执行期间登记进 `self.live`——审计要等
        操作结束才落库，看板问的「此刻谁在查」只有这里答得了。不给（如健康探测、
        语法预检这类内部动作）就不登记，免得看板被噪音刷屏。
        """
        self.health.check(project, connection)
        live_id = None if rec is None else self.live.begin(
            project, connection, rec.tool, rec.agent, rec.session_id, rec.sql)
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
        finally:
            if live_id is not None:
                self.live.end(live_id)
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
        """连接 exhausted 事件回调：只落 warn 日志，不发通知。

        原因：连接不可用会在 agent 侧被 `[connection_exhausted]` ToolError 直接告知，
        agent 会告诉用户；再发桌面/群通知反而形成噪音（尤其自建 server 抖动时会连发）。
        审批单等"必须人主动介入"的场景仍走通知（那里 agent 不再触达）。
        """
        logger.warning("connection %s/%s exhausted: %s", project, connection, error)

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
        from .inbox import DEFAULT_RETENTION_DAYS as INBOX_RETENTION
        stats = {"engines_reaped": 0, "redis_reaped": 0, "audit_purged": 0,
                 "changes_purged": 0, "notifications_purged": 0,
                 "workflow_runs_purged": 0, "exports_purged": 0}
        for key, fn in (
            ("engines_reaped", self.pool.reap_idle),
            ("redis_reaped", self.redis_pool.reap_idle),
            ("audit_purged", lambda: self.store.purge_old(retention_days)),
            ("changes_purged",
             (lambda: self.approvals.purge_old(retention_days)) if self.approvals else (lambda: 0)),
            # 通知短保留（7 天）：审批提醒/exhausted 告警不必久存
            ("notifications_purged",
             (lambda: self.inbox.purge_old(INBOX_RETENTION)) if self.inbox else (lambda: 0)),
            # workflow_run：30 天保留；同时清理 xlsx 产物目录
            ("workflow_runs_purged", self._purge_workflow_runs),
            # MCP 临时导出：固定一小时 TTL，与审计保留期无关
            ("exports_purged", self.purge_mcp_exports),
        ):
            try:
                stats[key] = fn()
            except Exception:
                logger.exception("housekeeping %s 失败", key)
        if any(stats.values()):
            logger.info("housekeeping: %s", stats)
        return stats

    def _purge_workflow_runs(self, days: int = 30) -> int:
        """清 30 天前的 workflow_run 记录 + 顺手删对应 xlsx 目录。"""
        if self.runs is None:
            return 0
        paths = self.runs.purge_older_than(days)
        if not paths or not self.data_dir:
            return len(paths)
        import shutil
        from pathlib import Path
        for rel in paths:
            try:
                full = Path(self.data_dir) / rel
                if full.parent.is_dir():
                    shutil.rmtree(full.parent, ignore_errors=True)
            except Exception:  # noqa: BLE001
                logger.exception("清理 xlsx 目录失败：%s", rel)
        return len(paths)

    # ---------- 调度：CRUD + tick 循环 ----------

    def workflow_schedule_upsert(self, name: str, cron_type: str, cron_value: str,
                                 enabled: bool = True, notify_on: str = "failure",
                                 attach_kinds: list[str] | None = None) -> dict:
        """增/改一条调度配置。校验 cron 语法和 notify_on/attach_kinds 值域。"""
        if self.schedules is None:
            raise RuntimeError("调度存储未初始化（需 serve 模式运行）")
        # workflow 存在性检查（调度只能挂在已有 workflow 上）
        if self.workflows is None or not any(w.name == name for w in self.workflows.list()):
            raise ValueError(f"workflow {name!r} 不存在")
        return self.schedules.upsert(name, cron_type, cron_value, enabled=enabled,
                                     notify_on=notify_on, attach_kinds=attach_kinds)

    def workflow_schedule_get(self, name: str) -> dict | None:
        if self.schedules is None:
            return None
        return self.schedules.get(name)

    def workflow_schedule_delete(self, name: str) -> None:
        if self.schedules is None:
            return
        self.schedules.delete(name)

    def workflow_schedule_list(self) -> list[dict]:
        if self.schedules is None:
            return []
        return self.schedules.list()

    def workflow_schedules_enriched(self) -> list[dict]:
        """定时任务全局列表：每条 schedule 附上 workflow 是否存在、是否有正在跑的实例。

        供 /admin/workflows/schedules 管理页用；纯只读，不改任何状态。
        """
        if self.schedules is None:
            return []
        scheds = self.schedules.list()
        wf_names = set()
        if self.workflows is not None:
            wf_names = {w.name for w in self.workflows.list()}
        out = []
        for s in scheds:
            name = s.get("name")
            running = False
            if self.runs is not None and name:
                running = bool(self.runs.running_for(name))
            out.append({**s, "workflow_exists": name in wf_names, "running": running})
        return out

    def workflow_schedule_trigger_now(self, name: str) -> None:
        """手动立即触发一次调度（后台线程跑，不阻塞调用方）。

        走完全跟 cron 到点相同的链路：入 workflow_run → 后台跑 → 通知。
        与用户在流程详情页 ▶ 运行的区别：那个是前台同步、不入 workflow_run 表、不发通知；
        这个是把「计划本来会自动跑的一次」提前到现在。
        """
        if self.workflows is None or self.workflows.get(name) is None:
            raise ValueError(f"workflow 不存在：{name!r}")
        if self.schedules is None or self.schedules.get(name) is None:
            raise ValueError(f"调度配置不存在：{name!r}")
        threading.Thread(
            target=self._run_scheduled, args=(name,),
            daemon=True, name=f"dbm-wf-run-{name}").start()

    # ---------- 运行历史（供详情页 & 通知 deeplink 用）----------

    def workflow_runs_list(self, name: str, limit: int = 50) -> list[dict]:
        if self.runs is None:
            return []
        return self.runs.list_by_name(name, limit)

    def workflow_run_get(self, run_id: int) -> dict | None:
        if self.runs is None:
            return None
        return self.runs.get(run_id)

    def workflow_running_list(self, triggered_by: str | None = "schedule") -> list[dict]:
        """当前正在执行的 workflow 运行列表；默认只列调度触发的（手动/agent 触发的用户在前端等结果，不列）。"""
        if self.runs is None:
            return []
        from datetime import UTC, datetime
        rows = self.runs.list_running(triggered_by)
        now_ts = datetime.now(UTC).timestamp()
        out = []
        for r in rows:
            elapsed = None
            started = r.get("started_at")
            if started:
                try:
                    elapsed = int(now_ts - datetime.fromisoformat(started).timestamp())
                except ValueError:
                    elapsed = None
            out.append({"id": r["id"], "name": r["name"],
                        "triggered_by": r["triggered_by"],
                        "started_at": started, "elapsed_s": elapsed})
        return out

    # ---------- 调度触发的一次执行 ----------

    def _run_scheduled(self, name: str) -> None:
        """一次调度触发：新建 workflow_run → 后台跑 → 更新状态 → 按 notify_on 发通知。

        同名 workflow 上一次尚未完成 → 跳过本次（防堆积；对齐"安静即正常"红线）。
        任何异常吞掉（scheduler tick 不应因单个 workflow 失败而挂）。
        """
        if self.runs is None or self.workflows is None:
            return
        try:
            if self.runs.running_for(name):
                logger.warning("scheduler: workflow %s 上次未完成，跳过本次调度", name)
                return
            sched = self.schedules.get(name) if self.schedules is not None else None
            run_id = self.runs.start(name, triggered_by="schedule")
            # 走既有 workflow_run（此处 caller 用 scheduler；审计记录里区分）
            sched_caller = CallerInfo(agent="scheduler", session_id=f"sched-{run_id}")
            try:
                out = self.workflow_run(name, sched_caller)
                ok = out.get("ok") is True
                # 输出预览：只留可控大小（保存整个 rows 的话表膨胀），前 100 行
                op = out.get("output") or None
                op_saved: dict = {}
                if op:
                    op_saved = {"columns": op.get("columns") or [],
                                "rows": (op.get("rows") or [])[:100],
                                "row_count": op.get("row_count", 0)}
                # xlsx 产物：attach_kinds 含 xlsx_link 才生成
                xlsx_path: str | None = None
                attach_kinds = (sched or {}).get("attach_kinds") or []
                if ok and op and "xlsx_link" in attach_kinds and self.data_dir:
                    xlsx_path = self._save_run_xlsx(run_id, op)
                self.runs.finish(run_id, "ok" if ok else "failed",
                                 steps=out.get("steps") or [],
                                 output_preview=op_saved,
                                 error="" if ok else (
                                     (out.get("steps") or [{}])[-1].get("error", "") or "运行失败"),
                                 xlsx_path=xlsx_path)
            except Exception as e:  # noqa: BLE001
                self.runs.finish(run_id, "failed", error=f"{type(e).__name__}: {e}")
                ok = False
            # 更新 schedule 元数据
            if self.schedules is not None:
                try:
                    self.schedules.mark_ran(name, "ok" if ok else "failed")
                except Exception:  # noqa: BLE001
                    logger.exception("mark_ran 失败：%s", name)
            # 通知
            self._notify_scheduled_run(name, run_id, sched)
        except Exception:  # noqa: BLE001
            logger.exception("_run_scheduled 未预期失败：%s", name)

    def _save_run_xlsx(self, run_id: int, output: dict) -> str | None:
        """把 workflow_run 的输出落成 xlsx，返回相对 data_dir 的路径（或 None）。"""
        from pathlib import Path

        from . import export
        try:
            cols = output.get("columns") or []
            rows = output.get("rows") or []
            if not cols:
                return None
            rel = f"workflow_runs/{run_id}/output.xlsx"
            full = Path(self.data_dir) / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            data = export.to_xlsx(cols, rows)
            full.write_bytes(data)
            return rel
        except Exception:  # noqa: BLE001
            logger.exception("生成 xlsx 失败：run_id=%s", run_id)
            return None

    def _notify_scheduled_run(self, name: str, run_id: int, sched: dict | None) -> None:
        """按 notify_on 决定是否发通知；富通知走 render_workflow_notification。"""
        if not sched:
            return
        notify_on = sched.get("notify_on") or "failure"
        if notify_on == "none":
            return
        run = self.runs.get(run_id) if self.runs is not None else None
        if not run:
            return
        ok = run.get("status") == "ok"
        if notify_on == "success" and not ok:
            return
        if notify_on == "failure" and ok:
            return
        try:
            from .notify import render_workflow_notification
            settings = self.get_settings() if self.settings is not None else {}
            admin_base_url = (settings.get("admin_base_url") or "").strip()
            download_path = None
            if run.get("xlsx_path"):
                download_path = f"/admin/workflows/runs/{run_id}/download/output.xlsx"
            payload = render_workflow_notification(
                name, run, sched.get("attach_kinds") or ["summary"],
                admin_base_url=admin_base_url, download_path=download_path)
            self.notifier.send(payload["title"], payload["body"], meta=payload["meta"])
        except Exception:  # noqa: BLE001
            logger.exception("workflow 通知发送失败：run_id=%s", run_id)

    def start_scheduler(self, interval_s: int = 30) -> None:
        """启动调度器 daemon 线程。tick 粒度 30s（下拉最小 1min，抖动 ≤30s）。

        安静即正常红线：__init__ 默认不启动（测试路径永不真跑），只 _cmd_serve 显式调。
        启动时先扫一遍 workflow_run 把 1h 前还挂 running 的记录标 failed（防重启阻塞）。
        """
        if self._scheduler_stop is not None or self.schedules is None or self.runs is None:
            return
        try:
            swept = self.runs.sweep_stale_running(older_than_hours=1)
            if swept:
                logger.info("scheduler: sweep %d stale running", swept)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: sweep stale 失败")
        stop = threading.Event()
        self._scheduler_stop = stop

        def _loop() -> None:
            while not stop.wait(interval_s):
                try:
                    self._scheduler_tick()
                except Exception:  # noqa: BLE001
                    logger.exception("scheduler tick 失败")

        threading.Thread(target=_loop, daemon=True, name="dbm-scheduler").start()

    def _scheduler_tick(self) -> None:
        """遍历 enabled 调度 → cron_matches(now) → 触发。同分钟内每个 workflow 只跑一次。"""
        if self.schedules is None:
            return
        from datetime import datetime

        from .workflows import cron_from_dropdown, cron_matches
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        for sched in self.schedules.list_enabled():
            name = sched["name"]
            key = (name, stamp)
            if key in self._sched_ticked_minute:
                continue
            try:
                cron_expr = cron_from_dropdown(sched["cron_type"], sched["cron_value"])
            except ValueError:
                logger.exception("scheduler: 非法 cron %s / %s / %s",
                                 name, sched["cron_type"], sched["cron_value"])
                continue
            if not cron_matches(cron_expr, now):
                continue
            self._sched_ticked_minute.add(key)
            # 清理旧分钟条目（防内存增长）
            if len(self._sched_ticked_minute) > 10000:
                self._sched_ticked_minute.clear()
            # 后台线程跑，别阻塞 tick 循环
            threading.Thread(
                target=self._run_scheduled, args=(name,),
                daemon=True, name=f"dbm-wf-run-{name}").start()

    def close(self) -> None:
        if self._housekeeping_stop is not None:
            self._housekeeping_stop.set()
            self._housekeeping_stop = None
        if self._scheduler_stop is not None:
            self._scheduler_stop.set()
            self._scheduler_stop = None
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
        if self.inbox is not None:
            self.inbox.close()
        if self.schedules is not None:
            self.schedules.close()
        if self.runs is not None:
            self.runs.close()
