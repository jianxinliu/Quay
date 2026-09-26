"""数据库体检：一次调用拿到结构化诊断报告，agent 不必多轮 SQL 摸底。

背景：agent 想知道「这台 DB 健康吗」，过去只能自己一轮轮 query（连了几个会话？
命中率多少？有没有长查询/锁等待？复制延迟？），每轮都耗一次往返 + 上下文，还容易
漏掉自己不知道该查的指标。本模块把常用诊断项打包成一组**只读**查询，一次返回
逐项结论（status + 人可读的 value + 解读），失败的单项降级为 unknown 不影响其它项。

设计要点：
- **只读**：所有 SQL 都是 SELECT / SHOW / PRAGMA，和 engines.estimate_row_count
  同一套信任模型（服务端硬编码的诊断语句，不经用户 SQL 分类器）。
- **逐项容错**：每个检查独立 try/except。权限不足、视图不存在、版本不支持 → status=unknown
  并在 message 里写清原因（如「账号无 pg_monitor 权限，只能看到自己的会话」），
  绝不让一个不可测的指标把整份报告拖垮，也不假装测到了。
- **按可见性选视图，而非「无权限整项放弃」**（v2 权限重新设计）：同一指标往往有
  「受限账号也能看到真实值」的查法——
    * PG：``pg_stat_activity`` 的**行**对所有人可见，只有 state/query/wait_event
      **列**对非 pg_monitor 角色置 NULL。于是连接占用改用 ``count(*)``（无需
      pg_monitor）；只有空闲事务/长查询/等待事件这类需要 state 列的才真正受限于
      pg_monitor。``pg_replication_slots``、``pg_stat_user_tables``、
      ``pg_stat_user_indexes``、``pg_stat_database`` 全部无权限限制。
    * MySQL：``information_schema.PROCESSLIST`` 无 PROCESS 只回自己的会话，但
      ``performance_schema.threads`` **不需要 PROCESS 就能看到全部线程**（实测
      8.4/9.x 均如此）；``data_lock_waits`` 同理。于是长查询/锁等待全部改走
      performance_schema，彻底去掉「可见行数对 Threads_connected」的启发式降级。
  真正缺权限的项，把所需权限与 GRANT 模板汇总到报告的 ``privileges`` 里，让用户
  一眼知道缺什么、怎么补，而不是面对一堆不解释的 unknown。
- **不给假数据**：受限视图在权限不足时只返回部分行，直接报会误导。需要完整可见性
  的项先探测权限（PG 的 pg_monitor），不满足就标 unknown 并给出 GRANT 模板。
- **阈值集中声明**，注释说明取值依据。
- **按维度组织**：每项检查归属一个维度（可用性/容量连接/查询性能/锁与并发/
  复制高可用/存储维护），报告同时是给人看的分组、也是 agent 的结构化清单。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine as SAEngine

from .i18n import current_locale, register, t

logger = logging.getLogger(__name__)

Status = Literal["ok", "info", "warn", "critical", "unknown"]

# ---------- 维度（前后端共用的分组框架） ----------

DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("availability", "可用性"),
    ("capacity", "容量与连接"),
    ("performance", "查询性能"),
    ("concurrency", "锁与并发"),
    ("replication", "复制与高可用"),
    ("maintenance", "存储维护"),
)
_DIM_ORDER = {k: i for i, (k, _) in enumerate(DIMENSIONS)}
_DIM_TITLE = {k: title for k, title in DIMENSIONS}

register({
    "checkup.dimension.availability": ("可用性", "Availability"),
    "checkup.dimension.capacity": ("容量与连接", "Capacity & Connections"),
    "checkup.dimension.performance": ("查询性能", "Query Performance"),
    "checkup.dimension.concurrency": ("锁与并发", "Locking & Concurrency"),
    "checkup.dimension.replication": ("复制与高可用", "Replication & HA"),
    "checkup.dimension.maintenance": ("存储维护", "Storage & Maintenance"),
    "checkup.common.unknown": ("未知", "unknown"),
    "checkup.common.username_placeholder": ("<用户名>", "<username>"),
    "checkup.common.grant_missing_privilege": ("-- 补权限: {priv}", "-- grant the missing privilege: {priv}"),
    "checkup.common.duration_days_hours": ("{days} 天 {hours} 小时", "{days}d {hours}h"),
    "checkup.common.duration_hours_mins": ("{hours} 小时 {mins} 分", "{hours}h {mins}m"),
    "checkup.common.duration_mins_secs": ("{mins} 分 {secs} 秒", "{mins}m {secs}s"),
    "checkup.common.duration_secs": ("{secs} 秒", "{secs}s"),
    "checkup.common.summary_critical": ("严重", "critical"),
    "checkup.common.summary_warn": ("需关注", "needs attention"),
    "checkup.common.summary_ok": ("正常", "ok"),
    "checkup.common.summary_info": ("参考", "info"),
    "checkup.common.summary_unknown": ("无法测量", "unmeasured"),
    "checkup.common.summary_item": ("{n} 项{label}", "{n} {label}"),
    "checkup.common.summary_total": ("（共 {n} 项）", " ({n} total)"),
    "checkup.merge.no_databases_title": ("无可体检的库", "No Databases to Check"),
    "checkup.merge.no_databases_message": (
        "该连接下没有可体检的用户库（可能全是系统库，或账号无权限列出）。",
        "This connection has no user databases to check (they may all be system databases, "
        "or the account lacks permission to list them).",
    ),
    "checkup.merge.scope_all_databases": ("全体 {n} 个库", "all {n} databases"),
    "checkup.entry.unsupported_title": ("不支持体检", "Checkup Not Supported"),
    "checkup.entry.unsupported_message": (
        "该引擎暂不支持体检（支持：{engines}）",
        "Checkup is not yet supported for this engine (supported: {engines})",
    ),
    "checkup.entry.connectivity_title": ("数据库连接", "Database Connection"),
    "checkup.entry.connectivity_value": ("无法连接", "Unreachable"),
    "checkup.entry.connectivity_message": (
        "数据库不可达：{why}。请确认数据库在运行、网络/SSH 隧道通畅，"
        "恢复后点「重新体检」即可测量全部指标。",
        "The database is unreachable: {why}. Confirm the database is running and the "
        "network/SSH tunnel is open; once it recovers, click \"re-run checkup\" to "
        "measure every metric again.",
    ),
    "checkup.entry.error_title": ("体检执行失败", "Checkup Failed"),
})

# ---------- 阈值（集中声明，便于调参；注释说明取值依据） ----------

# 连接数占用：>80% 警告（该扩容或查长连接了），>95% 严重（随时可能拒绝新连接）
CONN_WARN_PCT, CONN_CRIT_PCT = 0.80, 0.95
# 磁盘占用：>80% 警告，>95% 严重（Prometheus/Datadog 等监控的通用阈值）
DISK_WARN_PCT, DISK_CRIT_PCT = 0.80, 0.95
# InnoDB 缓冲池 / PG 缓存命中率：<95% 警告，<90% 严重（热数据放不下了）
CACHE_HIT_WARN, CACHE_HIT_CRIT = 0.95, 0.90
# InnoDB 数据总量相对缓冲池的倍数：>2 倍警告（热数据多半已不在内存，配合命中率一起看）
BP_VS_DATA_WARN = 2.0
# 长查询：>=60s 警告，>=300s 严重（多半是卡住的全表扫描，用查询台取消或 EXPLAIN 排查）
LONG_QUERY_WARN_S, LONG_QUERY_CRIT_S = 60, 300
# 复制延迟：>60s 警告（落后一分钟以上，读副本数据已旧），>600s 严重
REPL_LAG_WARN_S, REPL_LAG_CRIT_S = 60, 600
# 空闲事务：超过 5 分钟还 idle in transaction 的事务（拿着锁又不干活，挡 autovacuum）
IDLE_TXN_WARN_S = 300
# 死元组：单表死元组超过 1 万行该 autovacuum 了
DEAD_TUPLE_WARN = 10_000
# 临时表落盘比例：>25% 警告（排序/哈希超过 tmp_table_size 落临时盘，Percona 调优常规阈值）
TMP_DISK_WARN_PCT = 0.25
# PG 复制槽 WAL 堆积：非活跃槽不消费 WAL 会把 pg_wal 撑爆；>1GB 警告，>10GB 严重
SLOT_LAG_WARN_B, SLOT_LAG_CRIT_B = 1024**3, 10 * 1024**3
# PG 统计信息过期：超过 7 天未 ANALYZE 的表（autovacuum 默认 analyze 频率量级）
STATS_STALE_DAYS = 7
# PG 事务 ID 回卷：剩余可用事务数 < 200M（autovacuum_freeze_max_age 默认值量级）警告，
# < 100M 严重（此时库已接近被强制只读）。事务号上限 2^31 ≈ 2147M
XID_REMAINING_WARN, XID_REMAINING_CRIT = 200_000_000, 100_000_000
# 未使用索引的体量门槛：合计 >100MB 才值得提示删除（否则清理收益太小）
UNUSED_IDX_WARN_B = 100 * 1024**2
# ClickHouse 单表活跃 part 数：too_many_parts 默认阈值 300，留余量到 150 警告
CH_PARTS_WARN = 150

_STATUS_LEVEL = {"ok": 0, "info": 1, "unknown": 1, "warn": 2, "critical": 3}
# 维度内排序：严重的靠前，参考信息靠后
_STATUS_SORT = {"critical": 0, "warn": 1, "unknown": 2, "ok": 3, "info": 4}


@dataclass
class Check:
    """单项检查结论。value 是给人/agent 读的主值，message 是解读与下一步建议。"""

    name: str            # 稳定 key，如 buffer_pool_hit_ratio
    title: str           # 中文标题
    status: Status
    value: str = ""
    message: str = ""
    details: list[str] = field(default_factory=list)
    dimension: str = "availability"   # 归属维度（见 DIMENSIONS）
    privilege: str = ""               # 该项所需的权限；仅当 status=unknown 时有意义
    # 这项指标的值在**同一实例的任一库上查都一样**（连接占用、复制延迟这类全局计数器）。
    # merge_reports 据此把逐库跑出来的重复项并成一条、不加 [库名] 前缀。
    # 默认 False = 按库处理：新检查漏标最多是多一行带前缀的重复，不会漏数据。
    instance_scope: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "value": self.value,
            "message": self.message,
            "details": list(self.details),
            "dimension": self.dimension,
            "privilege": self.privilege,
            "instance_scope": self.instance_scope,
        }


@dataclass
class PrivilegeGap:
    """一项缺失的权限：影响哪些检查、补权限的 GRANT 语句（给人执行，不是给 agent）。"""

    privilege: str       # 权限名，如 pg_monitor / REPLICATION CLIENT
    grant_sql: str       # GRANT 模板（用户名已填好，由有权限的人执行）
    affects: list[str]   # 受影响的检查标题

    def to_dict(self) -> dict:
        return {
            "privilege": self.privilege,
            "grant_sql": self.grant_sql,
            "affects": list(self.affects),
        }


@dataclass
class CheckupReport:
    engine: str
    version: str = ""
    scope: str = ""             # 检视的库/schema（展示用）
    started_at: str = ""
    elapsed_ms: int = 0
    overall: Status = "ok"      # 所有检查里最严重的状态
    checks: list[Check] = field(default_factory=list)
    privileges: list[PrivilegeGap] = field(default_factory=list)

    @property
    def summary(self) -> str:
        counts: dict[str, int] = {}
        for c in self.checks:
            counts[c.status] = counts.get(c.status, 0) + 1
        parts = [t("checkup.common.summary_item", n=n, label=label) for label, n in
                 ((t("checkup.common.summary_critical"), counts.get("critical", 0)),
                  (t("checkup.common.summary_warn"), counts.get("warn", 0)),
                  (t("checkup.common.summary_ok"), counts.get("ok", 0)),
                  (t("checkup.common.summary_info"), counts.get("info", 0)),
                  (t("checkup.common.summary_unknown"), counts.get("unknown", 0)))
                 if n]
        sep = ", " if current_locale() == "en" else "、"
        return sep.join(parts) + t("checkup.common.summary_total", n=len(self.checks))

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "version": self.version,
            "scope": self.scope,
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "overall": self.overall,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
            "dimensions": [{"name": k, "title": t(f"checkup.dimension.{k}")} for k, _ in DIMENSIONS],
            "privileges": [p.to_dict() for p in self.privileges],
        }


_STATUS_LABEL_MD = {"ok": "正常", "info": "参考", "warn": "需关注",
                     "critical": "严重", "unknown": "无法测量"}


def report_to_markdown(report: dict) -> str:
    """把 to_dict() 序列化的体检报告渲染成 Markdown（服务端权威渲染）。

    给 AI 诊断当上下文、也给人贴工单用——前端 copyCheckupReport 是另一份给剪贴板的
    实现，两处保持同一套排版口径（摘要 → 维度分组 → 权限缺口），改的时候一起改。
    报告里的值都是已格式化好的文本（如 "<0.1 M"），这里不做再加工。
    """
    rep = report or {}
    out: list[str] = []
    out.append("# 数据库体检报告")
    out.append("")
    out.append(f"- 引擎：{rep.get('engine', '')}{(' ' + rep['version']) if rep.get('version') else ''}"
               + (f" · 范围：{rep['scope']}" if rep.get("scope") else ""))
    if rep.get("started_at"):
        out.append(f"- 体检时间：{rep['started_at']}（耗时 {rep.get('elapsed_ms', 0)} ms）")
    out.append(f"- 总体结论：{rep.get('summary', '')}")
    out.append("")

    # 维度顺序：report 里带 dimensions 就用它（前端浮层同一套），否则回落内置表。
    dim_names: list[str] = []
    dim_titles: dict[str, str] = {}
    for d in rep.get("dimensions") or []:
        if isinstance(d, dict) and d.get("name"):
            dim_names.append(d["name"])
            dim_titles[d["name"]] = d.get("title") or d["name"]
    if not dim_names:
        dim_names = [k for k, _ in DIMENSIONS]
        dim_titles = {k: title for k, title in DIMENSIONS}
    buckets: dict[str, list[dict]] = {}
    for c in rep.get("checks") or []:
        dim = c.get("dimension") or "maintenance"
        buckets.setdefault(dim, []).append(c)

    for dim in dim_names:
        title = dim_titles.get(dim) or dim
        checks = buckets.get(dim)
        if not checks:
            continue
        out.append(f"## {title}")
        for c in checks:
            label = _STATUS_LABEL_MD.get(c.get("status") or "", c.get("status") or "")
            out.append(f"- [{label}] **{c.get('title', '')}**"
                       + (f" — {c['value']}" if c.get("value") else ""))
            if c.get("message"):
                out.append(f"  - {c['message']}")
            for d in c.get("details") or []:
                out.append(f"  - {d}")
        out.append("")

    gaps = rep.get("privileges") or []
    if gaps:
        out.append("## 权限缺口（以下指标因账号权限不足无法测量）")
        for g in gaps:
            out.append(f"- 缺 {g.get('privilege', '')} 权限，影响：{'、'.join(g.get('affects') or [])}")
            out.append("  ```sql")
            out.append(f"  {g.get('grant_sql', '')}")
            out.append("  ```")
        out.append("")
    return "\n".join(out)


# =====================================================================
# 执行辅助
# =====================================================================

def _conn_error_text(e: Exception) -> str:
    """从连接失败里抽一句人可读的原因。

    SQLAlchemy 的 OperationalError 会包成 ``(psycopg.OperationalError) <真原因>
    (Background on SQLAlchemy at: ...)``——把 Background 尾巴和多层包装去掉，
    只留主干。连接错误不含密码（密码从不进异常文本），可安全展示。
    """
    msg = str(getattr(e, "orig", e) or e) or type(e).__name__
    msg = msg.split("\n(Background on SQLAlchemy", 1)[0]
    # 去掉开头的 "(psycopg.OperationalError) " 之类的前缀
    if msg.startswith("("):
        inner = msg.split(") ", 1)
        if len(inner) == 2:
            msg = inner[1]
    return msg.strip()[:300] or type(e).__name__


def connectivity_ok(engine: SAEngine) -> tuple[bool, str]:
    """跑一条最便宜的查询探活。

    整库不可达时没必要让十几项各自报一遍 OperationalError——先探一次，
    不通就直接给一条明确的「连不上」结论。返回 (是否可达, 失败原因)。
    """
    try:
        _rows(engine, "SELECT 1")
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, _conn_error_text(e)



def _rows(engine: SAEngine, sql: str, params: dict | None = None) -> list[list[Any]]:
    """执行一条诊断 SQL，返回行列表。任何错误原样抛给调用方（调用方逐项 catch）。

    失败时显式回滚再抛：诊断语句可能在事务中途失败（PG 会把连接置为 aborted），
    不清理就归还连接池，下一条诊断会被连锁毒害（current transaction is aborted）。
    """
    with engine.connect() as conn:
        try:
            res = conn.execute(text(sql), params or {})
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        if res.returns_rows:
            return [list(r) for r in res.fetchall()]
        return []


def _scalar(engine: SAEngine, sql: str, params: dict | None = None) -> Any:
    rows = _rows(engine, sql, params)
    return rows[0][0] if rows and rows[0] else None


def schema_filter_pg(schema: str | None, column: str = "schemaname") -> str:
    """PG 的 schema 过滤片段：无值时用 current_schema()，有值时用绑定参数。

    不用 ``:s IS NULL OR col = :s``：psycopg 对 NULL 绑参无法推断类型，会直接报
    AmbiguousParameter 让整个检查失败。
    """
    return f"{column} = current_schema()" if not schema else f"{column} = :s"


def schema_filter(engine_kind: str, schema: str | None, column: str) -> str:
    """MySQL/ClickHouse 的库过滤片段：无值时取当前库，有值时用绑定参数。"""
    current = {"clickhouse": "currentDatabase()"}.get(engine_kind, "DATABASE()")
    return f"{column} = {current}" if not schema else f"{column} = :s"


def schema_params(schema: str | None) -> dict:
    """有 schema 时才带上绑定参数（无值时 SQL 里不引用 :s）。"""
    return {"s": schema} if schema else {}


def _to_num(v: Any) -> float | None:
    """状态变量/计数器转数字（MySQL 的 VARIABLE_VALUE 是字符串，PG 可能是 Decimal）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _human_bytes(n: float | None) -> str:
    if n is None:
        return t("checkup.common.unknown")
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _human_duration(seconds: float | None) -> str:
    if seconds is None:
        return t("checkup.common.unknown")
    seconds = float(seconds)
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return t("checkup.common.duration_days_hours", days=days, hours=hours)
    if hours:
        return t("checkup.common.duration_hours_mins", hours=hours, mins=mins)
    if mins:
        return t("checkup.common.duration_mins_secs", mins=mins, secs=secs)
    return t("checkup.common.duration_secs", secs=secs)


def _rate_per_hour(count: float | None, uptime_s: float | None) -> float | None:
    if count is None or uptime_s is None or uptime_s <= 0:
        return None
    return count / uptime_s * 3600


def _worst(checks: list[Check]) -> Status:
    """取最严重的状态。info 与 unknown 同级，但同级里有无法测量的项时 overall 报 unknown
    ——「有指标没测到」不该被读成「一切正常」。"""
    if not checks:
        return "unknown"
    by_level: dict[int, list[Status]] = {}
    for c in checks:
        by_level.setdefault(_STATUS_LEVEL[c.status], []).append(c.status)
    top = max(by_level)
    if top == _STATUS_LEVEL["info"]:
        return "unknown" if "unknown" in by_level[top] else "info"
    return by_level[top][0]


