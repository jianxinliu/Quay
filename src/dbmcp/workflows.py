"""分析 workflow 存储：工作区取数配方 + 多语句 SQL 脚本，可一键重跑。

定义（JSON 存 SQLite）：
- sources: 保存时从工作区 provenance 自动收集（每个数据集怎么拉的）
- script:  编辑器里的多语句 SQL（分号分隔），运行时逐条执行
运行 = 按 sources 重拉数据 → 逐条执行 script → 最后一个有结果集的语句作为输出预览。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_workflow (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    workspace   TEXT NOT NULL,
    script      TEXT NOT NULL,
    sources     TEXT NOT NULL DEFAULT '[]',   -- JSON：取数配方列表
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


class WorkflowError(Exception):
    """workflow 操作失败。message 面向使用者。"""


@dataclass
class Workflow:
    id: int
    name: str
    workspace: str
    script: str
    sources: list[dict]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "workspace": self.workspace,
                "script": self.script, "sources": self.sources,
                "created_at": self.created_at, "updated_at": self.updated_at}


class WorkflowStore:
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

    def save(self, name: str, workspace: str, script: str, sources: list[dict]) -> Workflow:
        name = (name or "").strip()
        if not name:
            raise WorkflowError("workflow 名称不能为空")
        if not (script or "").strip():
            raise WorkflowError("workflow 脚本不能为空")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO analysis_workflow (name, workspace, script, sources, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET workspace = excluded.workspace,"
                " script = excluded.script, sources = excluded.sources,"
                " updated_at = excluded.updated_at",
                (name, workspace, script, json.dumps(sources, ensure_ascii=False), now, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM analysis_workflow WHERE name = ?", (name,)).fetchone()
        return _row(row)

    def get(self, name: str) -> Workflow:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analysis_workflow WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise WorkflowError(f"workflow {name!r} 不存在")
        return _row(row)

    def list(self) -> list[Workflow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM analysis_workflow ORDER BY updated_at DESC").fetchall()
        return [_row(r) for r in rows]

    def delete(self, name: str) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM analysis_workflow WHERE name = ?", (name,))
            if cur.rowcount == 0:
                raise WorkflowError(f"workflow {name!r} 不存在")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row(row: sqlite3.Row) -> Workflow:
    return Workflow(id=row["id"], name=row["name"], workspace=row["workspace"],
                    script=row["script"], sources=json.loads(row["sources"] or "[]"),
                    created_at=row["created_at"], updated_at=row["updated_at"])


def split_statements(script: str) -> list[str]:
    """按分号切分多条语句（跳过引号与注释），供 workflow 逐条执行。"""
    out, start, i, n = [], 0, 0, len(script)
    while i < n:
        c = script[i]
        two = script[i:i + 2]
        if c in ("'", '"'):
            q = c
            i += 1
            while i < n:
                if script[i] == "\\":
                    i += 2
                    continue
                if script[i] == q:
                    if q == "'" and script[i + 1:i + 2] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif two == "--":
            while i < n and script[i] != "\n":
                i += 1
        elif two == "/*":
            i += 2
            while i < n and script[i:i + 2] != "*/":
                i += 1
            i += 2
        elif c == ";":
            stmt = script[start:i].strip()
            if stmt:
                out.append(stmt)
            i += 1
            start = i
        else:
            i += 1
    tail = script[start:].strip()
    if tail:
        out.append(tail)
    return out
