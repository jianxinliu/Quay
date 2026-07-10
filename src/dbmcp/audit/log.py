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
"""


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
    _FILTERABLE = ("project", "connection", "agent", "status")

    def _where(self, filters: dict | None) -> tuple[str, list]:
        clauses, params = [], []
        for col in self._FILTERABLE:
            val = (filters or {}).get(col)
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
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

    def purge_old(self, retention_days: int) -> int:
        """删除超过保留期的审计记录，返回删除条数。"""
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(
            timespec="milliseconds"
        )
        with self._lock:
            cur = self._conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
