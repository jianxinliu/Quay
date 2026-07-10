"""操作记录（审计日志）：每次工具调用落一条记录到 SQLite。

记录内容见 DESIGN.md 第七节。密码等敏感信息永不入库。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
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

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
