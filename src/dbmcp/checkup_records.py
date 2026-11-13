"""数据库体检记录（历史留痕）。

体检是一次性快照：「上周这个库还健康吗」「这条严重项是什么时候出现的」只有存下来
才答得了。整个 report dict 原样存成 JSON——报告结构随检查项演进，按列拆字段就得
跟着改表，得不偿失。

与 workflow_run 同一套生命周期：`Service.checkup_records` 未注入时（单测、库内直用）
体检照常返回，只是不落记录；保留期由 housekeeping 按 created_at 清理。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkup_report (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL DEFAULT '',
    connection     TEXT NOT NULL,
    engine         TEXT NOT NULL DEFAULT '',
    scope          TEXT NOT NULL DEFAULT '',
    overall        TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    problems       INTEGER NOT NULL DEFAULT 0,
    instance_scope INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    report_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_checkup_report_conn
    ON checkup_report (connection, created_at DESC);
"""


class CheckupRecordStore:
    """体检历史（按连接归组，最近在前）。"""

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

    def save(self, project: str, connection: str, report: dict,
             *, instance_scope: bool = False) -> int:
        """存一份体检报告，返回记录 id。

        instance_scope 由调用方给：报告 dict 里没有这个顶层字段（`to_dict` 只导出
        连接/范围/检查项），从 report 里取永远是 False。
        """
        checks = report.get("checks") or []
        problems = sum(1 for c in checks if isinstance(c, dict)
                       and c.get("status") in ("critical", "warn"))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO checkup_report (project, connection, engine, scope, overall,"
                " summary, problems, instance_scope, started_at, created_at, report_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project, connection, str(report.get("engine") or ""),
                 str(report.get("scope") or ""), str(report.get("overall") or ""),
                 str(report.get("summary") or ""), problems,
                 1 if instance_scope else 0,
                 str(report.get("started_at") or ""), now,
                 json.dumps(report, ensure_ascii=False)))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list(self, project: str, connection: str, limit: int = 50,
             offset: int = 0) -> list[dict]:
        """某项目某连接的历史摘要（不含完整 checks，列表页用）。

        必须带 project：不同项目可以有同名连接（都叫 main），只按连接名筛会把
        另一个项目的体检历史串到这个连接下面。
        """
        with self._lock:
            # id 作 tie-breaker：同一秒存的两条 created_at 相同
            rows = self._conn.execute(
                "SELECT id, project, connection, engine, scope, overall, summary, problems,"
                " instance_scope, started_at, created_at FROM checkup_report"
                " WHERE project = ? AND connection = ?"
                " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (project, connection, limit, offset)).fetchall()
        return [_record_row(r) for r in rows]

    def get(self, record_id: int) -> dict | None:
        """单条完整记录（含 report，可原样重新渲染）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM checkup_report WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        out = _record_row(row)
        try:
            out["report"] = json.loads(row["report_json"] or "{}")
        except (ValueError, TypeError):
            out["report"] = None
        return out

    def delete(self, record_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM checkup_report WHERE id = ?", (record_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def purge_older_than(self, days: int) -> int:
        cutoff = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - days * 86400, UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM checkup_report WHERE created_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _record_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "project": row["project"], "connection": row["connection"],
            "engine": row["engine"], "scope": row["scope"], "overall": row["overall"],
            "summary": row["summary"], "problems": row["problems"],
            "instance_scope": bool(row["instance_scope"]),
            "started_at": row["started_at"], "created_at": row["created_at"]}
