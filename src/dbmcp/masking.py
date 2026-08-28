"""敏感字段脱敏：按列名匹配，替换查询结果中的值。

内置模式做子串匹配（列名含 password/token/... 即命中）；policy.mask_columns 做精确匹配
（不区分大小写），用于内置模式覆盖不到的业务列。

「是否启用内置模式」是**显式入参**而不是从 policy 里读——因为 `Policy.mask_default_patterns`
是三态（None = 跟随全局设置），直接 `if policy.mask_default_patterns` 会把「跟随全局」
静默当成关闭。调用方必须先用 `resolve_default_patterns()` 定下来。
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


def resolve_default_patterns(policy: Policy, global_default: bool) -> bool:
    """内置模式是否启用：连接级 Policy 优先，None 表示跟随全局设置。"""
    return global_default if policy.mask_default_patterns is None else policy.mask_default_patterns


def masked_indices(columns: list[str], policy: Policy, default_patterns: bool) -> set[int]:
    """命中脱敏的列下标。default_patterns 由调用方经 resolve_default_patterns 定下。

    注意：policy.mask_columns（显式点名的列）**不受 default_patterns 影响**——
    关掉内置模式只是不再按 password/token 这类词猜，手动点名的列照样脱敏。
    """
    explicit = {c.lower() for c in policy.mask_columns}
    hit: set[int] = set()
    for i, col in enumerate(columns):
        name = col.lower()
        if name in explicit:
            hit.add(i)
        elif default_patterns and any(p in name for p in DEFAULT_PATTERNS):
            hit.add(i)
    return hit


def apply_mask(columns: list[str], rows: list[list[Any]], policy: Policy,
               default_patterns: bool) -> tuple[list[list[Any]], list[str]]:
    """返回 (脱敏后的 rows, 被脱敏的列名列表)。无命中时原样返回。"""
    indices = masked_indices(columns, policy, default_patterns)
    if not indices:
        return rows, []
    masked_rows = [
        [MASK if i in indices and v is not None else v for i, v in enumerate(row)]
        for row in rows
    ]
    return masked_rows, [columns[i] for i in sorted(indices)]
