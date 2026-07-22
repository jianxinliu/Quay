"""风险评估引擎：对写操作产出风险报告，供自动策略与人工审批参考。

评估维度（DESIGN.md 第五节）：
- 语句分类（sqlglot AST）
- 影响范围：涉及表 + 表行数量级（元数据缓存）+ WHERE 条件列是否命中索引
- 风险分级：CRITICAL / HIGH / MEDIUM / LOW

评估以静态分析为主（sqlglot + 元数据），不依赖真实执行；表元数据通过注入的
provider 获取，评估引擎本身不触库，便于单测。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import sqlglot
from sqlglot import exp

# 风险等级，数值越大越危险
LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}

# 阈值
LARGE_TABLE_ROWS = 1_000_000  # 大表 DDL → 锁表风险
BULK_WRITE_ROWS = 10_000  # 预估影响行数超此值 → 批量写高危

_DIALECTS = {"mysql": "mysql", "postgres": "postgres", "sqlite": "sqlite",
             "clickhouse": "clickhouse"}


class TableMetaLike(Protocol):
    row_estimate: int | None
    indexed_columns: set[str]


MetaProvider = Callable[[str], TableMetaLike | None]


@dataclass
class RiskReport:
    level: str
    statement_kind: str
    tables: list[str]
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    row_estimate: int | None = None
    affected_estimate: int | None = None
    has_where: bool | None = None
    uses_index: bool | None = None

    @property
    def requires_approval(self) -> bool:
        # 目前所有写操作都需审批；保留字段供未来按等级放宽 dev 环境低风险写
        return True

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "statement_kind": self.statement_kind,
            "tables": self.tables,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "row_estimate": self.row_estimate,
            "affected_estimate": self.affected_estimate,
            "has_where": self.has_where,
            "uses_index": self.uses_index,
        }


def _bump(current: str, candidate: str) -> str:
    return candidate if _LEVEL_RANK[candidate] > _LEVEL_RANK[current] else current


def assess(sql: str, engine: str, meta_provider: MetaProvider) -> RiskReport:
    """评估一条（或多条）写 SQL 的风险。调用方应已通过 classify 确认这是写操作。

    多语句批量：逐条评估后聚合——等级取最高，reasons/warnings 逐条列出，
    表名合并去重（对应 classify 已放行的多语句进审批流）。
    """
    dialect = _DIALECTS.get(engine)
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.SqlglotError:
        # 解析/分词均失败（含引号不闭合等 TokenizeError）→ 按最高风险处理
        return RiskReport(
            level="CRITICAL",
            statement_kind="Unparseable",
            tables=[],
            reasons=["SQL 无法解析，无法评估影响范围，按最高风险处理"],
        )
    if not statements:
        return RiskReport(level="CRITICAL", statement_kind="Empty", tables=[],
                          reasons=["空语句，无法评估，按最高风险处理"])
    if len(statements) > 1:
        return _assess_batch(statements, meta_provider)
    return _assess_one(statements[0], meta_provider)


def _assess_batch(statements: list[exp.Expression], meta_provider: MetaProvider) -> RiskReport:
    """逐条评估多语句并聚合：等级取最高，逐条列出理由与警告。"""
    reports = [_assess_one(s, meta_provider) for s in statements]
    level = "LOW"
    for r in reports:
        level = _bump(level, r.level)
    tables = sorted({t for r in reports for t in r.tables})
    reasons = [f"多语句批量提交（{len(statements)} 条），逐条评估取最高风险等级 {level}"]
    for i, r in enumerate(reports, 1):
        detail = "；".join(r.reasons) if r.reasons else "—"
        reasons.append(f"[{i}] {r.statement_kind}（{r.level}）：{detail}")
    warnings = [f"[{i}] {w}" for i, r in enumerate(reports, 1) for w in r.warnings]
    return RiskReport(level=level, statement_kind="MultiStatement", tables=tables,
                      reasons=reasons, warnings=warnings)


def _assess_one(stmt: exp.Expression, meta_provider: MetaProvider) -> RiskReport:
    """评估单条已解析语句。"""
    kind = type(stmt).__name__
    tables = sorted({t.name for t in stmt.find_all(exp.Table) if t.name})
    report = RiskReport(level="MEDIUM", statement_kind=kind, tables=tables)

    if isinstance(stmt, (exp.Drop, exp.TruncateTable)):
        report.level = "CRITICAL"
        report.reasons.append(f"{kind} 会不可逆地删除对象/数据")
        _annotate_table_size(report, tables, meta_provider)
        return report

    if isinstance(stmt, (exp.Update, exp.Delete)):
        _assess_dml(report, stmt, kind, tables, meta_provider)
        return report

    if isinstance(stmt, exp.Alter):
        report.reasons.append("DDL 变更")
        big = _annotate_table_size(report, tables, meta_provider)
        report.level = _bump(report.level, "HIGH" if big else "MEDIUM")
        if big:
            report.warnings.append("大表 DDL 可能长时间锁表，建议低峰期或在线 DDL 工具执行")
        return report

    if isinstance(stmt, exp.Insert):
        report.reasons.append("插入数据")
        report.level = "MEDIUM"
        return report

    if isinstance(stmt, (exp.Create, exp.Grant)):
        report.level = "HIGH"
        report.reasons.append(f"{kind} 属于 DDL / 权限变更")
        return report

    # 无法映射到已知写类型：无法评估影响范围，按最高风险处理（默认拒绝原则）
    report.level = "CRITICAL"
    report.reasons.append(f"无法识别的语句类型 {kind}，无法评估影响范围，按最高风险处理")
    return report


def _assess_dml(
    report: RiskReport,
    stmt: exp.Expression,
    kind: str,
    tables: list[str],
    meta_provider: MetaProvider,
) -> None:
    where = stmt.args.get("where")
    report.has_where = where is not None
    _annotate_table_size(report, tables, meta_provider)

    if where is None:
        report.level = "CRITICAL"
        report.reasons.append(f"无 WHERE 条件的 {kind} 会影响全表所有行")
        report.affected_estimate = report.row_estimate
        return

    # 有 WHERE：检查条件列是否命中索引（判断是否可能全表扫描）
    where_columns = {c.name for c in where.find_all(exp.Column) if c.name}
    indexed = _indexed_columns_for(tables, meta_provider)
    if where_columns and indexed is not None:
        report.uses_index = bool(where_columns & indexed)
        if not report.uses_index:
            report.level = _bump(report.level, "HIGH")
            report.reasons.append(
                f"WHERE 条件列 {sorted(where_columns)} 未命中索引，可能全表扫描后写入"
            )
        else:
            report.reasons.append("WHERE 条件命中索引，影响范围可控")
    else:
        report.reasons.append(f"带 WHERE 条件的 {kind}")


def _annotate_table_size(
    report: RiskReport, tables: list[str], meta_provider: MetaProvider
) -> bool:
    """标注表行数量级，返回是否命中"大表"。"""
    biggest: int | None = None
    for t in tables:
        meta = meta_provider(t)
        if meta is not None and meta.row_estimate is not None:
            biggest = meta.row_estimate if biggest is None else max(biggest, meta.row_estimate)
    report.row_estimate = biggest
    if biggest is not None and biggest >= LARGE_TABLE_ROWS:
        report.warnings.append(f"目标表约 {biggest:,} 行，属于大表")
        return True
    return False


def _indexed_columns_for(tables: list[str], meta_provider: MetaProvider) -> set[str] | None:
    cols: set[str] = set()
    seen_meta = False
    for t in tables:
        meta = meta_provider(t)
        if meta is not None:
            seen_meta = True
            cols |= meta.indexed_columns
    return cols if seen_meta else None