def _current_user(engine: SAEngine, engine_kind: str) -> str:
    """当前连接的账号名，用于填 GRANT 模板。取不到就用占位符。"""
    sql = {"postgres": "SELECT current_user"}.get(engine_kind, "SELECT CURRENT_USER()")
    try:
        v = _scalar(engine, sql)
    except Exception:  # noqa: BLE001
        return t("checkup.common.username_placeholder")
    user = str(v or "")
    # MySQL 的 CURRENT_USER() 形如 'user@host'，GRANT 语句要拆成两个引号串
    if "@" in user and engine_kind != "postgres":
        u, h = user.split("@", 1)
        return f"'{u}'@'{h}'"
    return user


def _collect_gaps(checks: list[Check], user: str) -> list[PrivilegeGap]:
    """把标了 privilege 的 unknown 项按权限聚合，给出可复制的 GRANT 模板。"""
    by_priv: dict[str, list[str]] = {}
    for c in checks:
        if c.privilege:
            by_priv.setdefault(c.privilege, []).append(c.title)
    templates = {
        "pg_monitor": f"GRANT pg_monitor TO {user};",
        "PROCESS": f"GRANT PROCESS ON *.* TO {user};",
        "REPLICATION CLIENT": f"GRANT REPLICATION CLIENT ON *.* TO {user};",
    }
    return [PrivilegeGap(p, templates.get(p, t("checkup.common.grant_missing_privilege", priv=p)),
                         sorted(titles))
            for p, titles in by_priv.items()]


# =====================================================================
# MySQL
# =====================================================================

_MYSQL_SYSTEM_SCHEMAS = ("mysql", "sys", "information_schema", "performance_schema")

# 体检关心的状态变量（performance_schema.global_status 一次取回，避免逐项查询）
_MYSQL_STATUS_VARS = (
    "Uptime", "Threads_connected", "Threads_running", "Connections",
    "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads",
    "Slow_queries", "Select_full_join", "Select_range_join",
    "Aborted_clients", "Aborted_connects", "Innodb_deadlocks",
    # 被 max_connections 拒绝的连接（连接池打满的直接证据，比使用率更准）
    "Connection_errors_max_connection",
    # 缓冲池不足时刷脏被打断的次数（命中率之外更早的预警）
    "Innodb_buffer_pool_wait_free",
    # 临时表落盘比例（排序/哈希超过 tmp_table_size）
    "Created_tmp_tables", "Created_tmp_disk_tables",
    # 事务超过 binlog_cache_size 落临时文件
    "Binlog_cache_use", "Binlog_cache_disk_use",
)
_MYSQL_VARS = ("max_connections", "version", "long_query_time", "innodb_buffer_pool_size")


def _mysql_status(engine: SAEngine) -> tuple[dict[str, str], str]:
    """取全局状态变量。优先 performance_schema（无 PROCESS 权限也能看到真实的全局值），
    失败再退 SHOW GLOBAL STATUS（无 PROCESS 权限时只回会话级值，会误导，标注来源）。

    返回 (变量字典, 来源标注)。来源用于在报告里说明数据可信度。
    """
    try:
        rows = _rows(
            engine,
            "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status"
            " WHERE VARIABLE_NAME IN :vars",
            {"vars": tuple(_MYSQL_STATUS_VARS)},
        )
        return {str(r[0]): str(r[1]) for r in rows}, "performance_schema.global_status"
    except Exception:
        pass
    rows = _rows(engine, "SHOW GLOBAL STATUS")  # 无服务端过滤，客户端筛
    got = {str(r[0]): str(r[1]) for r in rows if str(r[0]) in _MYSQL_STATUS_VARS}
    return got, t("checkup.mysql.status_source_fallback")


def _mysql_variables(engine: SAEngine) -> dict[str, str]:
    try:
        rows = _rows(
            engine,
            "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_variables"
            " WHERE VARIABLE_NAME IN :vars",
            {"vars": tuple(_MYSQL_VARS)},
        )
        return {str(r[0]): str(r[1]) for r in rows}
    except Exception:
        rows = _rows(engine, "SHOW VARIABLES")
        return {str(r[0]): str(r[1]) for r in rows if str(r[0]) in _MYSQL_VARS}


register({
    "checkup.common.count_times": ("{n} 次", "{n}"),
    "checkup.mysql.server.title": ("服务器", "Server"),
    "checkup.mysql.status_source_fallback": (
        "SHOW GLOBAL STATUS（会话级回退值，若无 PROCESS 权限不代表全局）",
        "SHOW GLOBAL STATUS (session-level fallback; without PROCESS privilege this is "
        "not instance-wide)",
    ),
    "checkup.mysql.server.message": ("已运行 {dur}（状态来源：{src}）", "Running for {dur} (status source: {src})"),
    "checkup.mysql.connections.title": ("连接占用", "Connections"),
    "checkup.mysql.connections.detail_rejected": (
        "Connection_errors_max_connection {n} 次（因超 max_connections 被拒绝的连接）",
        "Connection_errors_max_connection: {n} (connections rejected for exceeding max_connections)",
    ),
    "checkup.mysql.connections.msg_rejected": (
        "已有连接因超 max_connections 被拒绝过，池子确实打满过；调大 max_connections 或查应用连接池泄漏",
        "Connections have already been rejected for exceeding max_connections — the pool has "
        "genuinely been full; increase max_connections or check the application for connection "
        "pool leaks",
    ),
    "checkup.mysql.connections.msg_warn": (
        "接近上限时新连接会被拒绝；长连接过多考虑调 max_connections 或查应用连接池泄漏",
        "New connections will be rejected once the limit is reached; if there are too many "
        "long-lived connections, consider raising max_connections or checking for connection "
        "pool leaks in the application",
    ),
    "checkup.mysql.connections.msg_ok": ("连接数在健康范围", "Connection count is within a healthy range"),
    "checkup.mysql.connections.msg_unknown": (
        "取不到 Threads_connected / max_connections（权限或版本不支持）",
        "Could not read Threads_connected / max_connections (insufficient privilege or "
        "unsupported version)",
    ),
    "checkup.mysql.threads_running.title": ("活跃线程", "Active Threads"),
    "checkup.mysql.threads_running.msg_warn": (
        "并发执行中的线程数；持续很高（>50）通常意味着慢查询在堆积",
        "Number of concurrently executing threads; sustained high values (>50) usually indicate "
        "slow queries are piling up",
    ),
    "checkup.mysql.threads_running.msg_info": (
        "参考值：并发执行中的线程数", "Informational: number of concurrently executing threads",
    ),
    "checkup.mysql.threads_running.msg_unknown": ("取不到 Threads_running", "Could not read Threads_running"),
    "checkup.mysql.buffer_pool_hit_ratio.title": (
        "InnoDB 缓冲池命中率", "InnoDB Buffer Pool Hit Ratio",
    ),
    "checkup.mysql.buffer_pool_hit_ratio.msg_bad": (
        "热数据已超出缓冲池，频繁读盘；考虑加大 innodb_buffer_pool_size 或优化全表扫描查询",
        "Hot data no longer fits in the buffer pool, causing frequent disk reads; consider "
        "increasing innodb_buffer_pool_size or optimizing full-table-scan queries",
    ),
    "checkup.mysql.buffer_pool_hit_ratio.msg_ok": (
        "热数据基本都缓存在内存里", "Hot data is mostly cached in memory",
    ),
    "checkup.mysql.buffer_pool_hit_ratio.detail_reads": (
        "逻辑读 {hit} 次，物理读 {disk} 次", "{hit} logical reads, {disk} physical reads",
    ),
    "checkup.mysql.buffer_pool_hit_ratio.detail_wait_free": (
        "Innodb_buffer_pool_wait_free {n} 次（等空闲页的刷脏被打断，缓冲池确实不够）",
        "Innodb_buffer_pool_wait_free: {n} (flushing was interrupted waiting for free pages; "
        "the buffer pool is genuinely undersized)",
    ),
    "checkup.mysql.buffer_pool_hit_ratio.value_no_activity": ("无读取活动", "No read activity"),
    "checkup.mysql.buffer_pool_hit_ratio.msg_no_activity": (
        "启动后还没有足够的读取活动来计算命中率",
        "Not enough read activity since startup to compute a hit ratio",
    ),
    "checkup.mysql.buffer_pool_hit_ratio.msg_unknown": (
        "取不到 Innodb_buffer_pool_read_requests / reads",
        "Could not read Innodb_buffer_pool_read_requests / reads",
    ),
    "checkup.mysql.slow_queries.title": ("慢查询（累计）", "Slow Queries (cumulative)"),
    "checkup.mysql.slow_queries.value_rate": ("{n} 条（约 {rate} 条/小时）", "{n} (about {rate}/hour)"),
    "checkup.mysql.slow_queries.value_plain": ("{n} 条", "{n}"),
    "checkup.mysql.slow_queries.message": (
        "阈值 long_query_time={lqt}s。看具体是哪些查询：查询台按耗时排序，或用 EXPLAIN 排查",
        "Threshold long_query_time={lqt}s. To find which queries: sort by duration in the query "
        "desk, or investigate with EXPLAIN",
    ),
    "checkup.mysql.slow_queries.msg_unknown": ("取不到 Slow_queries", "Could not read Slow_queries"),
    "checkup.mysql.full_scan_joins.title": ("全表扫描 JOIN", "Full-Scan JOINs"),
    "checkup.mysql.full_scan_joins.msg_warn": (
        "JOIN 没走索引（type=ALL），扫描行数被放大；检查被 JOIN 列上是否有索引",
        "The JOIN did not use an index (type=ALL), amplifying the rows scanned; check whether "
        "the joined columns are indexed",
    ),
    "checkup.mysql.full_scan_joins.msg_info": (
        "参考值：累计发生的不走索引的 JOIN 次数",
        "Informational: cumulative count of JOINs that did not use an index",
    ),
    "checkup.mysql.full_scan_joins.msg_unknown": (
        "取不到 Select_full_join", "Could not read Select_full_join",
    ),
    "checkup.mysql.aborted_clients.title": ("异常断开的连接", "Aborted Connections"),
    "checkup.mysql.aborted_clients.rate_suffix": (
        "（约 {rate} 次/小时）", " (about {rate}/hour)",
    ),
    "checkup.mysql.aborted_clients.msg_warn": (
        "客户端没正确关闭连接（连接池泄漏 / 网络抖动 / wait_timeout 过短）",
        "Clients are not closing connections properly (connection pool leak / network "
        "flakiness / wait_timeout too short)",
    ),
    "checkup.mysql.aborted_clients.msg_info": (
        "参考值：客户端未正常关闭的连接数；持续高频才需关注",
        "Informational: count of connections not closed properly by clients; only worth "
        "attention if sustained and frequent",
    ),
    "checkup.mysql.aborted_clients.detail": (
        "Aborted_connects（连接被拒）{n} 次", "Aborted_connects (connections refused): {n}",
    ),
    "checkup.mysql.aborted_clients.msg_unknown": (
        "取不到 Aborted_clients", "Could not read Aborted_clients",
    ),
})


def _mysql_checks(engine: SAEngine, schema: str | None) -> list[Check]:
    checks: list[Check] = []
    status, src = _mysql_status(engine)
    variables = _mysql_variables(engine)
    uptime = _to_num(status.get("Uptime"))
    version = variables.get("version") or t("checkup.common.unknown")

    # --- 服务器信息 ---
    checks.append(Check(
        "server", t("checkup.mysql.server.title"), "info", f"MySQL {version}",
        t("checkup.mysql.server.message", dur=_human_duration(uptime), src=src),
        dimension="availability",
    ))

    # --- 连接占用 ---
    used = _to_num(status.get("Threads_connected"))
    max_conn = _to_num(variables.get("max_connections"))
    rejected = _to_num(status.get("Connection_errors_max_connection"))
    if used is not None and max_conn:
        pct = used / max_conn
        level: Status = "critical" if pct >= CONN_CRIT_PCT else "warn" if pct >= CONN_WARN_PCT else "ok"
        # 有连接被 max_connections 拒绝过 = 池子确实打满过，比使用率更直接
        if rejected and rejected > 0 and level == "ok":
            level = "warn"
        detail = ([t("checkup.mysql.connections.detail_rejected", n=f"{int(rejected):,}")]
                  if rejected else [])
        pctxt = f"({pct:.0%})" if current_locale() == "en" else f"（{pct:.0%}）"
        checks.append(Check(
            "connections", t("checkup.mysql.connections.title"), level,
            f"{int(used)} / {int(max_conn)}{pctxt}",
            (t("checkup.mysql.connections.msg_rejected") if rejected and rejected > 0 else
             t("checkup.mysql.connections.msg_warn"))
            if level != "ok" else t("checkup.mysql.connections.msg_ok"),
            detail, dimension="capacity",
        ))
    else:
        checks.append(Check("connections", t("checkup.mysql.connections.title"), "unknown",
                            t("checkup.common.unknown"), t("checkup.mysql.connections.msg_unknown"),
                            dimension="capacity", privilege="PROCESS"))

    # --- 活跃线程数 ---
    running = _to_num(status.get("Threads_running"))
    if running is not None:
        level = "warn" if running >= 50 else "info"
        checks.append(Check(
            "threads_running", t("checkup.mysql.threads_running.title"), level, str(int(running)),
            t("checkup.mysql.threads_running.msg_warn") if level == "warn"
            else t("checkup.mysql.threads_running.msg_info"),
            dimension="capacity",
        ))
    else:
        checks.append(Check("threads_running", t("checkup.mysql.threads_running.title"), "unknown",
                            t("checkup.common.unknown"), t("checkup.mysql.threads_running.msg_unknown"),
                            dimension="capacity"))

    # --- InnoDB 缓冲池命中率 ---
    hit_req = _to_num(status.get("Innodb_buffer_pool_read_requests"))
    disk_req = _to_num(status.get("Innodb_buffer_pool_reads"))
    if hit_req is not None and disk_req is not None:
        total = hit_req + disk_req
        if total > 0:
            hit = hit_req / total
            level = "critical" if hit < CACHE_HIT_CRIT else "warn" if hit < CACHE_HIT_WARN else "ok"
            # 刷脏被打断过 = 缓冲池压力已到必须等空闲页的程度，即使命中率还行也该扩容
            wait_free = _to_num(status.get("Innodb_buffer_pool_wait_free"))
            if wait_free and wait_free > 0 and level == "ok":
                level = "warn"
            detail = [t("checkup.mysql.buffer_pool_hit_ratio.detail_reads",
                        hit=f"{int(hit_req):,}", disk=f"{int(disk_req):,}")]
            if wait_free and wait_free > 0:
                detail.append(t("checkup.mysql.buffer_pool_hit_ratio.detail_wait_free",
                                n=f"{int(wait_free):,}"))
            checks.append(Check(
                "buffer_pool_hit_ratio", t("checkup.mysql.buffer_pool_hit_ratio.title"), level,
                f"{hit:.2%}",
                t("checkup.mysql.buffer_pool_hit_ratio.msg_bad") if level != "ok"
                else t("checkup.mysql.buffer_pool_hit_ratio.msg_ok"),
                detail, dimension="performance",
            ))
        else:
            checks.append(Check(
                "buffer_pool_hit_ratio", t("checkup.mysql.buffer_pool_hit_ratio.title"), "info",
                t("checkup.mysql.buffer_pool_hit_ratio.value_no_activity"),
                t("checkup.mysql.buffer_pool_hit_ratio.msg_no_activity"),
                dimension="performance"))
    else:
        checks.append(Check(
            "buffer_pool_hit_ratio", t("checkup.mysql.buffer_pool_hit_ratio.title"), "unknown",
            t("checkup.common.unknown"), t("checkup.mysql.buffer_pool_hit_ratio.msg_unknown"),
            dimension="performance"))

    # --- 缓冲池 vs 数据总量（容量规划参考） ---
    checks.append(_mysql_buffer_pool_vs_data(engine, variables, schema))

    # --- 慢查询累计 ---
    slow = _to_num(status.get("Slow_queries"))
    if slow is not None and uptime is not None:
        rate = _rate_per_hour(slow, uptime)
        lqt = variables.get("long_query_time", "?")
        checks.append(Check(
            "slow_queries", t("checkup.mysql.slow_queries.title"),
            "warn" if (rate or 0) > 10 else "info",
            t("checkup.mysql.slow_queries.value_rate", n=f"{int(slow):,}", rate=f"{rate:.1f}")
            if rate is not None else t("checkup.mysql.slow_queries.value_plain", n=f"{int(slow):,}"),
            t("checkup.mysql.slow_queries.message", lqt=lqt),
            dimension="performance",
        ))
    else:
        checks.append(Check("slow_queries", t("checkup.mysql.slow_queries.title"), "unknown",
                            t("checkup.common.unknown"), t("checkup.mysql.slow_queries.msg_unknown"),
                            dimension="performance"))

    # --- 全表扫描 JOIN ---
    fj = _to_num(status.get("Select_full_join"))
    rj = _to_num(status.get("Select_range_join"))
    if fj is not None:
        level = "warn" if (fj > 0 and rj is not None and rj > 0 and fj / (fj + rj) > 0.1) else "info"
        checks.append(Check(
            "full_scan_joins", t("checkup.mysql.full_scan_joins.title"), level,
            t("checkup.common.count_times", n=f"{int(fj):,}"),
            t("checkup.mysql.full_scan_joins.msg_warn") if level == "warn"
            else t("checkup.mysql.full_scan_joins.msg_info"),
            dimension="performance",
        ))
    else:
        checks.append(Check("full_scan_joins", t("checkup.mysql.full_scan_joins.title"), "unknown",
                            t("checkup.common.unknown"), t("checkup.mysql.full_scan_joins.msg_unknown"),
                            dimension="performance"))

    # --- 临时表落盘比例 ---
    checks.append(_mysql_tmp_tables_on_disk(status, uptime))

    # --- 中止的连接 ---
    aborted = _to_num(status.get("Aborted_clients"))
    aborted_conn = _to_num(status.get("Aborted_connects"))
    if aborted is not None:
        rate = _rate_per_hour(aborted, uptime)
        level = "warn" if (rate or 0) > 20 else "info"
        checks.append(Check(
            "aborted_clients", t("checkup.mysql.aborted_clients.title"), level,
            t("checkup.common.count_times", n=f"{int(aborted):,}")
            + (t("checkup.mysql.aborted_clients.rate_suffix", rate=f"{rate:.1f}")
               if rate is not None else ""),
            t("checkup.mysql.aborted_clients.msg_warn") if level == "warn"
            else t("checkup.mysql.aborted_clients.msg_info"),
            [t("checkup.mysql.aborted_clients.detail", n=f"{int(aborted_conn or 0):,}")]
            if aborted_conn else [],
            dimension="availability",
        ))
    else:
        checks.append(Check("aborted_clients", t("checkup.mysql.aborted_clients.title"), "unknown",
                            t("checkup.common.unknown"), t("checkup.mysql.aborted_clients.msg_unknown"),
                            dimension="availability"))

    # --- 死锁 ---
    checks.append(_mysql_deadlocks(engine, status))

    # --- 长查询（performance_schema.threads 不需要 PROCESS 就能看到全部线程） ---
    checks.append(_mysql_long_queries(engine, status))

    # --- 锁等待（优先 data_lock_waits，同样不需要 PROCESS） ---
    checks.append(_mysql_lock_waits(engine))

    # --- 无主键表（row-based 复制下有风险） ---
    checks.append(_mysql_no_primary_key(engine, schema))

    # --- binlog 缓存落盘（大事务超过 binlog_cache_size） ---
    checks.append(_mysql_binlog_cache(status))

    # --- 复制延迟 ---
    checks.append(_mysql_replication_lag(engine))

    # --- 大表 TOP5 ---
    checks.append(_mysql_big_tables(engine, schema))

    for c in checks:
        c.instance_scope = c.name in _MYSQL_INSTANCE_SCOPE
    return checks

# 值在实例上任一库查都一样的检查（全局状态变量 / performance_schema 全局视图）。
# 剩下的（缓冲池 vs 数据量、无主键表、大表 TOP5）才真的按库不同。
_MYSQL_INSTANCE_SCOPE = frozenset({
    "server", "connections", "threads_running", "buffer_pool_hit_ratio",
    "slow_queries", "full_scan_joins", "tmp_tables_on_disk", "aborted_clients",
    "deadlocks", "long_queries", "lock_waits", "binlog_cache", "replication_lag",
})


