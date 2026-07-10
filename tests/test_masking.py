"""敏感字段脱敏测试。"""

from dbmcp.config import Policy
from dbmcp.masking import MASK, apply_mask


def test_default_patterns_mask():
    cols = ["id", "name", "password", "api_key", "user_token"]
    rows = [[1, "alice", "p@ss", "ak-123", "tok-1"]]
    masked_rows, masked_cols = apply_mask(cols, rows, Policy())
    assert masked_rows == [[1, "alice", MASK, MASK, MASK]]
    assert masked_cols == ["password", "api_key", "user_token"]


def test_custom_columns():
    policy = Policy(mask_columns=["salary"])
    cols = ["id", "salary"]
    rows = [[1, 99999], [2, 88888]]
    masked_rows, masked_cols = apply_mask(cols, rows, policy)
    assert masked_rows == [[1, MASK], [2, MASK]]
    assert masked_cols == ["salary"]


def test_disable_default_patterns():
    policy = Policy(mask_default_patterns=False)
    cols = ["id", "password"]
    rows = [[1, "x"]]
    masked_rows, masked_cols = apply_mask(cols, rows, policy)
    assert masked_rows == [[1, "x"]]
    assert masked_cols == []


def test_null_not_masked():
    cols = ["password"]
    rows = [[None]]
    masked_rows, _ = apply_mask(cols, rows, Policy())
    assert masked_rows == [[None]]  # NULL 保留，不然看不出有没有值


def test_no_hit_returns_original():
    cols = ["id", "name"]
    rows = [[1, "a"]]
    masked_rows, masked_cols = apply_mask(cols, rows, Policy())
    assert masked_rows is rows
    assert masked_cols == []


def test_case_insensitive():
    cols = ["PassWord", "SALARY"]
    rows = [["x", 1]]
    policy = Policy(mask_columns=["salary"])
    masked_rows, masked_cols = apply_mask(cols, rows, policy)
    assert masked_rows == [[MASK, MASK]]
    assert set(masked_cols) == {"PassWord", "SALARY"}
