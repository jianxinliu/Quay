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
_DIM_TITLE = {k: t for k, t in DIMENSIONS}

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
        parts = [f"{n} 项{label}" for label, n in
                 (("严重", counts.get("critical", 0)),
                  ("需关注", counts.get("warn", 0)),
                  ("正常", counts.get("ok", 0)),
                  ("参考", counts.get("info", 0)),
                  ("无法测量", counts.get("unknown", 0)))
                 if n]
        return "、".join(parts) + f"（共 {len(self.checks)} 项）"

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
            "dimensions": [{"name": k, "title": t} for k, t in DIMENSIONS],
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
        dim_titles = {k: t for k, t in DIMENSIONS}
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
        return "未知"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    seconds = float(seconds)
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {mins} 分"
    if mins:
        return f"{mins} 分 {secs} 秒"
    return f"{secs} 秒"


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
        return "<用户名>"
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
    return [PrivilegeGap(p, templates.get(p, f"-- 补权限: {p}"), sorted(t))
            for p, t in by_priv.items()]


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
    return got, "SHOW GLOBAL STATUS（会话级回退值，若无 PROCESS 权限不代表全局）"


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


def _mysql_checks(engine: SAEngine, schema: str | None) -> list[Check]:
    checks: list[Check] = []
    status, src = _mysql_status(engine)
    variables = _mysql_variables(engine)
    uptime = _to_num(status.get("Uptime"))
    version = variables.get("version", "未知")

    # --- 服务器信息 ---
    checks.append(Check(
        "server", "服务器", "info", f"MySQL {version}",
        f"已运行 {_human_duration(uptime)}（状态来源：{src}）",
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
        detail = ([f"Connection_errors_max_connection {int(rejected):,} 次"
                   "（因超 max_connections 被拒绝的连接）"] if rejected else [])
        checks.append(Check(
            "connections", "连接占用", level,
            f"{int(used)} / {int(max_conn)}（{pct:.0%}）",
            ("已有连接因超 max_connections 被拒绝过，池子确实打满过；调大 max_connections "
             "或查应用连接池泄漏" if rejected and rejected > 0 else
             "接近上限时新连接会被拒绝；长连接过多考虑调 max_connections 或查应用连接池泄漏")
            if level != "ok" else "连接数在健康范围",
            detail, dimension="capacity",
        ))
    else:
        checks.append(Check("connections", "连接占用", "unknown", "未知",
                            "取不到 Threads_connected / max_connections（权限或版本不支持）",
                            dimension="capacity", privilege="PROCESS"))

    # --- 活跃线程数 ---
    running = _to_num(status.get("Threads_running"))
    if running is not None:
        level = "warn" if running >= 50 else "info"
        checks.append(Check(
            "threads_running", "活跃线程", level, str(int(running)),
            "并发执行中的线程数；持续很高（>50）通常意味着慢查询在堆积" if level == "warn"
            else "参考值：并发执行中的线程数",
            dimension="capacity",
        ))
    else:
        checks.append(Check("threads_running", "活跃线程", "unknown", "未知",
                            "取不到 Threads_running", dimension="capacity"))

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
            detail = [f"逻辑读 {int(hit_req):,} 次，物理读 {int(disk_req):,} 次"]
            if wait_free and wait_free > 0:
                detail.append(f"Innodb_buffer_pool_wait_free {int(wait_free):,} 次"
                              "（等空闲页的刷脏被打断，缓冲池确实不够）")
            checks.append(Check(
                "buffer_pool_hit_ratio", "InnoDB 缓冲池命中率", level, f"{hit:.2%}",
                "热数据已超出缓冲池，频繁读盘；考虑加大 innodb_buffer_pool_size 或优化全表扫描查询"
                if level != "ok" else "热数据基本都缓存在内存里",
                detail, dimension="performance",
            ))
        else:
            checks.append(Check("buffer_pool_hit_ratio", "InnoDB 缓冲池命中率", "info",
                                "无读取活动", "启动后还没有足够的读取活动来计算命中率",
                                dimension="performance"))
    else:
        checks.append(Check("buffer_pool_hit_ratio", "InnoDB 缓冲池命中率", "unknown", "未知",
                            "取不到 Innodb_buffer_pool_read_requests / reads",
                            dimension="performance"))

    # --- 缓冲池 vs 数据总量（容量规划参考） ---
    checks.append(_mysql_buffer_pool_vs_data(engine, variables, schema))

    # --- 慢查询累计 ---
    slow = _to_num(status.get("Slow_queries"))
    if slow is not None and uptime is not None:
        rate = _rate_per_hour(slow, uptime)
        lqt = variables.get("long_query_time", "?")
        checks.append(Check(
            "slow_queries", "慢查询（累计）", "warn" if (rate or 0) > 10 else "info",
            f"{int(slow):,} 条（约 {rate:.1f} 条/小时）" if rate is not None else f"{int(slow):,} 条",
            f"阈值 long_query_time={lqt}s。看具体是哪些查询：查询台按耗时排序，或用 EXPLAIN 排查",
            dimension="performance",
        ))
    else:
        checks.append(Check("slow_queries", "慢查询（累计）", "unknown", "未知",
                            "取不到 Slow_queries", dimension="performance"))

    # --- 全表扫描 JOIN ---
    fj = _to_num(status.get("Select_full_join"))
    rj = _to_num(status.get("Select_range_join"))
    if fj is not None:
        level = "warn" if (fj > 0 and rj is not None and rj > 0 and fj / (fj + rj) > 0.1) else "info"
        checks.append(Check(
            "full_scan_joins", "全表扫描 JOIN", level, f"{int(fj):,} 次",
            "JOIN 没走索引（type=ALL），扫描行数被放大；检查被 JOIN 列上是否有索引"
            if level == "warn" else "参考值：累计发生的不走索引的 JOIN 次数",
            dimension="performance",
        ))
    else:
        checks.append(Check("full_scan_joins", "全表扫描 JOIN", "unknown", "未知",
                            "取不到 Select_full_join", dimension="performance"))

    # --- 临时表落盘比例 ---
    checks.append(_mysql_tmp_tables_on_disk(status, uptime))

    # --- 中止的连接 ---
    aborted = _to_num(status.get("Aborted_clients"))
    aborted_conn = _to_num(status.get("Aborted_connects"))
    if aborted is not None:
        rate = _rate_per_hour(aborted, uptime)
        level = "warn" if (rate or 0) > 20 else "info"
        checks.append(Check(
            "aborted_clients", "异常断开的连接", level,
            f"{int(aborted):,} 次" + (f"（约 {rate:.1f} 次/小时）" if rate is not None else ""),
            "客户端没正确关闭连接（连接池泄漏 / 网络抖动 / wait_timeout 过短）"
            if level == "warn" else "参考值：客户端未正常关闭的连接数；持续高频才需关注",
            [f"Aborted_connects（连接被拒）{int(aborted_conn or 0):,} 次"] if aborted_conn else [],
            dimension="availability",
        ))
    else:
        checks.append(Check("aborted_clients", "异常断开的连接", "unknown", "未知",
                            "取不到 Aborted_clients", dimension="availability"))

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
        return Check("deadlocks", "InnoDB 死锁", "unknown", "未知",
                     "取不到死锁计数（events_errors_summary_global_by_error 与 Innodb_deadlocks 都不可用）",
                     dimension="concurrency")
    return Check(
        "deadlocks", "InnoDB 死锁", "warn" if n > 0 else "ok", f"{int(n):,} 次",
        "有死锁发生：查最近的事务冲突，确保同一批资源按固定顺序加锁"
        if n > 0 else "启动以来无死锁",
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
        return Check("long_queries", "长查询", "unknown", "未知",
                     f"查 performance_schema.threads 失败（可能未开启 performance_schema）："
                     f"{type(e).__name__}",
                     dimension="concurrency")
    if not rows:
        return Check("long_queries", "长查询", "ok", f"无 >= {LONG_QUERY_WARN_S}s 的查询",
                     "当前没有长时间执行的查询", dimension="concurrency")
    worst = max(float(r[0]) for r in rows)
    level: Status = "critical" if worst >= LONG_QUERY_CRIT_S else "warn"
    return Check(
        "long_queries", "长查询", level, f"{worst:.0f}s（最长）",
        "查询台「取消」可中断（走 KILL QUERY）；再用 EXPLAIN 看是否全表扫描",
        [f"{int(r[0])}s | {str(r[1]) or '(无 SQL 文本)'}" for r in rows],
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
                "lock_waits", "行锁等待", "warn" if n > 0 else "ok", f"{int(n)} 个事务在等锁",
                "有事务被阻塞；查 INNODB_TRX 看谁持有锁，长事务考虑用查询台取消"
                if n > 0 else "当前没有事务在等待行锁",
                dimension="concurrency",
            )
        except Exception:
            continue
    return Check("lock_waits", "行锁等待", "unknown", "未知",
                 "查锁等待的视图都不可访问（performance_schema.data_lock_waits /"
                 " information_schema.INNODB_TRX / sys.innodb_lock_waits）",
                 dimension="concurrency")


def _mysql_tmp_tables_on_disk(status: dict[str, str], uptime: float | None) -> Check:
    """临时表落盘比例：Created_tmp_disk_tables / Created_tmp_tables。超过 1/4 落盘
    说明排序/哈希频繁超过 tmp_table_size，是 Percona/官方调优的常规关注项。"""
    disk = _to_num(status.get("Created_tmp_disk_tables"))
    total = _to_num(status.get("Created_tmp_tables"))
    if disk is None or total is None:
        return Check("tmp_tables_on_disk", "临时表落盘", "unknown", "未知",
                     "取不到 Created_tmp_tables / Created_tmp_disk_tables",
                     dimension="performance")
    if total <= 0:
        return Check("tmp_tables_on_disk", "临时表落盘", "ok", "无临时表活动",
                     "启动后还没创建过临时表", dimension="performance")
    pct = disk / total
    level: Status = "warn" if pct > TMP_DISK_WARN_PCT else "ok"
    return Check(
        "tmp_tables_on_disk", "临时表落盘", level, f"{pct:.0%}（{int(disk):,}/{int(total):,}）",
        "排序/哈希超过 tmp_table_size 落了临时磁盘表；调大 tmp_table_size 与 max_heap_table_size，"
        "或优化产生大临时表的查询（看 EXPLAIN 里的 Using temporary）"
        if level != "ok" else "临时表基本都在内存完成",
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
        return Check("no_primary_key", "无主键表", "unknown", "未知",
                     f"查无主键表失败：{type(e).__name__}", dimension="maintenance")
    if not rows:
        return Check("no_primary_key", "无主键表", "ok", "无",
                     "所有表都有主键或唯一索引", dimension="maintenance")
    return Check(
        "no_primary_key", "无主键表", "warn", f"{len(rows)} 张",
        "这些表没有主键或唯一非空索引：row-based 复制时回从表要全表扫描，也影响 binlog_group_commit；"
        "给每张表加自增主键（小表也建议）",
        [f"{r[0]}.{r[1]}（约 {int(float(r[2]) or 0):,} 行）" for r in rows],
        dimension="maintenance",
    )


def _mysql_binlog_cache(status: dict[str, str]) -> Check:
    """Binlog_cache_disk_use：事务超过 binlog_cache_size 落临时文件。大事务是
    复制延迟的主因之一（官方文档：事务太大会在磁盘缓存 binlog 事件）。"""
    use = _to_num(status.get("Binlog_cache_use"))
    disk = _to_num(status.get("Binlog_cache_disk_use"))
    if use is None or disk is None:
        return Check("binlog_cache", "大事务落盘", "unknown", "未知",
                     "取不到 Binlog_cache_use / Binlog_cache_disk_use",
                     dimension="replication")
    if use <= 0:
        return Check("binlog_cache", "大事务落盘", "ok", "无 binlog 缓存活动",
                     "该实例没开 binlog 或还没提交过事务", dimension="replication")
    pct = disk / use
    level: Status = "warn" if disk > 0 else "ok"
    return Check(
        "binlog_cache", "大事务落盘", level,
        f"{int(disk):,} 次落盘 / {int(use):,} 次提交（{pct:.0%}）",
        "有事务超过 binlog_cache_size 落了磁盘：这是大事务的信号，会拖慢复制与提交；"
        "调大 binlog_cache_size，并拆分批量写入"
        if level != "ok" else "所有事务的 binlog 都在内存缓存完成",
        dimension="replication",
    )


def _mysql_buffer_pool_vs_data(engine: SAEngine, variables: dict[str, str],
                                schema: str | None) -> Check:
    """缓冲池大小 vs 数据总量：数据量远超缓冲池时热数据必然部分在盘上，
    与命中率项互相印证。"""
    bp = _to_num(variables.get("innodb_buffer_pool_size"))
    if not bp:
        return Check("buffer_pool_vs_data", "缓冲池 vs 数据量", "unknown", "未知",
                     "取不到 innodb_buffer_pool_size", dimension="capacity")
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
        return Check("buffer_pool_vs_data", "缓冲池 vs 数据量", "unknown", "未知",
                     f"查数据总量失败：{type(e).__name__}", dimension="capacity")
    if total is None or total <= 0:
        return Check("buffer_pool_vs_data", "缓冲池 vs 数据量", "info", "无数据表",
                     "该库范围内没有用户表", dimension="capacity")
    ratio = total / bp
    level: Status = "warn" if ratio > BP_VS_DATA_WARN else "ok"
    return Check(
        "buffer_pool_vs_data", "缓冲池 vs 数据量", level,
        f"数据 {_human_bytes(total)} / 缓冲池 {_human_bytes(bp)}（{ratio:.1f} 倍）",
        "数据量远超缓冲池，热数据必然部分在盘上；结合命中率项一起看，"
        "命中率也低就该扩内存或缩小查询范围"
        if level != "ok" else "数据总量在缓冲池可覆盖的范围内",
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
            return Check("replication_lag", "复制延迟", "info", "非副本",
                         "该实例未配置源/副本复制，无延迟可测", dimension="replication")
        col = next((i for i, k in enumerate(keys)
                    if str(k).lower() in ("seconds_behind_master", "seconds_behind_source")), None)
        if col is None:
            continue
        host = str(rows[0][keys.index("Replica_Host")] if "Replica_Host" in keys else "") or "?"
        val = rows[0][col]
        lag = _to_num(val)
        if lag is None:
            return Check("replication_lag", "复制延迟", "warn", f"源 {host}：延迟未知（NULL）",
                         "Seconds_Behind_Master 为 NULL 通常是复制线程断开或正在追赶，查副本状态",
                         dimension="replication")
        level: Status = "critical" if lag >= REPL_LAG_CRIT_S else "warn" if lag >= REPL_LAG_WARN_S else "ok"
        return Check(
            "replication_lag", "复制延迟", level, f"{lag:.0f}s（源 {host}）",
            "落后过多时读副本数据已旧；查网络 / 大事务 / 长查询是否阻塞了回放线程"
            if level != "ok" else "副本追平源",
            dimension="replication",
        )
    return Check("replication_lag", "复制延迟", "unknown", "未知",
                 "无副本状态权限（需 REPLICATION CLIENT），或该 MySQL 版本不支持"
                 " SHOW REPLICA/SLAVE STATUS",
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
        return Check("big_tables", "大表 TOP5", "unknown", "未知",
                     f"查 information_schema.tables 失败：{type(e).__name__}",
                     dimension="maintenance")
    if not rows:
        return Check("big_tables", "大表 TOP5", "info", "无表",
                     "该库范围内没有用户表" + (f"（schema={schema}）" if schema else ""),
                     dimension="maintenance")
    return Check(
        "big_tables", "大表 TOP5", "info",
        f"最大 {str(rows[0][1])}（{_human_bytes(float(rows[0][2]))}）",
        "最大的几张表是维护成本的主要来源：DDL 变更久、备份慢、全表扫描风险高",
        [f"{r[0]}.{r[1]} — {_human_bytes(float(r[2]))}，约 {int(float(r[3] or 0)):,} 行" for r in rows],
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


def _postgres_checks(engine: SAEngine, schema: str | None) -> list[Check]:
    checks: list[Check] = []
    try:
        version = str(_scalar(engine, "SELECT version()")).split(",")[0]
    except Exception:
        version = "未知"

    uptime_s = None
    try:
        started = _scalar(engine, "SELECT pg_postmaster_start_time()")
        if isinstance(started, dt.datetime):
            uptime_s = (dt.datetime.now(started.tzinfo) - started).total_seconds()
    except Exception:
        pass
    checks.append(Check(
        "server", "服务器", "info", version,
        f"已运行 {_human_duration(uptime_s)}" if uptime_s is not None else "取不到启动时间",
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
        conn_err = f"查 pg_stat_activity 失败：{type(e).__name__}"
    if used is not None and max_conn:
        pct = used / max_conn
        level: Status = "critical" if pct >= CONN_CRIT_PCT else "warn" if pct >= CONN_WARN_PCT else "ok"
        checks.append(Check(
            "connections", "连接占用", level, f"{int(used)} / {int(max_conn)}（{pct:.0%}）",
            "接近上限时新连接会被拒绝；查 idle in transaction 的长连接，或调 max_connections"
            if level != "ok" else "连接数在健康范围",
            dimension="capacity",
        ))
    else:
        checks.append(Check(
            "connections", "连接占用", "unknown", "未知",
            conn_err if used is None else "取不到 max_connections",
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
        for name, title in (("idle_in_transaction", "空闲事务"),
                            ("long_queries", "长查询"),
                            ("wait_events", "等待事件")):
            checks.append(Check(name, title, "unknown", "未知",
                                "账号无 pg_monitor 权限：pg_stat_activity 的 state/query/wait_event "
                                "列对非 pg_monitor 角色返回 NULL，只能看到自己的会话",
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
                "cache_hit_ratio", "缓存命中率", level, f"{hit:.2%}",
                "shared_buffers 放不下热数据，频繁读盘；调大 shared_buffers 或优化全表扫描查询"
                if level != "ok" else "热数据基本都在 shared_buffers 里",
                [f"命中 {int(hit_n):,} 块，读盘 {int(read_n):,} 块"],
                dimension="performance",
            ))
        else:
            checks.append(Check("cache_hit_ratio", "缓存命中率", "info", "无读取活动",
                                "启动后还没有足够的读取活动来计算命中率",
                                dimension="performance"))
    except Exception as e:
        checks.append(Check("cache_hit_ratio", "缓存命中率", "unknown", "未知",
                            f"查 pg_stat_database 失败：{type(e).__name__}",
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
                "deadlocks", "死锁（累计）", "warn" if n > 0 else "ok", f"{int(n):,} 次",
                "有死锁发生：查冲突事务，确保同一批资源按固定顺序加锁" if n > 0 else "启动以来无死锁",
                dimension="concurrency",
            ))
        else:
            checks.append(Check("deadlocks", "死锁（累计）", "unknown", "未知",
                                "deadlocks 列取不到", dimension="concurrency"))
    except Exception as e:
        checks.append(Check("deadlocks", "死锁（累计）", "unknown", "未知",
                            f"查 pg_stat_database.deadlocks 失败：{type(e).__name__}",
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
        return Check("idle_in_transaction", "空闲事务", "unknown", "未知",
                     f"查 pg_stat_activity 失败：{type(e).__name__}", dimension="concurrency")
    if not rows:
        return Check("idle_in_transaction", "空闲事务", "ok",
                     f"无超过 {IDLE_TXN_WARN_S}s 的空闲事务",
                     "空闲事务拿着锁又挡 autovacuum，越长越该清掉",
                     dimension="concurrency")
    worst = max(float(r[1]) for r in rows)
    return Check(
        "idle_in_transaction", "空闲事务", "warn", f"{worst:.0f}s（最长）",
        "有事务开了不提交也不干活，挡住 autovacuum 并持锁；查应用是否漏提交，或取消该会话",
        [f"pid {r[0]} · {int(float(r[1]))}s | {str(r[2])}" for r in rows],
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
        return Check("long_queries", "长查询", "unknown", "未知",
                     f"查 pg_stat_activity 失败：{type(e).__name__}", dimension="performance")
    if not rows:
        return Check("long_queries", "长查询", "ok", f"无 >= {LONG_QUERY_WARN_S}s 的查询",
                     "当前没有长时间执行的查询", dimension="performance")
    worst = max(float(r[1]) for r in rows)
    level: Status = "critical" if worst >= LONG_QUERY_CRIT_S else "warn"
    return Check(
        "long_queries", "长查询", level, f"{worst:.0f}s（最长）",
        "用 pg_cancel_backend(pid) 或查询台「取消」中断；再 EXPLAIN 看是否全表扫描",
        [f"pid {r[0]} · {int(float(r[1]))}s | {str(r[2])}" for r in rows],
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
        return Check("wait_events", "等待事件", "unknown", "未知",
                     f"查 pg_stat_activity 等待事件失败：{type(e).__name__}",
                     dimension="performance")
    if not rows:
        return Check("wait_events", "等待事件", "ok", "无会话在等待",
                     "所有后端进程都在执行而非等待", dimension="performance")
    locks = sum(int(r[2]) for r in rows if str(r[0]) == "Lock")
    level: Status = "warn" if locks >= 5 else "info"
    return Check(
        "wait_events", "等待事件", level, f"TOP {str(rows[0][1])}（{int(rows[0][2])} 个会话）",
        "等待事件说明会话卡在哪里；Lock 类等待多时查持锁的 idle in transaction 会话"
        if level == "warn" else "参考值：当前各会话的等待事件分布",
        [f"{r[0]}/{r[1]} — {int(r[2])} 个" for r in rows],
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
        return Check("temp_files", "临时文件落盘", "unknown", "未知",
                     f"查 pg_stat_database 失败：{type(e).__name__}", dimension="performance")
    if not row:
        return Check("temp_files", "临时文件落盘", "unknown", "未知",
                     "pg_stat_database 没有当前库的行", dimension="performance")
    files = _to_num(row[0][0])
    bytes_ = _to_num(row[0][1])
    if files is None:
        return Check("temp_files", "临时文件落盘", "unknown", "未知",
                     "temp_files 列取不到", dimension="performance")
    if files <= 0:
        return Check("temp_files", "临时文件落盘", "ok", "无临时文件",
                     "启动以来没有查询把数据写到临时文件", dimension="performance")
    rate = _rate_per_hour(files, uptime_s)
    level: Status = "warn" if (rate or 0) > 10 else "info"
    return Check(
        "temp_files", "临时文件落盘", level,
        f"{int(files):,} 个文件 / {_human_bytes(bytes_)}"
        + (f"（约 {rate:.1f} 个/小时）" if rate is not None else ""),
        "查询的排序/哈希超过 work_mem 落了临时文件；调大 work_mem，或用 EXPLAIN 找出"
        " Using filesort / hash spill 的查询"
        if level == "warn" else "参考值：累计临时文件数与字节数",
        dimension="performance",
    )


def _pg_bloat(engine: SAEngine, schema: str | None) -> Check:
    try:
        rows = _rows(
            engine,
            "SELECT relname, n_dead_tup, n_live_tup,"
            " COALESCE(last_autovacuum::text, '从未')"
            " FROM pg_stat_user_tables"
            f" WHERE n_dead_tup >= :n AND {schema_filter_pg(schema)}"
            " ORDER BY n_dead_tup DESC LIMIT 5",
            {"n": DEAD_TUPLE_WARN, **({"s": schema} if schema else {})},
        )
    except Exception as e:
        return Check("bloat", "死元组膨胀", "unknown", "未知",
                     f"查 pg_stat_user_tables 失败：{type(e).__name__}", dimension="maintenance")
    if not rows:
        return Check("bloat", "死元组膨胀", "ok", f"无超过 {DEAD_TUPLE_WARN} 死元组的表",
                     "死元组由 autovacuum 回收；表删除/更新频繁时关注此项",
                     dimension="maintenance")
    return Check(
        "bloat", "死元组膨胀", "warn", f"最严重 {str(rows[0][0])}（{int(float(rows[0][1])):,} 死元组）",
        "死元组过多说明 autovacuum 跟不上；查 last_autovacuum 是否太久没跑，必要时手动 VACUUM",
        [f"{r[0]} — 死 {int(float(r[1])):,} / 活 {int(float(r[2]) or 0):,}，上次 autovacuum {r[3]}"
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
        return Check("stats_stale", "统计信息过期", "unknown", "未知",
                     f"查 pg_stat_user_tables 失败：{type(e).__name__}", dimension="maintenance")
    stale = [r for r in rows
             if r[1] is None or (_to_num(r[2]) or 0) > STATS_STALE_DAYS * 86400]
    if not stale:
        return Check("stats_stale", "统计信息过期", "ok",
                     f"全部在 {STATS_STALE_DAYS} 天内分析过",
                     "统计信息新鲜，规划器能拿到准确的行数估计", dimension="maintenance")
    worst_age = max((_to_num(r[2]) or 0) for r in stale)
    return Check(
        "stats_stale", "统计信息过期", "warn",
        f"{len(stale)} 张表超过 {STATS_STALE_DAYS} 天未分析"
        + (f"（最久 {worst_age / 86400:.0f} 天）" if worst_age > 0 else "（从未分析）"),
        "统计信息过期会让规划器选错索引；查 autovacuum 是否在跑，必要时手动 ANALYZE",
        [f"{r[0]} — {'从未分析' if r[1] is None else f'{_human_duration(_to_num(r[2]))} 前'}"
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
        return Check("unused_indexes", "未使用索引", "unknown", "未知",
                     f"查 pg_stat_user_indexes 失败：{type(e).__name__}", dimension="maintenance")
    if not rows:
        return Check("unused_indexes", "未使用索引", "ok", "无",
                     "所有非唯一索引都被使用过（或没有非唯一索引）", dimension="maintenance")
    total_b = sum(float(r[3] or 0) for r in rows)
    level: Status = "warn" if total_b > UNUSED_IDX_WARN_B else "info"
    return Check(
        "unused_indexes", "未使用索引", level,
        f"{len(rows)} 个从未使用（合计 {_human_bytes(total_b)}）",
        "这些索引自统计重置以来从未被扫描，却要为每次写入付出维护成本；"
        "确认无用后删除可减少写放大并回收空间",
        [f"{r[0]}.{r[1]} — {_human_bytes(float(r[3] or 0))}" for r in rows[:5]],
        dimension="maintenance",
    )


def _pg_replication_lag(engine: SAEngine, can_see: bool) -> Check:
    if not can_see:
        return Check("replication_lag", "复制延迟", "unknown", "未知",
                     "账号无 pg_monitor 权限，pg_stat_replication 只能看到自己的会话",
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
        return Check("replication_lag", "复制延迟", "unknown", "未知",
                     f"查 pg_stat_replication 失败（通常缺 pg_monitor 权限）：{type(e).__name__}",
                     dimension="replication", privilege="pg_monitor")
    if not rows:
        return Check("replication_lag", "复制延迟", "info", "无流复制",
                     "没有连接的流复制备用节点，或该实例是主节点且无订阅者",
                     dimension="replication")
    worst = max(float(r[3] or 0) for r in rows)
    level: Status = "critical" if worst >= REPL_LAG_CRIT_S else "warn" if worst >= REPL_LAG_WARN_S else "ok"
    return Check(
        "replication_lag", "复制延迟", level, f"{worst:.0f}s（最慢备库）",
        "写/刷盘/回放延迟之和过大时备库数据已旧；查网络、大事务或备库长查询"
        if level != "ok" else "所有备库追平主库",
        [f"{r[0]}（{r[1]}）总延迟 {float(r[3] or 0):.0f}s" for r in rows],
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
            return Check("replication_slots", "复制槽健康度", "unknown", "未知",
                         f"查 pg_replication_slots 失败：{type(e).__name__}",
                         dimension="replication")
    if not rows:
        return Check("replication_slots", "复制槽健康度", "info", "无复制槽",
                     "该实例没有配置复制槽（物理/逻辑都没有）", dimension="replication")
    worst_lag = max(float(r[5] or 0) for r in rows)
    level: Status = "ok"
    msg = "所有复制槽都在正常消费 WAL"
    # 官方枚举优先：wal_status 直接说明槽保留的 WAL 是否已不安全
    if not legacy:
        for r in rows:
            st = _SLOT_WAL_STATUS_LEVEL.get(str(r[3] or ""))
            if st == "critical":
                level, msg = "critical", f"复制槽 {r[0]} 的 wal_status={r[3]}：保留的 WAL 已不安全"
                break
            if st == "warn" and level != "critical":
                level, msg = "warn", f"复制槽 {r[0]} 的 wal_status={r[3]}：WAL 保留量已超上限"
    # 滞后量兜底
    if level == "ok":
        if worst_lag >= SLOT_LAG_CRIT_B:
            level, msg = "critical", "复制槽滞后过大：消费方不拉 WAL，pg_wal 会持续堆积直到撑爆磁盘"
        elif worst_lag >= SLOT_LAG_WARN_B:
            level, msg = "warn", "复制槽滞后较大：消费方不拉 WAL，pg_wal 会持续堆积"
    # 非活跃槽：无论滞后多少都在堆积
    inactive = [r for r in rows if not r[2]]
    if level == "ok" and inactive:
        level, msg = "warn", "有非活跃的复制槽：消费方断开后 WAL 仍在堆积，确认槽是否仍需要"
    safe = min((float(r[4]) for r in rows if r[4] is not None and float(r[4]) >= 0),
               default=None)
    value = f"最滞后 {str(rows[0][0])}（{_human_bytes(worst_lag)}）"
    if safe is not None and safe < SLOT_LAG_WARN_B:
        value += f"，{rows[0][0]} 离丢数据仅剩 {_human_bytes(safe)}"
    return Check(
        "replication_slots", "复制槽健康度", level, value,
        msg + ("；不再需要的槽用 pg_drop_replication_slot 删掉" if level != "ok" else ""),
        [f"{r[0]}（{r[1]}，{'活跃' if r[2] else '未活跃'}"
         + (f"，wal_status={r[3]}" if not legacy and r[3] else "")
         + f"）滞后 {_human_bytes(float(r[5] or 0))}" for r in rows],
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
        return Check("archiver", "WAL 归档", "unknown", "未知",
                     f"查 pg_stat_archiver 失败：{type(e).__name__}",
                     dimension="replication")
    if not rows:
        return Check("archiver", "WAL 归档", "unknown", "未知",
                     "pg_stat_archiver 没有数据", dimension="replication")
    archived, failed, last_fail = _to_num(rows[0][0]), _to_num(rows[0][1]), str(rows[0][2])
    if failed is None:
        return Check("archiver", "WAL 归档", "unknown", "未知",
                     "failed_count 列取不到", dimension="replication")
    if failed <= 0:
        return Check("archiver", "WAL 归档", "ok", f"已归档 {int(archived or 0):,} 段，无失败",
                     "archive_command 一直在正常工作", dimension="replication")
    return Check(
        "archiver", "WAL 归档", "warn", f"{int(failed):,} 次失败",
        "归档失败会让 pg_wal 无法回收（撑爆磁盘）且时间点恢复不可用；"
        "查 archive_command 与 last_failed_time",
        [f"最近失败段：{last_fail}" if last_fail else "last_failed_wal 为空（旧的失败记录已轮换）"],
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
        return Check("xid_wraparound", "事务 ID 回卷风险", "unknown", "未知",
                     f"查 pg_database.datfrozenxid 失败：{type(e).__name__}",
                     dimension="maintenance")
    if not rows or rows[0][0] is None:
        return Check("xid_wraparound", "事务 ID 回卷风险", "unknown", "未知",
                     "age(datfrozenxid) 取不到", dimension="maintenance")
    age_, remaining = _to_num(rows[0][0]), _to_num(rows[0][1])
    if remaining is None:
        return Check("xid_wraparound", "事务 ID 回卷风险", "unknown", "未知",
                     "剩余事务数算不出来", dimension="maintenance")
    if remaining < XID_REMAINING_CRIT:
        level: Status = "critical"
    elif remaining < XID_REMAINING_WARN:
        level = "warn"
    else:
        level = "ok"
    return Check(
        "xid_wraparound", "事务 ID 回卷风险", level,
        f"剩余 {remaining / 1e6:.0f}M 个事务 ID",
        "剩余事务号不足时库会被强制只读（防回卷），在此之前必须让 autovacuum 把"
        " datfrozenxid 推进；查是否有长事务挡住 vacuum（idle_in_transaction 项）"
        if level != "ok" else "各库的 datfrozenxid 都在被正常推进",
        [f"最老的库已用 {age_ / 1e6:.0f}M 个事务 ID（上限 2147M）"],
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
        return Check("big_tables", "库大小与大表 TOP5", "unknown", "未知",
                     f"查 pg_class 大小失败：{type(e).__name__}", dimension="maintenance")
    details = [f"{r[0]} — {_human_bytes(float(r[1]))}" for r in rows]
    return Check(
        "big_tables", "库大小与大表 TOP5", "info",
        f"库 {total or '未知'}" + (f"，最大表 {rows[0][0]}" if rows else ""),
        "最大的几张表是维护成本的主要来源：DDL 变更久、备份慢、全表扫描风险高",
        details, dimension="maintenance",
    )


# =====================================================================
# SQLite
# =====================================================================


def _sqlite_checks(engine: SAEngine, _schema: str | None) -> list[Check]:
    checks: list[Check] = []

    try:
        version = str(_scalar(engine, "SELECT sqlite_version()"))
    except Exception:
        version = "未知"
    try:
        pages = _to_num(_scalar(engine, "PRAGMA page_count"))
        freelist = _to_num(_scalar(engine, "PRAGMA freelist_count"))
        page_size = _to_num(_scalar(engine, "PRAGMA page_size"))
    except Exception:
        pages = freelist = page_size = None

    checks.append(Check(
        "server", "数据库", "info", f"SQLite {version}",
        f"{_human_bytes((pages or 0) * (page_size or 0))}（{int(pages or 0):,} 页 × "
        f"{int(page_size or 0)} B）" if pages and page_size else "取不到页信息",
        dimension="availability",
    ))

    # --- 完整性检查（quick_check 比 integrity_check 快，覆盖绝大多数损坏） ---
    try:
        rows = _rows(engine, "PRAGMA quick_check")
        result = "; ".join(str(r[0]) for r in rows[:3]) if rows else ""
        ok = result.strip().lower() == "ok"
        checks.append(Check(
            "integrity", "完整性检查", "ok" if ok else "critical",
            "正常" if ok else (result or "异常"),
            "数据库文件无结构损坏" if ok else "数据库已损坏！立即备份后用 .recover 或 integrity_check 定位",
            dimension="availability",
        ))
    except Exception as e:
        checks.append(Check("integrity", "完整性检查", "unknown", "未知",
                            f"PRAGMA quick_check 失败：{type(e).__name__}",
                            dimension="availability"))

    # --- 空闲页碎片 ---
    if pages and freelist is not None and pages > 0:
        frag = freelist / pages
        checks.append(Check(
            "fragmentation", "空闲页碎片", "warn" if frag > 0.2 else "ok",
            f"{frag:.1%}（{int(freelist):,}/{int(pages):,} 页空闲）",
            "空闲页占比高时 VACUUM 可回收空间并加速扫描" if frag > 0.2
            else "删除产生的空闲页比例正常",
            dimension="maintenance",
        ))
    else:
        checks.append(Check("fragmentation", "空闲页碎片", "unknown", "未知",
                            "取不到 freelist/page 计数", dimension="maintenance"))

    # --- journal 模式（WAL 与否影响并发） ---
    try:
        mode = str(_scalar(engine, "PRAGMA journal_mode"))
        checks.append(Check(
            "journal_mode", "日志模式", "info", mode,
            "WAL 支持读写并发、崩溃恢复更快；DELETE 模式下写会阻塞读" if mode.upper() == "WAL"
            else "并发写较多时考虑切到 WAL（PRAGMA journal_mode=WAL）",
            dimension="concurrency",
        ))
    except Exception:
        checks.append(Check("journal_mode", "日志模式", "unknown", "未知",
                            "PRAGMA journal_mode 失败", dimension="concurrency"))

    # --- 表行数（有 sqlite_stat1 时用统计值，避免逐表 count） ---
    try:
        rows = _rows(engine, "SELECT tbl, stat FROM sqlite_stat1 ORDER BY 1 LIMIT 10")
        details = [f"{r[0]} — 约 {str(r[1]).split(' ')[0]} 行" for r in rows if r[1]]
        checks.append(Check(
            "tables", "表与行数", "info",
            f"{len(details)} 张表有统计信息" if details else "无统计信息",
            "行数来自 ANALYZE 写入的 sqlite_stat1（近似值）；跑过 ANALYZE 才有",
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
                "tables", "表与行数", "info", f"{int(n or 0)} 张表",
                "无 sqlite_stat1 统计信息，故不逐表 count（大表 count 慢）；"
                "ANALYZE 后可看近似行数",
                dimension="maintenance",
            ))
        except Exception as e:
            checks.append(Check("tables", "表与行数", "unknown", "未知",
                                f"查 sqlite_master 失败：{type(e).__name__}",
                                dimension="maintenance"))

    return checks


# =====================================================================
# ClickHouse（轻量：核心指标 + 副本/part 健康）
# =====================================================================


def _clickhouse_checks(engine: SAEngine, schema: str | None) -> list[Check]:
    checks: list[Check] = []
    try:
        version = str(_scalar(engine, "SELECT version()"))
    except Exception:
        version = "未知"
    try:
        uptime = _to_num(_scalar(engine, "SELECT uptime()"))
    except Exception:
        uptime = None
    checks.append(Check("server", "服务器", "info", f"ClickHouse {version}",
                        f"已运行 {_human_duration(uptime)}" if uptime is not None else "取不到 uptime",
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
                "metrics", "核心指标", "info", f"{len(metrics)} 项",
                "Query=正在执行的查询数；Merge/BackgroundMergesAndMutationsPoolTask=后台合并任务积压"
                "（持续接近上限说明合并跟不上写入，逼近 too_many_parts 时新 part 会被拒绝写入）；"
                "ReadonlyReplica>0 说明有副本只读",
                [f"{k} = {int(v)}" for k, v in sorted(metrics.items()) if v],
                dimension="capacity",
            ))
        else:
            checks.append(Check("metrics", "核心指标", "unknown", "未知",
                                "system.metrics 没有匹配的指标", dimension="capacity"))
    except Exception as e:
        checks.append(Check("metrics", "核心指标", "unknown", "未知",
                            f"查 system.metrics 失败：{type(e).__name__}", dimension="capacity"))

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
                msg = (f"副本会话已过期（{', '.join(r[0] + '.' + r[1] for r in expired)}）："
                       "与 Keeper 的连接断开，同步已停止；查 Keeper 状态与网络")
            elif worst >= 1000:
                level = "critical"
                msg = "队列积压过大，副本严重落后；查网络 / Keeper / 大 mutation"
            elif behind:
                level = "warn"
                msg = ("log_pointer 远小于 log_max_index：拉取线程落后于日志产生速度"
                       "（官方文档明确指出这个差值过大意味着副本有问题）")
            elif worst >= 100:
                level = "warn"
                msg = "queue_size 是待同步的日志条数；持续增长说明副本追不上，查网络/ZK/大 mutation"
            else:
                level = "ok"
                msg = "副本同步队列正常消费"
            checks.append(Check(
                "replication_queue", "副本同步队列", level,
                f"最长队列 {int(worst)}（{rows[0][0]}.{rows[0][1]}）",
                msg,
                [f"{r[0]}.{r[1]} — 队列 {int(float(r[2] or 0))}，待合并 {int(float(r[3] or 0))}，"
                 f"绝对延迟 {int(float(r[4] or 0))}s"
                 + ("，会话已过期" if r[5] else "")
                 + (f"，log {int(float(r[7]) or 0)}/{int(float(r[6]) or 0)}"
                    if r[6] is not None and r[7] is not None else "")
                 for r in rows],
                dimension="replication",
            ))
        else:
            checks.append(Check("replication_queue", "副本同步队列", "info", "无 ReplicatedMergeTree 表",
                                "没有需要同步的副本表（非副本部署，或未用 ReplicatedMergeTree 引擎）",
                                dimension="replication"))
    except Exception as e:
        checks.append(Check("replication_queue", "副本同步队列", "unknown", "未知",
                            f"查 system.replicas 失败：{type(e).__name__}",
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
                "parts", "活跃 part 数", level, f"最多 {int(worst)}（{rows[0][0]}.{rows[0][1]}）",
                "part 数逼近 too_many_parts 阈值时会拒绝写入；降低写入频率或扩大分区粒度",
                [f"{r[0]}.{r[1]} — {int(float(r[2]))} 个活跃 part" for r in rows],
                dimension="maintenance",
            ))
        else:
            checks.append(Check("parts", "活跃 part 数", "ok",
                                f"无超过 {CH_PARTS_WARN} part 的表", "合并跟得上写入",
                                dimension="maintenance"))
    except Exception as e:
        checks.append(Check("parts", "活跃 part 数", "unknown", "未知",
                            f"查 system.parts 失败：{type(e).__name__}", dimension="maintenance"))

    # --- 未完成的 mutation ---
    try:
        n = _to_num(_scalar(
            engine,
            "SELECT count() FROM system.mutations WHERE NOT is_done"
            + (f" AND {schema_filter('clickhouse', schema, 'database')}" if schema else ""),
            schema_params(schema) if schema else None))
        if n is not None:
            checks.append(Check(
                "mutations", "未完成的 mutation", "warn" if n > 0 else "ok", f"{int(n)} 个",
                "ALTER ... UPDATE/DELETE 是异步 mutation；堆积的 mutation 会拖慢合并和查询"
                if n > 0 else "没有在跑的 mutation",
                dimension="maintenance",
            ))
        else:
            checks.append(Check("mutations", "未完成的 mutation", "unknown", "未知",
                                "count 返回 NULL", dimension="maintenance"))
    except Exception as e:
        checks.append(Check("mutations", "未完成的 mutation", "unknown", "未知",
                            f"查 system.mutations 失败：{type(e).__name__}",
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
                "big_tables", "大表 TOP5", "info",
                f"最大 {rows[0][0]}.{rows[0][1]}（{_human_bytes(float(rows[0][2]))}）",
                "最大的几张表是合并/存储成本的主要来源，TTL 与分区设计要重点 review",
                [f"{r[0]}.{r[1]} — {_human_bytes(float(r[2]))}，{int(float(r[3] or 0)):,} 行"
                 for r in rows],
                dimension="maintenance",
            ))
        else:
            checks.append(Check("big_tables", "大表 TOP5", "info", "无表",
                                "没有可统计的 part", dimension="maintenance"))
    except Exception as e:
        checks.append(Check("big_tables", "大表 TOP5", "unknown", "未知",
                            f"查 system.parts 大小失败：{type(e).__name__}",
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
        return Check("disk_space", "磁盘健康", "unknown", "未知",
                     f"查 system.disks 失败：{type(e).__name__}", dimension="capacity")
    if not rows:
        return Check("disk_space", "磁盘健康", "unknown", "未知",
                     "system.disks 没有磁盘记录", dimension="capacity")
    # 确定性状态优先：有盘坏了或只读，其它空间指标都没意义了
    broken = [r for r in rows if r[6]]
    readonly = [r for r in rows if r[5] and not r[6]]
    if broken:
        return Check(
            "disk_space", "磁盘健康", "critical",
            f"{len(broken)} 块盘损坏（{', '.join(str(r[0]) for r in broken)}）",
            "磁盘被标记为 broken，写盘会直接失败；查存储底层与 CH 日志",
            [f"{r[0]}（{str(r[1])}）is_broken=1" for r in broken],
            dimension="capacity",
        )
    if readonly:
        return Check(
            "disk_space", "磁盘健康", "warn",
            f"{len(readonly)} 块盘只读（{', '.join(str(r[0]) for r in readonly)}）",
            "磁盘被置只读（磁盘满或手动设置），写入会失败；查 free_space 与挂载",
            [f"{r[0]}（{str(r[1])}）is_read_only=1" for r in readonly],
            dimension="capacity",
        )
    worst_pct = min((float(r[3]) / float(r[2]) for r in rows if r[2]))
    worst_row = min(rows, key=lambda r: float(r[3]) / float(r[2]) if r[2] else 1)
    level: Status = ("critical" if worst_pct < 1 - DISK_CRIT_PCT
                     else "warn" if worst_pct < 1 - DISK_WARN_PCT else "ok")
    return Check(
        "disk_space", "磁盘健康", level,
        f"最紧张 {str(worst_row[0])}（剩余 {worst_pct:.0%}）",
        "磁盘剩余空间不足时 ClickHouse 会拒绝写入；清理过期 TTL 数据、"
        "扩大磁盘或把冷数据移到其它存储卷"
        if level != "ok" else "数据盘剩余空间充足",
        [f"{r[0]}（{str(r[1])}）— 剩余 {_human_bytes(float(r[4] if r[4] is not None else r[3]))}"
         f" / {_human_bytes(float(r[2]))}"
         + ("" if r[4] is None or float(r[4]) == float(r[3])
            else f"（扣除预留后 {_human_bytes(float(r[4]))}）")
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
        return Check("failed_queries", "失败查询", "unknown", "未知",
                     f"查 system.events 失败：{type(e).__name__}", dimension="performance")
    got = {str(r[0]): _to_num(r[1]) for r in rows}
    failed = sum(v for k, v in got.items() if k == "FailedQuery")
    if failed is None:
        return Check("failed_queries", "失败查询", "unknown", "未知",
                     "system.events 没有 FailedQuery 指标", dimension="performance")
    rate = _rate_per_hour(failed, uptime)
    if failed <= 0:
        return Check("failed_queries", "失败查询", "ok", "无失败查询",
                     "启动以来没有查询失败", dimension="performance")
    level: Status = "warn" if (rate or 0) > 10 or failed > 100 else "info"
    return Check(
        "failed_queries", "失败查询", level,
        f"{int(failed):,} 次" + (f"（约 {rate:.1f} 次/小时）" if rate is not None else ""),
        "失败查询明显变多时查 system.query_log 的 type='ExceptionWhileProcessing' 看具体错误"
        if level == "warn" else "参考值：累计失败的查询数；突然飙升才需关注",
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
            "unsupported", "不支持体检", "unknown", engine_kind,
            f"该引擎暂不支持体检（支持：{', '.join(supported_engines())}）",
        )]
        report.elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        return report

    # 先探活：库都连不上时，逐项报 OperationalError 只是噪音，给一句明确的结论
    ok, why = connectivity_ok(engine)
    if not ok:
        logger.warning("checkup: %s connection unreachable: %s", engine_kind, why)
        report.overall = "critical"
        report.checks = [Check(
            "connectivity", "数据库连接", "critical", "无法连接",
            f"数据库不可达：{why}。请确认数据库在运行、网络/SSH 隧道通畅，"
            "恢复后点「重新体检」即可测量全部指标。",
            dimension="availability",
        )]
        report.elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        return report

    try:
        checks = runner(engine, schema)
    except Exception as e:  # noqa: BLE001 - 兜底：任何未预期的失败都给出可读报告，不裸抛
        logger.exception("checkup failed for %s", engine_kind)
        report.overall = "unknown"
        report.checks = [Check("error", "体检执行失败", "unknown", type(e).__name__, str(e))]
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
            "no_databases", "无可体检的库", "unknown", "0",
            "该连接下没有可体检的用户库（可能全是系统库，或账号无权限列出）。",
            dimension="availability",
        )]
        merged.elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        return merged

    merged.scope = "全体 " + str(len(reports)) + " 个库"
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