register({
    "checkup.mysql.deadlocks.title": ("InnoDB 死锁", "InnoDB Deadlocks"),
    "checkup.mysql.deadlocks.msg_unknown": (
        "取不到死锁计数（events_errors_summary_global_by_error 与 Innodb_deadlocks 都不可用）",
        "Could not read deadlock count (neither events_errors_summary_global_by_error nor "
        "Innodb_deadlocks is available)",
    ),
    "checkup.mysql.deadlocks.msg_warn": (
        "有死锁发生：查最近的事务冲突，确保同一批资源按固定顺序加锁",
        "Deadlocks have occurred: review recent transaction conflicts and make sure the same "
        "set of resources is always locked in a fixed order",
    ),
    "checkup.mysql.deadlocks.msg_ok": ("启动以来无死锁", "No deadlocks since startup"),
    "checkup.mysql.long_queries.title": ("长查询", "Long-Running Queries"),
    "checkup.mysql.long_queries.msg_unknown": (
        "查 performance_schema.threads 失败（可能未开启 performance_schema）：{err}",
        "Failed to query performance_schema.threads (performance_schema may not be enabled): {err}",
    ),
    "checkup.mysql.long_queries.value_none": (
        "无 >= {threshold}s 的查询", "No queries >= {threshold}s",
    ),
    "checkup.mysql.long_queries.msg_none": (
        "当前没有长时间执行的查询", "No long-running queries at the moment",
    ),
    "checkup.mysql.long_queries.value_worst": ("{secs}s（最长）", "{secs}s (longest)"),
    "checkup.mysql.long_queries.message": (
        "查询台「取消」可中断（走 KILL QUERY）；再用 EXPLAIN 看是否全表扫描",
        "Use the query desk's Cancel to interrupt it (via KILL QUERY); then check with EXPLAIN "
        "whether it's a full table scan",
    ),
    "checkup.mysql.long_queries.detail": ("{secs}s | {sql}", "{secs}s | {sql}"),
    "checkup.mysql.long_queries.no_sql_text": ("(无 SQL 文本)", "(no SQL text)"),
    "checkup.mysql.lock_waits.title": ("行锁等待", "Row Lock Waits"),
    "checkup.mysql.lock_waits.value": ("{n} 个事务在等锁", "{n} transaction(s) waiting on a lock"),
    "checkup.mysql.lock_waits.msg_warn": (
        "有事务被阻塞；查 INNODB_TRX 看谁持有锁，长事务考虑用查询台取消",
        "Transactions are blocked; check INNODB_TRX to see who holds the lock, and consider "
        "cancelling long-running transactions from the query desk",
    ),
    "checkup.mysql.lock_waits.msg_ok": (
        "当前没有事务在等待行锁", "No transactions are currently waiting on a row lock",
    ),
    "checkup.mysql.lock_waits.msg_unknown": (
        "查锁等待的视图都不可访问（performance_schema.data_lock_waits /"
        " information_schema.INNODB_TRX / sys.innodb_lock_waits）",
        "None of the lock-wait views are accessible (performance_schema.data_lock_waits / "
        "information_schema.INNODB_TRX / sys.innodb_lock_waits)",
    ),
    "checkup.mysql.tmp_tables_on_disk.title": ("临时表落盘", "Temp Tables Spilled to Disk"),
    "checkup.mysql.tmp_tables_on_disk.msg_unknown": (
        "取不到 Created_tmp_tables / Created_tmp_disk_tables",
        "Could not read Created_tmp_tables / Created_tmp_disk_tables",
    ),
    "checkup.mysql.tmp_tables_on_disk.value_none": ("无临时表活动", "No temp table activity"),
    "checkup.mysql.tmp_tables_on_disk.msg_none": (
        "启动后还没创建过临时表", "No temp tables have been created since startup",
    ),
    "checkup.mysql.tmp_tables_on_disk.msg_warn": (
        "排序/哈希超过 tmp_table_size 落了临时磁盘表；调大 tmp_table_size 与 max_heap_table_size，"
        "或优化产生大临时表的查询（看 EXPLAIN 里的 Using temporary）",
        "Sorts/hashes exceeded tmp_table_size and spilled to on-disk temp tables; increase "
        "tmp_table_size and max_heap_table_size, or optimize the queries that create large "
        "temp tables (look for \"Using temporary\" in EXPLAIN)",
    ),
    "checkup.mysql.tmp_tables_on_disk.msg_ok": (
        "临时表基本都在内存完成", "Temp tables are mostly handled in memory",
    ),
    "checkup.mysql.no_primary_key.title": ("无主键表", "Tables Without a Primary Key"),
    "checkup.mysql.no_primary_key.msg_unknown": (
        "查无主键表失败：{err}", "Failed to query tables without a primary key: {err}",
    ),
    "checkup.mysql.no_primary_key.value_ok": ("无", "None"),
    "checkup.mysql.no_primary_key.value_count": ("{n} 张", "{n}"),
    "checkup.mysql.no_primary_key.msg_ok": (
        "所有表都有主键或唯一索引", "Every table has a primary key or a unique index",
    ),
    "checkup.mysql.no_primary_key.msg_warn": (
        "这些表没有主键或唯一非空索引：row-based 复制时回从表要全表扫描，也影响 binlog_group_commit；"
        "给每张表加自增主键（小表也建议）",
        "These tables have no primary key or non-null unique index: with row-based replication "
        "the replica has to full-scan to locate rows, and it also affects binlog_group_commit; "
        "add an auto-increment primary key to every table (recommended even for small tables)",
    ),
    "checkup.mysql.no_primary_key.detail": (
        "{schema}.{table}（约 {rows} 行）", "{schema}.{table} (~{rows} rows)",
    ),
    "checkup.mysql.binlog_cache.title": ("大事务落盘", "Large Transactions Spilled to Disk"),
    "checkup.mysql.binlog_cache.msg_unknown": (
        "取不到 Binlog_cache_use / Binlog_cache_disk_use",
        "Could not read Binlog_cache_use / Binlog_cache_disk_use",
    ),
    "checkup.mysql.binlog_cache.value_none": ("无 binlog 缓存活动", "No binlog cache activity"),
    "checkup.mysql.binlog_cache.msg_none": (
        "该实例没开 binlog 或还没提交过事务",
        "This instance either has binlog disabled or has not committed any transaction yet",
    ),
    "checkup.mysql.binlog_cache.value": (
        "{disk} 次落盘 / {use} 次提交（{pct}）", "{disk} spilled / {use} committed ({pct})",
    ),
    "checkup.mysql.binlog_cache.msg_warn": (
        "有事务超过 binlog_cache_size 落了磁盘：这是大事务的信号，会拖慢复制与提交；"
        "调大 binlog_cache_size，并拆分批量写入",
        "Transactions have exceeded binlog_cache_size and spilled to disk: a sign of large "
        "transactions that slow down replication and commits; increase binlog_cache_size and "
        "split up bulk writes",
    ),
    "checkup.mysql.binlog_cache.msg_ok": (
        "所有事务的 binlog 都在内存缓存完成", "All transactions' binlog fit in the in-memory cache",
    ),
    "checkup.mysql.buffer_pool_vs_data.title": ("缓冲池 vs 数据量", "Buffer Pool vs. Data Size"),
    "checkup.mysql.buffer_pool_vs_data.msg_no_bp": (
        "取不到 innodb_buffer_pool_size", "Could not read innodb_buffer_pool_size",
    ),
    "checkup.mysql.buffer_pool_vs_data.msg_query_failed": (
        "查数据总量失败：{err}", "Failed to query total data size: {err}",
    ),
    "checkup.mysql.buffer_pool_vs_data.value_none": ("无数据表", "No data tables"),
    "checkup.mysql.buffer_pool_vs_data.msg_none": (
        "该库范围内没有用户表", "There are no user tables in this database's scope",
    ),
    "checkup.mysql.buffer_pool_vs_data.value": (
        "数据 {data} / 缓冲池 {bp}（{ratio} 倍）", "data {data} / buffer pool {bp} ({ratio}x)",
    ),
    "checkup.mysql.buffer_pool_vs_data.msg_warn": (
        "数据量远超缓冲池，热数据必然部分在盘上；结合命中率项一起看，"
        "命中率也低就该扩内存或缩小查询范围",
        "The data size far exceeds the buffer pool, so some hot data is necessarily on disk; "
        "cross-check with the hit-ratio item — if that's also low, add memory or narrow the "
        "query range",
    ),
    "checkup.mysql.buffer_pool_vs_data.msg_ok": (
        "数据总量在缓冲池可覆盖的范围内", "The total data size fits within what the buffer pool can cover",
    ),
    "checkup.mysql.replication_lag.title": ("复制延迟", "Replication Lag"),
    "checkup.mysql.replication_lag.value_not_replica": ("非副本", "Not a replica"),
    "checkup.mysql.replication_lag.msg_not_replica": (
        "该实例未配置源/副本复制，无延迟可测",
        "This instance has no source/replica replication configured, so there is no lag to measure",
    ),
    "checkup.mysql.replication_lag.value_null": (
        "源 {host}：延迟未知（NULL）", "Source {host}: lag unknown (NULL)",
    ),
    "checkup.mysql.replication_lag.msg_null": (
        "Seconds_Behind_Master 为 NULL 通常是复制线程断开或正在追赶，查副本状态",
        "Seconds_Behind_Master being NULL usually means the replication thread is disconnected "
        "or catching up; check the replica status",
    ),
    "checkup.mysql.replication_lag.value": ("{lag}s（源 {host}）", "{lag}s (source {host})"),
    "checkup.mysql.replication_lag.msg_warn": (
        "落后过多时读副本数据已旧；查网络 / 大事务 / 长查询是否阻塞了回放线程",
        "If it falls too far behind, reads from the replica return stale data; check whether "
        "the network / a large transaction / a long query is blocking the replay thread",
    ),
    "checkup.mysql.replication_lag.msg_ok": ("副本追平源", "The replica has caught up with the source"),
    "checkup.mysql.replication_lag.msg_unknown": (
        "无副本状态权限（需 REPLICATION CLIENT），或该 MySQL 版本不支持"
        " SHOW REPLICA/SLAVE STATUS",
        "No permission to view replica status (requires REPLICATION CLIENT), or this MySQL "
        "version does not support SHOW REPLICA/SLAVE STATUS",
    ),
    "checkup.mysql.big_tables.title": ("大表 TOP5", "Top 5 Largest Tables"),
    "checkup.mysql.big_tables.msg_unknown": (
        "查 information_schema.tables 失败：{err}",
        "Failed to query information_schema.tables: {err}",
    ),
    "checkup.mysql.big_tables.value_none": ("无表", "No tables"),
    "checkup.mysql.big_tables.msg_none": (
        "该库范围内没有用户表", "There are no user tables in this database's scope",
    ),
    "checkup.mysql.big_tables.msg_none_schema_suffix": ("（schema={schema}）", " (schema={schema})"),
    "checkup.mysql.big_tables.value": ("最大 {name}（{size}）", "largest {name} ({size})"),
    "checkup.mysql.big_tables.message": (
        "最大的几张表是维护成本的主要来源：DDL 变更久、备份慢、全表扫描风险高",
        "The largest tables are the main source of maintenance cost: DDL changes take longer, "
        "backups are slower, and full-table-scan risk is higher",
    ),
    "checkup.mysql.big_tables.detail": (
        "{schema}.{table} — {size}，约 {rows} 行", "{schema}.{table} — {size}, ~{rows} rows",
    ),
})


def _mysql_deadlocks(engine: SAEngine, status: dict[str, str]) -> Check:
    """死锁计数。MySQL 9.x 移除了 Innodb_deadlocks 状态变量，改用 performance_schema
    的错误汇总表（ER_LOCK_DEADLOCK）；8.0 退回状态变量。两路都不通就 unknown。"""
    n = None
    for sql in (
        "SELECT SUM_ERROR_RAISED FROM performance_schema.events_errors_summary_global_by_error"
        " WHERE ERROR_NAME = 'ER_LOCK_DEADLOCK'",
    ):
        try:
            n = _to_num(_scalar(engine, sql))
        except Exception:
            continue
    if n is None:
        n = _to_num(status.get("Innodb_deadlocks"))
    if n is None:
        return Check("deadlocks", t("checkup.mysql.deadlocks.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.mysql.deadlocks.msg_unknown"),
                     dimension="concurrency")
    return Check(
        "deadlocks", t("checkup.mysql.deadlocks.title"), "warn" if n > 0 else "ok",
        t("checkup.common.count_times", n=f"{int(n):,}"),
        t("checkup.mysql.deadlocks.msg_warn") if n > 0 else t("checkup.mysql.deadlocks.msg_ok"),
        dimension="concurrency",
    )


def _mysql_long_queries(engine: SAEngine, status: dict[str, str]) -> Check:
    """长查询：走 performance_schema.threads（无需 PROCESS 权限即可看到全部线程，
    实测 8.4/9.x 都如此），替代 information_schema.PROCESSLIST——后者无 PROCESS 时
    只回自己的会话，会漏掉别人的长查询。

    只看真正的用户前台连接：threads.NAME = 'thread/sql/one_connection'。**必须**用 NAME
    精确圈定——只按 COMMAND 过滤会漏掉复制线程：MySQL 8 并行复制（replica_parallel_workers>0）
    的 replica_io / replica_worker 线程 COMMAND 是 'Connect'（不是 Sleep、也不是 Daemon），
    STATE 是 'Waiting for source to send event' / 'Waiting for an event from Coordinator'，
    它们的 PROCESSLIST_TIME 是**复制连接的存活时长**（≈ 从库运行时间），而 PROCESSLIST_INFO
    恒为空——按 ``NOT IN ('Sleep','Daemon')`` 过滤会把它们当成「运行 28 小时、无 SQL 文本」
    的假 critical（真实 8.0.32 从库、16 个 worker 稳定复现）。同理 event_scheduler /
    compress_gtid_table 是 Daemon，NAME 也不是 one_connection，一并排除。"""
    try:
        rows = _rows(
            engine,
            "SELECT PROCESSLIST_TIME, LEFT(COALESCE(PROCESSLIST_INFO,''), 80), PROCESSLIST_STATE"
            " FROM performance_schema.threads"
            " WHERE NAME = 'thread/sql/one_connection'"
            " AND PROCESSLIST_COMMAND NOT IN ('Sleep', 'Daemon')"
            " AND PROCESSLIST_ID IS NOT NULL"
            " AND PROCESSLIST_TIME >= :t"
            " ORDER BY PROCESSLIST_TIME DESC LIMIT 5",
            {"t": LONG_QUERY_WARN_S},
        )
    except Exception as e:
        return Check("long_queries", t("checkup.mysql.long_queries.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.mysql.long_queries.msg_unknown", err=type(e).__name__),
                     dimension="concurrency")
    if not rows:
        return Check("long_queries", t("checkup.mysql.long_queries.title"), "ok",
                     t("checkup.mysql.long_queries.value_none", threshold=LONG_QUERY_WARN_S),
                     t("checkup.mysql.long_queries.msg_none"), dimension="concurrency")
    worst = max(float(r[0]) for r in rows)
    level: Status = "critical" if worst >= LONG_QUERY_CRIT_S else "warn"
    return Check(
        "long_queries", t("checkup.mysql.long_queries.title"), level,
        t("checkup.mysql.long_queries.value_worst", secs=f"{worst:.0f}"),
        t("checkup.mysql.long_queries.message"),
        [t("checkup.mysql.long_queries.detail", secs=int(r[0]),
           sql=str(r[1]) or t("checkup.mysql.long_queries.no_sql_text")) for r in rows],
        dimension="concurrency",
    )


def _mysql_lock_waits(engine: SAEngine) -> Check:
    """行锁等待。优先 performance_schema.data_lock_waits（无 PROCESS 权限可见），
    再退 information_schema.INNODB_TRX / sys.innodb_lock_waits（需 PROCESS）。"""
    for sql in (
        "SELECT COUNT(*) FROM performance_schema.data_lock_waits",
        "SELECT COUNT(*) FROM information_schema.INNODB_TRX WHERE trx_state = 'LOCK WAIT'",
        "SELECT COUNT(*) FROM sys.innodb_lock_waits",
    ):
        try:
            n = _to_num(_scalar(engine, sql))
            if n is None:
                continue
            return Check(
                "lock_waits", t("checkup.mysql.lock_waits.title"), "warn" if n > 0 else "ok",
                t("checkup.mysql.lock_waits.value", n=int(n)),
                t("checkup.mysql.lock_waits.msg_warn") if n > 0
                else t("checkup.mysql.lock_waits.msg_ok"),
                dimension="concurrency",
            )
        except Exception:
            continue
    return Check("lock_waits", t("checkup.mysql.lock_waits.title"), "unknown",
                 t("checkup.common.unknown"), t("checkup.mysql.lock_waits.msg_unknown"),
                 dimension="concurrency")


def _mysql_tmp_tables_on_disk(status: dict[str, str], uptime: float | None) -> Check:
    """临时表落盘比例：Created_tmp_disk_tables / Created_tmp_tables。超过 1/4 落盘
    说明排序/哈希频繁超过 tmp_table_size，是 Percona/官方调优的常规关注项。"""
    disk = _to_num(status.get("Created_tmp_disk_tables"))
    total = _to_num(status.get("Created_tmp_tables"))
    if disk is None or total is None:
        return Check("tmp_tables_on_disk", t("checkup.mysql.tmp_tables_on_disk.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.mysql.tmp_tables_on_disk.msg_unknown"),
                     dimension="performance")
    if total <= 0:
        return Check("tmp_tables_on_disk", t("checkup.mysql.tmp_tables_on_disk.title"), "ok",
                     t("checkup.mysql.tmp_tables_on_disk.value_none"),
                     t("checkup.mysql.tmp_tables_on_disk.msg_none"), dimension="performance")
    pct = disk / total
    level: Status = "warn" if pct > TMP_DISK_WARN_PCT else "ok"
    disk_frac = f"({int(disk):,}/{int(total):,})" if current_locale() == "en" else f"（{int(disk):,}/{int(total):,}）"
    return Check(
        "tmp_tables_on_disk", t("checkup.mysql.tmp_tables_on_disk.title"), level,
        f"{pct:.0%}{disk_frac}",
        t("checkup.mysql.tmp_tables_on_disk.msg_warn") if level != "ok"
        else t("checkup.mysql.tmp_tables_on_disk.msg_ok"),
        dimension="performance",
    )


def _mysql_no_primary_key(engine: SAEngine, schema: str | None) -> Check:
    """无主键（且无唯一非空索引）的表：row-based 复制下全表扫描才能定位行，
    MySQL 官方文档明确建议每张表都要有主键。"""
    try:
        rows = _rows(
            engine,
            "SELECT t.table_schema, t.table_name, COALESCE(t.table_rows, 0)"
            " FROM information_schema.tables t"
            " LEFT JOIN information_schema.statistics s"
            " ON s.table_schema = t.table_schema AND s.table_name = t.table_name AND s.non_unique = 0"
            f" WHERE t.table_type = 'BASE TABLE' AND {schema_filter('mysql', schema, 't.table_schema')}"
            " AND t.table_schema NOT IN :sys AND s.table_name IS NULL"
            " ORDER BY t.table_rows DESC LIMIT 10",
            {**schema_params(schema), "sys": _MYSQL_SYSTEM_SCHEMAS},
        )
    except Exception as e:
        return Check("no_primary_key", t("checkup.mysql.no_primary_key.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.mysql.no_primary_key.msg_unknown", err=type(e).__name__),
                     dimension="maintenance")
    if not rows:
        return Check("no_primary_key", t("checkup.mysql.no_primary_key.title"), "ok",
                     t("checkup.mysql.no_primary_key.value_ok"),
                     t("checkup.mysql.no_primary_key.msg_ok"), dimension="maintenance")
    return Check(
        "no_primary_key", t("checkup.mysql.no_primary_key.title"), "warn",
        t("checkup.mysql.no_primary_key.value_count", n=len(rows)),
        t("checkup.mysql.no_primary_key.msg_warn"),
        [t("checkup.mysql.no_primary_key.detail", schema=r[0], table=r[1],
           rows=f"{int(float(r[2]) or 0):,}") for r in rows],
        dimension="maintenance",
    )


