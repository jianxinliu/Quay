"""SQL 语句分类：判定是否只读。

安全红线（CLAUDE.md）：默认拒绝——解析失败、多语句、无法分类的一律按写操作处理。

sqlglot 行为要点（已实测验证）：
- ``WITH x AS (INSERT ...) SELECT`` 顶层是 Select，必须遍历整棵树找写节点；
- PG 方言下 ``EXPLAIN`` / ``SHOW`` 解析为不透明的 Command 节点，需要特判；
- MySQL 方言下 ``EXPLAIN SELECT`` 解析为 Describe（内部包含被解释的语句，树遍历可覆盖）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

_DIALECTS = {"mysql": "mysql", "postgres": "postgres", "sqlite": "sqlite"}

# 只读语句的顶层节点白名单
_READONLY_ROOTS = (exp.Select, exp.Show, exp.Describe)

# 树中任意位置出现即判写的节点
_WRITE_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
)

# PG EXPLAIN 的裸选项关键词：PG 文法里 EXPLAIN 后只有 ANALYZE / VERBOSE 允许不带括号，
# 其余选项（COSTS/BUFFERS/…）必须写在括号内（已由括号剥离处理）。收窄到文法真实允许的两个，
# 避免"剥掉未知词后把写语句误解析成只读"（H4）。
_EXPLAIN_OPTION_WORDS = {"analyze", "verbose"}

# 有副作用/危险的函数：即便语法上出现在 SELECT 里也不能按只读放行——否则只读账号即可
# 被用来 DoS（SLEEP/BENCHMARK）、读服务器文件（pg_read_file/LOAD_FILE）、读写文件（lo_export）
# 或外连执行（dblink），绕过整个审批体系（H2）。命中即按写操作处理（默认拒绝原则）。
_UNSAFE_FUNCTIONS = {
    "SLEEP", "PG_SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK",           # DoS / 会话锁
    "LOAD_FILE", "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_LS_DIR", "PG_STAT_FILE",  # 读文件/目录
    "LO_EXPORT", "LO_IMPORT", "LO_GET", "LO_PUT",                          # 大对象读写服务器文件
    "DBLINK", "DBLINK_EXEC",                                               # 外部连接执行
}


def _unsafe_function_name(stmt: exp.Expression) -> str | None:
    """遍历语句树，返回首个命中危险函数黑名单的函数名（大写），无则 None。"""
    for node in stmt.walk():
        if isinstance(node, exp.Anonymous):
            name = str(node.this or "").upper()
            if name in _UNSAFE_FUNCTIONS:
                return name
        elif isinstance(node, exp.Func):
            name = (node.sql_names()[0] if node.sql_names() else "").upper()
            if name in _UNSAFE_FUNCTIONS:
                return name
    return None


@dataclass
class Verdict:
    readonly: bool
    reason: str
    statement_kind: str = "unknown"
    tables: list[str] = field(default_factory=list)


def classify(sql: str, engine: str) -> Verdict:
    """判定一条 SQL 是否只读。engine 为 mysql / postgres / sqlite。"""
    dialect = _DIALECTS.get(engine)
    if dialect is None:
        return Verdict(False, f"引擎 {engine!r} 不支持 SQL 分类")

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.SqlglotError as e:
        # 解析/分词均失败（含引号不闭合等 TokenizeError）→ 默认拒绝，按写操作处理。
        # statement_kind=ParseError 让上层（后台查询台）能把"语法错误"与"真写操作"区分开，
        # 给出正确提示而非误导的"确认写操作"卡片。
        errs = getattr(e, "errors", None)
        desc = errs[0].get("description") if errs else str(e)
        return Verdict(False, f"SQL 解析失败: {desc}", "ParseError")

    if not statements:
        return Verdict(False, "空语句")
    if len(statements) > 1:
        # 多语句批量：按写操作统一进审批流（安全红线）。逐条评估在 risk.assess，
        # 执行时按语句拆开单事务逐条跑（engines.run_write）。
        kinds = [type(s).__name__ for s in statements]
        tables = sorted({t for s in statements for t in _extract_tables(s)})
        return Verdict(
            False,
            f"多语句批量提交（{len(statements)} 条：{'、'.join(kinds)}），按写操作进审批流",
            "MultiStatement",
            tables,
        )

    stmt = statements[0]
    kind = type(stmt).__name__
    tables = _extract_tables(stmt)

    # PG 下 EXPLAIN / SHOW 退化为 Command，需特判
    if isinstance(stmt, exp.Command):
        keyword = str(stmt.this or "").strip().upper()
        if keyword == "SHOW":
            return Verdict(True, "SHOW 命令", "Show", tables)
        if keyword == "EXPLAIN":
            return _classify_explain(sql, engine)
        return Verdict(False, f"无法识别的命令 {keyword or '(空)'}，按写操作处理", kind, tables)

    if not isinstance(stmt, _READONLY_ROOTS):
        return Verdict(False, f"{kind} 属于写操作/DDL", kind, tables)

    # 顶层是只读类型，仍需遍历整棵树排除 CTE/子查询中的写操作
    for node in stmt.walk():
        if isinstance(node, _WRITE_NODES):
            return Verdict(False, f"语句内部包含写操作节点 {type(node).__name__}（如 CTE 中的 DML）", kind, tables)
        if isinstance(node, exp.Lock):
            return Verdict(False, "SELECT ... FOR UPDATE/SHARE 会加锁，按写操作处理", kind, tables)
        if isinstance(node, exp.Into):
            return Verdict(False, "SELECT ... INTO 会写出数据，按写操作处理", kind, tables)

    unsafe = _unsafe_function_name(stmt)
    if unsafe:
        return Verdict(False, f"包含有副作用/危险函数 {unsafe}()，不按只读放行（按写操作处理）", kind, tables)

    return Verdict(True, "只读语句", kind, tables)


def _classify_explain(sql: str, engine: str) -> Verdict:
    """剥离 EXPLAIN 前缀与选项，递归判定被解释的语句是否只读。

    PG 的 EXPLAIN ANALYZE 会真实执行语句，因此只要内层语句只读即可放行，
    内层是 DML 时无论是否 ANALYZE 一律拒绝。
    """
    rest = re.sub(r"^\s*EXPLAIN\s*", "", sql, count=1, flags=re.IGNORECASE)
    # 剥离括号选项形式: EXPLAIN (ANALYZE, BUFFERS) ...
    rest = re.sub(r"^\(\s*[^)]*\)\s*", "", rest, count=1)
    # 剥离裸选项关键词形式: EXPLAIN ANALYZE VERBOSE ...
    words = rest.split()
    while words and words[0].lower() in _EXPLAIN_OPTION_WORDS:
        words.pop(0)
    inner = " ".join(words)
    if not inner:
        return Verdict(False, "EXPLAIN 后缺少语句")
    inner_verdict = classify(inner, engine)
    if inner_verdict.readonly:
        return Verdict(True, "EXPLAIN 只读语句", "Explain", inner_verdict.tables)
    return Verdict(False, f"EXPLAIN 的目标语句非只读: {inner_verdict.reason}", "Explain", inner_verdict.tables)


def _extract_tables(stmt: exp.Expression) -> list[str]:
    try:
        return sorted({t.sql() for t in stmt.find_all(exp.Table)})
    except Exception:
        return []


def fingerprint(sql: str, engine: str) -> str:
    """SQL 规范化指纹：用于审计关联与（M3）审批单匹配。"""
    dialect = _DIALECTS.get(engine)
    normalized: str
    try:
        normalized = ";".join(sqlglot.transpile(sql, read=dialect, write=dialect))
    except Exception:
        normalized = " ".join(sql.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
