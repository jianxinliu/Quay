"""操作记录（审计日志）：每次工具调用落一条记录到 SQLite。

记录内容见 DESIGN.md 第七节。密码等敏感信息永不入库。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    agent       TEXT,
    session_id  TEXT,
    project     TEXT NOT NULL,
    connection  TEXT NOT NULL,
    environment TEXT,
    engine      TEXT,
    tool        TEXT NOT NULL,
    sql         TEXT,
    fingerprint TEXT,
    status      TEXT NOT NULL,      -- ok / rejected / error
    detail      TEXT,               -- 拒绝原因或错误消息
    row_count   INTEGER,
    duration_ms INTEGER,
    change_id   INTEGER,           -- 关联的审批单号（写操作才有），供回溯改动与回滚备注
    result_bytes INTEGER           -- 结果集估算字节数（见 metrics.estimate_result_bytes），供看板统计流量
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);
CREATE INDEX IF NOT EXISTS idx_audit_conn ON audit_log (project, connection);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log (session_id);

-- agent 会话元信息：agent 用 begin_session 主动声明会话的名字/简介，
-- 之后同一 MCP 连接会话（session_id 相同）里跑的 SQL 都能按此归类回溯。
-- 只存标识信息，SQL 本身仍在 audit_log 里按 session_id 关联。
CREATE TABLE IF NOT EXISTS agent_session (
    session_id  TEXT PRIMARY KEY,
    agent       TEXT,
    title       TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

# 需人工审批 / 后台旁路写入的工具（其余工具均为只读，不需审批）。
# 审计页「读/写（需审批）」过滤按此集合下推到 SQL。
# execute = agent 写（走审批单）；admin_execute/admin_import/redis_command = 后台旁路写入。
_WRITE_TOOLS = ("execute", "admin_execute", "admin_import", "admin_dcl",
                "redis_command", "sync_write")


def parse_time_filter(value: str | None, end_of_day: bool = False) -> str | None:
    """把用户/agent 传的时间过滤值归一成可与 audit_log.ts 直接比较的 UTC ISO 串。

    audit_log.ts 存的是 UTC ISO，而人和 agent 说的「2026-09-08」是**本地日期**，
    直接拿去比会整体偏移一个时区（东八区会漏掉当天早 8 点前的记录）。
    所以：不带时区的输入按本地时区解释再转 UTC；只给日期的补成当天 00:00:00
    （end_of_day=True 时补 23:59:59.999，用于「截止到这一天」含当天）；
    已带时区偏移的（如 2026-09-08T10:00:00+08:00）按其自身时区转 UTC。
    """
    value = (value or "").strip()
    if not value:
        return None
    date_only = len(value) == 10
    text = value + ("T23:59:59.999" if date_only and end_of_day else "T00:00:00" if date_only else "")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(
            f"时间格式无法解析: {value!r}（用 YYYY-MM-DD 或 ISO 时间，如 2026-09-08T10:00:00）"
        ) from e
    if dt.tzinfo is None:  # 无时区 = 本地时间
        dt = dt.astimezone()
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


@dataclass
class AuditRecord:
    project: str
    connection: str
    tool: str
    status: str  # ok / rejected / error
    agent: str = "unknown"
    session_id: str = ""
    environment: str = ""
    engine: str = ""
    sql: str = ""
    fingerprint: str = ""
    detail: str = ""
    row_count: int | None = None
    duration_ms: int | None = None
    # 关联审批单号：写操作生成审批单时、以及核销执行时都写上，
    # 让「这条审计记录」能直接找回「那张审批单」（含回滚备注）。
    change_id: int | None = None
    # 本次操作从库里取回的结果集估算字节数（写操作无结果集，为 0/None）。
    result_bytes: int | None = None


class AuditStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 老库迁移：change_id 是后加的列（CREATE TABLE IF NOT EXISTS 不会补列）
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(audit_log)")}
            if "change_id" not in cols:
                self._conn.execute("ALTER TABLE audit_log ADD COLUMN change_id INTEGER")
            if "result_bytes" not in cols:
                self._conn.execute("ALTER TABLE audit_log ADD COLUMN result_bytes INTEGER")
            self._conn.commit()

    def record(self, rec: AuditRecord) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO audit_log
                   (ts, agent, session_id, project, connection, environment, engine,
                    tool, sql, fingerprint, status, detail, row_count, duration_ms,
                    change_id, result_bytes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(UTC).isoformat(timespec="milliseconds"),
                    rec.agent,
                    rec.session_id,
                    rec.project,
                    rec.connection,
                    rec.environment,
                    rec.engine,
                    rec.tool,
                    rec.sql,
                    rec.fingerprint,
                    rec.status,
                    rec.detail,
                    rec.row_count,
                    rec.duration_ms,
                    rec.change_id,
                    rec.result_bytes,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    # 可筛选列（等值匹配），下推到 SQL 而非内存过滤
    _FILTERABLE = ("project", "connection", "agent", "status", "session_id")

    def _where(self, filters: dict | None) -> tuple[str, list]:
        clauses, params = [], []
        for col in self._FILTERABLE:
            val = (filters or {}).get(col)
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
            # 排除条件：key 形如 "agent__ne"（审计页默认隐藏 admin-ui 自身操作）
            nev = (filters or {}).get(col + "__ne")
            if nev:
                clauses.append(f"{col} != ?")
                params.append(nev)
        # 读/写（是否需审批）过滤：write=只看写/需审批工具，read=只看只读工具
        rw = (filters or {}).get("rw")
        if rw in ("read", "write"):
            marks = ",".join("?" * len(_WRITE_TOOLS))
            op = "IN" if rw == "write" else "NOT IN"
            clauses.append(f"tool {op} ({marks})")
            params.extend(_WRITE_TOOLS)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def recent(self, limit: int = 100, offset: int = 0, filters: dict | None = None) -> list[dict]:
        where, params = self._where(filters)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self, filters: dict | None = None) -> int:
        where, params = self._where(filters)
        with self._lock:
            row = self._conn.execute(f"SELECT count(*) FROM audit_log{where}", params).fetchone()
        return int(row[0])

    def distinct_values(self, column: str, limit: int = 200) -> list[str]:
        """某列的去重值（供筛选下拉）。列名白名单校验，防注入。"""
        if column not in self._FILTERABLE:
            raise ValueError(f"不可筛选的列: {column}")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT DISTINCT {column} FROM audit_log"
                f" WHERE {column} IS NOT NULL AND {column} <> '' ORDER BY {column} LIMIT ?",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]

    def upsert_session(self, session_id: str, agent: str, title: str, note: str = "") -> None:
        """登记/更新一个 agent 会话的名字与简介（begin_session 调用）。

        按 session_id 幂等 upsert：同一会话重复声明覆盖标题/简介、刷新 updated_at，
        created_at 保留首次值。session_id 为空（如 stdio 无会话 id）时不落库。
        """
        if not session_id:
            return
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        with self._lock:
            self._conn.execute(
                """INSERT INTO agent_session (session_id, agent, title, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       agent=excluded.agent, title=excluded.title,
                       note=excluded.note, updated_at=excluded.updated_at""",
                (session_id, agent, title, note, now, now),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> dict | None:
        """取一个会话的登记信息（begin_session 声明的名字/简介）；没登记过返回 None。"""
        if not session_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_session WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(
        self,
        limit: int = 200,
        agent: str | None = None,
        since: str | None = None,
        until: str | None = None,
        keyword: str | None = None,
        project: str | None = None,
        connection: str | None = None,
        status: str | None = None,
        only_with_writes: bool = False,
    ) -> list[dict]:
        """列出有过操作的 agent 会话（供审计页会话筛选与 agent 自查历史）。

        以 audit_log 里出现过的 session_id 为准（覆盖没调 begin_session 的会话），
        左连 agent_session 取名字/简介；带每会话操作数与首末时间，按最近活动倒序。

        筛选条件都下推到 SQL：
        - since/until：ISO 时间串（UTC，与返回的 ts 同一时区），按操作时间过滤；
        - project/connection/status：只保留在该项目/连接上、或有该结果状态的操作的会话，
          且 ops/writes/首末时间只统计符合条件的那些操作（「我在这个库上成功做成过什么」
          这个问法的自然语义）；
        - keyword：模糊匹配会话名、简介，或该会话跑过的任意一条 SQL；
        - only_with_writes：只保留跑过写操作（需审批的那类工具）的会话。
        """
        marks = ",".join("?" * len(_WRITE_TOOLS))
        clauses, params = ["a.session_id <> ''"], []
        for col, val in (("a.agent", agent), ("a.project", project),
                         ("a.connection", connection), ("a.status", status)):
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
        if since:
            clauses.append("a.ts >= ?")
            params.append(since)
        if until:
            clauses.append("a.ts <= ?")
            params.append(until)
        if keyword:
            like = f"%{keyword}%"
            # SQL 命中用相关子查询而不是直接进 WHERE：否则会把不匹配的行过滤掉，
            # 使 ops/writes 只数到「含关键词的那几条」，与「这个会话有多大」的语义不符。
            clauses.append(
                "(s.title LIKE ? OR s.note LIKE ?"
                " OR EXISTS (SELECT 1 FROM audit_log x"
                "            WHERE x.session_id = a.session_id AND x.sql LIKE ?))"
            )
            params.extend([like, like, like])
        having = " HAVING writes > 0" if only_with_writes else ""
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT a.session_id                    AS session_id,
                           MAX(a.agent)                    AS agent,
                           s.title                         AS title,
                           s.note                          AS note,
                           COUNT(*)                        AS ops,
                           SUM(CASE WHEN a.tool IN ({marks})
                                    THEN 1 ELSE 0 END)     AS writes,
                           MIN(a.ts)                       AS first_ts,
                           MAX(a.ts)                       AS last_ts
                    FROM audit_log a
                    LEFT JOIN agent_session s ON s.session_id = a.session_id
                    WHERE {" AND ".join(clauses)}
                    GROUP BY a.session_id{having}
                    ORDER BY last_ts DESC
                    LIMIT ?""",
                (*_WRITE_TOOLS, *params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 看板统计
    #
    # 看板要的是「一段时间内系统被怎么用了」，全部由 audit_log 聚合下推到 SQL——
    # 不在 Python 里拉全表再统计（审计表按保留期能有几十万行）。
    # 时间参数都是 UTC ISO 串，与 ts 同一格式，可直接字符串比较。

    # 可分组的列白名单（防注入：列名要拼进 SQL，不能来自外部原样字符串）
    _GROUPABLE = ("project", "connection", "tool", "agent", "session_id",
                  "status", "engine", "environment")

    def traffic_summary(self, since: str, until: str | None = None) -> dict:
        """时间窗内的总量：操作数、按状态分布、读出行数/字节、写入影响行数、耗时。

        读/写按 _WRITE_TOOLS 区分：写工具的 row_count 是「影响行数」而非「读出行数」，
        两者混在一起加毫无意义，所以分开统计。
        """
        marks = ",".join("?" * len(_WRITE_TOOLS))
        clauses, params = ["ts >= ?"], [since]
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        where = " AND ".join(clauses)
        with self._lock:
            row = self._conn.execute(
                f"""SELECT COUNT(*)                                              AS ops,
                           SUM(CASE WHEN status='ok'       THEN 1 ELSE 0 END)    AS ok,
                           SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END)    AS rejected,
                           SUM(CASE WHEN status='error'    THEN 1 ELSE 0 END)    AS error,
                           SUM(CASE WHEN tool IN ({marks}) THEN 1 ELSE 0 END)    AS writes,
                           COALESCE(SUM(result_bytes), 0)                        AS bytes_read,
                           COALESCE(SUM(CASE WHEN tool NOT IN ({marks})
                                             THEN row_count ELSE 0 END), 0)      AS rows_read,
                           COALESCE(SUM(CASE WHEN tool IN ({marks}) AND status='ok'
                                             THEN row_count ELSE 0 END), 0)      AS rows_written,
                           COALESCE(SUM(duration_ms), 0)                         AS total_ms,
                           COALESCE(MAX(duration_ms), 0)                         AS max_ms,
                           COUNT(DISTINCT session_id)                            AS sessions,
                           COUNT(DISTINCT project || '/' || connection)          AS connections
                    FROM audit_log WHERE {where}""",
                (*_WRITE_TOOLS, *_WRITE_TOOLS, *_WRITE_TOOLS, *params),
            ).fetchone()
        out = {k: (row[k] or 0) for k in row.keys()}
        out["avg_ms"] = int(out["total_ms"] / out["ops"]) if out["ops"] else 0
        return out

    def traffic_series(self, since: str, bucket: str = "hour", limit: int = 800) -> list[dict]:
        """时间序列（供看板柱图）。按 UTC 的分钟 / 小时 / 天聚合。

        桶标签直接取 ts 的前缀（ts 是 UTC ISO：前 10 位是天、13 位是小时、16 位是分钟），
        前端据此补齐空桶并转成本地时间显示——不在 SQLite 里做时区转换。
        """
        width = {"day": 10, "hour": 13, "minute": 16}.get(bucket)
        if width is None:
            raise ValueError(
                f"不支持的聚合粒度: {bucket!r}（只支持 minute / hour / day）")
        marks = ",".join("?" * len(_WRITE_TOOLS))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT substr(ts, 1, {width})                                 AS bucket,
                           COUNT(*)                                               AS ops,
                           SUM(CASE WHEN status<>'ok' THEN 1 ELSE 0 END)          AS failed,
                           SUM(CASE WHEN tool IN ({marks}) THEN 1 ELSE 0 END)     AS writes,
                           COALESCE(SUM(result_bytes), 0)                         AS bytes_read,
                           COALESCE(SUM(row_count), 0)                            AS rows_total
                    FROM audit_log WHERE ts >= ?
                    GROUP BY bucket ORDER BY bucket LIMIT ?""",
                (*_WRITE_TOOLS, since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def top_groups(self, column: str, since: str, limit: int = 8) -> list[dict]:
        """时间窗内按某列排行（连接/工具/agent/会话），按操作数倒序。"""
        if column not in self._GROUPABLE:
            raise ValueError(f"不可分组的列: {column}")
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT {column}                                AS name,
                           COUNT(*)                                AS ops,
                           SUM(CASE WHEN status<>'ok' THEN 1 ELSE 0 END) AS failed,
                           COALESCE(SUM(result_bytes), 0)          AS bytes_read,
                           COALESCE(SUM(row_count), 0)             AS rows_total,
                           MAX(ts)                                 AS last_ts
                    FROM audit_log
                    WHERE ts >= ? AND {column} IS NOT NULL AND {column} <> ''
                    GROUP BY {column} ORDER BY ops DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_old(self, retention_days: int) -> int:
        """删除超过保留期的审计记录，返回删除条数。"""
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(
            timespec="milliseconds"
        )
        with self._lock:
            cur = self._conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            # 顺手清掉不再被任何审计记录引用的会话元信息（避免 agent_session 无限增长）
            self._conn.execute(
                "DELETE FROM agent_session WHERE session_id NOT IN"
                " (SELECT DISTINCT session_id FROM audit_log)"
            )
            self._conn.commit()
        return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