def _mysql_binlog_cache(status: dict[str, str]) -> Check:
    """Binlog_cache_disk_use：事务超过 binlog_cache_size 落临时文件。大事务是
    复制延迟的主因之一（官方文档：事务太大会在磁盘缓存 binlog 事件）。"""
    use = _to_num(status.get("Binlog_cache_use"))
    disk = _to_num(status.get("Binlog_cache_disk_use"))
    if use is None or disk is None:
        return Check("binlog_cache", t("checkup.mysql.binlog_cache.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.mysql.binlog_cache.msg_unknown"),
                     dimension="replication")
    if use <= 0:
        return Check("binlog_cache", t("checkup.mysql.binlog_cache.title"), "ok",
                     t("checkup.mysql.binlog_cache.value_none"),
                     t("checkup.mysql.binlog_cache.msg_none"), dimension="replication")
    pct = disk / use
    level: Status = "warn" if disk > 0 else "ok"
    return Check(
        "binlog_cache", t("checkup.mysql.binlog_cache.title"), level,
        t("checkup.mysql.binlog_cache.value", disk=f"{int(disk):,}", use=f"{int(use):,}", pct=f"{pct:.0%}"),
        t("checkup.mysql.binlog_cache.msg_warn") if level != "ok"
        else t("checkup.mysql.binlog_cache.msg_ok"),
        dimension="replication",
    )


def _mysql_buffer_pool_vs_data(engine: SAEngine, variables: dict[str, str],
                                schema: str | None) -> Check:
    """缓冲池大小 vs 数据总量：数据量远超缓冲池时热数据必然部分在盘上，
    与命中率项互相印证。"""
    bp = _to_num(variables.get("innodb_buffer_pool_size"))
    if not bp:
        return Check("buffer_pool_vs_data", t("checkup.mysql.buffer_pool_vs_data.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.mysql.buffer_pool_vs_data.msg_no_bp"),
                     dimension="capacity")
    try:
        total = _to_num(_scalar(
            engine,
            "SELECT COALESCE(sum(COALESCE(data_length,0)+COALESCE(index_length,0)), 0)"
            " FROM information_schema.tables"
            f" WHERE {schema_filter('mysql', schema, 'table_schema')}"
            " AND table_schema NOT IN :sys",
            {**schema_params(schema), "sys": _MYSQL_SYSTEM_SCHEMAS},
        ))
    except Exception as e:
        return Check("buffer_pool_vs_data", t("checkup.mysql.buffer_pool_vs_data.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.mysql.buffer_pool_vs_data.msg_query_failed", err=type(e).__name__),
                     dimension="capacity")
    if total is None or total <= 0:
        return Check("buffer_pool_vs_data", t("checkup.mysql.buffer_pool_vs_data.title"), "info",
                     t("checkup.mysql.buffer_pool_vs_data.value_none"),
                     t("checkup.mysql.buffer_pool_vs_data.msg_none"), dimension="capacity")
    ratio = total / bp
    level: Status = "warn" if ratio > BP_VS_DATA_WARN else "ok"
    return Check(
        "buffer_pool_vs_data", t("checkup.mysql.buffer_pool_vs_data.title"), level,
        t("checkup.mysql.buffer_pool_vs_data.value",
          data=_human_bytes(total), bp=_human_bytes(bp), ratio=f"{ratio:.1f}"),
        t("checkup.mysql.buffer_pool_vs_data.msg_warn") if level != "ok"
        else t("checkup.mysql.buffer_pool_vs_data.msg_ok"),
        dimension="capacity",
    )


def _mysql_replication_lag(engine: SAEngine) -> Check:
    for sql in ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"):
        try:
            with engine.connect() as conn:
                res = conn.execute(text(sql))
                if not res.returns_rows:
                    continue
                keys = list(res.keys())
                rows = [list(r) for r in res.fetchall()]
        except Exception:
            continue
        if not rows:
            # 有权限但不是副本（空结果）
            return Check("replication_lag", t("checkup.mysql.replication_lag.title"), "info",
                         t("checkup.mysql.replication_lag.value_not_replica"),
                         t("checkup.mysql.replication_lag.msg_not_replica"), dimension="replication")
        col = next((i for i, k in enumerate(keys)
                    if str(k).lower() in ("seconds_behind_master", "seconds_behind_source")), None)
        if col is None:
            continue
        host = str(rows[0][keys.index("Replica_Host")] if "Replica_Host" in keys else "") or "?"
        val = rows[0][col]
        lag = _to_num(val)
        if lag is None:
            return Check("replication_lag", t("checkup.mysql.replication_lag.title"), "warn",
                         t("checkup.mysql.replication_lag.value_null", host=host),
                         t("checkup.mysql.replication_lag.msg_null"),
                         dimension="replication")
        level: Status = "critical" if lag >= REPL_LAG_CRIT_S else "warn" if lag >= REPL_LAG_WARN_S else "ok"
        return Check(
            "replication_lag", t("checkup.mysql.replication_lag.title"), level,
            t("checkup.mysql.replication_lag.value", lag=f"{lag:.0f}", host=host),
            t("checkup.mysql.replication_lag.msg_warn") if level != "ok"
            else t("checkup.mysql.replication_lag.msg_ok"),
            dimension="replication",
        )
    return Check("replication_lag", t("checkup.mysql.replication_lag.title"), "unknown",
                 t("checkup.common.unknown"), t("checkup.mysql.replication_lag.msg_unknown"),
                 dimension="replication", privilege="REPLICATION CLIENT")


def _mysql_big_tables(engine: SAEngine, schema: str | None) -> Check:
    try:
        rows = _rows(
            engine,
            "SELECT table_schema, table_name,"
            " COALESCE(data_length,0)+COALESCE(index_length,0) AS bytes, table_rows"
            " FROM information_schema.tables"
            f" WHERE {schema_filter('mysql', schema, 'table_schema')}"
            " AND table_schema NOT IN :sys"
            " ORDER BY bytes DESC LIMIT 5",
            {**schema_params(schema), "sys": _MYSQL_SYSTEM_SCHEMAS},
        )
    except Exception as e:
        return Check("big_tables", t("checkup.mysql.big_tables.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.mysql.big_tables.msg_unknown", err=type(e).__name__),
                     dimension="maintenance")
    if not rows:
        return Check("big_tables", t("checkup.mysql.big_tables.title"), "info",
                     t("checkup.mysql.big_tables.value_none"),
                     t("checkup.mysql.big_tables.msg_none")
                     + (t("checkup.mysql.big_tables.msg_none_schema_suffix", schema=schema)
                        if schema else ""),
                     dimension="maintenance")
    return Check(
        "big_tables", t("checkup.mysql.big_tables.title"), "info",
        t("checkup.mysql.big_tables.value", name=str(rows[0][1]), size=_human_bytes(float(rows[0][2]))),
        t("checkup.mysql.big_tables.message"),
        [t("checkup.mysql.big_tables.detail", schema=r[0], table=r[1],
           size=_human_bytes(float(r[2])), rows=f"{int(float(r[3] or 0)):,}") for r in rows],
        dimension="maintenance",
    )


# =====================================================================
# PostgreSQL
# =====================================================================

def _pg_can_see_session_detail(engine: SAEngine) -> bool:
    """pg_stat_activity 的**行**对所有人可见，但 state/query/wait_event **列**对非
    pg_monitor 角色一律返回 NULL——看到行却看不到状态，照样测不了空闲事务/长查询。
    需要看这些列的检查先探测此能力。"""
    try:
        return bool(_scalar(
            engine, "SELECT pg_has_role(current_user, 'pg_monitor', 'member')"))
    except Exception:
        return False


register({
    "checkup.pg.server.title": ("服务器", "Server"),
    "checkup.pg.server.msg_running": ("已运行 {dur}", "Running for {dur}"),
    "checkup.pg.server.msg_unknown_start": ("取不到启动时间", "Could not determine the startup time"),
    "checkup.pg.connections.title": ("连接占用", "Connections"),
    "checkup.pg.connections.msg_err": (
        "查 pg_stat_activity 失败：{err}", "Failed to query pg_stat_activity: {err}",
    ),
    "checkup.pg.connections.msg_warn": (
        "接近上限时新连接会被拒绝；查 idle in transaction 的长连接，或调 max_connections",
        "New connections will be rejected once the limit is reached; check for long-lived "
        "idle-in-transaction connections, or raise max_connections",
    ),
    "checkup.pg.connections.msg_ok": ("连接数在健康范围", "Connection count is within a healthy range"),
    "checkup.pg.connections.msg_no_max_conn": (
        "取不到 max_connections", "Could not read max_connections",
    ),
    "checkup.pg.idle_in_transaction.title": ("空闲事务", "Idle Transactions"),
    "checkup.pg.long_queries.title": ("长查询", "Long-Running Queries"),
    "checkup.pg.wait_events.title": ("等待事件", "Wait Events"),
    "checkup.pg.no_pg_monitor.message": (
        "账号无 pg_monitor 权限：pg_stat_activity 的 state/query/wait_event "
        "列对非 pg_monitor 角色返回 NULL，只能看到自己的会话",
        "The account lacks the pg_monitor role: the state/query/wait_event columns of "
        "pg_stat_activity return NULL for non-pg_monitor roles, so only its own session is visible",
    ),
    "checkup.pg.cache_hit_ratio.title": ("缓存命中率", "Cache Hit Ratio"),
    "checkup.pg.cache_hit_ratio.msg_bad": (
        "shared_buffers 放不下热数据，频繁读盘；调大 shared_buffers 或优化全表扫描查询",
        "shared_buffers cannot hold the hot data, causing frequent disk reads; increase "
        "shared_buffers or optimize full-table-scan queries",
    ),
    "checkup.pg.cache_hit_ratio.msg_ok": (
        "热数据基本都在 shared_buffers 里", "Hot data is mostly cached in shared_buffers",
    ),
    "checkup.pg.cache_hit_ratio.detail": (
        "命中 {hit} 块，读盘 {read} 块", "{hit} blocks hit, {read} blocks read from disk",
    ),
    "checkup.pg.cache_hit_ratio.value_no_activity": ("无读取活动", "No read activity"),
    "checkup.pg.cache_hit_ratio.msg_no_activity": (
        "启动后还没有足够的读取活动来计算命中率",
        "Not enough read activity since startup to compute a hit ratio",
    ),
    "checkup.pg.cache_hit_ratio.msg_unknown": (
        "查 pg_stat_database 失败：{err}", "Failed to query pg_stat_database: {err}",
    ),
    "checkup.pg.deadlocks.title": ("死锁（累计）", "Deadlocks (cumulative)"),
    "checkup.pg.deadlocks.msg_warn": (
        "有死锁发生：查冲突事务，确保同一批资源按固定顺序加锁",
        "Deadlocks have occurred: review the conflicting transactions and make sure the same "
        "set of resources is always locked in a fixed order",
    ),
    "checkup.pg.deadlocks.msg_ok": ("启动以来无死锁", "No deadlocks since startup"),
    "checkup.pg.deadlocks.msg_col_unavailable": (
        "deadlocks 列取不到", "Could not read the deadlocks column",
    ),
    "checkup.pg.deadlocks.msg_query_failed": (
        "查 pg_stat_database.deadlocks 失败：{err}",
        "Failed to query pg_stat_database.deadlocks: {err}",
    ),
})


def _postgres_checks(engine: SAEngine, schema: str | None) -> list[Check]:
    checks: list[Check] = []
    try:
        version = str(_scalar(engine, "SELECT version()")).split(",")[0]
    except Exception:
        version = t("checkup.common.unknown")

    uptime_s = None
    try:
        started = _scalar(engine, "SELECT pg_postmaster_start_time()")
        if isinstance(started, dt.datetime):
            uptime_s = (dt.datetime.now(started.tzinfo) - started).total_seconds()
    except Exception:
        pass
    checks.append(Check(
        "server", t("checkup.pg.server.title"), "info", version,
        t("checkup.pg.server.msg_running", dur=_human_duration(uptime_s)) if uptime_s is not None
        else t("checkup.pg.server.msg_unknown_start"),
        dimension="availability",
    ))

    # --- 连接占用：count(*) 对所有人可见，pg_stat_activity 的行级可见性不受
    # pg_monitor 限制，只有 state 等敏感列才会被置 NULL ---
    max_conn = None
    try:
        max_conn = _to_num(_scalar(engine, "SELECT current_setting('max_connections')::int"))
    except Exception:
        pass
    used, conn_err = None, ""
    try:
        used = _to_num(_scalar(engine, "SELECT count(*) FROM pg_stat_activity"))
    except Exception as e:
        conn_err = t("checkup.pg.connections.msg_err", err=type(e).__name__)
    if used is not None and max_conn:
        pct = used / max_conn
        level: Status = "critical" if pct >= CONN_CRIT_PCT else "warn" if pct >= CONN_WARN_PCT else "ok"
        pctxt = f"({pct:.0%})" if current_locale() == "en" else f"（{pct:.0%}）"
        checks.append(Check(
            "connections", t("checkup.pg.connections.title"), level,
            f"{int(used)} / {int(max_conn)}{pctxt}",
            t("checkup.pg.connections.msg_warn") if level != "ok"
            else t("checkup.pg.connections.msg_ok"),
            dimension="capacity",
        ))
    else:
        checks.append(Check(
            "connections", t("checkup.pg.connections.title"), "unknown", t("checkup.common.unknown"),
            conn_err if used is None else t("checkup.pg.connections.msg_no_max_conn"),
            dimension="capacity",
        ))

    # --- 空闲事务 / 长查询 / 等待事件：需要 state/query/wait_event 列，缺 pg_monitor
    # 时明确标 unknown 并附权限说明（报告汇总成可复制的 GRANT 模板） ---
    can_see = _pg_can_see_session_detail(engine)
    if can_see:
        checks.append(_pg_idle_transactions(engine))
        checks.append(_pg_long_queries(engine))
        checks.append(_pg_wait_events(engine))
    else:
        for name, title in (("idle_in_transaction", t("checkup.pg.idle_in_transaction.title")),
                            ("long_queries", t("checkup.pg.long_queries.title")),
                            ("wait_events", t("checkup.pg.wait_events.title"))):
            checks.append(Check(name, title, "unknown", t("checkup.common.unknown"),
                                t("checkup.pg.no_pg_monitor.message"),
                                dimension="concurrency" if name == "idle_in_transaction" else "performance",
                                privilege="pg_monitor"))

    # --- 缓存命中率（pg_stat_database 无权限限制） ---
    try:
        row = _rows(
            engine,
            "SELECT sum(blks_hit), sum(blks_read) FROM pg_stat_database"
            " WHERE datname = current_database()",
        )
        hit_n, read_n = (_to_num(row[0][0]) if row and row[0][0] is not None else 0.0,
                         _to_num(row[0][1]) if row and row[0][1] is not None else 0.0)
        total = hit_n + read_n
        if total > 0:
            hit = hit_n / total
            level = "critical" if hit < CACHE_HIT_CRIT else "warn" if hit < CACHE_HIT_WARN else "ok"
            checks.append(Check(
                "cache_hit_ratio", t("checkup.pg.cache_hit_ratio.title"), level, f"{hit:.2%}",
                t("checkup.pg.cache_hit_ratio.msg_bad") if level != "ok"
                else t("checkup.pg.cache_hit_ratio.msg_ok"),
                [t("checkup.pg.cache_hit_ratio.detail", hit=f"{int(hit_n):,}", read=f"{int(read_n):,}")],
                dimension="performance",
            ))
        else:
            checks.append(Check(
                "cache_hit_ratio", t("checkup.pg.cache_hit_ratio.title"), "info",
                t("checkup.pg.cache_hit_ratio.value_no_activity"),
                t("checkup.pg.cache_hit_ratio.msg_no_activity"),
                dimension="performance"))
    except Exception as e:
        checks.append(Check(
            "cache_hit_ratio", t("checkup.pg.cache_hit_ratio.title"), "unknown",
            t("checkup.common.unknown"),
            t("checkup.pg.cache_hit_ratio.msg_unknown", err=type(e).__name__),
            dimension="performance"))

    # --- 临时文件落盘（排序/哈希超过 work_mem；pg_stat_database 无权限限制） ---
    checks.append(_pg_temp_files(engine, uptime_s))

    # --- 死锁累计 ---
    try:
        n = _to_num(_scalar(
            engine,
            "SELECT sum(deadlocks) FROM pg_stat_database WHERE datname = current_database()"))
        if n is not None:
            checks.append(Check(
                "deadlocks", t("checkup.pg.deadlocks.title"), "warn" if n > 0 else "ok",
                t("checkup.common.count_times", n=f"{int(n):,}"),
                t("checkup.pg.deadlocks.msg_warn") if n > 0 else t("checkup.pg.deadlocks.msg_ok"),
                dimension="concurrency",
            ))
        else:
            checks.append(Check("deadlocks", t("checkup.pg.deadlocks.title"), "unknown",
                                t("checkup.common.unknown"),
                                t("checkup.pg.deadlocks.msg_col_unavailable"), dimension="concurrency"))
    except Exception as e:
        checks.append(Check("deadlocks", t("checkup.pg.deadlocks.title"), "unknown",
                            t("checkup.common.unknown"),
                            t("checkup.pg.deadlocks.msg_query_failed", err=type(e).__name__),
                            dimension="concurrency"))

    # --- 死元组膨胀（该 autovacuum 了） ---
    checks.append(_pg_bloat(engine, schema))

    # --- 统计信息过期（last_analyze 太久；pg_stat_user_tables 无权限限制） ---
    checks.append(_pg_stats_stale(engine, schema))

    # --- 未使用索引（pg_stat_user_indexes 无权限限制） ---
    checks.append(_pg_unused_indexes(engine, schema))

    # --- 复制延迟（需 pg_monitor 才能看到 pg_stat_replication） ---
    checks.append(_pg_replication_lag(engine, can_see))

    # --- 复制槽健康度（pg_replication_slots 无权限限制，非活跃槽会撑爆 pg_wal） ---
    checks.append(_pg_replication_slots(engine))

    # --- WAL 归档失败（pg_stat_archiver 无权限限制） ---
    checks.append(_pg_archiver(engine))

    # --- 事务 ID 回卷风险（pg_database 无权限限制，PG 独有的强制只读风险） ---
    checks.append(_pg_xid_wraparound(engine))

    # --- 库与大表 ---
    checks.append(_pg_sizes(engine, schema))

    for c in checks:
        c.instance_scope = c.name in _PG_INSTANCE_SCOPE
    return checks

# 实例级：pg_stat_activity / pg_stat_replication / pg_replication_slots /
# pg_stat_archiver / pg_database 都是整实例可见的。
# 库级（不在此列）：缓存命中率、死锁、临时文件（pg_stat_database 按 current_database()
# 过滤）、膨胀、统计信息过期、未用索引、库与大表大小。
_PG_INSTANCE_SCOPE = frozenset({
    "server", "connections", "idle_in_transaction", "long_queries", "wait_events",
    "replication_lag", "replication_slots", "archiver", "xid_wraparound",
})


