"""SQL 片段库：把常用 SQL 存进本服务的 SQLite，可加载回查询台即执行。

面向已认证的后台使用者（非 agent）；每条片段带标题、备注，以及保存时所用连接
（加载时预选，可为空）。与审计/审批共用同一个 dbm.sqlite3 文件、各自独立表。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sql_snippet (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    sql         TEXT NOT NULL,
    connection  TEXT NOT NULL DEFAULT '',   -- "project/connection"，加载时预选，可空
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snippet_updated ON sql_snippet (updated_at DESC);
"""


class SnippetError(Exception):
    """片段操作失败（不存在 / 参数非法）。message 面向使用者。"""


@dataclass
class Snippet:
    id: int
    title: str
    note: str
    sql: str
    connection: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "note": self.note,
            "sql": self.sql,
            "connection": self.connection,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SnippetStore:
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

    def create(self, title: str, sql: str, note: str = "", connection: str = "") -> Snippet:
        title = (title or "").strip()
        sql = (sql or "").strip()
        if not title:
            raise SnippetError("片段标题不能为空")
        if not sql:
            raise SnippetError("片段 SQL 不能为空")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sql_snippet (title, note, sql, connection, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (title, note or "", sql, connection or "", now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sql_snippet WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _row_to_snippet(row)

    def update(
        self, snippet_id: int, title: str, sql: str, note: str = "", connection: str = ""
    ) -> Snippet:
        title = (title or "").strip()
        sql = (sql or "").strip()
        if not title:
            raise SnippetError("片段标题不能为空")
        if not sql:
            raise SnippetError("片段 SQL 不能为空")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sql_snippet SET title = ?, note = ?, sql = ?, connection = ?, updated_at = ?"
                " WHERE id = ?",
                (title, note or "", sql, connection or "", now, snippet_id),
            )
            if cur.rowcount == 0:
                raise SnippetError(f"片段 #{snippet_id} 不存在")
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sql_snippet WHERE id = ?", (snippet_id,)
            ).fetchone()
        return _row_to_snippet(row)

    def get(self, snippet_id: int) -> Snippet:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sql_snippet WHERE id = ?", (snippet_id,)
            ).fetchone()
        if row is None:
            raise SnippetError(f"片段 #{snippet_id} 不存在")
        return _row_to_snippet(row)

    def list(self) -> list[Snippet]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sql_snippet ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [_row_to_snippet(r) for r in rows]

    def delete(self, snippet_id: int) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sql_snippet WHERE id = ?", (snippet_id,))
            if cur.rowcount == 0:
                raise SnippetError(f"片段 #{snippet_id} 不存在")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_snippet(row: sqlite3.Row) -> Snippet:
    return Snippet(
        id=row["id"],
        title=row["title"],
        note=row["note"],
        sql=row["sql"],
        connection=row["connection"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
