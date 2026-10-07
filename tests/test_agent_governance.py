"""对 agent 的两道「用法治理」：会话首次调用附使用说明 + 会话级结果配额。

两件事都不是数据库功能，而是防止 agent 误用/滥用：
- 说明在**会话第一次调用工具**时随结果送达（instructions 各客户端处理不一，实测靠不住）；
- 配额在**同一会话累计返回量**超限时拒绝取数，逼 agent 回去问用户，而不是闷头继续拉数据。
"""

import sqlite3

import pytest

from dbmcp.approvals import ApprovalStore
from dbmcp.audit.log import AuditStore
from dbmcp.budget import (
    ResultBudgetExceeded,
    SessionBudget,
    count_tokens,
    estimate_tokens,
    tokenizer_ready,
    usage_note,
    warm_tokenizer,
)
from dbmcp.config import AppConfig
from dbmcp.guide import FIRST_CALL_GUIDE, USAGE_GUIDE
from dbmcp.server import build_mcp
from dbmcp.service import CallerInfo, DbmService

CALLER = CallerInfo(agent="pytest/1.0", session_id="sess-gov")


@pytest.fixture
def service(tmp_path):
    db_file = tmp_path / "biz.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);
           INSERT INTO users (name) VALUES ('alice'), ('bob'), ('carol');"""
    )
    conn.commit()
    conn.close()
    cfg = AppConfig.model_validate(
        {"projects": {"demo": {"connections": {"main": {
            "engine": "sqlite", "database": str(db_file), "environment": "dev",
        }}}}}
    )
    svc = DbmService(cfg, AuditStore(tmp_path / "a.sqlite3"),
                     ApprovalStore(tmp_path / "a.sqlite3"))
    yield svc
    svc.close()


# ---------------- 配额（纯逻辑） ----------------

class TestSessionBudget:
    def test_allows_until_limit_then_refuses(self):
        b = SessionBudget(limit_chars=100)
        b.check("s1")
        b.charge("s1", "x" * 99)
        b.check("s1")                      # 还没到，放行
        b.charge("s1", "x" * 1)
        with pytest.raises(ResultBudgetExceeded):
            b.check("s1")

    def test_sessions_are_independent(self):
        """一个 agent 用超了不该连累另一个会话。"""
        b = SessionBudget(limit_chars=10)
        b.charge("s1", "x" * 100)
        b.check("s2")
        with pytest.raises(ResultBudgetExceeded):
            b.check("s1")

    def test_message_tells_agent_to_ask_the_user(self):
        b = SessionBudget(limit_chars=10)
        b.charge("s1", "x" * 50)
        with pytest.raises(ResultBudgetExceeded) as ei:
            b.check("s1")
        msg = str(ei.value)
        assert "ask the user" in msg
        assert "allow_more_results" in msg
        assert "export_table" in msg and "analysis_" in msg   # 同时给出更省的做法

    def test_grant_releases_one_more_allowance(self):
        b = SessionBudget(limit_chars=100)
        b.charge("s1", "x" * 150)               # 已经超了
        with pytest.raises(ResultBudgetExceeded):
            b.check("s1")
        usage = b.grant("s1", "用户已确认：还要核对 3 张表")
        b.check("s1")                     # 放行后可以继续
        assert usage["grants"] == 1
        assert usage["last_reason"] == "用户已确认：还要核对 3 张表"
        # 额度在「当前已用量」之上叠加，而不是从 0 重来——否则超得越多反而赚得越多
        assert usage["allowance_chars"] == 250

    def test_grant_twice_accumulates(self):
        b = SessionBudget(limit_chars=100)
        b.charge("s1", "x" * 100)
        b.grant("s1")
        b.charge("s1", "x" * 100)
        b.grant("s1")
        b.check("s1")
        assert b.usage("s1")["grants"] == 2

    def test_zero_limit_disables_the_gate(self):
        b = SessionBudget(limit_chars=0)
        b.charge("s1", "x" * 10_000)
        b.check("s1")                     # 不限制
        assert b.usage("s1")["enabled"] is False

    def test_missing_session_id_shares_one_anonymous_bucket(self):
        b = SessionBudget(limit_chars=100)
        b.charge("", "x" * 150)
        with pytest.raises(ResultBudgetExceeded):
            b.check("")

    def test_snapshot_sorted_by_usage(self):
        b = SessionBudget(limit_chars=100)
        b.charge("small", "x" * 10)
        b.charge("big", "x" * 90)
        assert [u["session_id"] for u in b.snapshot()] == ["big", "small"]

    def test_tokens_accumulate_across_calls(self):
        b = SessionBudget(limit_chars=100_000)
        first = b.charge("s1", "a" * 400)["used_tokens"]
        usage = b.charge("s1", "a" * 400)
        assert usage["used_chars"] == 800
        assert usage["used_tokens"] == first * 2

    def test_snapshot_says_whether_the_count_is_exact(self):
        """界面据此决定写「N token」还是「≈N token（粗估）」——
        不能把估算值伪装成精确计数。"""
        b = SessionBudget(limit_chars=100)
        assert b.charge("s1", "abc")["tokens_exact"] is tokenizer_ready()


class TestTokenCounting:
    """token 数是给人看的注解；配额本身按字符强制，不依赖分词器。"""

    def test_heuristic_is_character_class_aware(self):
        """同样长度的中英文 token 密度差三倍，用一个固定除数换算会误导人。"""
        assert estimate_tokens("") == 0
        en = estimate_tokens("a" * 400)
        cn = estimate_tokens("中" * 400)
        assert en == 100                      # ASCII ≈ 4 字符/token
        assert cn > en * 3                    # 中文密得多

    def test_count_falls_back_to_heuristic_without_tokenizer(self, monkeypatch):
        """装不上 tiktoken、或词表下不来（离线/代理）时，取数路径绝不能因此受影响。"""
        import dbmcp.budget as bud

        monkeypatch.setattr(bud, "_encoder", None)
        monkeypatch.setattr(bud, "_encoder_tried", True)
        assert bud.count_tokens("abcd" * 100) == bud.estimate_tokens("abcd" * 100)
        assert bud.tokenizer_ready() is False

    def test_broken_tokenizer_does_not_break_counting(self, monkeypatch):
        import dbmcp.budget as bud

        class Boom:
            def encode(self, *a, **k):
                raise RuntimeError("boom")

        monkeypatch.setattr(bud, "_encoder", Boom())
        monkeypatch.setattr(bud, "_encoder_tried", True)
        assert bud.count_tokens("abcd") == bud.estimate_tokens("abcd")

    @pytest.mark.skipif(not tokenizer_ready() and not warm_tokenizer(),
                        reason="未安装 tiktoken 附加依赖")
    def test_real_tokenizer_beats_the_heuristic_on_tsv(self):
        """TSV 结果是本服务最常见的内容，而启发式对它少报近一半——
        制表符、纯数字 id、短字段各自成 token，密度远高于「4 字符/token」。"""
        tsv = "id\tname\tamount\n" + "".join(
            f"{i}\tuser{i}\t{i * 13.5}\n" for i in range(200))
        real = count_tokens(tsv)
        assert real > estimate_tokens(tsv) * 1.5
        assert count_tokens("") == 0

    def test_session_table_is_bounded(self):
        """常驻进程不能无限攒会话 id。"""
        b = SessionBudget(limit_chars=100, max_sessions=3)
        for i in range(10):
            b.charge(f"s{i}", "x")
        assert len(b.snapshot()) == 3


class TestUsageNote:
    def test_quiet_below_threshold(self):
        assert usage_note({"enabled": True, "percent": 10}) == ""

    def test_warns_near_limit(self):
        note = usage_note({"enabled": True, "percent": 80, "used_chars": 8000,
                           "used_tokens": 2285, "calls": 12})
        assert note.startswith("# budget:")     # TSV 注释行，不会被当成数据
        assert "80%" in note

    def test_silent_when_disabled(self):
        assert usage_note({"enabled": False, "percent": 999}) == ""


# ---------------- 服务层接线 ----------------

class TestBudgetWiring:
    def test_limit_follows_settings(self, service, tmp_path):
        from dbmcp.settings import SettingsStore

        service.settings = SettingsStore(tmp_path / "s.sqlite3")
        service.save_settings({"agent_session_budget_chars": 1234})
        assert service.result_budget().limit_chars == 1234

    def test_default_used_without_settings_store(self, service):
        assert service.result_budget().limit_chars > 0

    def test_dashboard_reports_budgets(self, service):
        service.result_budget().charge("sess-gov", "x" * 4321)
        budgets = service.dashboard_snapshot()["budgets"]
        assert budgets[0]["session_id"] == "sess-gov"
        assert budgets[0]["used_chars"] == 4321


# ---------------- 真实 MCP 协议 ----------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_first_call_carries_the_guide(service):
    """会话第一次调用工具时随结果附说明，之后不再重复。"""
    from fastmcp import Client

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        first = await c.call_tool("list_projects", {})
        texts = [b.text for b in first.content if hasattr(b, "text")]
        assert any(FIRST_CALL_GUIDE in t for t in texts)
        # 结构化返回值不受影响：按 schema 消费的客户端读到的还是原来的东西
        assert first.data == [{"project": "demo", "connections": ["main"]}]

        second = await c.call_tool("list_projects", {})
        assert not any(FIRST_CALL_GUIDE in b.text
                       for b in second.content if hasattr(b, "text"))


@pytest.mark.anyio
async def test_guide_can_be_switched_off(service, tmp_path):
    from fastmcp import Client

    from dbmcp.settings import SettingsStore

    service.settings = SettingsStore(tmp_path / "s.sqlite3")
    service.save_settings({"agent_guide_on_first_call": False})
    mcp = build_mcp(service)
    async with Client(mcp) as c:
        r = await c.call_tool("list_projects", {})
        assert not any(FIRST_CALL_GUIDE in b.text
                       for b in r.content if hasattr(b, "text"))


@pytest.mark.anyio
async def test_usage_guide_tool_returns_full_text(service):
    from fastmcp import Client

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        assert "usage_guide" not in {t.name for t in await c.list_tools()}
        assert (await c.call_tool("call_capability", {
            "name": "usage_guide", "arguments": {}})).data == USAGE_GUIDE


@pytest.mark.anyio
async def test_budget_blocks_query_then_grant_unblocks(service, tmp_path):
    """闭环：查到超额 → 被拒并被告知去问用户 → allow_more_results → 能继续查。"""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from dbmcp.settings import SettingsStore

    service.settings = SettingsStore(tmp_path / "s.sqlite3")
    service.save_settings({"agent_session_budget_chars": 50})

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        await c.call_tool("query", {"project": "demo", "connection": "main",
                                    "sql": "SELECT * FROM users"})
        with pytest.raises(ToolError) as ei:
            await c.call_tool("query", {"project": "demo", "connection": "main",
                                        "sql": "SELECT 1"})
        assert "[result_budget_exceeded]" in str(ei.value)
        assert "allow_more_results" in str(ei.value)

        grant = await c.call_tool("allow_more_results",
                                  {"reason": "用户已确认：还要核对对账差异"})
        assert grant.data["granted"] is True and grant.data["grants"] == 1

        again = await c.call_tool("query", {"project": "demo", "connection": "main",
                                            "sql": "SELECT 1"})
        assert "1" in again.data

    # 放行理由留痕，人在看板上能核对 agent 到底问没问过
    assert service.result_budget().snapshot()[0]["last_reason"].startswith("用户已确认")


@pytest.mark.anyio
async def test_budget_note_appended_near_limit(service, tmp_path):
    from fastmcp import Client

    from dbmcp.settings import SettingsStore

    service.settings = SettingsStore(tmp_path / "s.sqlite3")
    service.save_settings({"agent_session_budget_chars": 120})
    mcp = build_mcp(service)
    async with Client(mcp) as c:
        out = (await c.call_tool("query", {"project": "demo", "connection": "main",
                                           "sql": "SELECT * FROM users"})).data
    assert "# budget:" in out


@pytest.mark.anyio
async def test_sample_rows_shares_the_same_budget(service, tmp_path):
    """两个取数工具共用一份配额，否则换个工具就能绕过去。"""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from dbmcp.settings import SettingsStore

    service.settings = SettingsStore(tmp_path / "s.sqlite3")
    service.save_settings({"agent_session_budget_chars": 40})
    mcp = build_mcp(service)
    async with Client(mcp) as c:
        await c.call_tool("query", {"project": "demo", "connection": "main",
                                    "sql": "SELECT * FROM users"})
        with pytest.raises(ToolError, match="result_budget_exceeded"):
            await c.call_tool("sample_rows", {"project": "demo", "connection": "main",
                                              "table": "users"})


@pytest.mark.anyio
async def test_budget_off_by_default_settings_value(service):
    """默认配额足够大，正常会话不会被打断（回归：别把默认值设得太小）。"""
    from fastmcp import Client

    mcp = build_mcp(service)
    async with Client(mcp) as c:
        for _ in range(5):
            await c.call_tool("query", {"project": "demo", "connection": "main",
                                        "sql": "SELECT * FROM users"})
    assert service.result_budget().usage("")["percent"] < 5
