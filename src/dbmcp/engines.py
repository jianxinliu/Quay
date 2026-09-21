"""SQL 执行层（引擎无关）：连接池 + 执行/反射/类型归一。

引擎特有行为（建连参数、取消、DDL、容量估算、语法复核、体检…）已拆成可插拔驱动，
见 ``dbmcp/drivers/``——新增一种数据库只需加一个驱动模块并注册。本模块只保留
所有引擎共享的执行/反射逻辑，并把这些引擎特有入口**委托**给对应驱动。

只读防线（写操作走独立的 writer 账号连接）：
- MySQL:      会话设置 SESSION TRANSACTION READ ONLY + max_execution_time（drivers/mysql.py）
- Postgres:   options 设置 default_transaction_read_only=on + statement_timeout（drivers/postgres.py）
- SQLite:     连接建立时 PRAGMA query_only=ON（drivers/sqlite.py）
- ClickHouse: URL query 参数 readonly=1 + max_execution_time（drivers/clickhouse.py）
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

# create_engine / event：驱动的 build_engine 通过 engines.create_engine / engines.event
# 引用它们（保持 SQLAlchemy 的单一引用面，测试 patch dbmcp.engines.create_engine 对所有
# 驱动生效），故这里虽然本模块没直接调用也必须 import（下方 noqa）。
from sqlalchemy import create_engine, event, inspect, text  # noqa: F401
from sqlalchemy.engine import Engine as SAEngine

from .config import ConnectionConfig, SshIdentity
# 引擎特有实现已挪进 drivers/；这些 re-export 是为了兼容既有 import（测试、service 等）
from .drivers import (  # noqa: F401
    DB_CLIENT_NAME,
    Role,
    SyntaxCheck,
    UnsupportedEngineError,
    driver_for_engine,
    first_sql_keyword,
    get_driver,
)
from .drivers.base import role_timeouts  # noqa: F401
from .drivers.mysql import mysql_read_timeout, mysql_session_statements  # noqa: F401
from .drivers.postgres import pg_database_name  # noqa: F401
from .metrics import estimate_cell_bytes, estimate_result_bytes
from .tunnel import SSHTunnel, open_tunnel
DEFAULT_IDLE_RECLAIM_S = 600  # 隧道 + 引擎空闲 10 分钟回收
DEFAULT_ENGINE_POOL_SIZE = 15  # 单引擎最大连接数（= SQLAlchemy pool_size + max_overflow）


def _sa_pool_kwargs(total: int) -> dict[str, int]:
    """把「单引擎最大连接数 total」拆成 SQLAlchemy 的 pool_size/max_overflow。

    保持既有语义：最多 5 条常驻热连接，其余作 overflow 按需开、用完回收——
    total=15 时正好还原历史默认（pool_size=5, max_overflow=10）。
    """
    base = min(5, max(1, total))
    return {"pool_size": base, "max_overflow": max(0, total - base)}


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    duration_ms: int
    # 每列的权威类型分类（number/string/datetime/date/time/bool/json/binary/""），
    # 由原始 Python 值类型推断——供前端类型图标用，尤其大整数以字符串传输后仍标为 number。
    column_types: list[str] = field(default_factory=list)
    # 结果集估算字节数（见 metrics.estimate_result_bytes）：落进审计供看板统计数据传输量。
    # 写语句无结果集，恒为 0。
    result_bytes: int = 0


@dataclass
class _PooledEngine:
    engine: SAEngine
    tunnel: SSHTunnel | None
    last_used: float

    def dispose(self) -> None:
        self.engine.dispose()
        if self.tunnel is not None:
            self.tunnel.close()


class EnginePool:
    """按 (project, connection, role, schema) 缓存引擎，并托管其 SSH 隧道生命周期。

    schema 是查询台的「执行 schema」上下文：MySQL 以该库为默认库建独立引擎、
    PG 设 search_path——比在共享连接上执行 USE 干净（不污染池内会话状态）。
    """

    def __init__(self, idle_reclaim_s: int = DEFAULT_IDLE_RECLAIM_S):
        self._entries: dict[tuple[str, str, Role, str], _PooledEngine] = {}
        self._lock = threading.Lock()
        self._idle_reclaim_s = idle_reclaim_s
        # SSH 证书库（名字→证书）的活引用，建隧道时解析每跳的 identity。
        # service 构造时指向 AppConfig.ssh_identities（同一 dict，原地增删即时可见）。
        self.identities: dict[str, SshIdentity] | None = None
        # 单引擎最大连接数（pool_size + max_overflow）。service 从设置同步，改后 dispose 重建。
        self.engine_pool_size: int = DEFAULT_ENGINE_POOL_SIZE

    def get(
        self,
        project: str,
        connection: str,
        cfg: ConnectionConfig,
        role: Role = "reader",
        schema: str | None = None,
        identities: dict[str, "SshIdentity"] | None = None,
        database: str | None = None,
    ) -> SAEngine:
        # database 只对 PostgreSQL 有意义：**一条 PG 连接只能绑一个 database**，
        # 跨库查询在协议层就不存在（`SELECT … FROM otherdb.public.t` 直接报
        # cross-database references are not implemented）。所以「换库」= 换一条连接，
        # 只能靠给每个库建独立引擎来实现，和当初为 schema 上下文做的是同一套机制。
        key = (project, connection, role, schema or "", database or "")
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry.tunnel is None or entry.tunnel.is_alive()):
                entry.last_used = time.monotonic()
                return entry.engine
            if entry is not None:
                # 隧道已死，连引擎一起回收后重建
                entry.dispose()
                del self._entries[key]
            entry = _build_pooled_engine(cfg, role, schema, identities or self.identities,
                                         pool_size=self.engine_pool_size, database=database)
            self._entries[key] = entry
            return entry.engine

    def reap_idle(self) -> int:
        """回收空闲超过阈值的引擎与隧道，返回回收数量。"""
        now = time.monotonic()
        reaped = 0
        with self._lock:
            for key in list(self._entries):
                entry = self._entries[key]
                if now - entry.last_used >= self._idle_reclaim_s:
                    entry.dispose()
                    del self._entries[key]
                    reaped += 1
        return reaped

    def dispose_connection(self, project: str, connection: str) -> None:
        """回收某连接的所有角色引擎与隧道（配置变更后强制重建）。"""
        with self._lock:
            for key in [k for k in self._entries if k[0] == project and k[1] == connection]:
                self._entries.pop(key).dispose()

    def stats(self) -> list[dict]:
        """池内每个引擎的实时状态（看板「连接数」用）。

        连接数分两层，看板要都给出来才有意义：**引擎数**是我们按
        (project, connection, role, schema, database) 缓存了几个 SQLAlchemy 引擎；
        **checked_out** 才是此刻真正占用的 DB 物理连接。SQLite 等用的池类型没有
        QueuePool 的计数方法，取不到就给 None（不硬编 0，免得看板把「没这个指标」
        显示成「有 0 条连接」）。
        """
        now = time.monotonic()
        out = []
        with self._lock:
            items = list(self._entries.items())
        for (project, connection, role, schema, database), entry in items:
            pool = entry.engine.pool
            def _num(name: str):  # noqa: ANN202
                fn = getattr(pool, name, None)
                if not callable(fn):
                    return None
                try:
                    return int(fn())
                except Exception:  # noqa: BLE001
                    return None
            out.append({
                "kind": "sql",
                "project": project,
                "connection": connection,
                "role": role,
                "schema": schema,
                "database": database,
                "pool_class": type(pool).__name__,
                "checked_out": _num("checkedout"),
                "checked_in": _num("checkedin"),
                "pool_size": _num("size"),
                "overflow": _num("overflow"),
                "idle_s": int(now - entry.last_used),
                "tunnel": None if entry.tunnel is None else entry.tunnel.is_alive(),
            })
        out.sort(key=lambda e: (e["project"], e["connection"], e["role"]))
        return out

    def dispose(self) -> None:
        with self._lock:
            for entry in self._entries.values():
                entry.dispose()
            self._entries.clear()


def build_probe_engine(
    cfg: ConnectionConfig, role: Role = "reader",
    identities: dict[str, "SshIdentity"] | None = None,
) -> _PooledEngine:
    """用给定配置临时建连（含隧道），不入池。调用方用完必须 .dispose()。

    用于"测试连接/权限探测"——针对表单里尚未保存的配置，不影响 EnginePool。
    """
    return _build_pooled_engine(cfg, role, identities=identities)


def _build_pooled_engine(
    cfg: ConnectionConfig, role: Role, schema: str | None = None,
    identities: dict[str, "SshIdentity"] | None = None,
    pool_size: int = DEFAULT_ENGINE_POOL_SIZE,
    database: str | None = None,
) -> _PooledEngine:
    tunnel: SSHTunnel | None = None
    host, port = cfg.host, cfg.port

    # SQLite 无网络，跳板不适用；默认端口问驱动要（加新引擎时这里不用改）
    if cfg.engine != "sqlite" and cfg.jump_hosts:
        default_port = get_driver(cfg.engine).default_port or 3306
        tunnel = open_tunnel(cfg.host, cfg.port or default_port,
                             cfg.jump_hosts, cfg.ssh_options, identities)
        host, port = "127.0.0.1", tunnel.local_port

    try:
        engine = _create_readonly_engine(cfg, role, host, port, schema, pool_size=pool_size,
                                         database=database)
    except Exception:
        if tunnel is not None:
            tunnel.close()
        raise
    return _PooledEngine(engine=engine, tunnel=tunnel, last_used=time.monotonic())


def _create_readonly_engine(
    cfg: ConnectionConfig,
    role: Role,
    host: str | None,
    port: int | None,
    schema: str | None = None,
    pool_size: int = DEFAULT_ENGINE_POOL_SIZE,
    database: str | None = None,
) -> SAEngine:
    """schema：查询台执行 schema 上下文（MySQL 覆盖默认库；PG 设 search_path）。

    database：**仅 PostgreSQL**——选定这条连接要绑的 database（PG 一条连接只能绑一个，
    换库只能换连接）。缺省沿用配置里的 `database`，没配则回退到 reader 账号名
    （见 `drivers.postgres.pg_database_name`）。

    引擎特有建连逻辑在对应驱动里（drivers/<engine>.py 的 build_engine）。
    """
    return get_driver(cfg.engine).build_engine(cfg, role, host, port, schema, pool_size, database)


def paginate_sql(sql: str, engine_kind: str, limit: int, offset: int) -> tuple[str, bool, bool]:
    """给缺 LIMIT 的顶层 SELECT/UNION 注入 LIMIT/OFFSET。

    返回 (新SQL, 是否已分页, 是否带 ORDER BY)——无 ORDER BY 的 LIMIT/OFFSET 翻页
    顺序不稳定（可能重复/漏行），调用方据此提示使用者。

    分页兜底——防止 `SELECT * FROM 大表` 把全表拉进客户端把 DB/进程跑挂。
    用户自带 LIMIT 则尊重不改；SHOW/DESCRIBE/EXPLAIN 等非 SELECT 不动；解析失败也不动。
    """
    import sqlglot  # noqa: PLC0415
    from sqlglot import exp  # noqa: PLC0415

    dialect = get_driver(engine_kind).dialect
    try:
        expr = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return sql, False, False
    if not isinstance(expr, (exp.Select, exp.Union)):
        return sql, False, False
    ordered = expr.args.get("order") is not None
    if expr.args.get("limit"):
        return sql, False, ordered
    expr = expr.limit(limit)
    if offset:
        expr = expr.offset(offset)
    return expr.sql(dialect=dialect), True, ordered


def make_canceller(engine: SAEngine, sa_conn) -> Callable[[], None]:  # noqa: ANN001
    """为一次正在执行的查询构造「取消函数」：在 DB 层中断它，而不是杀线程。

    具体手段由驱动提供（MySQL KILL QUERY / PG pg_cancel_backend / SQLite interrupt；
    ClickHouse 取不到稳定 query_id，取消为空操作，由服务端 max_execution_time 兜底）。
    取不到连接标识时返回空操作（cancel 变成对排队任务生效、对运行中无害）。
    """
    return driver_for_engine(engine).make_canceller(engine, sa_conn)


def run_query(
    engine: SAEngine, sql: str, max_rows: int, max_cell_chars: int = 4096,
    on_start: Callable[[Callable[[], None]], None] | None = None,
) -> QueryResult:
    """执行只读 SQL（调用方必须先通过 classify 判定），行数截到 max_rows，单元格截到 max_cell_chars。

    注：不用流式游标（pymysql SSCursor 提前关闭 + 连接池复用会「commands out of sync」）；
    改由调用方注入 LIMIT 兜底（paginate_sql）限制 DB 返回行数，缓冲游标即可安全。

    on_start：拿到连接后回调一次，传入该查询的取消函数（供上层串行队列的 cancel 使用）。
    """
    start = dt.datetime.now()
    with engine.connect() as conn:
        if on_start is not None:
            on_start(make_canceller(engine, conn))
        result = conn.execute(text(sql))
        if result.returns_rows:
            columns = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            page = fetched[:max_rows]
            # 列类型分类须在 _jsonable 前用原始 Python 值算（大整数一旦转字符串就丢了类型）
            column_types = _col_categories(len(columns), page)
            rows = [
                [truncate_cell(_jsonable(v), max_cell_chars) for v in row]
                for row in page
            ]
        else:
            columns, rows, truncated, column_types = [], [], False, []
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns, rows, len(rows), truncated, duration_ms, column_types,
                       estimate_result_bytes(columns, rows))


# 流式读取的每批大小：太小则 Python↔DB 往返太多、进度刷新太碎；太大则单批内存驻留久。
# 2000 行/批在常见宽度下约几百 KB，既能及时把行交给导出写盘、也能每 2000 行
# 向前端报一次「已导出 N 行」（用户看到的进度条基本是连续流动的）。
_STREAM_BATCH = 2000


def stream_rows(
    engine: SAEngine, sql: str, max_rows: int, max_cell_chars: int = 4096,
    on_start: Callable[[Callable[[], None]], None] | None = None,
    on_meta: Callable[[list[str], list[str]], None] | None = None,
):
    """流式执行只读 SQL：逐批 yield 已 _jsonable + 截断的行（list），最多 yield max_rows 行。

    与 run_query 的差别是**内存**：run_query 把整页 fetchmany 进一个列表再返回，
    对「导出完整结果集」（可能上百万行）会把进程内存撑爆；本函数每批取 _STREAM_BATCH 行、
    转换后立刻 yield，调用方（导出）边收边写盘，任意时刻内存只占一批。缓冲游标 +
    分批 fetchmany 依然保持连接池复用干净（不用 SSCursor，理由见 run_query 注释）。

    on_start：拿到连接后回调一次，传入取消函数（导出任务经它注册 KILL QUERY）。
    on_meta(columns, column_types)：execute 之后、首批行转换之前回调一次——导出需要
      列名写表头，且列类型分类必须用**原始** Python 值算（_jsonable 后大整数已成字符串）。

    取消：取消器在别的连接上发 KILL，本连接的 fetchmany 会抛错，生成器随 with 一起
    收尾，异常原样冒泡给调用方（JobManager 据此判 canceled）。
    """
    with engine.connect() as conn:
        if on_start is not None:
            on_start(make_canceller(engine, conn))
        result = conn.execute(text(sql))
        if not result.returns_rows:
            return
        columns = list(result.keys())
        fetched = result.fetchmany(_STREAM_BATCH)
        column_types = _col_categories(len(columns), fetched)
        if on_meta is not None:
            on_meta(columns, column_types)
        taken = 0
        while fetched:
            if taken >= max_rows:
                return
            batch = fetched if taken + len(fetched) <= max_rows else fetched[: max_rows - taken]
            for row in batch:
                yield [truncate_cell(_jsonable(v), max_cell_chars) for v in row]
            taken += len(batch)
            fetched = result.fetchmany(_STREAM_BATCH)


def truncate_cell(value: Any, max_chars: int) -> Any:
    """超长字符串单元格截断并标注原始长度（含 bytes 的 base64 包装形式）。"""
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"…[已截断，原 {len(value)} 字符]"
    if isinstance(value, dict) and "__bytes_base64__" in value:
        b64 = value["__bytes_base64__"]
        if isinstance(b64, str) and len(b64) > max_chars:
            return {"__bytes_base64__": b64[:max_chars] + f"…[已截断，原 {len(b64)} 字符]"}
    return value


def estimate_row_count(engine: SAEngine, engine_kind: str, table: str,
                       schema: str | None = None) -> int | None:
    """表行数量级估算。优先用引擎统计（避免大表全表 count），取不到再退回精确 count。

    具体来源由驱动给出（MySQL information_schema / PG pg_class.reltuples / SQLite count(*)）。
    调用方必须已校验 table 存在。返回 None 表示无法估算。
    schema：MySQL/ClickHouse 为库名，PG 为 schema；不传用连接当前的库 / schema。
    """
    return get_driver(engine_kind).estimate_row_count(engine, table, schema)


def collect_table_meta(engine: SAEngine, engine_kind: str, table: str,
                       schema: str | None = None) -> dict:
    """采集单表的结构 + 索引 + 行数估算，供元数据缓存与风险评估使用。"""
    info = describe_table(engine, table, schema)
    info["row_estimate"] = estimate_row_count(engine, engine_kind, table, schema)
    return info


def run_write(
    engine: SAEngine, sql: str,
    on_start: Callable[[Callable[[], None]], None] | None = None,
) -> QueryResult:
    """执行写 SQL（调用方必须确保已通过审批），返回受影响行数。事务自动提交。

    on_start：拿到连接后回调一次，传入取消函数（KILL QUERY），供串行队列 cancel 中断大写操作。

    多语句批量：按分号拆成单条、在同一事务里逐条执行（pymysql/psycopg 单条 execute
    不支持多语句），受影响行数为各 DML 语句 rowcount 之和。注意 MySQL 的 DDL 会隐式
    提交，ALTER+DML 混合批次无法整体回滚（MySQL 本身限制）。
    """
    from .workflows import split_statements  # 懒加载避免与 workflows 循环导入

    stmts = split_statements(sql) or [sql]
    start = dt.datetime.now()
    affected = 0
    with engine.begin() as conn:
        if on_start is not None:
            on_start(make_canceller(engine, conn))
        for stmt in stmts:
            result = conn.execute(text(stmt))
            rc = result.rowcount if result.rowcount is not None else -1
            if rc > 0:
                affected += rc
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns=[], rows=[], row_count=affected, truncated=False, duration_ms=duration_ms)


def search_tables(engine: SAEngine, engine_kind: str, q: str, limit: int = 50) -> list[dict]:
    """跨库按名模糊搜表（查询台 ⌘P 跳转）。返回 [{db, table}]，全程参数化。"""
    return get_driver(engine_kind).search_tables(engine, q, limit)


def fetch_rows_for_copy(
    engine: SAEngine, sql: str, max_rows: int, max_bytes: int | None = None
) -> tuple[list[str], list[list], bool]:
    """取数用于**跨库复制**：返回原生 Python 值，不做 JSON 化、不截断、不脱敏。

    与 run_query 的区别正在于此——run_query 是给人和 agent「看」的，会把 Decimal 转字符串、
    bytes 包成 base64 dict、超长单元格截断；这些对展示无害，但作为写入目标库的值就是**数据
    损坏**。复制路径必须拿驱动返回的原值，再原样绑参写回目标表（datetime/Decimal/bytes
    都由目标驱动自己适配）。

    返回 (列名, 行, 是否被截断)。行数上限由调用方注入进 SQL 的 LIMIT 保证，这里多取一行
    只为判断「源侧其实还有更多」。

    max_bytes 是**体积**上限：行数管不住行很宽的表（1 万行 BLOB 能有几个 GB）。累计到预算
    就停止收行并标 truncated。注意它保护的是本进程内存与目标库的写入量——**源库那边**
    已经按 LIMIT 把这些行发过来了（缓冲游标），要减轻源库压力只能调小 limit / 收窄 where。
    """
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        if not result.returns_rows:
            return [], [], False
        columns = list(result.keys())
        fetched = result.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = [list(r) for r in fetched[:max_rows]]
    if max_bytes and rows:
        total, kept = 0, 0
        for row in rows:
            total += sum(estimate_cell_bytes(v) for v in row)
            if total > max_bytes:
                break
            kept += 1
        if kept < len(rows):
            # 至少留一行：一行就超预算说明该表本身就宽，返回空会让调用方以为源表是空的
            rows = rows[: max(kept, 1)]
            truncated = True
    return columns, rows, truncated


def insert_rows(engine: SAEngine, table: str, columns: list[str],
                rows: list[list], schema: str | None = None,
                delete_first: bool = False) -> QueryResult:
    """参数化批量 INSERT（单事务，全部成功或整体回滚）。

    表名/列名由调用方经表结构校验后传入；此处用 SQLAlchemy 构造以正确按方言加引号，
    值全部走绑定参数——导入数据永不拼接 SQL。

    delete_first：先清空目标表再写（表同步的 data=replace）。用 DELETE 而不是 TRUNCATE，
    因为 TRUNCATE 在 MySQL 是 DDL、会隐式提交，那样「清空成功但写入失败」就回滚不了。
    """
    import sqlalchemy as sa

    start = dt.datetime.now()
    t = sa.table(table, *[sa.column(c) for c in columns], schema=schema or None)
    params = [dict(zip(columns, r, strict=False)) for r in rows]
    with engine.begin() as conn:
        if delete_first:
            conn.execute(sa.delete(t))
        if params:
            conn.execute(sa.insert(t), params)
    duration_ms = int((dt.datetime.now() - start).total_seconds() * 1000)
    return QueryResult(columns=[], rows=[], row_count=len(rows), truncated=False,
                       duration_ms=duration_ms)


PLAN_MAX_ROWS = 50           # 计划行数上限：审批人只看概览，超长计划不必全存
PLAN_MAX_CELL_CHARS = 500    # 单格字符上限（MySQL 的 Extra、PG 的计划行可能很长）

# MySQL 9 起 explain_format 默认 TREE，而 TREE 解释不了 DML（只回一句
# "not executable by iterator executor"）；显式要传统表格式才有 type/key/rows 可看。
def explainable(sql: str, engine_kind: str) -> bool:
    """这条语句值不值得去取执行计划：只有单条 DML / 查询才有计划可看。

    DDL、COMMENT、GRANT 之类 EXPLAIN 必然报错，去试只会白白往库上多发请求——
    取计划要先试 reader 再试 writer，远程库上 writer 连接常已被空闲回收，
    为一次注定失败的 EXPLAIN 重建连接（可能还要过 SSH 隧道）会明显拖慢写确认。
    """
    import sqlglot  # noqa: PLC0415
    from sqlglot import exp  # noqa: PLC0415

    dialect = get_driver(engine_kind).dialect
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except Exception:
        return False
    return len(trees) == 1 and isinstance(
        trees[0], (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Select, exp.Union))


def explain(engine: SAEngine, sql: str, engine_kind: str) -> dict | None:
    """取执行计划，返回 {"columns": [...], "rows": [[...]]}；失败返回 None（不阻断主流程）。

    带列名回传是为了让审批人看得懂每列是什么——MySQL EXPLAIN 有十余列，
    只给一行值等于没给。计划格式由驱动决定（MySQL 要 TRADITIONAL 才有 type/key/rows）。
    """
    prefix = get_driver(engine_kind).explain_prefix  # 不带 ANALYZE，避免真实执行写语句
    try:
        with engine.connect() as conn:
            result = conn.execute(text(prefix + sql))
            columns = [str(c) for c in result.keys()]
            rows = result.fetchmany(PLAN_MAX_ROWS)
    except Exception:
        return None
    if not rows:
        return None
    return {"columns": columns, "rows": [[_plan_cell(v) for v in row] for row in rows]}


def _plan_cell(value: object) -> str | None:
    if value is None:
        return None  # 保留 NULL 语义（MySQL 的 key/ref 常为 NULL），由展示层渲染
    s = str(value)
    return s if len(s) <= PLAN_MAX_CELL_CHARS else s[:PLAN_MAX_CELL_CHARS] + "…"


# 列库/schema 时过滤掉系统库，减少噪音
_SYSTEM_SCHEMAS = {
    "information_schema", "performance_schema", "mysql", "sys", "pg_catalog", "pg_toast",
    "system",  # ClickHouse 系统库（information_schema/INFORMATION_SCHEMA 由 .lower() 归一后命中上面）
}


def list_databases(engine: SAEngine) -> list[str]:
    """列出可选的库 / schema（MySQL 是数据库，PG 是 schema），过滤系统库。

    用于未绑定默认库的连接：先选库，再在该库下列表（避免 no-database 反射崩溃）。
    """
    names = inspect(engine).get_schema_names()
    return sorted(n for n in names if n and n.lower() not in _SYSTEM_SCHEMAS)


def list_server_databases(engine: SAEngine, engine_kind: str) -> list[str]:
    """列出**服务器上的 database**（不是 schema）。

    PG 的 `inspect().get_schema_names()` 返回的是 schema，看不到同一台服务器上的其它
    database——因为一条 PG 连接只绑一个库。要浏览别的库必须查 `pg_database` 拿到清单，
    再对选中的库另建一条连接（`EnginePool.get(database=…)`）。
    MySQL / ClickHouse 的「schema」本身就是 database，直接复用 list_databases。
    """
    return get_driver(engine_kind).list_server_databases(engine)


def list_tables(engine: SAEngine, schema: str | None = None) -> list[str]:
    return sorted(inspect(engine).get_table_names(schema=schema))


def describe_table(engine: SAEngine, table: str, schema: str | None = None) -> dict:
    insp = inspect(engine)
    _ensure_table_exists(insp, table, schema)
    columns = [
        {
            "name": c["name"],
            "type": str(c["type"]),
            "nullable": c.get("nullable", True),
            "default": _jsonable(c.get("default")),
            "comment": c.get("comment"),
        }
        for c in insp.get_columns(table, schema=schema)
    ]
    indexes = [
        {"name": i.get("name"), "columns": i.get("column_names"), "unique": i.get("unique")}
        for i in insp.get_indexes(table, schema=schema)
    ]
    pk = insp.get_pk_constraint(table, schema=schema)
    return {"table": table, "schema": schema, "columns": columns, "indexes": indexes,
            "primary_key": pk.get("constrained_columns", [])}


def table_sizes(engine: SAEngine, engine_kind: str, schema: str | None = None) -> dict[str, int]:
    """按表返回存储容量（字节，数据+索引），供树右侧分级展示。

    一次查询拿整个库（不逐表），取不到（引擎不支持/权限不足）返回空 dict 不阻断。
    """
    return get_driver(engine_kind).table_sizes(engine, schema)


def get_table_ddl(engine: SAEngine, engine_kind: str, table: str, schema: str | None = None) -> str:
    """取建表语句。取不到服务器原文的引擎（PG）由驱动反射拼近似 DDL。

    表名先经存在性校验，再用方言引用符包裹，杜绝注入。
    """
    return get_driver(engine_kind).get_table_ddl(engine, table, schema)


def sample_rows(
    engine: SAEngine, table: str, limit: int, max_cell_chars: int = 4096,
    schema: str | None = None,
) -> QueryResult:
    insp = inspect(engine)
    _ensure_table_exists(insp, table, schema)
    # 表名经存在性校验后再用方言引用符包裹，杜绝注入
    preparer = engine.dialect.identifier_preparer
    qname = (preparer.quote(schema) + "." if schema else "") + preparer.quote(table)
    return run_query(engine, f"SELECT * FROM {qname}", max_rows=limit, max_cell_chars=max_cell_chars)


def _ensure_table_exists(insp, table: str, schema: str | None = None) -> None:  # noqa: ANN001
    names = insp.get_table_names(schema=schema)
    if table not in names:
        raise ValueError(f"表 {table!r} 不存在，可用表: {', '.join(sorted(names)) or '（无）'}")


# JS Number.MAX_SAFE_INTEGER = 2^53-1；超过它的整数（雪花 ID/int64）在前端 JSON.parse
# 时会被 double 近似而丢精度（末位改变），必须以字符串下发；列类型另由 column_types 标注。
_JS_SAFE_INT = 9007199254740991


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, float, str)):
        return value
    if isinstance(value, int):
        # 超 JS 安全整数范围 → 转字符串保精度（不影响小整数，仍是数字）
        return str(value) if abs(value) > _JS_SAFE_INT else value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_base64__": base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)


def _value_category(v: Any) -> str:
    """由原始 Python 值推断列类型分类，与前端 COL_GLYPH 词表对齐。

    只对**类型明确**的 Python 值给出分类；字符串返回 ""（未知），让前端按内容推断
    （保留查询台对日期串/JSON 串的既有识别，不回退）。大整数虽以字符串下发，但这里
    看到的是原始 int → 归类 number，前端图标据此显示 #。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float, decimal.Decimal)):
        return "number"
    if isinstance(v, dt.datetime):
        return "datetime"
    if isinstance(v, dt.date):
        return "date"
    if isinstance(v, dt.time):
        return "time"
    if isinstance(v, (bytes, bytearray)):
        return "binary"
    if isinstance(v, (dict, list)):
        return "json"
    return ""  # 字符串等：交给前端按内容推断


