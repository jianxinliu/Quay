"""跨连接的表同步：计划构造、DDL 转写、SQL 生成（纯函数，便于单测）。

典型场景是「把线上库的一张表按条件取一小撮同步到本地」。设计要点：

- **agent 只描述意图**（哪张表、什么条件、最多多少行、同步到哪），SELECT/DDL 全部由这里
  按方言生成并加引号——页面/agent 传不进任意 SQL 片段拼接（WHERE/ORDER BY 除外，它们会
  被并入生成的 SELECT 后由 classify 复核只读性）。
- **建表**：同引擎直接用源库 `SHOW CREATE TABLE` 原文（最保真）；跨引擎用 sqlglot 转写到
  目标方言，并剥掉方言私有成分（ENGINE/CHARSET/COLLATE/AUTO_INCREMENT/二级索引），
  结果标注为「近似 DDL」并把剥掉的东西列进 warnings 交人过目。
- **进审批的是"计划"而不是某一批数据**：审批单里存整份 SyncSpec + 建表语句，人批准后
  重新按计划取数执行，与「执行的永远是审批单里存储的内容」这条红线一致。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

import sqlglot
from sqlglot import exp

# 引擎清单与方言全部来自驱动注册表（见 drivers/），新增引擎不用改这里
from .drivers import connectable_engines, engine_dialect, get_driver

# 结构同步模式
DDL_SKIP = "skip"                      # 不建表：目标表必须已存在
DDL_CREATE_IF_MISSING = "create_if_missing"  # 目标表不存在才建
DDL_RECREATE = "recreate"              # 先 DROP 再按源结构重建（破坏性）
DDL_MODES = (DDL_SKIP, DDL_CREATE_IF_MISSING, DDL_RECREATE)

# 数据同步模式
DATA_NONE = "none"        # 只同步结构
DATA_APPEND = "append"    # 直接追加写入
DATA_REPLACE = "replace"  # 先清空目标表再写入（破坏性）
DATA_MODES = (DATA_NONE, DATA_APPEND, DATA_REPLACE)

DEFAULT_SYNC_ROWS = 1000      # 不传 limit 时的默认行数
MAX_SYNC_ROWS = 200_000       # 绝对上限；实际上限还受系统设置 sync_max_rows 约束

# 可作为同步源/目标的引擎，由驱动注册表给出（运行时计算，晚注册的驱动也生效）：
# - 源：所有可连接的引擎（redis 是键值模型、不在注册表里，天然被排除）
# - 目标：能执行 CREATE TABLE + INSERT 的引擎（驱动的 sync_target 能力，ClickHouse
#   本项目只读、不配 writer，故只能做源）
def source_engines() -> tuple[str, ...]:
    return tuple(connectable_engines())


def target_engines() -> tuple[str, ...]:
    return tuple(e for e in source_engines() if get_driver(e).sync_target)


# 模块级常量仅用于错误提示文案（启动时定型）；判定一律走上面的函数
SOURCE_ENGINES = source_engines()
TARGET_ENGINES = target_engines()

# sqlglot 方言来自驱动；新增引擎不用来这里登记
def _dialect(engine: str) -> str | None:
    return engine_dialect(engine)

# 标识符白名单：库/表名只允许字母/数字/下划线/$（\w 含中文等 Unicode 字母），
# 杜绝把引号、分号、点号带进生成的 SQL。
_IDENT_RE = re.compile(r"^[\w$]+$")

# 跨引擎建表时要剥掉的列约束（目标方言多半不认，留着必然建表失败）
_DROP_CONSTRAINTS = (
    exp.CollateColumnConstraint,        # MySQL 的 utf8mb4_* collation 在 PG/SQLite 不存在
    exp.CharacterSetColumnConstraint,
    exp.AutoIncrementColumnConstraint,  # 数据带着显式主键值复制，不需要目标侧自增
    exp.OnUpdateColumnConstraint,       # MySQL 专有
    exp.CommentColumnConstraint,        # PG 的列注释是独立的 COMMENT ON 语句
)


class SyncError(ValueError):
    """The sync plan is invalid (bad mode/engine/table name, etc.). A fixable input error for the agent."""


@dataclass(frozen=True)
class SyncSpec:
    """一次表同步的完整意图。审批单里存的就是它（JSON），核销时按它重新取数执行。"""

    source_project: str
    source_connection: str
    source_table: str
    target_project: str
    target_connection: str
    target_table: str
    ddl: str = DDL_CREATE_IF_MISSING
    data: str = DATA_APPEND
    where: str = ""
    order_by: str = ""
    limit: int = DEFAULT_SYNC_ROWS
    source_database: str | None = None
    target_database: str | None = None
    # 仅 PG：源/目标连接上要连的 database（上面两个对 PG 指的是 schema）
    source_pg_database: str | None = None
    target_pg_database: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # 未指定时不出现在字典里：计划指纹由它算出，升级前已批准的审批单（字典里没有这两个键）
        # 升级后重提仍要算出同一个指纹
        for k in ("source_pg_database", "target_pg_database"):
            if not d[k]:
                del d[k]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SyncSpec":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


def validate_spec(spec: SyncSpec) -> None:
    """Validate mode values, identifier legality, and self-sync. Pure input validation, no DB access."""
    if spec.ddl not in DDL_MODES:
        raise SyncError(f"ddl must be one of {'/'.join(DDL_MODES)}, got {spec.ddl!r}")
    if spec.data not in DATA_MODES:
        raise SyncError(f"data must be one of {'/'.join(DATA_MODES)}, got {spec.data!r}")
    if spec.ddl == DDL_SKIP and spec.data == DATA_NONE:
        raise SyncError("ddl=skip and data=none: this sync would do nothing")
    for label, name in (("source table", spec.source_table), ("target table", spec.target_table)):
        if not _IDENT_RE.match(name or ""):
            raise SyncError(
                f"{label} name {name!r} is not a valid identifier (only letters, digits, underscore, $ allowed)")
    for label, name in (("source database", spec.source_database),
                        ("target database", spec.target_database),
                        ("source PG database", spec.source_pg_database),
                        ("target PG database", spec.target_pg_database)):
        if name and not _IDENT_RE.match(name):
            raise SyncError(f"{label} name {name!r} is not a valid identifier")
    if not 1 <= spec.limit <= MAX_SYNC_ROWS:
        raise SyncError(f"limit must be between 1..{MAX_SYNC_ROWS}, got {spec.limit}")
    same_conn = (spec.source_project, spec.source_connection) == (
        spec.target_project, spec.target_connection)
    same_place = ((spec.source_database or "") == (spec.target_database or "")
                  and (spec.source_pg_database or "") == (spec.target_pg_database or ""))
    if same_conn and same_place and spec.source_table == spec.target_table:
        raise SyncError("Source and target are the same table; there is nothing to sync")


def spec_fingerprint(spec: SyncSpec) -> str:
    """计划指纹：核销审批单时校验「重提的计划」与「已批准的计划」逐字段一致。

    对应 SQL 审批流里的 SQL 指纹——改了任一条件（表/模式/WHERE/行数）都要重新审批。
    """
    payload = json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def quote_ident(engine_kind: str, name: str) -> str:
    """按方言给标识符加引号。名字已经过 _IDENT_RE 校验，这里只负责引号风格。"""
    if engine_kind in ("mysql", "clickhouse"):
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def build_select_sql(
    engine_kind: str, table: str, columns: list[str], limit: int,
    where: str = "", order_by: str = "", database: str | None = None,
) -> str:
    """生成取数 SELECT。表名/列名由本函数加引号，WHERE/ORDER BY 原样并入。

    WHERE/ORDER BY 是 agent 给的自由片段——调用方**必须**再用 classify 复核整条 SQL
    确实只读（同一个 agent 本就能用 query 在源库跑任意 SELECT，这里不放大权限，
    但要挡住 `1=1; DROP TABLE` 这类把 SELECT 变成多语句的写法）。
    """
    qname = quote_ident(engine_kind, table)
    if database:
        qname = f"{quote_ident(engine_kind, database)}.{qname}"
    cols = ", ".join(quote_ident(engine_kind, c) for c in columns) if columns else "*"
    sql = f"SELECT {cols} FROM {qname}"
    if where.strip():
        sql += f" WHERE {where.strip()}"
    if order_by.strip():
        sql += f" ORDER BY {order_by.strip()}"
    return f"{sql} LIMIT {int(limit)}"


def build_drop_sql(engine_kind: str, table: str) -> str:
    return f"DROP TABLE IF EXISTS {quote_ident(engine_kind, table)}"


def _table_node(create: exp.Create) -> exp.Table:
    node = create.this
    node = node.this if isinstance(node, exp.Schema) else node
    if not isinstance(node, exp.Table):
        raise SyncError("Could not find the table-name node in the source CREATE TABLE statement; cannot rewrite it")
    return node


def _signed_type(dtype: exp.DataType) -> None:
    """把 MySQL 的无符号类型（sqlglot 解析成 UBIGINT/UINT/…）降级成有符号同类。

    PG/SQLite 没有无符号整型，原样输出会得到 `UBIGINT` 这种目标库不认识的类型名。
    """
    name = dtype.this.name if dtype.this is not None else ""
    if not name.startswith("U"):
        return
    signed = name[1:]
    if signed in exp.DataType.Type.__members__:
        dtype.set("this", exp.DataType.Type[signed])


def _clean_column(col: exp.ColumnDef, warnings: list[str]) -> None:
    kept = []
    for cons in col.constraints:
        if isinstance(cons.kind, _DROP_CONSTRAINTS):
            if isinstance(cons.kind, exp.AutoIncrementColumnConstraint):
                warnings.append(f"Cross-engine table creation dropped the AUTO_INCREMENT "
                                f"attribute on column {col.name} (data is copied with its original values)")
            continue
        kept.append(cons)
    col.set("constraints", kept)
    kind = col.args.get("kind")
    if isinstance(kind, exp.DataType):
        _signed_type(kind)


def _strip_dialect_specifics(create: exp.Create, warnings: list[str]) -> None:
    """剥掉跨引擎必然失败的成分：表属性、二级索引、方言私有列约束。"""
    if create.args.get("properties") is not None:
        create.set("properties", None)  # ENGINE / CHARSET / COLLATE / COMMENT / AUTO_INCREMENT=
    schema = create.this
    if not isinstance(schema, exp.Schema):
        return
    kept = []
    for e in schema.expressions:
        if isinstance(e, exp.IndexColumnConstraint):
            name = e.this.name if e.this is not None else "?"
            warnings.append(f"Cross-engine table creation did not sync secondary index "
                            f"{name} (create it manually on the target if needed)")
            continue
        if isinstance(e, exp.UniqueColumnConstraint) and isinstance(e.this, exp.Schema):
            # MySQL 的 `UNIQUE KEY 名字 (列)` 会渲染成 `UNIQUE "名字" (列)`——PG/SQLite 都不认
            # 这种写法（要写 `CONSTRAINT 名字 UNIQUE (...)`）。去掉名字保留唯一约束语义即可。
            e.this.set("this", None)
        if isinstance(e, exp.ColumnDef):
            _clean_column(e, warnings)
        kept.append(e)
    schema.set("expressions", kept)


def rewrite_ddl(
    source_ddl: str, source_engine: str, target_engine: str,
    source_table: str, target_table: str,
) -> tuple[str, list[str]]:
    """把源库建表语句改写成可在目标库执行的建表语句。

    返回 (DDL, 警告列表)。同引擎同表名直接用原文——`SHOW CREATE TABLE` 的原文比任何
    重新渲染都保真（字符集、行格式、生成列、注释一个不少）；只有改表名或跨引擎时才过
    sqlglot。跨引擎的产物是**近似 DDL**，warnings 会列出被剥掉的东西。
    """
    source_ddl = (source_ddl or "").strip().rstrip(";")
    if not source_ddl:
        raise SyncError("Source table CREATE TABLE statement is empty; cannot sync structure")
    same_engine = source_engine == target_engine
    if same_engine and source_table == target_table:
        return source_ddl, []

    warnings: list[str] = []
    read_d = _dialect(source_engine)
    write_d = _dialect(target_engine)
    try:
        # Use parse rather than parse_one: SQLite's raw CREATE TABLE text can be followed
        # by that table's CREATE INDEX statements (get_table_ddl concatenates sqlite_master
        # rows), and parse_one would error out on that.
        statements = [st for st in sqlglot.parse(source_ddl, read=read_d) if st is not None]
    except sqlglot.errors.SqlglotError as e:
        raise SyncError(f"Could not parse the source table's CREATE TABLE statement "
                        f"({type(e).__name__}: {e}); use ddl=skip and create the table "
                        f"manually on the target instead") from e
    creates = [st for st in statements if isinstance(st, exp.Create) and st.kind == "TABLE"]
    if not creates:
        raise SyncError("No CREATE TABLE found in the source table's DDL; refusing to rewrite it")
    tree = creates[0]
    if len(statements) > 1:
        warnings.append("Rewriting the table name discarded other statements bundled with "
                        "the source DDL (such as a standalone CREATE INDEX)")

    table = _table_node(tree)
    table.set("this", exp.to_identifier(target_table, quoted=True))
    table.set("db", None)       # the database is bound to the target connection/engine, not qualified in the DDL
    table.set("catalog", None)
    if not same_engine:
        _strip_dialect_specifics(tree, warnings)
        warnings.insert(0, f"Cross-engine table creation ({source_engine} -> {target_engine}): "
                           f"the following is an approximate DDL rewritten by sqlglot — "
                           f"confirm the type mapping before approving")
    return tree.sql(dialect=write_d, pretty=True), warnings


def render_plan(
    spec: SyncSpec, source_env: str, source_engine: str, target_env: str, target_engine: str,
    columns: list[str], ddl_sql: str, warnings: list[str],
    target_exists: bool, source_row_estimate: int | None,
) -> str:
    """Render the plan as a text block shown on the approval page/to the session (this is what the approval record's sql field stores)."""
    src_db = "".join(f".{x}" for x in (spec.source_pg_database, spec.source_database) if x)
    tgt_db = "".join(f".{x}" for x in (spec.target_pg_database, spec.target_database) if x)
    est = "unknown" if source_row_estimate is None else f"~{source_row_estimate:,} rows"
    lines = [
        "-- Table sync plan (data is re-fetched and executed against this plan once approved)",
        f"-- source: {spec.source_project}/{spec.source_connection}{src_db}.{spec.source_table}"
        f"  [{source_env} · {source_engine} · full table {est}]",
        f"-- target: {spec.target_project}/{spec.target_connection}{tgt_db}.{spec.target_table}"
        f"  [{target_env} · {target_engine} · {'exists' if target_exists else 'does not exist'}]",
        f"-- structure: {spec.ddl}    data: {spec.data}    up to {spec.limit:,} rows",
    ]
    if spec.where.strip():
        lines.append(f"-- WHERE: {spec.where.strip()}")
    if spec.order_by.strip():
        lines.append(f"-- ORDER BY: {spec.order_by.strip()}")
    lines.append(f"-- columns({len(columns)}): {', '.join(columns) if columns else '-'}")
    for w in warnings:
        lines.append(f"-- ⚠ {w}")
    lines.append("--")
    if spec.ddl == DDL_RECREATE:
        lines.append(f"{build_drop_sql(target_engine, spec.target_table)};")
    if ddl_sql:
        lines.append(ddl_sql.rstrip(";") + ";")
    elif spec.ddl != DDL_SKIP:
        lines.append("-- target table already exists, skipping table creation")
    if spec.data == DATA_REPLACE:
        lines.append(f"DELETE FROM {quote_ident(target_engine, spec.target_table)};  -- clear the target table")
    if spec.data != DATA_NONE:
        lines.append(
            build_select_sql(source_engine, spec.source_table, columns, spec.limit,
                             spec.where, spec.order_by, spec.source_database)
            + ";  -- fetch from the source (one extra row is actually requested to detect "
              "more rows on the source side), written to the target in a parameterized batch"
        )
    return "\n".join(lines)


def assess_plan(spec: SyncSpec, target_env: str, source_env: str) -> dict:
    """给同步计划评风险等级，形状与 audit.risk.RiskReport.to_dict() 一致（审批页共用渲染）。

    不走 risk.assess——那是给单条 SQL 用的解析式评估，对「计划」只会得到 Unparseable。
    """
    level = "MEDIUM"
    reasons = [f"Cross-connection table sync: {spec.source_project}/{spec.source_connection} -> "
               f"{spec.target_project}/{spec.target_connection}"]
    warnings: list[str] = []
    if spec.ddl == DDL_RECREATE:
        level = "HIGH"
        reasons.append(f"ddl=recreate: will DROP the target table {spec.target_table} first, "
                       f"destroying all existing data in it")
    if spec.data == DATA_REPLACE:
        level = "HIGH"
        reasons.append(f"data=replace: the target table {spec.target_table} will be cleared before writing")
    if target_env in ("staging", "prod"):
        level = "HIGH"
        reasons.append(f"Target is a {target_env} environment, not local/dev")
    if source_env == "prod":
        warnings.append("Data comes from production: after syncing, the target will hold a copy "
                        "of production data (not masked, copied with original values)")
    return {
        "level": level,
        "statement_kind": "TableSync",
        "tables": [spec.source_table, spec.target_table],
        "reasons": reasons,
        "warnings": warnings,
        "row_estimate": None,
        "affected_estimate": spec.limit if spec.data != DATA_NONE else 0,
        "has_where": bool(spec.where.strip()),
        "uses_index": None,
    }
