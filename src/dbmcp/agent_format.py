r"""Render a query-result dict into a compact TSV text block for the agent (saves tokens).

Compared to columnar JSON, TSV drops the per-row `[ ]`, quotes, and commas, saving
roughly 25-30% tokens on wide/long results.
Format (documented in the tool descriptions too, so the agent knows how to parse it):
    # shown=200 truncated=true reason=char_budget elapsed_ms=103 stmt=Select
    # types: number, string, number, string, date
    # note: result was truncated... (only present when truncated)
    id\tchannel\tamount\tstatus\tcreated_at        <- header row (column names)
    1726...\torganic\t12.5\t\N\t2026-07-01          <- data row
- Tab-separated; NULL is written as `\N` (distinct from an empty string); bool is written
  as true/false; dict/list values become compact JSON; `\ \t \n \r` in values are
  backslash-escaped (guaranteeing one record per line).
- Character budget: accumulated row by row; once the budget is hit, no more rows are
  added and the result is marked truncated=char_budget (a hard cap, to protect context).
"""

from __future__ import annotations

import json

_NULL = "\\N"


def _cell(v: object) -> str:
    """Render a single value as one TSV field (escape separators, NULL -> \\N)."""
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
    """Render a query/sample_rows result dict into a compact TSV text block.

    char_budget: the approximate character cap for the output (~token x4). Rows are
    accumulated one by one; once the cap is exceeded, stop and mark truncated.
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
        out.append("# note: result truncated (" + reason + "). Don't re-fetch the full set —"
                   " narrow it with WHERE/LIMIT/aggregation, or push the computation into"
                   " the analysis workbench (analysis_*) and bring back only the small result.")
    out.append(col_line)
    out.extend(body)
    return "\n".join(out)