register({
    "checkup.pg.idle_in_transaction.msg_err": (
        "查 pg_stat_activity 失败：{err}", "Failed to query pg_stat_activity: {err}",
    ),
    "checkup.pg.idle_in_transaction.value_none": (
        "无超过 {threshold}s 的空闲事务", "No idle transactions over {threshold}s",
    ),
    "checkup.pg.idle_in_transaction.msg_none": (
        "空闲事务拿着锁又挡 autovacuum，越长越该清掉",
        "Idle transactions hold locks and block autovacuum; the longer they persist, the more "
        "they should be cleared",
    ),
    "checkup.pg.idle_in_transaction.value_worst": ("{secs}s（最长）", "{secs}s (longest)"),
    "checkup.pg.idle_in_transaction.message": (
        "有事务开了不提交也不干活，挡住 autovacuum 并持锁；查应用是否漏提交，或取消该会话",
        "A transaction is open but neither committing nor doing work, blocking autovacuum while "
        "holding locks; check whether the application is missing a commit, or cancel the session",
    ),
    "checkup.pg.idle_in_transaction.detail": ("pid {pid} · {secs}s | {sql}", "pid {pid} · {secs}s | {sql}"),
    "checkup.pg.long_queries.msg_err": (
        "查 pg_stat_activity 失败：{err}", "Failed to query pg_stat_activity: {err}",
    ),
    "checkup.pg.long_queries.value_none": (
        "无 >= {threshold}s 的查询", "No queries >= {threshold}s",
    ),
    "checkup.pg.long_queries.msg_none": (
        "当前没有长时间执行的查询", "No long-running queries at the moment",
    ),
    "checkup.pg.long_queries.value_worst": ("{secs}s（最长）", "{secs}s (longest)"),
    "checkup.pg.long_queries.message": (
        "用 pg_cancel_backend(pid) 或查询台「取消」中断；再 EXPLAIN 看是否全表扫描",
        "Interrupt it with pg_cancel_backend(pid) or the query desk's Cancel; then check with "
        "EXPLAIN whether it's a full table scan",
    ),
    "checkup.pg.long_queries.detail": ("pid {pid} · {secs}s | {sql}", "pid {pid} · {secs}s | {sql}"),
    "checkup.pg.wait_events.msg_err": (
        "查 pg_stat_activity 等待事件失败：{err}",
        "Failed to query pg_stat_activity wait events: {err}",
    ),
    "checkup.pg.wait_events.value_none": ("无会话在等待", "No sessions are waiting"),
    "checkup.pg.wait_events.msg_none": (
        "所有后端进程都在执行而非等待", "All backend processes are executing, not waiting",
    ),
    "checkup.pg.wait_events.value": ("TOP {name}（{n} 个会话）", "top {name} ({n} sessions)"),
    "checkup.pg.wait_events.msg_warn": (
        "等待事件说明会话卡在哪里；Lock 类等待多时查持锁的 idle in transaction 会话",
        "Wait events show where sessions are stuck; when Lock waits are frequent, check for "
        "idle-in-transaction sessions holding locks",
    ),
    "checkup.pg.wait_events.msg_info": (
        "参考值：当前各会话的等待事件分布", "Informational: the current distribution of wait events across sessions",
    ),
    "checkup.pg.wait_events.detail": ("{type}/{event} — {n} 个", "{type}/{event} — {n}"),
    "checkup.pg.temp_files.title": ("临时文件落盘", "Temp Files Spilled to Disk"),
    "checkup.pg.temp_files.msg_err": (
        "查 pg_stat_database 失败：{err}", "Failed to query pg_stat_database: {err}",
    ),
    "checkup.pg.temp_files.msg_no_row": (
        "pg_stat_database 没有当前库的行", "pg_stat_database has no row for the current database",
    ),
    "checkup.pg.temp_files.msg_no_col": ("temp_files 列取不到", "Could not read the temp_files column"),
    "checkup.pg.temp_files.value_none": ("无临时文件", "No temp files"),
    "checkup.pg.temp_files.msg_none": (
        "启动以来没有查询把数据写到临时文件",
        "No query has written data to a temp file since startup",
    ),
    "checkup.pg.temp_files.value": ("{n} 个文件 / {size}", "{n} files / {size}"),
    "checkup.pg.temp_files.rate_suffix": ("（约 {rate} 个/小时）", " (about {rate}/hour)"),
    "checkup.pg.temp_files.msg_warn": (
        "查询的排序/哈希超过 work_mem 落了临时文件；调大 work_mem，或用 EXPLAIN 找出"
        " Using filesort / hash spill 的查询",
        "Queries' sorts/hashes exceeded work_mem and spilled to temp files; increase work_mem, "
        "or use EXPLAIN to find the queries using filesort / a hash spill",
    ),
    "checkup.pg.temp_files.msg_info": (
        "参考值：累计临时文件数与字节数", "Informational: cumulative temp file count and bytes",
    ),
    "checkup.pg.bloat.title": ("死元组膨胀", "Dead Tuple Bloat"),
    "checkup.pg.bloat.msg_err": (
        "查 pg_stat_user_tables 失败：{err}", "Failed to query pg_stat_user_tables: {err}",
    ),
    "checkup.pg.bloat.value_none": (
        "无超过 {threshold} 死元组的表", "No table with more than {threshold} dead tuples",
    ),
    "checkup.pg.bloat.msg_none": (
        "死元组由 autovacuum 回收；表删除/更新频繁时关注此项",
        "Dead tuples are reclaimed by autovacuum; watch this item on tables with frequent "
        "deletes/updates",
    ),
    "checkup.pg.bloat.value": ("最严重 {name}（{n} 死元组）", "worst {name} ({n} dead tuples)"),
    "checkup.pg.bloat.message": (
        "死元组过多说明 autovacuum 跟不上；查 last_autovacuum 是否太久没跑，必要时手动 VACUUM",
        "Too many dead tuples means autovacuum can't keep up; check whether last_autovacuum has "
        "not run in a long time, and run VACUUM manually if needed",
    ),
    "checkup.pg.bloat.detail": (
        "{name} — 死 {dead} / 活 {live}，上次 autovacuum {last}",
        "{name} — dead {dead} / live {live}, last autovacuum {last}",
    ),
    "checkup.pg.bloat.never": ("从未", "never"),
    "checkup.pg.stats_stale.title": ("统计信息过期", "Stale Statistics"),
    "checkup.pg.stats_stale.msg_err": (
        "查 pg_stat_user_tables 失败：{err}", "Failed to query pg_stat_user_tables: {err}",
    ),
    "checkup.pg.stats_stale.value_none": (
        "全部在 {days} 天内分析过", "All tables analyzed within {days} days",
    ),
    "checkup.pg.stats_stale.msg_none": (
        "统计信息新鲜，规划器能拿到准确的行数估计",
        "Statistics are fresh, so the planner gets accurate row estimates",
    ),
    "checkup.pg.stats_stale.value": (
        "{n} 张表超过 {days} 天未分析", "{n} table(s) not analyzed in over {days} days",
    ),
    "checkup.pg.stats_stale.suffix_age": ("（最久 {days} 天）", " (oldest: {days} days)"),
    "checkup.pg.stats_stale.suffix_never": ("（从未分析）", " (never analyzed)"),
    "checkup.pg.stats_stale.message": (
        "统计信息过期会让规划器选错索引；查 autovacuum 是否在跑，必要时手动 ANALYZE",
        "Stale statistics can lead the planner to pick the wrong index; check whether "
        "autovacuum is running, and run ANALYZE manually if needed",
    ),
    "checkup.pg.stats_stale.detail_never": ("{name} — 从未分析", "{name} — never analyzed"),
    "checkup.pg.stats_stale.detail_ago": ("{name} — {dur} 前", "{name} — {dur} ago"),
    "checkup.pg.unused_indexes.title": ("未使用索引", "Unused Indexes"),
    "checkup.pg.unused_indexes.msg_err": (
        "查 pg_stat_user_indexes 失败：{err}", "Failed to query pg_stat_user_indexes: {err}",
    ),
    "checkup.pg.unused_indexes.value_none": ("无", "None"),
    "checkup.pg.unused_indexes.msg_none": (
        "所有非唯一索引都被使用过（或没有非唯一索引）",
        "Every non-unique index has been used at least once (or there are none)",
    ),
    "checkup.pg.unused_indexes.value": (
        "{n} 个从未使用（合计 {size}）", "{n} never used (totaling {size})",
    ),
    "checkup.pg.unused_indexes.message": (
        "这些索引自统计重置以来从未被扫描，却要为每次写入付出维护成本；"
        "确认无用后删除可减少写放大并回收空间",
        "These indexes have never been scanned since statistics were last reset, yet still cost "
        "maintenance on every write; after confirming they're unused, dropping them reduces "
        "write amplification and reclaims space",
    ),
    "checkup.pg.unused_indexes.detail": ("{table}.{index} — {size}", "{table}.{index} — {size}"),
    "checkup.pg.replication_lag.title": ("复制延迟", "Replication Lag"),
    "checkup.pg.replication_lag.msg_no_pg_monitor": (
        "账号无 pg_monitor 权限，pg_stat_replication 只能看到自己的会话",
        "The account lacks the pg_monitor role; pg_stat_replication only shows its own session",
    ),
    "checkup.pg.replication_lag.msg_err": (
        "查 pg_stat_replication 失败（通常缺 pg_monitor 权限）：{err}",
        "Failed to query pg_stat_replication (usually missing the pg_monitor role): {err}",
    ),
    "checkup.pg.replication_lag.value_none": ("无流复制", "No streaming replication"),
    "checkup.pg.replication_lag.msg_none": (
        "没有连接的流复制备用节点，或该实例是主节点且无订阅者",
        "There is no connected streaming-replication standby, or this instance is a primary "
        "with no subscribers",
    ),
    "checkup.pg.replication_lag.value": ("{secs}s（最慢备库）", "{secs}s (slowest standby)"),
    "checkup.pg.replication_lag.msg_warn": (
        "写/刷盘/回放延迟之和过大时备库数据已旧；查网络、大事务或备库长查询",
        "When the sum of write/flush/replay lag is too large, the standby's data is stale; "
        "check the network, large transactions, or long-running queries on the standby",
    ),
    "checkup.pg.replication_lag.msg_ok": ("所有备库追平主库", "All standbys have caught up with the primary"),
    "checkup.pg.replication_lag.detail": (
        "{name}（{addr}）总延迟 {secs}s", "{name} ({addr}) total lag {secs}s",
    ),
    "checkup.pg.replication_slots.title": ("复制槽健康度", "Replication Slot Health"),
    "checkup.pg.replication_slots.msg_err": (
        "查 pg_replication_slots 失败：{err}", "Failed to query pg_replication_slots: {err}",
    ),
    "checkup.pg.replication_slots.value_none": ("无复制槽", "No replication slots"),
    "checkup.pg.replication_slots.msg_none": (
        "该实例没有配置复制槽（物理/逻辑都没有）",
        "This instance has no replication slots configured (neither physical nor logical)",
    ),
    "checkup.pg.replication_slots.msg_ok": (
        "所有复制槽都在正常消费 WAL", "All replication slots are consuming WAL normally",
    ),
    "checkup.pg.replication_slots.msg_wal_status_critical": (
        "复制槽 {slot} 的 wal_status={status}：保留的 WAL 已不安全",
        "Replication slot {slot} has wal_status={status}: the retained WAL is no longer safe",
    ),
    "checkup.pg.replication_slots.msg_wal_status_warn": (
        "复制槽 {slot} 的 wal_status={status}：WAL 保留量已超上限",
        "Replication slot {slot} has wal_status={status}: WAL retention has exceeded its limit",
    ),
    "checkup.pg.replication_slots.msg_lag_critical": (
        "复制槽滞后过大：消费方不拉 WAL，pg_wal 会持续堆积直到撑爆磁盘",
        "A replication slot is lagging severely: the consumer isn't pulling WAL, and pg_wal will "
        "keep accumulating until it fills the disk",
    ),
    "checkup.pg.replication_slots.msg_lag_warn": (
        "复制槽滞后较大：消费方不拉 WAL，pg_wal 会持续堆积",
        "A replication slot is lagging significantly: the consumer isn't pulling WAL, and pg_wal "
        "will keep accumulating",
    ),
    "checkup.pg.replication_slots.msg_inactive": (
        "有非活跃的复制槽：消费方断开后 WAL 仍在堆积，确认槽是否仍需要",
        "There is an inactive replication slot: WAL keeps accumulating after the consumer "
        "disconnects; confirm whether the slot is still needed",
    ),
    "checkup.pg.replication_slots.value": ("最滞后 {name}（{size}）", "most lagging {name} ({size})"),
    "checkup.pg.replication_slots.value_safe_suffix": (
        "，{name} 离丢数据仅剩 {size}", "; {name} has only {size} left before data loss",
    ),
    "checkup.pg.replication_slots.msg_drop_suffix": (
        "；不再需要的槽用 pg_drop_replication_slot 删掉",
        "; drop slots that are no longer needed with pg_drop_replication_slot",
    ),
    "checkup.pg.replication_slots.detail": (
        "{name}（{type}，{active}{wal_status}）滞后 {lag}",
        "{name} ({type}, {active}{wal_status}) lag {lag}",
    ),
    "checkup.pg.replication_slots.active_yes": ("活跃", "active"),
    "checkup.pg.replication_slots.active_no": ("未活跃", "inactive"),
    "checkup.pg.replication_slots.wal_status_suffix": ("，wal_status={status}", ", wal_status={status}"),
    "checkup.pg.archiver.title": ("WAL 归档", "WAL Archiving"),
    "checkup.pg.archiver.msg_err": (
        "查 pg_stat_archiver 失败：{err}", "Failed to query pg_stat_archiver: {err}",
    ),
    "checkup.pg.archiver.msg_no_data": (
        "pg_stat_archiver 没有数据", "pg_stat_archiver has no data",
    ),
    "checkup.pg.archiver.msg_no_col": (
        "failed_count 列取不到", "Could not read the failed_count column",
    ),
    "checkup.pg.archiver.value_ok": (
        "已归档 {n} 段，无失败", "{n} segments archived, no failures",
    ),
    "checkup.pg.archiver.msg_ok": (
        "archive_command 一直在正常工作", "archive_command has been working normally",
    ),
    "checkup.pg.archiver.value_warn": ("{n} 次失败", "{n} failure(s)"),
    "checkup.pg.archiver.msg_warn": (
        "归档失败会让 pg_wal 无法回收（撑爆磁盘）且时间点恢复不可用；"
        "查 archive_command 与 last_failed_time",
        "Archive failures prevent pg_wal from being reclaimed (filling the disk) and make "
        "point-in-time recovery unavailable; check archive_command and last_failed_time",
    ),
    "checkup.pg.archiver.detail_last_fail": ("最近失败段：{wal}", "Most recent failed segment: {wal}"),
    "checkup.pg.archiver.detail_no_last_fail": (
        "last_failed_wal 为空（旧的失败记录已轮换）",
        "last_failed_wal is empty (the old failure record has rotated out)",
    ),
    "checkup.pg.xid_wraparound.title": ("事务 ID 回卷风险", "Transaction ID Wraparound Risk"),
    "checkup.pg.xid_wraparound.msg_err": (
        "查 pg_database.datfrozenxid 失败：{err}", "Failed to query pg_database.datfrozenxid: {err}",
    ),
    "checkup.pg.xid_wraparound.msg_no_age": (
        "age(datfrozenxid) 取不到", "Could not read age(datfrozenxid)",
    ),
    "checkup.pg.xid_wraparound.msg_no_remaining": (
        "剩余事务数算不出来", "Could not compute the remaining transaction count",
    ),
    "checkup.pg.xid_wraparound.value": ("剩余 {m}M 个事务 ID", "{m}M transaction IDs remaining"),
    "checkup.pg.xid_wraparound.msg_warn": (
        "剩余事务号不足时库会被强制只读（防回卷），在此之前必须让 autovacuum 把"
        " datfrozenxid 推进；查是否有长事务挡住 vacuum（idle_in_transaction 项）",
        "When the remaining transaction IDs run low, the database is forced read-only (to "
        "prevent wraparound); before that happens, autovacuum must be allowed to advance "
        "datfrozenxid — check whether a long transaction is blocking vacuum (see the "
        "idle-in-transaction item)",
    ),
    "checkup.pg.xid_wraparound.msg_ok": (
        "各库的 datfrozenxid 都在被正常推进", "Every database's datfrozenxid is advancing normally",
    ),
    "checkup.pg.xid_wraparound.detail": (
        "最老的库已用 {m}M 个事务 ID（上限 2147M）", "The oldest database has used {m}M transaction IDs (limit 2147M)",
    ),
    "checkup.pg.sizes.title": ("库大小与大表 TOP5", "Database Size & Top 5 Largest Tables"),
    "checkup.pg.sizes.msg_err": ("查 pg_class 大小失败：{err}", "Failed to query pg_class sizes: {err}"),
    "checkup.pg.sizes.value": ("库 {total}", "database {total}"),
    "checkup.pg.sizes.value_biggest_suffix": ("，最大表 {name}", ", largest table {name}"),
    "checkup.pg.sizes.message": (
        "最大的几张表是维护成本的主要来源：DDL 变更久、备份慢、全表扫描风险高",
        "The largest tables are the main source of maintenance cost: DDL changes take longer, "
        "backups are slower, and full-table-scan risk is higher",
    ),
    "checkup.pg.sizes.detail": ("{name} — {size}", "{name} — {size}"),
})


def _pg_idle_transactions(engine: SAEngine) -> Check:
    try:
        rows = _rows(
            engine,
            "SELECT pid, extract(epoch FROM (now() - xact_start))::int, left(query, 80)"
            " FROM pg_stat_activity"
            " WHERE state = 'idle in transaction'"
            "   AND now() - xact_start > make_interval(secs => :s)"
            " ORDER BY xact_start LIMIT 5",
            {"s": IDLE_TXN_WARN_S},
        )
    except Exception as e:
        return Check("idle_in_transaction", t("checkup.pg.idle_in_transaction.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.pg.idle_in_transaction.msg_err", err=type(e).__name__),
                     dimension="concurrency")
    if not rows:
        return Check("idle_in_transaction", t("checkup.pg.idle_in_transaction.title"), "ok",
                     t("checkup.pg.idle_in_transaction.value_none", threshold=IDLE_TXN_WARN_S),
                     t("checkup.pg.idle_in_transaction.msg_none"),
                     dimension="concurrency")
    worst = max(float(r[1]) for r in rows)
    return Check(
        "idle_in_transaction", t("checkup.pg.idle_in_transaction.title"), "warn",
        t("checkup.pg.idle_in_transaction.value_worst", secs=f"{worst:.0f}"),
        t("checkup.pg.idle_in_transaction.message"),
        [t("checkup.pg.idle_in_transaction.detail", pid=r[0], secs=int(float(r[1])), sql=str(r[2]))
         for r in rows],
        dimension="concurrency",
    )


def _pg_long_queries(engine: SAEngine) -> Check:
    try:
        rows = _rows(
            engine,
            "SELECT pid, extract(epoch FROM (now() - query_start))::int, left(query, 80)"
            " FROM pg_stat_activity"
            " WHERE state = 'active' AND query_start IS NOT NULL"
            "   AND now() - query_start > make_interval(secs => :s)"
            " ORDER BY query_start DESC LIMIT 5",
            {"s": LONG_QUERY_WARN_S},
        )
    except Exception as e:
        return Check("long_queries", t("checkup.pg.long_queries.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.pg.long_queries.msg_err", err=type(e).__name__), dimension="performance")
    if not rows:
        return Check("long_queries", t("checkup.pg.long_queries.title"), "ok",
                     t("checkup.pg.long_queries.value_none", threshold=LONG_QUERY_WARN_S),
                     t("checkup.pg.long_queries.msg_none"), dimension="performance")
    worst = max(float(r[1]) for r in rows)
    level: Status = "critical" if worst >= LONG_QUERY_CRIT_S else "warn"
    return Check(
        "long_queries", t("checkup.pg.long_queries.title"), level,
        t("checkup.pg.long_queries.value_worst", secs=f"{worst:.0f}"),
        t("checkup.pg.long_queries.message"),
        [t("checkup.pg.long_queries.detail", pid=r[0], secs=int(float(r[1])), sql=str(r[2]))
         for r in rows],
        dimension="performance",
    )


