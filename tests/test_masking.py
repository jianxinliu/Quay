"""敏感字段脱敏测试。"""

from dbmcp.config import Policy
from dbmcp.masking import MASK, apply_mask, resolve_default_patterns


def test_default_patterns_mask():
    cols = ["id", "name", "password", "api_key", "user_token"]
    rows = [[1, "alice", "p@ss", "ak-123", "tok-1"]]
    masked_rows, masked_cols = apply_mask(cols, rows, Policy(), True)
    assert masked_rows == [[1, "alice", MASK, MASK, MASK]]
    assert masked_cols == ["password", "api_key", "user_token"]


def test_custom_columns():
    policy = Policy(mask_columns=["salary"])
    cols = ["id", "salary"]
    rows = [[1, 99999], [2, 88888]]
    masked_rows, masked_cols = apply_mask(cols, rows, policy, True)
    assert masked_rows == [[1, MASK], [2, MASK]]
    assert masked_cols == ["salary"]


def test_disable_default_patterns():
    policy = Policy(mask_default_patterns=False)
    cols = ["id", "password"]
    rows = [[1, "x"]]
    on = resolve_default_patterns(policy, global_default=True)   # 连接级 False 覆盖全局 True
    masked_rows, masked_cols = apply_mask(cols, rows, policy, on)
    assert on is False
    assert masked_rows == [[1, "x"]]
    assert masked_cols == []


class TestResolveDefaultPatterns:
    """三态解析：连接级 Policy 优先，None 表示跟随全局设置。"""

    def test_none_follows_global_on(self):
        assert resolve_default_patterns(Policy(), global_default=True) is True

    def test_none_follows_global_off(self):
        assert resolve_default_patterns(Policy(), global_default=False) is False

    def test_connection_true_overrides_global_off(self):
        policy = Policy(mask_default_patterns=True)
        assert resolve_default_patterns(policy, global_default=False) is True

    def test_connection_false_overrides_global_on(self):
        policy = Policy(mask_default_patterns=False)
        assert resolve_default_patterns(policy, global_default=True) is False


def test_explicit_columns_still_masked_when_patterns_off():
    """关掉内置模式只是不再按词猜；手动点名的列照样脱敏。"""
    policy = Policy(mask_default_patterns=False, mask_columns=["salary"])
    cols = ["password", "salary"]
    rows = [["x", 1]]
    masked_rows, masked_cols = apply_mask(cols, rows, policy, False)
    assert masked_rows == [["x", MASK]]
    assert masked_cols == ["salary"]


def test_null_not_masked():
    cols = ["password"]
    rows = [[None]]
    masked_rows, _ = apply_mask(cols, rows, Policy(), True)
    assert masked_rows == [[None]]  # NULL 保留，不然看不出有没有值


def test_no_hit_returns_original():
    cols = ["id", "name"]
    rows = [[1, "a"]]
    masked_rows, masked_cols = apply_mask(cols, rows, Policy(), True)
    assert masked_rows is rows
    assert masked_cols == []


def test_case_insensitive():
    cols = ["PassWord", "SALARY"]
    rows = [["x", 1]]
    policy = Policy(mask_columns=["salary"])
    masked_rows, masked_cols = apply_mask(cols, rows, policy, True)
    assert masked_rows == [[MASK, MASK]]
    assert set(masked_cols) == {"PassWord", "SALARY"}