def _col_categories(ncols: int, rows: list) -> list[str]:
    """每列取首个非空原始值推断类型分类；全空列为 ""。"""
    cats = [""] * ncols
    for j in range(ncols):
        for row in rows:
            if row[j] is not None:
                cats[j] = _value_category(row[j])
                break
    return cats


# ---------- SQL 语法的 DB 侧权威复核 ----------

# 背景：sqlglot 解析失败 **不等于** SQL 真有语法错——它的方言覆盖并不完整。
# 已踩过的坑：MySQL 合法的 `ALTER TABLE t DROP PARTITION p1, p2`（无括号）sqlglot 解析不了
# （见 CLAUDE.md「sqlglot 与 MySQL 对 DROP PARTITION 语法要求恰好相反」）。
# 所以 agent 侧的语法预检采用两级：sqlglot 初筛 → 失败时让目标 DB **只解析不执行**地复核，
# DB 也说语法错才拒绝。零假阳性，代价是一次轻量往返（仅在初筛失败时发生）。
#
# 各引擎的「只解析不执行」手段：
#   MySQL       PREPARE ... FROM @var（覆盖 DML 与 DDL，最完整）
#   PostgreSQL  PREPARE ... AS <sql>（**只支持 DML**，DDL 会被误报语法错 → 不复核）
#   SQLite      EXPLAIN <sql>（编译成 VDBE 程序但不执行，覆盖所有语句）
#   ClickHouse  EXPLAIN SYNTAX <sql>（只支持 SELECT）
# 无法复核的组合返回 supported=False，由调用方退回「默认拒绝」的保守路径。

