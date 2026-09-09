"""会话级结果配额：一个 agent 会话累计能把多少数据搬进上下文。

**要挡的是什么**：单次调用早就有两级上限（行数 + 字符预算），但它管不住「同一个会话里
一直查」——一次 1 万字符、查两百次，照样烧掉几十万 token，而且这种情况多半是 agent 陷进了
某种循环（反复重拉同一份数据、逐行取代替聚合），继续跑下去对谁都没好处。

**做法**：按会话累计已返回的字符数，超过预算就**拒绝下一次取数**，并在错误里明确告诉
agent：去问用户要不要继续；用户同意后调 `allow_more_results` 再放行一个额度。
额度不是自动续的——必须有人点头，这正是这道闸门的意义。

只作用于 **agent 侧的取数工具**（query / sample_rows）。管理后台查询台、导出、
分析工作台内部计算都不经过这里：人自己看数据不该被配额挡。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

# 估算用：一个 token 大致对应多少字符。只用来把字符数翻译成 agent 有体感的 token 数，
# 不参与任何判定，所以不需要精确（中文更省 token，英文/SQL 更费，取个中间值）。
CHARS_PER_TOKEN = 3.5


class ResultBudgetExceeded(Exception):
    """会话累计返回量超出配额。对 agent 是「去问用户」而不是「换个写法重试」。"""


@dataclass
class _SessionUsage:
    used_chars: int = 0
    calls: int = 0
    allowance: int = 0          # 已获批的总额度（初始 = 一份基础预算）
    grants: int = 0             # 用户额外放行过几次
    last_reason: str = ""       # 最近一次放行时 agent 给的理由（供人在看板核对）
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class SessionBudget:
    """按会话累计 agent 取回的结果量，超额即拒。线程安全。

    状态是进程内的：daemon 常驻，一个 MCP 会话的生命周期不会跨进程；重启即清零
    （重启后 agent 也换了会话，从头计费是对的）。会话数有界，老会话按 FIFO 淘汰。
    """

    def __init__(self, limit_chars: int, max_sessions: int = 512):
        self.limit_chars = limit_chars
        self._sessions: OrderedDict[str, _SessionUsage] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_sessions

    def _entry(self, session_id: str) -> _SessionUsage:
        key = session_id or "-"      # 无会话 id 的客户端合并成一个「匿名会话」
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                entry = _SessionUsage(allowance=self.limit_chars)
                self._sessions[key] = entry
                while len(self._sessions) > self._max:
                    self._sessions.popitem(last=False)
            self._sessions.move_to_end(key)
            return entry

    def check(self, session_id: str) -> None:
        """取数前调用：已超额就抛 ResultBudgetExceeded（附带该怎么办）。"""
        entry = self._entry(session_id)
        if self.limit_chars <= 0:      # 0/负数 = 关闭配额
            return
        if entry.used_chars < entry.allowance:
            return
        raise ResultBudgetExceeded(
            f"本会话累计已返回约 {entry.used_chars:,} 字符"
            f"（≈{int(entry.used_chars / CHARS_PER_TOKEN):,} token，共 {entry.calls} 次取数），"
            f"达到会话结果配额上限。**请先停下来问用户**：是否确认继续这些会消耗大量 token "
            "的查询？说明你还打算查什么、大概还要多少。用户同意后调 "
            "allow_more_results(reason=\"用户已确认：……\") 再放行一个额度，然后继续。\n"
            "在问之前，先想想有没有更省的做法：把聚合写进 SQL 只取结论；"
            "整份数据用 export_table 落文件（用程序下载，别读进上下文）；"
            "多步/跨源处理用 analysis_import + analysis_sql 把计算下推到本地沙箱。"
        )

    def charge(self, session_id: str, chars: int) -> dict:
        """取数后记账，返回该会话的用量快照（含是否已接近上限）。"""
        entry = self._entry(session_id)
        with entry._lock:
            entry.used_chars += max(int(chars), 0)
            entry.calls += 1
            return self._snapshot_of(session_id, entry)

    def grant(self, session_id: str, reason: str = "") -> dict:
        """用户点头后再放行一个额度（在当前已用量之上叠加一份完整预算）。"""
        entry = self._entry(session_id)
        with entry._lock:
            entry.allowance = max(entry.allowance, entry.used_chars) + self.limit_chars
            entry.grants += 1
            entry.last_reason = (reason or "").strip()[:200]
            return self._snapshot_of(session_id, entry)

    def usage(self, session_id: str) -> dict:
        entry = self._entry(session_id)
        return self._snapshot_of(session_id, entry)

    def snapshot(self) -> list[dict]:
        """所有在册会话的用量，用得最多的在前（看板用）。"""
        with self._lock:
            items = list(self._sessions.items())
        out = [self._snapshot_of(sid, e) for sid, e in items]
        out.sort(key=lambda u: u["used_chars"], reverse=True)
        return out

    def _snapshot_of(self, session_id: str, entry: _SessionUsage) -> dict:
        allowance = entry.allowance or self.limit_chars
        pct = int(entry.used_chars * 100 / allowance) if allowance > 0 else 0
        return {
            "session_id": session_id,
            "used_chars": entry.used_chars,
            "used_tokens": int(entry.used_chars / CHARS_PER_TOKEN),
            "calls": entry.calls,
            "allowance_chars": allowance,
            "percent": pct,
            "grants": entry.grants,
            "last_reason": entry.last_reason,
            "enabled": self.limit_chars > 0,
        }


# 用量到这个比例时，在结果末尾提醒一句——别等撞墙才知道，那时 agent 已经白跑一次
WARN_AT_PERCENT = 75


def usage_note(usage: dict) -> str:
    """接近上限时给 agent 的一行提醒；未接近则返回空串。"""
    if not usage.get("enabled") or usage.get("percent", 0) < WARN_AT_PERCENT:
        return ""
    return (f"# budget: 本会话已用 {usage['used_chars']:,} 字符"
            f"（≈{usage['used_tokens']:,} token，{usage['percent']}% 配额，{usage['calls']} 次取数）。"
            "接近上限后将被拒绝取数——请改用聚合/导出文件/分析工作台收窄结果。")
