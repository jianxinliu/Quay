r"""把查询结果 dict 渲染成给 agent 的紧凑 TSV 文本块（省 token）。

相比 columnar JSON，TSV 去掉了每行的 [ ] 引号逗号，宽/长结果省 ~25-30% token。
格式（工具描述里同步说明，agent 据此解析）：
    # shown=200 truncated=true reason=char_budget elapsed_ms=103 stmt=Select
    # types: number, string, number, string, date
    # note: 结果被截断……（仅 truncated 时出现）
    id\tchannel\tamount\tstatus\tcreated_at        ← 首行列名
    1726...\torganic\t12.5\t\N\t2026-07-01          ← 数据行
- 制表符分隔；NULL 记作 `\N`（与空字符串区分）；bool 记作 true/false；
  dict/list 值转紧凑 JSON；值里的 \\ \t \n \r 做反斜杠转义（保证一行一条记录）。
- 字符预算：逐行累加，超预算即停止加行并标 truncated=char_budget（硬限，防吃爆上下文）。
"""

from __future__ import annotations

import json

_NULL = "\\N"


def _cell(v: object) -> str:
    """把单个值渲染成一个 TSV 字段（转义分隔符，NULL→\\N）。"""
    if v is None:
        return _NULL
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    else:
        s = str(v)
    return (s.replace("\\", "\\\\").replace("\t", "\\t")
             .replace("\n", "\\n").replace("\r", "\\r"))


def render_agent_result(result: dict, char_budget: int) -> str:
    """把 query/sample_rows 的结果 dict 渲染成紧凑 TSV 文本块。

    char_budget：输出的近似字符上限（≈ token×4）。逐行累加超限即停，标 truncated。
    """
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    types = result.get("column_types") or []
    db_truncated = bool(result.get("truncated"))
    elapsed = result.get("duration_ms")
    stmt = result.get("statement_kind") or ""
    masked = result.get("masked_columns") or []

    col_line = "\t".join(_cell(c) for c in cols)
    has_types = any(str(t or "") for t in types)
    type_line = "# types: " + ", ".join(str(t or "") for t in types) if has_types else ""

    # 逐行累加，受字符预算限制（预留头部 + 列名 + 类型行 + note 的开销）
    reserve = len(col_line) + len(type_line) + 220
    used = reserve
    body: list[str] = []
    shown = 0
    budget_hit = False
    for r in rows:
        line = "\t".join(_cell(v) for v in r)
        # 至少给 1 行，避免超宽单行时返回空
        if shown >= 1 and used + len(line) + 1 > char_budget:
            budget_hit = True
            break
        body.append(line)
        used += len(line) + 1
        shown += 1

    truncated = budget_hit or db_truncated
    reason = "char_budget" if budget_hit else ("row_cap" if db_truncated else "")

    meta = [f"shown={shown}"]
    meta.append("truncated=true" if truncated else "truncated=false")
    if reason:
        meta.append(f"reason={reason}")
    if elapsed is not None:
        meta.append(f"elapsed_ms={elapsed}")
    if stmt:
        meta.append(f"stmt={stmt}")
    if masked:
        meta.append("masked=" + ",".join(masked))

    out = ["# " + " ".join(meta)]
    if type_line:
        out.append(type_line)
    if truncated:
        out.append("# note: 结果被截断（" + reason + "）。别重复拉全量——用 WHERE/LIMIT/聚合"
                   "收窄，或用分析工作台（analysis_*）下推计算后只取小结果。")
    out.append(col_line)
    out.extend(body)
    return "\n".join(out)
