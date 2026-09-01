"""数据库/驱动异常 → 分类化、已脱敏的错误消息（面向 agent 与工具返回值）。

**为什么要有这一层**：驱动异常直接冒泡给 agent 有三个问题——
1. 泄漏内部细节：SQLAlchemy 会把 DSN（含 `user:password@host`）、完整 SQL、绑定参数、
   `(Background on this error at: https://sqlalche.me/e/...)` 一并拼进消息，
   违反「密码永不出现在日志/审计/工具返回值中」红线；
2. 对 agent 无法行动：`(pymysql.err.ProgrammingError) (1064, "You have an error in your
   SQL syntax; check the manual that corresponds to your MySQL server version …")`
   这种噪音让模型很难判断该改 SQL 还是该重试；
3. 未被捕获的异常会变成传输层 500 / 原始 traceback。

本模块把常见错误按**驱动错误码优先、消息片段兜底**归成有限几类，配一句「该怎么办」，
并剥掉所有敏感/无用片段。纯函数，无 IO，好单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 消息里必须剥掉的片段（顺序相关：先剥块状再剥行内）
_SQLALCHEMY_BACKGROUND_RE = re.compile(r"\s*\(Background on this error at:[^)]*\)", re.I)
# SQLAlchemy 把原 SQL 与绑定参数附在消息尾部：`[SQL: ...]` / `[parameters: ...]`
# （二者恒在末尾）。参数里可能含明文密码/身份证等，且 agent 本就知道自己发了什么 SQL
# —— 从第一个标记起整段截掉。
_SQL_ECHO_RE = re.compile(r"\s*\[(?:SQL|parameters|cached since)[:\s][\s\S]*$", re.I)
# DSN 里的账号密码：scheme://user:password@host → scheme://***@host
_DSN_CRED_RE = re.compile(r"([a-z0-9+.\-]+://)[^\s/@:]+(?::[^\s/@]*)?@", re.I)
# 驱动异常类名前缀，如 "(pymysql.err.OperationalError) " / "(psycopg.errors.UndefinedTable) "。
# psycopg3 的异常类名**多数不以 Error 结尾**（UndefinedTable / InsufficientPrivilege /
# AdminShutdown…），只按 `Error|Exception` 结尾匹配会让 PG 的错误一律带着驱动类名外发。
# 故分两支：带模块路径的点分类名一律剥；无路径的裸类名仍要求以 Error/Exception 结尾
# （避免误伤 `(SELECT …)` 这类真的以括号开头的内容）。
_DRIVER_PREFIX_RE = re.compile(
    r"^\((?:[A-Za-z_]\w*\.)+[A-Z]\w*\)\s*"
    r"|^\(\w*(?:Error|Exception)\)\s*"
)
# PG 把出错位置画成单独一行的插入符（`      ^`）。换行折叠后这个符号失去对位意义、纯噪音。
_CARET_LINE_RE = re.compile(r"^[ \t]*\^[ \t]*$", re.M)
# 形如 `(1064, "...")` / `(3024, '...')` 的错误码元组
_CODE_TUPLE_RE = re.compile(r"^\((\d{3,5}),\s*[\"'](.*)[\"']\)\s*$", re.S)

MAX_MESSAGE_CHARS = 600


@dataclass(frozen=True)
class DbErrorInfo:
    """一次 DB 失败的归类结果。kind 是稳定的机器可读标签，agent 可据此决定行为。"""

    kind: str
    message: str   # 已脱敏的原始错误摘要
    hint: str      # 一句「该怎么办」

    def as_text(self) -> str:
        """拼成给 agent 的一行文案：`[kind] 摘要。建议：…`"""
        out = f"[{self.kind}] {self.message}"
        if self.hint:
            out += f" 建议：{self.hint}"
        return out


# 分类规则：(kind, 各引擎错误码集合, 消息片段, hint)
# 匹配顺序即优先级——先码后消息、先具体后笼统。
_RULES: tuple[tuple[str, frozenset[int], tuple[str, ...], str], ...] = (
    (
        "sql_syntax_error",
        frozenset({1064, 1149}),              # MySQL
        ("syntax error at or near",           # PostgreSQL
         "error in your sql syntax",           # MySQL 1064 原文
         "syntax error",                      # SQLite / 通用
         "code: 62"),                         # ClickHouse SYNTAX_ERROR
        "SQL 语法有误，请对照目标库方言修正后重试；不要原样重发。",
    ),
    (
        "table_not_found",
        frozenset({1146, 1051}),
        ("doesn't exist", "does not exist", "no such table", "code: 60"),
        "先用 list_tables / describe_table 确认表名与库名（未绑定默认库时用「库名.表名」）。",
    ),
    (
        "column_not_found",
        frozenset({1054}),
        ("unknown column", "no such column", "code: 47"),
        "用 describe_table 确认列名后重写 SQL。",
    ),
    (
        "unknown_database",
        frozenset({1049}),
        ("unknown database", "database \"", "code: 81"),
        "用 list_databases 确认库名。",
    ),
    (
        "permission_denied",
        frozenset({1142, 1143, 1044, 1045}),
        ("permission denied", "access denied", "command denied", "code: 497"),
        "当前账号无此权限。只读账号不能写；写操作请走 execute 的审批流程。",
    ),
    (
        "readonly_violation",
        frozenset({1792}),
        ("read only transaction", "read-only transaction", "readonly mode", "code: 164"),
        "该连接的只读账号禁止写操作。数据变更请用 execute 工具走审批流程。",
    ),
    (
        "query_timeout",
        frozenset({3024}),
        ("maximum statement execution time exceeded",
         "canceling statement due to statement timeout",
         "query execution was interrupted",
         "read operation timed out",
         "code: 159"),
        "查询超时。请用 WHERE 收窄范围、加索引命中的过滤条件，或改用聚合/分析工作台下推计算。",
    ),
    (
        "query_canceled",
        frozenset(),
        ("canceling statement due to user request",   # PG：pg_cancel_backend（查询台取消/管理员中断）
         "query was cancelled"),
        "查询被取消（人工取消或管理员中断），不是 SQL 本身的问题；需要结果可重发。",
    ),
    (
        "deadlock",
        frozenset({1213, 1614}),
        ("deadlock",),
        "发生死锁，可稍后重试；反复出现请缩小事务范围。",
    ),
    (
        "lock_timeout",
        frozenset({1205}),
        ("lock wait timeout", "could not obtain lock"),
        "等锁超时，稍后重试；长事务占锁时需人工介入。",
    ),
    (
        "duplicate_key",
        frozenset({1062}),
        ("duplicate entry", "duplicate key value"),
        "唯一键冲突，检查待写入数据或改用 UPSERT 语义。",
    ),
    (
        "constraint_violation",
        frozenset({1451, 1452, 1048}),
        ("foreign key constraint", "violates foreign key", "cannot be null",
         "violates not-null"),
        "违反约束（外键/非空）。先确认关联数据与必填列。",
    ),
    (
        "data_too_long",
        frozenset({1406, 1264}),
        ("data too long", "out of range value", "value too long"),
        "值超出列定义范围，检查数据或列类型。",
    ),
    (
        "result_too_large",
        frozenset(),
        ("result set too large", "memory limit", "code: 241"),
        "结果集/内存超限。用聚合或 LIMIT 收窄，或改用分析工作台。",
    ),
)


# PostgreSQL 的 SQLSTATE → 分类。**比消息片段可靠得多**，故排在所有规则之前：
# PG 的「列不存在」与「表不存在」消息高度雷同——`column "x" does not exist` /
# `relation "x" does not exist`——只按 "does not exist" 片段判会把列错误误判成表错误
# （真机实测过）。psycopg 的异常对象带 .sqlstate，直接取。
# 只列能稳定映射到既有分类的；未列出的仍走错误码 / 消息片段兜底。
_PG_SQLSTATES: dict[str, str] = {
    "42601": "sql_syntax_error",      # syntax_error
    "42P01": "table_not_found",       # undefined_table
    "42703": "column_not_found",      # undefined_column
    "42501": "permission_denied",     # insufficient_privilege
    "28000": "permission_denied",     # invalid_authorization_specification
    "28P01": "permission_denied",     # invalid_password
    "25006": "readonly_violation",    # read_only_sql_transaction
    "40P01": "deadlock",              # deadlock_detected
    "55P03": "lock_not_available",    # lock_not_available
    "23505": "duplicate_key",         # unique_violation
    "23502": "constraint_violation",  # not_null_violation
    "23503": "constraint_violation",  # foreign_key_violation
    "23514": "constraint_violation",  # check_violation
    "22001": "data_too_long",         # string_data_right_truncation
    "22003": "data_too_long",         # numeric_value_out_of_range
    "3D000": "unknown_database",      # invalid_catalog_name
    "3F000": "unknown_database",      # invalid_schema_name
    "53200": "result_too_large",      # out_of_memory
    "53400": "result_too_large",      # configuration_limit_exceeded
}
# 57014 (query_canceled) 有意不入表：语句超时与人工取消共用这个 SQLSTATE，
# 要靠消息文案区分（"statement timeout" vs "user request"），交给下面的消息规则。


# 分类 → 给人看的中文标签（管理后台查询台用；agent 侧仍用机器可读的 kind）
_KIND_LABELS: dict[str, str] = {
    "sql_syntax_error": "SQL 语法错误",
    "table_not_found": "表不存在",
    "column_not_found": "列不存在",
    "unknown_database": "库/schema 不存在",
    "permission_denied": "权限不足",
    "readonly_violation": "只读连接禁止写入",
    "query_timeout": "查询超时",
    "query_canceled": "查询已取消",
    "deadlock": "死锁",
    "lock_timeout": "等锁超时",
    "duplicate_key": "唯一键冲突",
    "constraint_violation": "违反约束",
    "data_too_long": "值超出列范围",
    "result_too_large": "结果集/内存超限",
}


def error_label(kind: str) -> str:
    """分类标签的中文名；未知分类返回空串（调用方据此决定不加前缀）。"""
    return _KIND_LABELS.get(kind, "")


def pg_sqlstate(exc: BaseException) -> str:
    """沿异常链取 psycopg 的 SQLSTATE（5 位字符串）；取不到返回空串。"""
    for cur in _causes(exc):
        state = getattr(cur, "sqlstate", None)
        if isinstance(state, str) and state:
            return state
    return ""


def sanitize_db_message(raw: str) -> str:
    """剥掉驱动异常消息里的敏感与噪音片段，返回可安全外发的一行摘要。

    剥除：DSN 中的账号密码、SQLAlchemy 附加的 `[SQL: …]`/`[parameters: …]`/背景链接、
    驱动类名前缀、多余空白与换行。保留错误码与人类可读描述。
    """
    text = (raw or "").strip()
    text = _SQL_ECHO_RE.sub("", text)
    text = _SQLALCHEMY_BACKGROUND_RE.sub("", text)
    text = _CARET_LINE_RE.sub("", text)
    text = _DRIVER_PREFIX_RE.sub("", text.strip())
    m = _CODE_TUPLE_RE.match(text.strip())
    if m:
        # `(1064, "You have an error ...")` → `1064: You have an error ...`
        text = f"{m.group(1)}: {m.group(2)}"
    text = _DSN_CRED_RE.sub(r"\1***@", text)
    text = re.sub(r"\s*\n\s*", " ", text).strip()
    text = re.sub(r"\s{2,}", " ", text)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS].rstrip() + "…"
    return text


# 驱动错误码在消息里的形态：`(1064, "…")`——SQLAlchemy 会把它嵌在自己的包装文案中间
_CODE_IN_TEXT_RE = re.compile(r"\((\d{3,5}),\s*[\"\']")


def _error_code(exc: BaseException) -> int | None:
    """从驱动异常里抽错误码：pymysql/psycopg 的 args[0]，或消息中的 `(1064, "…")`。"""
    for cur in _causes(exc):
        args = getattr(cur, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
        m = _CODE_IN_TEXT_RE.search(str(cur))
        if m:
            return int(m.group(1))
    return None


def _causes(exc: BaseException, limit: int = 8):
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < limit:
        yield cur
        cur = cur.__cause__ or cur.__context__
        seen += 1


def classify_db_error(exc: BaseException) -> str:
    """只返回分类标签（不组装文案），供审计/统计使用。"""
    state = pg_sqlstate(exc)
    if state in _PG_SQLSTATES:
        return _PG_SQLSTATES[state]
    code = _error_code(exc)
    low = " ".join(str(c) for c in _causes(exc)).lower()
    # PG 的「库不存在」与「表不存在」消息都以 does not exist 结尾，而 table_not_found
    # 在规则表里排在前面 → 会把 `FATAL: database "root" does not exist` 报成「表不存在」
    # （真机 drama-u 撞到）。建连阶段失败的异常往往连 sqlstate 都没有，只能按消息认。
    if 'database "' in low and "does not exist" in low:
        return "unknown_database"
    for kind, codes, parts, _hint in _RULES:
        if code is not None and code in codes:
            return kind
        if any(p in low for p in parts):
            return kind
    return "db_error"


def translate_db_error(exc: BaseException) -> DbErrorInfo:
    """把驱动异常翻译成分类化 + 已脱敏的错误信息。

    未识别的错误归入 `db_error`，仍然会脱敏并保留摘要——不静默吞掉，
    但也绝不把 traceback / DSN / 绑定参数外发。
    """
    kind = classify_db_error(exc)
    message = sanitize_db_message(str(exc)) or type(exc).__name__
    hint = ""
    for k, _codes, _parts, h in _RULES:
        if k == kind:
            hint = h
            break
    if kind == "db_error":
        hint = "这是数据库返回的错误，重发相同 SQL 通常仍会失败；请据错误信息调整。"
    return DbErrorInfo(kind=kind, message=message, hint=hint)
