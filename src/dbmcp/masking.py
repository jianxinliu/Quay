"""敏感字段脱敏：按列名匹配，替换查询结果中的值。

内置模式做子串匹配（列名含 password/token/... 即命中），策略可关闭；
policy.mask_columns 做精确匹配（不区分大小写），用于内置模式覆盖不到的业务列。
"""

from __future__ import annotations

from typing import Any

from .config import Policy

MASK = "***MASKED***"

# 内置敏感列名模式（子串匹配，小写比较）
DEFAULT_PATTERNS = (
    "password", "passwd", "pwd",
    "secret", "token", "api_key", "apikey", "access_key", "private_key",
    "credential", "credit_card", "card_no", "cvv", "ssn", "id_card",
)


def masked_indices(columns: list[str], policy: Policy) -> set[int]:
    explicit = {c.lower() for c in policy.mask_columns}
    hit: set[int] = set()
    for i, col in enumerate(columns):
        name = col.lower()
        if name in explicit:
            hit.add(i)
        elif policy.mask_default_patterns and any(p in name for p in DEFAULT_PATTERNS):
            hit.add(i)
    return hit


def apply_mask(columns: list[str], rows: list[list[Any]], policy: Policy) -> tuple[list[list[Any]], list[str]]:
    """返回 (脱敏后的 rows, 被脱敏的列名列表)。无命中时原样返回。"""
    indices = masked_indices(columns, policy)
    if not indices:
        return rows, []
    masked_rows = [
        [MASK if i in indices and v is not None else v for i, v in enumerate(row)]
        for row in rows
    ]
    return masked_rows, [columns[i] for i in sorted(indices)]