def _pg_wait_events(engine: SAEngine) -> Check:
    """等待事件 TOP：PG 定位瓶颈的标准手段（官方文档「等待事件」章节）。"""
    try:
        rows = _rows(
            engine,
            "SELECT wait_event_type, wait_event, count(*)"
            " FROM pg_stat_activity"
            " WHERE wait_event IS NOT NULL AND pid <> pg_backend_pid()"
            " GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5",
        )
    except Exception as e:
        return Check("wait_events", t("checkup.pg.wait_events.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.pg.wait_events.msg_err", err=type(e).__name__),
                     dimension="performance")
    if not rows:
        return Check("wait_events", t("checkup.pg.wait_events.title"), "ok",
                     t("checkup.pg.wait_events.value_none"),
                     t("checkup.pg.wait_events.msg_none"), dimension="performance")
    locks = sum(int(r[2]) for r in rows if str(r[0]) == "Lock")
    level: Status = "warn" if locks >= 5 else "info"
    return Check(
        "wait_events", t("checkup.pg.wait_events.title"), level,
        t("checkup.pg.wait_events.value", name=str(rows[0][1]), n=int(rows[0][2])),
        t("checkup.pg.wait_events.msg_warn") if level == "warn"
        else t("checkup.pg.wait_events.msg_info"),
        [t("checkup.pg.wait_events.detail", type=r[0], event=r[1], n=int(r[2])) for r in rows],
        dimension="performance",
    )


def _pg_temp_files(engine: SAEngine, uptime_s: float | None) -> Check:
    """临时文件落盘：排序/哈希超过 work_mem 写临时文件，是查询性能与 work_mem
    调优的核心信号（官方文档 pg_stat_database.temp_files）。"""
    try:
        row = _rows(
            engine,
            "SELECT temp_files, temp_bytes FROM pg_stat_database"
            " WHERE datname = current_database()",
        )
    except Exception as e:
        return Check("temp_files", t("checkup.pg.temp_files.title"),
                     "unknown", t("checkup.common.unknown"),
                     t("checkup.pg.temp_files.msg_err", err=type(e).__name__), dimension="performance")
    if not row:
        return Check("temp_files", t("checkup.pg.temp_files.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.pg.temp_files.msg_no_row"),
                     dimension="performance")
    files = _to_num(row[0][0])
    bytes_ = _to_num(row[0][1])
    if files is None:
        return Check("temp_files", t("checkup.pg.temp_files.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.pg.temp_files.msg_no_col"),
                     dimension="performance")
    if files <= 0:
        return Check("temp_files", t("checkup.pg.temp_files.title"), "ok",
                     t("checkup.pg.temp_files.value_none"),
                     t("checkup.pg.temp_files.msg_none"), dimension="performance")
    rate = _rate_per_hour(files, uptime_s)
    level: Status = "warn" if (rate or 0) > 10 else "info"
    return Check(
        "temp_files", t("checkup.pg.temp_files.title"), level,
        t("checkup.pg.temp_files.value", n=f"{int(files):,}", size=_human_bytes(bytes_))
        + (t("checkup.pg.temp_files.rate_suffix", rate=f"{rate:.1f}") if rate is not None else ""),
        t("checkup.pg.temp_files.msg_warn") if level == "warn"
        else t("checkup.pg.temp_files.msg_info"),
        dimension="performance",
    )


def _pg_bloat(engine: SAEngine, schema: str | None) -> Check:
    try:
        rows = _rows(
            engine,
            "SELECT relname, n_dead_tup, n_live_tup, last_autovacuum::text"
            " FROM pg_stat_user_tables"
            f" WHERE n_dead_tup >= :n AND {schema_filter_pg(schema)}"
            " ORDER BY n_dead_tup DESC LIMIT 5",
            {"n": DEAD_TUPLE_WARN, **({"s": schema} if schema else {})},
        )
    except Exception as e:
        return Check("bloat", t("checkup.pg.bloat.title"), "unknown", t("checkup.common.unknown"),
                     t("checkup.pg.bloat.msg_err", err=type(e).__name__), dimension="maintenance")
    if not rows:
        return Check("bloat", t("checkup.pg.bloat.title"), "ok",
                     t("checkup.pg.bloat.value_none", threshold=DEAD_TUPLE_WARN),
                     t("checkup.pg.bloat.msg_none"),
                     dimension="maintenance")
    return Check(
        "bloat", t("checkup.pg.bloat.title"), "warn",
        t("checkup.pg.bloat.value", name=str(rows[0][0]), n=f"{int(float(rows[0][1])):,}"),
        t("checkup.pg.bloat.message"),
        [t("checkup.pg.bloat.detail", name=r[0], dead=f"{int(float(r[1])):,}",
           live=f"{int(float(r[2]) or 0):,}", last=r[3] or t("checkup.pg.bloat.never"))
         for r in rows],
        dimension="maintenance",
    )


def _pg_stats_stale(engine: SAEngine, schema: str | None) -> Check:
    """统计信息过期：超过 N 天未 ANALYZE 的表，规划器只能用过时统计，是烂执行计划的
    常见来源（官方文档建议关注 last_analyze）。"""
    try:
        rows = _rows(
            engine,
            "SELECT relname,"
            " COALESCE(last_analyze, last_autoanalyze) AS last_an,"
            " extract(epoch FROM (now() - COALESCE(last_analyze, last_autoanalyze)))::int"
            f" FROM pg_stat_user_tables WHERE {schema_filter_pg(schema)}"
            " ORDER BY last_an NULLS FIRST LIMIT 10",
            {"s": schema} if schema else None,
        )
    except Exception as e:
        return Check("stats_stale", t("checkup.pg.stats_stale.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.pg.stats_stale.msg_err", err=type(e).__name__), dimension="maintenance")
    stale = [r for r in rows
             if r[1] is None or (_to_num(r[2]) or 0) > STATS_STALE_DAYS * 86400]
    if not stale:
        return Check("stats_stale", t("checkup.pg.stats_stale.title"), "ok",
                     t("checkup.pg.stats_stale.value_none", days=STATS_STALE_DAYS),
                     t("checkup.pg.stats_stale.msg_none"), dimension="maintenance")
    worst_age = max((_to_num(r[2]) or 0) for r in stale)
    return Check(
        "stats_stale", t("checkup.pg.stats_stale.title"), "warn",
        t("checkup.pg.stats_stale.value", n=len(stale), days=STATS_STALE_DAYS)
        + (t("checkup.pg.stats_stale.suffix_age", days=f"{worst_age / 86400:.0f}") if worst_age > 0
           else t("checkup.pg.stats_stale.suffix_never")),
        t("checkup.pg.stats_stale.message"),
        [t("checkup.pg.stats_stale.detail_never", name=r[0]) if r[1] is None
         else t("checkup.pg.stats_stale.detail_ago", name=r[0], dur=_human_duration(_to_num(r[2])))
         for r in stale[:5]],
        dimension="maintenance",
    )


def _pg_unused_indexes(engine: SAEngine, schema: str | None) -> Check:
    """从未被使用的非唯一索引：写放大与空间浪费（PostgreSQL wiki「Unused Indexes」）。
    排除唯一/主键索引——它们可能被用于约束与锁定，不能仅凭 idx_scan 判定无用。
    indisunique/indisprimary 在 pg_index 系统表里（系统目录人人可读，无权限限制），
    pg_stat_user_indexes 本身只有 relid/indexrelid/schemaname/relname/indexrelname/idx_scan
    等统计列。"""
    try:
        rows = _rows(
            engine,
            "SELECT s.relname, s.indexrelname, s.idx_scan,"
            " COALESCE(pg_relation_size(s.indexrelid), 0)"
            " FROM pg_stat_user_indexes s"
            " JOIN pg_index i ON i.indexrelid = s.indexrelid"
            f" WHERE {schema_filter_pg(schema, 's.schemaname')}"
            " AND s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary"
            " ORDER BY 4 DESC LIMIT 10",
            {"s": schema} if schema else None,
        )
    except Exception as e:
        return Check("unused_indexes", t("checkup.pg.unused_indexes.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.pg.unused_indexes.msg_err", err=type(e).__name__), dimension="maintenance")
    if not rows:
        return Check("unused_indexes", t("checkup.pg.unused_indexes.title"), "ok",
                     t("checkup.pg.unused_indexes.value_none"),
                     t("checkup.pg.unused_indexes.msg_none"), dimension="maintenance")
    total_b = sum(float(r[3] or 0) for r in rows)
    level: Status = "warn" if total_b > UNUSED_IDX_WARN_B else "info"
    return Check(
        "unused_indexes", t("checkup.pg.unused_indexes.title"), level,
        t("checkup.pg.unused_indexes.value", n=len(rows), size=_human_bytes(total_b)),
        t("checkup.pg.unused_indexes.message"),
        [t("checkup.pg.unused_indexes.detail", table=r[0], index=r[1], size=_human_bytes(float(r[3] or 0)))
         for r in rows[:5]],
        dimension="maintenance",
    )


def _pg_replication_lag(engine: SAEngine, can_see: bool) -> Check:
    if not can_see:
        return Check("replication_lag", t("checkup.pg.replication_lag.title"),
                     "unknown", t("checkup.common.unknown"),
                     t("checkup.pg.replication_lag.msg_no_pg_monitor"),
                     dimension="replication", privilege="pg_monitor")
    try:
        rows = _rows(
            engine,
            "SELECT application_name, client_addr,"
            " COALESCE(extract(epoch FROM write_lag), 0)"
            " + COALESCE(extract(epoch FROM flush_lag), 0)"
            " + COALESCE(extract(epoch FROM replay_lag), 0)"
            " FROM pg_stat_replication ORDER BY 3 DESC NULLS LAST LIMIT 5",
        )
    except Exception as e:
        return Check("replication_lag", t("checkup.pg.replication_lag.title"), "unknown", t("checkup.common.unknown"),
                     t("checkup.pg.replication_lag.msg_err", err=type(e).__name__),
                     dimension="replication", privilege="pg_monitor")
    if not rows:
        return Check("replication_lag", t("checkup.pg.replication_lag.title"), "info",
                     t("checkup.pg.replication_lag.value_none"),
                     t("checkup.pg.replication_lag.msg_none"),
                     dimension="replication")
    worst = max(float(r[3] or 0) for r in rows)
    level: Status = "critical" if worst >= REPL_LAG_CRIT_S else "warn" if worst >= REPL_LAG_WARN_S else "ok"
    return Check(
        "replication_lag", t("checkup.pg.replication_lag.title"), level,
        t("checkup.pg.replication_lag.value", secs=f"{worst:.0f}"),
        t("checkup.pg.replication_lag.msg_warn") if level != "ok"
        else t("checkup.pg.replication_lag.msg_ok"),
        [t("checkup.pg.replication_lag.detail", name=r[0], addr=r[1], secs=f"{float(r[3] or 0):.0f}")
         for r in rows],
        dimension="replication",
    )


_PG_SLOT_SQL_V14 = (
    "SELECT slot_name, slot_type, active, wal_status,"
    " COALESCE(safe_wal_size, -1),"
    " COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0)"
    " FROM pg_replication_slots ORDER BY 6 DESC LIMIT 5"
)
# PG 13 及以下没有 wal_status / safe_wal_size 列
_PG_SLOT_SQL_LEGACY = (
    "SELECT slot_name, slot_type, active, NULL, -1,"
    " COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0)"
    " FROM pg_replication_slots ORDER BY 6 DESC LIMIT 5"
)
# wal_status 官方枚举（PG14+）：reserved=安全 / extended=已超过 max_slot_wal_keep_size
# 但仍可恢复 / unreserved=可能丢数据 / lost=已丢数据，必须人工处理
_SLOT_WAL_STATUS_LEVEL = {"reserved": None, "extended": "warn",
                          "unreserved": "critical", "lost": "critical"}


def _pg_replication_slots(engine: SAEngine) -> Check:
    """复制槽健康度：非活跃槽不消费 WAL，pg_wal 会被无限堆积撑爆——PG 运维里最经典的
    磁盘爆炸场景之一。PG 14+ 用官方 wal_status 枚举做确定性判定（lost=已丢数据），
    辅以 safe_wal_size（离丢数据还剩多少）；旧版本退到 WAL 滞后量。
    pg_replication_slots 无权限限制，是受限账号也能查的关键项。"""
    rows, legacy = None, False
    try:
        rows = _rows(engine, _PG_SLOT_SQL_V14)
    except Exception:
        try:
            rows = _rows(engine, _PG_SLOT_SQL_LEGACY)
            legacy = True
        except Exception as e:
            return Check("replication_slots", t("checkup.pg.replication_slots.title"), "unknown",
                         t("checkup.common.unknown"),
                         t("checkup.pg.replication_slots.msg_err", err=type(e).__name__),
                         dimension="replication")
    if not rows:
        return Check("replication_slots", t("checkup.pg.replication_slots.title"), "info",
                     t("checkup.pg.replication_slots.value_none"),
                     t("checkup.pg.replication_slots.msg_none"), dimension="replication")
    worst_lag = max(float(r[5] or 0) for r in rows)
    level: Status = "ok"
    msg = t("checkup.pg.replication_slots.msg_ok")
    # 官方枚举优先：wal_status 直接说明槽保留的 WAL 是否已不安全
    if not legacy:
        for r in rows:
            st = _SLOT_WAL_STATUS_LEVEL.get(str(r[3] or ""))
            if st == "critical":
                level = "critical"
                msg = t("checkup.pg.replication_slots.msg_wal_status_critical", slot=r[0], status=r[3])
                break
            if st == "warn" and level != "critical":
                level = "warn"
                msg = t("checkup.pg.replication_slots.msg_wal_status_warn", slot=r[0], status=r[3])
    # 滞后量兜底
    if level == "ok":
        if worst_lag >= SLOT_LAG_CRIT_B:
            level, msg = "critical", t("checkup.pg.replication_slots.msg_lag_critical")
        elif worst_lag >= SLOT_LAG_WARN_B:
            level, msg = "warn", t("checkup.pg.replication_slots.msg_lag_warn")
    # 非活跃槽：无论滞后多少都在堆积
    inactive = [r for r in rows if not r[2]]
    if level == "ok" and inactive:
        level, msg = "warn", t("checkup.pg.replication_slots.msg_inactive")
    safe = min((float(r[4]) for r in rows if r[4] is not None and float(r[4]) >= 0),
               default=None)
    value = t("checkup.pg.replication_slots.value", name=str(rows[0][0]), size=_human_bytes(worst_lag))
    if safe is not None and safe < SLOT_LAG_WARN_B:
        value += t("checkup.pg.replication_slots.value_safe_suffix", name=rows[0][0],
                   size=_human_bytes(safe))
    return Check(
        "replication_slots", t("checkup.pg.replication_slots.title"), level, value,
        msg + (t("checkup.pg.replication_slots.msg_drop_suffix") if level != "ok" else ""),
        [t("checkup.pg.replication_slots.detail", name=r[0], type=r[1],
           active=(t("checkup.pg.replication_slots.active_yes") if r[2]
                   else t("checkup.pg.replication_slots.active_no")),
           wal_status=(t("checkup.pg.replication_slots.wal_status_suffix", status=r[3])
                       if not legacy and r[3] else ""),
           lag=_human_bytes(float(r[5] or 0))) for r in rows],
        dimension="replication",
    )


def _pg_archiver(engine: SAEngine) -> Check:
    """WAL 归档失败计数（pg_stat_archiver 无权限限制）。归档卡住会让 pg_wal
    无法回收，是磁盘爆炸的另一条路径，也是 PITR 备份失效的直接信号。"""
    try:
        rows = _rows(
            engine,
            "SELECT archived_count, failed_count, COALESCE(last_failed_wal, '')"
            " FROM pg_stat_archiver",
        )
    except Exception as e:
        return Check("archiver", t("checkup.pg.archiver.title"), "unknown", t("checkup.common.unknown"),
                     t("checkup.pg.archiver.msg_err", err=type(e).__name__),
                     dimension="replication")
    if not rows:
        return Check("archiver", t("checkup.pg.archiver.title"), "unknown", t("checkup.common.unknown"),
                     t("checkup.pg.archiver.msg_no_data"), dimension="replication")
    archived, failed, last_fail = _to_num(rows[0][0]), _to_num(rows[0][1]), str(rows[0][2])
    if failed is None:
        return Check("archiver", t("checkup.pg.archiver.title"), "unknown", t("checkup.common.unknown"),
                     t("checkup.pg.archiver.msg_no_col"), dimension="replication")
    if failed <= 0:
        return Check("archiver", t("checkup.pg.archiver.title"), "ok",
                     t("checkup.pg.archiver.value_ok", n=f"{int(archived or 0):,}"),
                     t("checkup.pg.archiver.msg_ok"), dimension="replication")
    return Check(
        "archiver", t("checkup.pg.archiver.title"), "warn",
        t("checkup.pg.archiver.value_warn", n=f"{int(failed):,}"),
        t("checkup.pg.archiver.msg_warn"),
        [t("checkup.pg.archiver.detail_last_fail", wal=last_fail) if last_fail
         else t("checkup.pg.archiver.detail_no_last_fail")],
        dimension="replication",
    )


def _pg_xid_wraparound(engine: SAEngine) -> Check:
    """事务 ID 回卷风险：PG 独有的「不停服就炸」问题——datfrozenxid 迟迟不推进，
    剩余事务号耗尽时库会被强制只读（数据可能丢失）。pg_database 无权限限制。"""
    try:
        rows = _rows(
            engine,
            "SELECT max(age(datfrozenxid)), 2147483648::bigint - max(age(datfrozenxid))"
            " FROM pg_database",
        )
    except Exception as e:
        return Check("xid_wraparound", t("checkup.pg.xid_wraparound.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.pg.xid_wraparound.msg_err", err=type(e).__name__),
                     dimension="maintenance")
    if not rows or rows[0][0] is None:
        return Check("xid_wraparound", t("checkup.pg.xid_wraparound.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.pg.xid_wraparound.msg_no_age"),
                     dimension="maintenance")
    age_, remaining = _to_num(rows[0][0]), _to_num(rows[0][1])
    if remaining is None:
        return Check("xid_wraparound", t("checkup.pg.xid_wraparound.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.pg.xid_wraparound.msg_no_remaining"),
                     dimension="maintenance")
    if remaining < XID_REMAINING_CRIT:
        level: Status = "critical"
    elif remaining < XID_REMAINING_WARN:
        level = "warn"
    else:
        level = "ok"
    return Check(
        "xid_wraparound", t("checkup.pg.xid_wraparound.title"), level,
        t("checkup.pg.xid_wraparound.value", m=f"{remaining / 1e6:.0f}"),
        t("checkup.pg.xid_wraparound.msg_warn") if level != "ok"
        else t("checkup.pg.xid_wraparound.msg_ok"),
        [t("checkup.pg.xid_wraparound.detail", m=f"{age_ / 1e6:.0f}")],
        dimension="maintenance",
    )


def _pg_sizes(engine: SAEngine, schema: str | None) -> Check:
    try:
        total = _scalar(engine, "SELECT pg_size_pretty(pg_database_size(current_database()))")
    except Exception:
        total = None
    try:
        rows = _rows(
            engine,
            "SELECT c.relname, pg_total_relation_size(c.oid)"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relkind IN ('r','p')"
            f" AND {schema_filter_pg(schema, 'n.nspname')}"
            " AND n.nspname NOT IN ('pg_catalog','information_schema')"
            " ORDER BY 2 DESC LIMIT 5",
            {"s": schema} if schema else None,
        )
    except Exception as e:
        return Check("big_tables", t("checkup.pg.sizes.title"), "unknown", t("checkup.common.unknown"),
                     t("checkup.pg.sizes.msg_err", err=type(e).__name__), dimension="maintenance")
    details = [t("checkup.pg.sizes.detail", name=r[0], size=_human_bytes(float(r[1]))) for r in rows]
    return Check(
        "big_tables", t("checkup.pg.sizes.title"), "info",
        t("checkup.pg.sizes.value", total=total or t("checkup.common.unknown"))
        + (t("checkup.pg.sizes.value_biggest_suffix", name=rows[0][0]) if rows else ""),
        t("checkup.pg.sizes.message"),
        details, dimension="maintenance",
    )


