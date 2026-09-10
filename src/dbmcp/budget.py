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

import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- token 估算
#
# **配额本身以字符为单位强制执行**——字符数是确定的、可复现的，不依赖任何模型的分词器。
# token 数只是给人和 agent 看的注解，用来回答「这大概值多少上下文」。
#
# 但用一个固定除数去换算是不准的：同样 1000 个字符，纯英文大约 250 token，
# 纯中文能到 700–1000。所以这里按**字符类别**分别估：
#   - ASCII（英文、数字、SQL、制表符）≈ 4 字符 / token
#   - 非 ASCII（中日韩等）≈ 1.2 字符 / token
# 这仍然是估算，不是分词结果——要精确只能调对应厂商的分词器/计数接口，
# 而为了在结果末尾显示一行提示去做一次网络调用并不划算。
CHARS_PER_TOKEN_ASCII = 4.0
CHARS_PER_TOKEN_WIDE = 1.2

# 没有原文、只有一个字符数上限时（设置页把配额换算成 token）用的粗略除数。
# 取两者之间偏英文的值——配额多半是被 SQL 结果占满的，而结果以 ASCII 为主。
CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """按字符类别估算 token 数。见上面的说明：是估算，不是分词。"""
    if not text:
        return 0
    wide = sum(1 for ch in text if ord(ch) > 0x7F)
    ascii_n = len(text) - wide
    return int(ascii_n / CHARS_PER_TOKEN_ASCII + wide / CHARS_PER_TOKEN_WIDE)


# ---------------------------------------------------------------- 真实分词计数
#
# 上面的启发式对**中英文混排**已经够用，但对本服务最常见的内容——查询结果 TSV——
# 会少报近一半：制表符、纯数字 id、短字段各自成 token，密度远高于「4 字符/token」。
# 实测一份 43KB 的结果集：真实 16131 token，启发式只估出 8000 上下。
#
# 所以装了 tiktoken 就用真实分词（o200k_base）。它是 GPT-4o 的分词器、不是 Claude 的，
# 但同为现代 BPE、中文处理正确，比启发式接近得多；43KB 编码只要 1.5ms。
# 装不上或词表下不来（离线/代理）就回退启发式，并在界面上标明是粗估——
# **绝不能因为一个注解性的数字把取数路径拖住或弄挂**。
TOKENIZER_ENCODING = "o200k_base"

_encoder: object | None = None
_encoder_tried = False
_encoder_lock = threading.Lock()


def set_tokenizer_cache_dir(path: str) -> None:
    """把 tiktoken 的词表缓存钉到给定目录（daemon 传数据目录）。

    默认缓存在 `TMPDIR/data-gym-cache`，而 macOS 会清理 TMPDIR——那意味着
    每隔一段时间就要重新下一次 3.4MB 词表。必须在首次加载编码器之前调用。
    """
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", path)


def _get_encoder():  # noqa: ANN202
    """惰性取分词器；取不到就永久回退（不每次调用都重试，免得反复等网络）。"""
    global _encoder, _encoder_tried
    if _encoder_tried:
        return _encoder
    with _encoder_lock:
        if _encoder_tried:
            return _encoder
        try:
            import tiktoken  # noqa: PLC0415

            _encoder = tiktoken.get_encoding(TOKENIZER_ENCODING)
        except Exception:  # noqa: BLE001 - 没装、下不到词表、词表损坏都退回启发式
            logger.info("tiktoken 不可用，token 数改用字符类别估算", exc_info=True)
            _encoder = None
        finally:
            _encoder_tried = True
    return _encoder


def warm_tokenizer() -> bool:
    """预热分词器，返回是否可用。daemon 启动时在后台线程调一次——

    首次加载要下 3.4MB 词表，不该让某一次查询替所有人承担这几秒。
    """
    return _get_encoder() is not None


def tokenizer_ready() -> bool:
    """分词器是否已就绪（决定界面上写「token」还是「≈token（粗估）」）。"""
    return _encoder is not None


def count_tokens(text: str) -> int:
    """文本的 token 数：装了 tiktoken 走真实分词，否则回退字符类别估算。"""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is None:
        return estimate_tokens(text)
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception:  # noqa: BLE001 - 分词失败不能影响取数
        return estimate_tokens(text)


class ResultBudgetExceeded(Exception):
    """会话累计返回量超出配额。对 agent 是「去问用户」而不是「换个写法重试」。"""


@dataclass
class _SessionUsage:
    used_chars: int = 0
    used_tokens: int = 0        # 按实际文本估的累计 token（见 estimate_tokens）
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
            f"（{'' if tokenizer_ready() else '≈'}{entry.used_tokens:,} token，"
            f"共 {entry.calls} 次取数），"
            f"达到会话结果配额上限。**请先停下来问用户**：是否确认继续这些会消耗大量 token "
            "的查询？说明你还打算查什么、大概还要多少。用户同意后调 "
            "allow_more_results(reason=\"用户已确认：……\") 再放行一个额度，然后继续。\n"
            "在问之前，先想想有没有更省的做法：把聚合写进 SQL 只取结论；"
            "整份数据用 export_table 落文件（用程序下载，别读进上下文）；"
            "多步/跨源处理用 analysis_import + analysis_sql 把计算下推到本地沙箱。"
        )

    def charge(self, session_id: str, text: str) -> dict:
        """取数后记账，返回该会话的用量快照（含是否已接近上限）。

        收的是**原文**而不是字符数：配额按字符算（确定、可复现），
        但同时要按字符类别估一份 token 数——中英文的 token 密度差三倍，
        拿一个固定除数换算出来的数字会误导人。
        """
        entry = self._entry(session_id)
        with entry._lock:
            entry.used_chars += len(text or "")
            entry.used_tokens += count_tokens(text or "")
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
            "used_tokens": entry.used_tokens,
            "calls": entry.calls,
            "allowance_chars": allowance,
            "percent": pct,
            "grants": entry.grants,
            "last_reason": entry.last_reason,
            "enabled": self.limit_chars > 0,
            # 界面据此决定写「N token」还是「≈N token（粗估）」——
            # 不能把估算值伪装成精确计数
            "tokens_exact": tokenizer_ready(),
        }


# 用量到这个比例时，在结果末尾提醒一句——别等撞墙才知道，那时 agent 已经白跑一次
WARN_AT_PERCENT = 75


def usage_note(usage: dict) -> str:
    """接近上限时给 agent 的一行提醒；未接近则返回空串。"""
    if not usage.get("enabled") or usage.get("percent", 0) < WARN_AT_PERCENT:
        return ""
    tilde = "" if usage.get("tokens_exact") else "≈"
    return (f"# budget: 本会话已用 {usage['used_chars']:,} 字符"
            f"（{tilde}{usage['used_tokens']:,} token，{usage['percent']}% 配额，"
            f"{usage['calls']} 次取数）。"
            "接近上限后将被拒绝取数——请改用聚合/导出文件/分析工作台收窄结果。")