def _syntax_check_supported(engine_kind: str, stmt: str) -> bool:
    try:
        return get_driver(engine_kind).syntax_check_supported(stmt)
    except UnsupportedEngineError:
        # 未注册驱动的引擎：没法复核就如实说「不支持」，调用方退回默认拒绝，不抛异常
        return False


def _check_one_statement(conn, stmt: str, engine_kind: str) -> SyntaxCheck:  # noqa: ANN001
    """在已有连接上复核单条语句。**只解析不执行**，不提交任何事务。"""
    try:
        return get_driver(engine_kind).check_statement(conn, stmt)
    except UnsupportedEngineError:
        return SyntaxCheck(supported=False, ok=True)


def _ensure_table_exists(insp, table: str, schema: str | None = None) -> None:  # noqa: ANN001
    names = insp.get_table_names(schema=schema)
    if table not in names:
        raise ValueError(f"表 {table!r} 不存在，可用表: {', '.join(sorted(names)) or '（无）'}")


# JS Number.MAX_SAFE_INTEGER = 2^53-1；超过它的整数（雪花 ID/int64）在前端 JSON.parse
# 时会被 double 近似而丢精度（末位改变），必须以字符串下发；列类型另由 column_types 标注。
_JS_SAFE_INT = 9007199254740991


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, float, str)):
        return value
    if isinstance(value, int):
        # 超 JS 安全整数范围 → 转字符串保精度（不影响小整数，仍是数字）
        return str(value) if abs(value) > _JS_SAFE_INT else value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_base64__": base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)