# =====================================================================
# SQLite
# =====================================================================


register({
    "checkup.sqlite.server.title": ("数据库", "Database"),
    "checkup.sqlite.server.value": (
        "{size}（{pages} 页 × {page_size} B）", "{size} ({pages} pages x {page_size} B)",
    ),
    "checkup.sqlite.server.msg_no_page_info": ("取不到页信息", "Could not read page info"),
    "checkup.sqlite.integrity.title": ("完整性检查", "Integrity Check"),
    "checkup.sqlite.integrity.value_ok": ("正常", "OK"),
    "checkup.sqlite.integrity.value_bad_fallback": ("异常", "Failed"),
    "checkup.sqlite.integrity.msg_ok": ("数据库文件无结构损坏", "The database file has no structural corruption"),
    "checkup.sqlite.integrity.msg_bad": (
        "数据库已损坏！立即备份后用 .recover 或 integrity_check 定位",
        "The database is corrupted! Back it up immediately, then use .recover or "
        "integrity_check to locate the damage",
    ),
    "checkup.sqlite.integrity.msg_err": (
        "PRAGMA quick_check 失败：{err}", "PRAGMA quick_check failed: {err}",
    ),
    "checkup.sqlite.fragmentation.title": ("空闲页碎片", "Free-Page Fragmentation"),
    "checkup.sqlite.fragmentation.value": (
        "{pct}（{freelist}/{pages} 页空闲）", "{pct} ({freelist}/{pages} pages free)",
    ),
    "checkup.sqlite.fragmentation.msg_warn": (
        "空闲页占比高时 VACUUM 可回收空间并加速扫描",
        "When the free-page ratio is high, VACUUM can reclaim space and speed up scans",
    ),
    "checkup.sqlite.fragmentation.msg_ok": (
        "删除产生的空闲页比例正常", "The free-page ratio from deletions is normal",
    ),
    "checkup.sqlite.fragmentation.msg_unknown": (
        "取不到 freelist/page 计数", "Could not read the freelist/page counts",
    ),
    "checkup.sqlite.journal_mode.title": ("日志模式", "Journal Mode"),
    "checkup.sqlite.journal_mode.msg_wal": (
        "WAL 支持读写并发、崩溃恢复更快；DELETE 模式下写会阻塞读",
        "WAL supports concurrent reads and writes and recovers faster from crashes; in DELETE "
        "mode, writes block reads",
    ),
    "checkup.sqlite.journal_mode.msg_other": (
        "并发写较多时考虑切到 WAL（PRAGMA journal_mode=WAL）",
        "If there are many concurrent writes, consider switching to WAL "
        "(PRAGMA journal_mode=WAL)",
    ),
    "checkup.sqlite.journal_mode.msg_unknown": (
        "PRAGMA journal_mode 失败", "PRAGMA journal_mode failed",
    ),
    "checkup.sqlite.tables.title": ("表与行数", "Tables & Row Counts"),
    "checkup.sqlite.tables.value_with_stats": ("{n} 张表有统计信息", "{n} table(s) have statistics"),
    "checkup.sqlite.tables.value_no_stats": ("无统计信息", "No statistics"),
    "checkup.sqlite.tables.msg_stats": (
        "行数来自 ANALYZE 写入的 sqlite_stat1（近似值）；跑过 ANALYZE 才有",
        "Row counts come from sqlite_stat1 written by ANALYZE (approximate); only available "
        "after ANALYZE has run",
    ),
    "checkup.sqlite.tables.detail": ("{name} — 约 {rows} 行", "{name} — ~{rows} rows"),
    "checkup.sqlite.tables.value_count": ("{n} 张表", "{n} table(s)"),
    "checkup.sqlite.tables.msg_no_stats": (
        "无 sqlite_stat1 统计信息，故不逐表 count（大表 count 慢）；"
        "ANALYZE 后可看近似行数",
        "No sqlite_stat1 statistics, so tables are not counted individually (counting large "
        "tables is slow); run ANALYZE to see approximate row counts",
    ),
    "checkup.sqlite.tables.msg_err": (
        "查 sqlite_master 失败：{err}", "Failed to query sqlite_master: {err}",
    ),
})


