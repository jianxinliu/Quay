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

# PG EXPLAIN 的选项关键词（用于剥离后递归判定被解释的语句）
_EXPLAIN_OPTION_WORDS = {"analyze", "verbose", "costs", "buffers", "timing", "summary", "wal"}


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
        # 解析/分词均失败（含引号不闭合等 TokenizeError）→ 默认拒绝，按写操作处理
        errs = getattr(e, "errors", None)
        desc = errs[0].get("description") if errs else str(e)
        return Verdict(False, f"SQL 解析失败，按写操作处理: {desc}")

    if not statements:
        return Verdict(False, "空语句")
    if len(statements) > 1:
        return Verdict(False, "不支持多语句提交，请拆分为单条语句")

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