def _value_category(v: Any) -> str:
    """由原始 Python 值推断列类型分类，与前端 COL_GLYPH 词表对齐。

    只对**类型明确**的 Python 值给出分类；字符串返回 ""（未知），让前端按内容推断
    （保留查询台对日期串/JSON 串的既有识别，不回退）。大整数虽以字符串下发，但这里
    看到的是原始 int → 归类 number，前端图标据此显示 #。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float, decimal.Decimal)):
        return "number"
    if isinstance(v, dt.datetime):
        return "datetime"
    if isinstance(v, dt.date):
        return "date"
    if isinstance(v, dt.time):
        return "time"
    if isinstance(v, (bytes, bytearray)):
        return "binary"
    if isinstance(v, (dict, list)):
        return "json"
    return ""  # 字符串等：交给前端按内容推断


def _col_categories(ncols: int, rows: list) -> list[str]:
    """每列取首个非空原始值推断类型分类；全空列为 ""。"""
    cats = [""] * ncols
    for j in range(ncols):
        for row in rows:
            if row[j] is not None:
                cats[j] = _value_category(row[j])
                break
    return cats


# ---------- SQL 语法的 DB 侧权威复核 ----------

# 背景：sqlglot 解析失败 **不等于** SQL 真有语法错——它的方言覆盖并不完整。
# 已踩过的坑：MySQL 合法的 `ALTER TABLE t DROP PARTITION p1, p2`（无括号）sqlglot 解析不了
# （见 CLAUDE.md「sqlglot 与 MySQL 对 DROP PARTITION 语法要求恰好相反」）。
# 所以 agent 侧的语法预检采用两级：sqlglot 初筛 → 失败时让目标 DB **只解析不执行**地复核，
# DB 也说语法错才拒绝。零假阳性，代价是一次轻量往返（仅在初筛失败时发生）。
#
# 各引擎的「只解析不执行」手段：
#   MySQL       PREPARE ... FROM @var（覆盖 DML 与 DDL，最完整）
#   PostgreSQL  PREPARE ... AS <sql>（**只支持 DML**，DDL 会被误报语法错 → 不复核）
#   SQLite      EXPLAIN <sql>（编译成 VDBE 程序但不执行，覆盖所有语句）
#   ClickHouse  EXPLAIN SYNTAX <sql>（只支持 SELECT）
# 无法复核的组合返回 supported=False，由调用方退回「默认拒绝」的保守路径。

def dry_run_syntax_check(engine: SAEngine, sql: str, engine_kind: str) -> SyntaxCheck:
    """让目标 DB 只解析不执行地复核 SQL 语法。多语句逐条复核，报第一条出错的。

    绝不执行语句、绝不提交事务。任何非语法类错误（权限/表不存在）都按「语法没问题」处理，
    交给正常执行路径去报真实原因。
    """
    from .workflows import split_statements  # 懒加载避免循环导入

    stmts = [s for s in split_statements(sql) if s.strip()] or [sql]
    if not all(_syntax_check_supported(engine_kind, s) for s in stmts):
        return SyntaxCheck(supported=False, ok=True)
    with engine.connect() as conn:
        for i, stmt in enumerate(stmts, start=1):
            res = _check_one_statement(conn, stmt, engine_kind)
            if not res.ok:
                return SyntaxCheck(supported=True, ok=False, error=res.error, stmt_index=i)
            if not res.supported:
                return SyntaxCheck(supported=False, ok=True)
    return SyntaxCheck(supported=True, ok=True)