def _sqlite_checks(engine: SAEngine, _schema: str | None) -> list[Check]:
    checks: list[Check] = []

    try:
        version = str(_scalar(engine, "SELECT sqlite_version()"))
    except Exception:
        version = t("checkup.common.unknown")
    try:
        pages = _to_num(_scalar(engine, "PRAGMA page_count"))
        freelist = _to_num(_scalar(engine, "PRAGMA freelist_count"))
        page_size = _to_num(_scalar(engine, "PRAGMA page_size"))
    except Exception:
        pages = freelist = page_size = None

    checks.append(Check(
        "server", t("checkup.sqlite.server.title"), "info", f"SQLite {version}",
        t("checkup.sqlite.server.value", size=_human_bytes((pages or 0) * (page_size or 0)),
          pages=f"{int(pages or 0):,}", page_size=int(page_size or 0))
        if pages and page_size else t("checkup.sqlite.server.msg_no_page_info"),
        dimension="availability",
    ))

    # --- 完整性检查（quick_check 比 integrity_check 快，覆盖绝大多数损坏） ---
    try:
        rows = _rows(engine, "PRAGMA quick_check")
        result = "; ".join(str(r[0]) for r in rows[:3]) if rows else ""
        ok = result.strip().lower() == "ok"
        checks.append(Check(
            "integrity", t("checkup.sqlite.integrity.title"), "ok" if ok else "critical",
            t("checkup.sqlite.integrity.value_ok") if ok
            else (result or t("checkup.sqlite.integrity.value_bad_fallback")),
            t("checkup.sqlite.integrity.msg_ok") if ok else t("checkup.sqlite.integrity.msg_bad"),
            dimension="availability",
        ))
    except Exception as e:
        checks.append(Check("integrity", t("checkup.sqlite.integrity.title"), "unknown",
                            t("checkup.common.unknown"),
                            t("checkup.sqlite.integrity.msg_err", err=type(e).__name__),
                            dimension="availability"))

    # --- 空闲页碎片 ---
    if pages and freelist is not None and pages > 0:
        frag = freelist / pages
        checks.append(Check(
            "fragmentation", t("checkup.sqlite.fragmentation.title"), "warn" if frag > 0.2 else "ok",
            t("checkup.sqlite.fragmentation.value", pct=f"{frag:.1%}",
              freelist=f"{int(freelist):,}", pages=f"{int(pages):,}"),
            t("checkup.sqlite.fragmentation.msg_warn") if frag > 0.2
            else t("checkup.sqlite.fragmentation.msg_ok"),
            dimension="maintenance",
        ))
    else:
        checks.append(Check("fragmentation", t("checkup.sqlite.fragmentation.title"), "unknown",
                            t("checkup.common.unknown"), t("checkup.sqlite.fragmentation.msg_unknown"),
                            dimension="maintenance"))

    # --- journal 模式（WAL 与否影响并发） ---
    try:
        mode = str(_scalar(engine, "PRAGMA journal_mode"))
        checks.append(Check(
            "journal_mode", t("checkup.sqlite.journal_mode.title"), "info", mode,
            t("checkup.sqlite.journal_mode.msg_wal") if mode.upper() == "WAL"
            else t("checkup.sqlite.journal_mode.msg_other"),
            dimension="concurrency",
        ))
    except Exception:
        checks.append(Check("journal_mode", t("checkup.sqlite.journal_mode.title"), "unknown",
                            t("checkup.common.unknown"), t("checkup.sqlite.journal_mode.msg_unknown"),
                            dimension="concurrency"))

    # --- 表行数（有 sqlite_stat1 时用统计值，避免逐表 count） ---
    try:
        rows = _rows(engine, "SELECT tbl, stat FROM sqlite_stat1 ORDER BY 1 LIMIT 10")
        details = [t("checkup.sqlite.tables.detail", name=r[0], rows=str(r[1]).split(" ")[0])
                   for r in rows if r[1]]
        checks.append(Check(
            "tables", t("checkup.sqlite.tables.title"), "info",
            t("checkup.sqlite.tables.value_with_stats", n=len(details)) if details
            else t("checkup.sqlite.tables.value_no_stats"),
            t("checkup.sqlite.tables.msg_stats"),
            details, dimension="maintenance",
        ))
    except Exception:
        # 没有 sqlite_stat1 表时查表清单，不逐表 count（大表 count 慢）
        try:
            n = _to_num(_scalar(
                engine,
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'"))
            checks.append(Check(
                "tables", t("checkup.sqlite.tables.title"), "info",
                t("checkup.sqlite.tables.value_count", n=int(n or 0)),
                t("checkup.sqlite.tables.msg_no_stats"),
                dimension="maintenance",
            ))
        except Exception as e:
            checks.append(Check("tables", t("checkup.sqlite.tables.title"), "unknown",
                                t("checkup.common.unknown"),
                                t("checkup.sqlite.tables.msg_err", err=type(e).__name__),
                                dimension="maintenance"))

    return checks


# =====================================================================
# ClickHouse（轻量：核心指标 + 副本/part 健康）
# =====================================================================


register({
    "checkup.ch.server.title": ("服务器", "Server"),
    "checkup.ch.server.msg_running": ("已运行 {dur}", "Running for {dur}"),
    "checkup.ch.server.msg_no_uptime": ("取不到 uptime", "Could not read uptime"),
    "checkup.ch.metrics.title": ("核心指标", "Core Metrics"),
    "checkup.ch.metrics.value": ("{n} 项", "{n}"),
    "checkup.ch.metrics.message": (
        "Query=正在执行的查询数；Merge/BackgroundMergesAndMutationsPoolTask=后台合并任务积压"
        "（持续接近上限说明合并跟不上写入，逼近 too_many_parts 时新 part 会被拒绝写入）；"
        "ReadonlyReplica>0 说明有副本只读",
        "Query = number of currently executing queries; "
        "Merge/BackgroundMergesAndMutationsPoolTask = background merge task backlog "
        "(sustained near the limit means merges can't keep up with inserts; new parts get "
        "rejected once too_many_parts is approached); ReadonlyReplica>0 means a replica is "
        "read-only",
    ),
    "checkup.ch.metrics.msg_none": (
        "system.metrics 没有匹配的指标", "system.metrics has no matching metrics",
    ),
    "checkup.ch.metrics.msg_err": (
        "查 system.metrics 失败：{err}", "Failed to query system.metrics: {err}",
    ),
    "checkup.ch.replication_queue.title": ("副本同步队列", "Replica Sync Queue"),
    "checkup.ch.replication_queue.msg_expired": (
        "副本会话已过期（{tables}）：与 Keeper 的连接断开，同步已停止；查 Keeper 状态与网络",
        "The replica session has expired ({tables}): the connection to Keeper is down and "
        "sync has stopped; check Keeper status and the network",
    ),
    "checkup.ch.replication_queue.msg_lag_critical": (
        "队列积压过大，副本严重落后；查网络 / Keeper / 大 mutation",
        "The queue backlog is too large and the replica is severely behind; check the "
        "network / Keeper / large mutations",
    ),
    "checkup.ch.replication_queue.msg_pointer_behind": (
        "log_pointer 远小于 log_max_index：拉取线程落后于日志产生速度"
        "（官方文档明确指出这个差值过大意味着副本有问题）",
        "log_pointer is far behind log_max_index: the fetch thread is lagging behind the "
        "rate log entries are produced (the official docs note that too large a gap means "
        "the replica has a problem)",
    ),
    "checkup.ch.replication_queue.msg_growing": (
        "queue_size 是待同步的日志条数；持续增长说明副本追不上，查网络/ZK/大 mutation",
        "queue_size is the number of log entries pending sync; sustained growth means the "
        "replica can't keep up — check the network/ZooKeeper/large mutations",
    ),
    "checkup.ch.replication_queue.msg_ok": (
        "副本同步队列正常消费", "The replica sync queue is draining normally",
    ),
    "checkup.ch.replication_queue.value": (
        "最长队列 {n}（{table}）", "longest queue {n} ({table})",
    ),
    "checkup.ch.replication_queue.detail": (
        "{table} — 队列 {queue}，待合并 {merges}，绝对延迟 {delay}s",
        "{table} — queue {queue}, merges pending {merges}, absolute delay {delay}s",
    ),
    "checkup.ch.replication_queue.detail_expired_suffix": ("，会话已过期", ", session expired"),
    "checkup.ch.replication_queue.detail_log_suffix": (
        "，log {pointer}/{index}", ", log {pointer}/{index}",
    ),
    "checkup.ch.replication_queue.value_none": ("无 ReplicatedMergeTree 表", "No ReplicatedMergeTree tables"),
    "checkup.ch.replication_queue.msg_none": (
        "没有需要同步的副本表（非副本部署，或未用 ReplicatedMergeTree 引擎）",
        "There are no replicated tables to sync (a non-replicated deployment, or no "
        "ReplicatedMergeTree engine in use)",
    ),
    "checkup.ch.replication_queue.msg_err": (
        "查 system.replicas 失败：{err}", "Failed to query system.replicas: {err}",
    ),
    "checkup.ch.parts.title": ("活跃 part 数", "Active Part Count"),
    "checkup.ch.parts.value": ("最多 {n}（{table}）", "highest {n} ({table})"),
    "checkup.ch.parts.message": (
        "part 数逼近 too_many_parts 阈值时会拒绝写入；降低写入频率或扩大分区粒度",
        "Once the part count approaches the too_many_parts threshold, writes get rejected; "
        "reduce the insert frequency or make the partition granularity coarser",
    ),
    "checkup.ch.parts.detail": ("{table} — {n} 个活跃 part", "{table} — {n} active parts"),
    "checkup.ch.parts.value_none": (
        "无超过 {threshold} part 的表", "No table with more than {threshold} parts",
    ),
    "checkup.ch.parts.msg_none": ("合并跟得上写入", "Merges are keeping up with inserts"),
    "checkup.ch.parts.msg_err": (
        "查 system.parts 失败：{err}", "Failed to query system.parts: {err}",
    ),
    "checkup.ch.mutations.title": ("未完成的 mutation", "Pending Mutations"),
    "checkup.ch.mutations.value": ("{n} 个", "{n}"),
    "checkup.ch.mutations.msg_warn": (
        "ALTER ... UPDATE/DELETE 是异步 mutation；堆积的 mutation 会拖慢合并和查询",
        "ALTER ... UPDATE/DELETE is an asynchronous mutation; a backlog of mutations slows "
        "down merges and queries",
    ),
    "checkup.ch.mutations.msg_ok": ("没有在跑的 mutation", "No mutation is currently running"),
    "checkup.ch.mutations.msg_null": ("count 返回 NULL", "count returned NULL"),
    "checkup.ch.mutations.msg_err": (
        "查 system.mutations 失败：{err}", "Failed to query system.mutations: {err}",
    ),
    "checkup.ch.big_tables.title": ("大表 TOP5", "Top 5 Largest Tables"),
    "checkup.ch.big_tables.value": ("最大 {table}（{size}）", "largest {table} ({size})"),
    "checkup.ch.big_tables.message": (
        "最大的几张表是合并/存储成本的主要来源，TTL 与分区设计要重点 review",
        "The largest tables are the main source of merge/storage cost; review their TTL and "
        "partitioning design",
    ),
    "checkup.ch.big_tables.detail": (
        "{table} — {size}，{rows} 行", "{table} — {size}, {rows} rows",
    ),
    "checkup.ch.big_tables.value_none": ("无表", "No tables"),
    "checkup.ch.big_tables.msg_none": ("没有可统计的 part", "There are no parts to account for"),
    "checkup.ch.big_tables.msg_err": (
        "查 system.parts 大小失败：{err}", "Failed to query system.parts sizes: {err}",
    ),
    "checkup.ch.disk_space.title": ("磁盘健康", "Disk Health"),
    "checkup.ch.disk_space.msg_err": (
        "查 system.disks 失败：{err}", "Failed to query system.disks: {err}",
    ),
    "checkup.ch.disk_space.msg_no_disks": (
        "system.disks 没有磁盘记录", "system.disks has no disk records",
    ),
    "checkup.ch.disk_space.value_broken": (
        "{n} 块盘损坏（{names}）", "{n} disk(s) broken ({names})",
    ),
    "checkup.ch.disk_space.msg_broken": (
        "磁盘被标记为 broken，写盘会直接失败；查存储底层与 CH 日志",
        "The disk is marked broken; writes will fail outright — check the underlying "
        "storage and the ClickHouse logs",
    ),
    "checkup.ch.disk_space.detail_broken": ("{name}（{path}）is_broken=1", "{name} ({path}) is_broken=1"),
    "checkup.ch.disk_space.value_readonly": (
        "{n} 块盘只读（{names}）", "{n} disk(s) read-only ({names})",
    ),
    "checkup.ch.disk_space.msg_readonly": (
        "磁盘被置只读（磁盘满或手动设置），写入会失败；查 free_space 与挂载",
        "The disk has been set read-only (full disk or a manual setting); writes will fail — "
        "check free_space and the mount",
    ),
    "checkup.ch.disk_space.detail_readonly": (
        "{name}（{path}）is_read_only=1", "{name} ({path}) is_read_only=1",
    ),
    "checkup.ch.disk_space.value": ("最紧张 {name}（剩余 {pct}）", "tightest {name} ({pct} free)"),
    "checkup.ch.disk_space.msg_warn": (
        "磁盘剩余空间不足时 ClickHouse 会拒绝写入；清理过期 TTL 数据、"
        "扩大磁盘或把冷数据移到其它存储卷",
        "When a disk runs low on space, ClickHouse rejects writes; clean up expired TTL data, "
        "grow the disk, or move cold data to another storage volume",
    ),
    "checkup.ch.disk_space.msg_ok": ("数据盘剩余空间充足", "The data disk has plenty of free space"),
    "checkup.ch.disk_space.detail": (
        "{name}（{path}）— 剩余 {free} / {total}", "{name} ({path}) — {free} free / {total}",
    ),
    "checkup.ch.disk_space.detail_reserved_suffix": (
        "（扣除预留后 {unreserved}）", " (after reservations: {unreserved})",
    ),
    "checkup.ch.failed_queries.title": ("失败查询", "Failed Queries"),
    "checkup.ch.failed_queries.msg_err": (
        "查 system.events 失败：{err}", "Failed to query system.events: {err}",
    ),
    "checkup.ch.failed_queries.msg_no_metric": (
        "system.events 没有 FailedQuery 指标", "system.events has no FailedQuery metric",
    ),
    "checkup.ch.failed_queries.value_none": ("无失败查询", "No failed queries"),
    "checkup.ch.failed_queries.msg_none": (
        "启动以来没有查询失败", "No query has failed since startup",
    ),
    "checkup.ch.failed_queries.value": ("{n} 次", "{n}"),
    "checkup.ch.failed_queries.rate_suffix": ("（约 {rate} 次/小时）", " (about {rate}/hour)"),
    "checkup.ch.failed_queries.msg_warn": (
        "失败查询明显变多时查 system.query_log 的 type='ExceptionWhileProcessing' 看具体错误",
        "When failed queries increase noticeably, check system.query_log where "
        "type='ExceptionWhileProcessing' for the specific errors",
    ),
    "checkup.ch.failed_queries.msg_info": (
        "参考值：累计失败的查询数；突然飙升才需关注",
        "Informational: cumulative count of failed queries; only worth attention on a sudden spike",
    ),
})


def _clickhouse_checks(engine: SAEngine, schema: str | None) -> list[Check]:
    checks: list[Check] = []
    try:
        version = str(_scalar(engine, "SELECT version()"))
    except Exception:
        version = t("checkup.common.unknown")
    try:
        uptime = _to_num(_scalar(engine, "SELECT uptime()"))
    except Exception:
        uptime = None
    checks.append(Check("server", t("checkup.ch.server.title"), "info", f"ClickHouse {version}",
                        t("checkup.ch.server.msg_running", dur=_human_duration(uptime))
                        if uptime is not None else t("checkup.ch.server.msg_no_uptime"),
                        dimension="availability"))

    # --- 磁盘空间（system.disks；CH 的数据盘满了会直接拒绝写入） ---
    checks.append(_ch_disk_space(engine))

    # --- 核心指标（指标名按 CH 25.x 实际存在的取，版本差异时取不到也不影响其它项） ---
    try:
        rows = _rows(
            engine,
            "SELECT metric, value FROM system.metrics"
            " WHERE metric IN ('Query','Merge','BackgroundMergesAndMutationsPoolTask',"
            " 'ReadonlyReplica','AttachedReplicatedTable')",
        )
        metrics = {str(r[0]): _to_num(r[1]) for r in rows}
        if metrics:
            checks.append(Check(
                "metrics", t("checkup.ch.metrics.title"), "info",
                t("checkup.ch.metrics.value", n=len(metrics)),
                t("checkup.ch.metrics.message"),
                [f"{k} = {int(v)}" for k, v in sorted(metrics.items()) if v],
                dimension="capacity",
            ))
        else:
            checks.append(Check("metrics", t("checkup.ch.metrics.title"), "unknown",
                                t("checkup.common.unknown"), t("checkup.ch.metrics.msg_none"),
                                dimension="capacity"))
    except Exception as e:
        checks.append(Check("metrics", t("checkup.ch.metrics.title"), "unknown",
                            t("checkup.common.unknown"),
                            t("checkup.ch.metrics.msg_err", err=type(e).__name__), dimension="capacity"))

    # --- 失败查询（system.events 的 FailedQuery 系列计数器） ---
    checks.append(_ch_failed_queries(engine, uptime))

    # --- 副本同步队列（relative_delay 列在 CH 新版已移除，只用稳定存在的列） ---
    try:
        rows = _rows(
            engine,
            "SELECT database, table, queue_size, merges_in_queue, absolute_delay,"
            " is_session_expired, log_max_index, log_pointer"
            " FROM system.replicas WHERE is_readonly = 0"
            " ORDER BY queue_size DESC LIMIT 5",
        )
        if rows:
            worst = max(float(r[2] or 0) for r in rows)
            expired = [r for r in rows if r[5]]
            behind = [r for r in rows
                    if r[6] is not None and r[7] is not None
                    and float(r[6]) - float(r[7]) > 100]
            # 会话过期 = 与 Keeper 的连接断了，副本已停止同步，官方文档的确定性状态
            if expired:
                level: Status = "critical"
                msg = t("checkup.ch.replication_queue.msg_expired",
                       tables=", ".join(r[0] + "." + r[1] for r in expired))
            elif worst >= 1000:
                level = "critical"
                msg = t("checkup.ch.replication_queue.msg_lag_critical")
            elif behind:
                level = "warn"
                msg = t("checkup.ch.replication_queue.msg_pointer_behind")
            elif worst >= 100:
                level = "warn"
                msg = t("checkup.ch.replication_queue.msg_growing")
            else:
                level = "ok"
                msg = t("checkup.ch.replication_queue.msg_ok")
            checks.append(Check(
                "replication_queue", t("checkup.ch.replication_queue.title"), level,
                t("checkup.ch.replication_queue.value", n=int(worst), table=f"{rows[0][0]}.{rows[0][1]}"),
                msg,
                [t("checkup.ch.replication_queue.detail", table=f"{r[0]}.{r[1]}",
                   queue=int(float(r[2] or 0)), merges=int(float(r[3] or 0)),
                   delay=int(float(r[4] or 0)))
                 + (t("checkup.ch.replication_queue.detail_expired_suffix") if r[5] else "")
                 + (t("checkup.ch.replication_queue.detail_log_suffix",
                      pointer=int(float(r[7]) or 0), index=int(float(r[6]) or 0))
                    if r[6] is not None and r[7] is not None else "")
                 for r in rows],
                dimension="replication",
            ))
        else:
            checks.append(Check("replication_queue", t("checkup.ch.replication_queue.title"), "info",
                                t("checkup.ch.replication_queue.value_none"),
                                t("checkup.ch.replication_queue.msg_none"),
                                dimension="replication"))
    except Exception as e:
        checks.append(Check("replication_queue", t("checkup.ch.replication_queue.title"), "unknown",
                            t("checkup.common.unknown"),
                            t("checkup.ch.replication_queue.msg_err", err=type(e).__name__),
                            dimension="replication"))

    # --- part 数（too many parts 预警） ---
    try:
        rows = _rows(
            engine,
            "SELECT database, table, count() AS parts"
            " FROM system.parts WHERE active"
            f" AND {schema_filter('clickhouse', schema, 'database')}"
            " GROUP BY database, table HAVING parts >= :n"
            " ORDER BY parts DESC LIMIT 5",
            {**schema_params(schema), "n": CH_PARTS_WARN},
        )
        if rows:
            worst = max(float(r[2]) for r in rows)
            level = "critical" if worst >= 300 else "warn"
            checks.append(Check(
                "parts", t("checkup.ch.parts.title"), level,
                t("checkup.ch.parts.value", n=int(worst), table=f"{rows[0][0]}.{rows[0][1]}"),
                t("checkup.ch.parts.message"),
                [t("checkup.ch.parts.detail", table=f"{r[0]}.{r[1]}", n=int(float(r[2]))) for r in rows],
                dimension="maintenance",
            ))
        else:
            checks.append(Check("parts", t("checkup.ch.parts.title"), "ok",
                                t("checkup.ch.parts.value_none", threshold=CH_PARTS_WARN),
                                t("checkup.ch.parts.msg_none"),
                                dimension="maintenance"))
    except Exception as e:
        checks.append(Check("parts", t("checkup.ch.parts.title"), "unknown",
                            t("checkup.common.unknown"),
                            t("checkup.ch.parts.msg_err", err=type(e).__name__), dimension="maintenance"))

    # --- 未完成的 mutation ---
    try:
        n = _to_num(_scalar(
            engine,
            "SELECT count() FROM system.mutations WHERE NOT is_done"
            + (f" AND {schema_filter('clickhouse', schema, 'database')}" if schema else ""),
            schema_params(schema) if schema else None))
        if n is not None:
            checks.append(Check(
                "mutations", t("checkup.ch.mutations.title"), "warn" if n > 0 else "ok",
                t("checkup.ch.mutations.value", n=int(n)),
                t("checkup.ch.mutations.msg_warn") if n > 0 else t("checkup.ch.mutations.msg_ok"),
                dimension="maintenance",
            ))
        else:
            checks.append(Check("mutations", t("checkup.ch.mutations.title"), "unknown",
                                t("checkup.common.unknown"), t("checkup.ch.mutations.msg_null"),
                                dimension="maintenance"))
    except Exception as e:
        checks.append(Check("mutations", t("checkup.ch.mutations.title"), "unknown",
                            t("checkup.common.unknown"),
                            t("checkup.ch.mutations.msg_err", err=type(e).__name__),
                            dimension="maintenance"))

    # --- 大表 TOP5 ---
    try:
        rows = _rows(
            engine,
            "SELECT database, table, sum(bytes_on_disk) AS bytes, sum(rows) AS rows"
            " FROM system.parts WHERE active"
            f" AND {schema_filter('clickhouse', schema, 'database')}"
            " GROUP BY database, table ORDER BY bytes DESC LIMIT 5",
            schema_params(schema),
        )
        if rows:
            checks.append(Check(
                "big_tables", t("checkup.ch.big_tables.title"), "info",
                t("checkup.ch.big_tables.value", table=f"{rows[0][0]}.{rows[0][1]}",
                  size=_human_bytes(float(rows[0][2]))),
                t("checkup.ch.big_tables.message"),
                [t("checkup.ch.big_tables.detail", table=f"{r[0]}.{r[1]}",
                   size=_human_bytes(float(r[2])), rows=f"{int(float(r[3] or 0)):,}")
                 for r in rows],
                dimension="maintenance",
            ))
        else:
            checks.append(Check("big_tables", t("checkup.ch.big_tables.title"), "info",
                                t("checkup.ch.big_tables.value_none"),
                                t("checkup.ch.big_tables.msg_none"), dimension="maintenance"))
    except Exception as e:
        checks.append(Check("big_tables", t("checkup.ch.big_tables.title"), "unknown",
                            t("checkup.common.unknown"),
                            t("checkup.ch.big_tables.msg_err", err=type(e).__name__),
                            dimension="maintenance"))

    return checks


def _ch_disk_space(engine: SAEngine) -> Check:
    """磁盘健康：CH 的数据盘写满会拒绝所有写入。除了剩余比例，官方的 is_broken /
    is_read_only 是确定性状态（盘坏了或被置只读，直接 critical/warn）；
    unreserved_space 扣除了 merge/insert 的预留，比 free_space 更接近真实可用。"""
    try:
        rows = _rows(
            engine,
            "SELECT name, path, total_space, free_space, unreserved_space,"
            " is_read_only, is_broken FROM system.disks"
            " ORDER BY (free_space / NULLIF(total_space,0)) ASC LIMIT 5",
        )
    except Exception as e:
        return Check("disk_space", t("checkup.ch.disk_space.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.ch.disk_space.msg_err", err=type(e).__name__), dimension="capacity")
    if not rows:
        return Check("disk_space", t("checkup.ch.disk_space.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.ch.disk_space.msg_no_disks"),
                     dimension="capacity")
    # 确定性状态优先：有盘坏了或只读，其它空间指标都没意义了
    broken = [r for r in rows if r[6]]
    readonly = [r for r in rows if r[5] and not r[6]]
    if broken:
        return Check(
            "disk_space", t("checkup.ch.disk_space.title"), "critical",
            t("checkup.ch.disk_space.value_broken", n=len(broken),
              names=", ".join(str(r[0]) for r in broken)),
            t("checkup.ch.disk_space.msg_broken"),
            [t("checkup.ch.disk_space.detail_broken", name=r[0], path=str(r[1])) for r in broken],
            dimension="capacity",
        )
    if readonly:
        return Check(
            "disk_space", t("checkup.ch.disk_space.title"), "warn",
            t("checkup.ch.disk_space.value_readonly", n=len(readonly),
              names=", ".join(str(r[0]) for r in readonly)),
            t("checkup.ch.disk_space.msg_readonly"),
            [t("checkup.ch.disk_space.detail_readonly", name=r[0], path=str(r[1])) for r in readonly],
            dimension="capacity",
        )
    worst_pct = min((float(r[3]) / float(r[2]) for r in rows if r[2]))
    worst_row = min(rows, key=lambda r: float(r[3]) / float(r[2]) if r[2] else 1)
    level: Status = ("critical" if worst_pct < 1 - DISK_CRIT_PCT
                     else "warn" if worst_pct < 1 - DISK_WARN_PCT else "ok")
    return Check(
        "disk_space", t("checkup.ch.disk_space.title"), level,
        t("checkup.ch.disk_space.value", name=str(worst_row[0]), pct=f"{worst_pct:.0%}"),
        t("checkup.ch.disk_space.msg_warn") if level != "ok" else t("checkup.ch.disk_space.msg_ok"),
        [t("checkup.ch.disk_space.detail", name=r[0], path=str(r[1]),
           free=_human_bytes(float(r[4] if r[4] is not None else r[3])),
           total=_human_bytes(float(r[2])))
         + ("" if r[4] is None or float(r[4]) == float(r[3])
            else t("checkup.ch.disk_space.detail_reserved_suffix", unreserved=_human_bytes(float(r[4]))))
         for r in rows],
        dimension="capacity",
    )


def _ch_failed_queries(engine: SAEngine, uptime: float | None) -> Check:
    """失败查询计数（system.events 的 FailedQuery 系列）：失败率升高通常意味着
    结构问题（分布式表坏掉、超时、内存限制）。"""
    try:
        rows = _rows(
            engine,
            "SELECT event, value FROM system.events"
            " WHERE event IN ('FailedQuery','FailedSelectQuery','FailedInsertQuery')",
        )
    except Exception as e:
        return Check("failed_queries", t("checkup.ch.failed_queries.title"), "unknown",
                     t("checkup.common.unknown"),
                     t("checkup.ch.failed_queries.msg_err", err=type(e).__name__),
                     dimension="performance")
    got = {str(r[0]): _to_num(r[1]) for r in rows}
    failed = sum(v for k, v in got.items() if k == "FailedQuery")
    if failed is None:
        return Check("failed_queries", t("checkup.ch.failed_queries.title"), "unknown",
                     t("checkup.common.unknown"), t("checkup.ch.failed_queries.msg_no_metric"),
                     dimension="performance")
    rate = _rate_per_hour(failed, uptime)
    if failed <= 0:
        return Check("failed_queries", t("checkup.ch.failed_queries.title"), "ok",
                     t("checkup.ch.failed_queries.value_none"),
                     t("checkup.ch.failed_queries.msg_none"), dimension="performance")
    level: Status = "warn" if (rate or 0) > 10 or failed > 100 else "info"
    return Check(
        "failed_queries", t("checkup.ch.failed_queries.title"), level,
        t("checkup.ch.failed_queries.value", n=f"{int(failed):,}")
        + (t("checkup.ch.failed_queries.rate_suffix", rate=f"{rate:.1f}") if rate is not None else ""),
        t("checkup.ch.failed_queries.msg_warn") if level == "warn"
        else t("checkup.ch.failed_queries.msg_info"),
        [f"{k} = {int(v or 0):,}" for k, v in sorted(got.items()) if v],
        dimension="performance",
    )


# =====================================================================
# 入口
# =====================================================================

_DISPATCH = {
    "mysql": _mysql_checks,
    "postgres": _postgres_checks,
    "sqlite": _sqlite_checks,
    "clickhouse": _clickhouse_checks,
}


def supported_engines() -> list[str]:
    return list(_DISPATCH)


def run_checkup(engine: SAEngine, engine_kind: str, schema: str | None = None) -> CheckupReport:
    """对一条连接跑完整体检。schema 为 MySQL/ClickHouse 的库名、PG 的 schema 名。

    逐项容错：某个检查失败（权限不足/视图不存在/版本差异）只把那一项标成 unknown，
    其它项照常出。所有查询都是只读的。缺权限的项额外汇总成 GRANT 模板
    （report.privileges），让用户知道怎么补。
    """
    runner = _DISPATCH.get(engine_kind)
    started = dt.datetime.now(dt.timezone.utc)
    report = CheckupReport(
        engine=engine_kind,
        scope=schema or "",
        started_at=started.isoformat(timespec="seconds"),
    )
    if runner is None:
        report.overall = "unknown"
        report.checks = [Check(
            "unsupported", t("checkup.entry.unsupported_title"), "unknown", engine_kind,
            t("checkup.entry.unsupported_message", engines=", ".join(supported_engines())),
        )]
        report.elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        return report

    # 先探活：库都连不上时，逐项报 OperationalError 只是噪音，给一句明确的结论
    ok, why = connectivity_ok(engine)
    if not ok:
        logger.warning("checkup: %s connection unreachable: %s", engine_kind, why)
        report.overall = "critical"
        report.checks = [Check(
            "connectivity", t("checkup.entry.connectivity_title"), "critical",
            t("checkup.entry.connectivity_value"),
            t("checkup.entry.connectivity_message", why=why),
            dimension="availability",
        )]
        report.elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        return report

    try:
        checks = runner(engine, schema)
    except Exception as e:  # noqa: BLE001 - 兜底：任何未预期的失败都给出可读报告，不裸抛
        logger.exception("checkup failed for %s", engine_kind)
        report.overall = "unknown"
        report.checks = [Check("error", t("checkup.entry.error_title"), "unknown",
                               type(e).__name__, str(e))]
        report.elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        return report

    report.checks = checks
    report.overall = _worst(checks)
    try:
        report.privileges = _collect_gaps(checks, _current_user(engine, engine_kind))
    except Exception:  # noqa: BLE001
        report.privileges = []
    report.elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
    return report


def _same_check(a: Check, b: Check) -> bool:
    """两条同名检查是否实质相同（值/解读/明细全都一样）。

    用于 merge_reports 判定「这条指标是不是库级」：实例级指标（连接占用、缓存命中率、
    长查询、锁、复制…）在任一库上查结果都相同，逐库跑只是浪费时间；库级指标
    （大表、无主键表、膨胀、未用索引…）各库不同。按「完全相同」判定无需为每条
    检查维护是不是库级的元数据——新引擎、新检查加进来自动正确。
    """
    return (a.value == b.value and a.message == b.message
            and a.status == b.status and a.details == b.details)


def merge_reports(engine_kind: str, reports: list[tuple[str, CheckupReport]]) -> CheckupReport:
    """把逐库体检合并成一份实例级报告（查询台「体检」在未选库时走这条路）。

    用户要的是「针对全体，而不是某个库某个 schema」——慢查询、大表在哪个库都可能
    发生。合并规则：

    - **实例级指标**（各库查出来完全一样）：只留一条，不加库名前缀；
    - **库级指标**（各库不同）：取最严重的那条，标题加 ``[库名]`` 前缀——报告保持
      「一份」的篇幅，既不漏掉任何库的问题，也不把同一项连接占用复制 N 遍；
    - 某个库体检失败已在 db_checkup_all 那层挡掉，这里不处理。

    reports: [(db_name, report)]，overall 是全部检查里最严重的状态。
    """
    started = dt.datetime.now(dt.timezone.utc)
    merged = CheckupReport(
        engine=engine_kind,
        scope="",
        started_at=started.isoformat(timespec="seconds"),
    )
    if not reports:
        merged.overall = "unknown"
        merged.checks = [Check(
            "no_databases", t("checkup.merge.no_databases_title"), "unknown", "0",
            t("checkup.merge.no_databases_message"),
            dimension="availability",
        )]
        merged.elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        return merged

    merged.scope = t("checkup.merge.scope_all_databases", n=len(reports))
    total_ms = 0
    gaps: dict[str, PrivilegeGap] = {}
    # name -> [(db, check)]：按检查名归拢，同名才能比较「各库是否相同」
    by_name: dict[str, list[tuple[str, Check]]] = {}
    for db, rep in reports:
        if not merged.version and rep.version:
            merged.version = rep.version
        total_ms += rep.elapsed_ms or 0
        for c in rep.checks:
            by_name.setdefault(c.name, []).append((db, c))
        for g in rep.privileges:
            key = g.privilege
            if key in gaps:
                for a in g.affects:
                    if a not in gaps[key].affects:
                        gaps[key].affects.append(a)
            else:
                gaps[key] = PrivilegeGap(g.privilege, g.grant_sql, list(g.affects))

    for items in by_name.values():
        pick = items[0][1]
        first = items[0][1]
        # 声明成实例级的（连接占用这类）：值在哪个库查都一样，并成一条、不加前缀
        # ——注意我们自己的连接会让全局计数器微小漂移（多一次连接就读数 +N），
        # 按值比较会把这些漂移误判成「各库不同」，所以这里信声明而不是信值。
        same = len(items) == 1 or first.instance_scope \
            or all(_same_check(first, c) for _, c in items[1:])
        if not same:
            # 各库不同 = 库级指标：取最严重那条，标上它来自哪个库
            db, pick = max(items, key=lambda it: _STATUS_LEVEL[it[1].status])
            pick.title = f"[{db}] {pick.title}"
        merged.checks.append(pick)

    merged.overall = _worst(merged.checks)
    merged.privileges = list(gaps.values())
    merged.elapsed_ms = total_ms
    return merged
