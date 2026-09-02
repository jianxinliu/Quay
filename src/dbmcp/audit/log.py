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
    duration_ms INTEGER
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
            self._conn.commit()

    def record(self, rec: AuditRecord) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO audit_log
                   (ts, agent, session_id, project, connection, environment, engine,
                    tool, sql, fingerprint, status, detail, row_count, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

    def list_sessions(self, limit: int = 200, agent: str | None = None) -> list[dict]:
        """列出有过操作的 agent 会话（供审计页会话筛选/回溯）。

        以 audit_log 里出现过的 session_id 为准（覆盖没调 begin_session 的会话），
        左连 agent_session 取名字/简介；带每会话操作数与首末时间，按最近活动倒序。
        """
        clause, params = "a.session_id <> ''", []
        if agent:
            clause += " AND a.agent = ?"
            params.append(agent)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT a.session_id                    AS session_id,
                           MAX(a.agent)                    AS agent,
                           s.title                         AS title,
                           s.note                          AS note,
                           COUNT(*)                        AS ops,
                           SUM(CASE WHEN a.tool IN ({",".join("?" * len(_WRITE_TOOLS))})
                                    THEN 1 ELSE 0 END)     AS writes,
                           MIN(a.ts)                       AS first_ts,
                           MAX(a.ts)                       AS last_ts
                    FROM audit_log a
                    LEFT JOIN agent_session s ON s.session_id = a.session_id
                    WHERE {clause}
                    GROUP BY a.session_id
                    ORDER BY last_ts DESC
                    LIMIT ?""",
                (*_WRITE_TOOLS, *params, limit),
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
