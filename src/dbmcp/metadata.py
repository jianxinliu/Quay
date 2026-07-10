"""元数据缓存：schema / 索引 / 表行数量级，缓存到 SQLite，供风险评估与审批展示。

缓存策略：按需拉取 + TTL。表结构变化不频繁，默认缓存 1 小时；
风险评估需要"对库的认知"（表多大、有没有索引），但不需要实时精确。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import engines
from .config import ConnectionConfig

DEFAULT_TTL_S = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS table_meta (
    project     TEXT NOT NULL,
    connection  TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    payload     TEXT NOT NULL,     -- JSON: columns / indexes / primary_key / row_estimate
    fetched_at  REAL NOT NULL,     -- time.time()
    PRIMARY KEY (project, connection, table_name)
);
"""


@dataclass
class TableMeta:
    table: str
    columns: list[dict]
    indexes: list[dict]
    primary_key: list[str]
    row_estimate: int | None
    fetched_at: float

    @property
    def indexed_columns(self) -> set[str]:
        cols: set[str] = set(self.primary_key)
        for idx in self.indexes:
            for c in idx.get("columns") or []:
                if c:
                    cols.add(c)
        return cols


class MetadataCache:
    """(project, connection, table) -> TableMeta，带 TTL 的 SQLite 缓存。"""

    def __init__(self, db_path: str | Path, pool: engines.EnginePool, ttl_s: int = DEFAULT_TTL_S):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._pool = pool
        self._ttl_s = ttl_s
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def get(
        self,
        project: str,
        connection: str,
        cfg: ConnectionConfig,
        table: str,
        *,
        refresh: bool = False,
    ) -> TableMeta:
        if not refresh:
            cached = self._read(project, connection, table)
            if cached is not None and (time.time() - cached.fetched_at) < self._ttl_s:
                return cached
        return self._refresh(project, connection, cfg, table)

    def _refresh(
        self, project: str, connection: str, cfg: ConnectionConfig, table: str
    ) -> TableMeta:
        engine = self._pool.get(project, connection, cfg)
        payload = engines.collect_table_meta(engine, cfg.engine, table)
        fetched_at = time.time()
        meta = TableMeta(
            table=table,
            columns=payload["columns"],
            indexes=payload["indexes"],
            primary_key=payload["primary_key"],
            row_estimate=payload.get("row_estimate"),
            fetched_at=fetched_at,
        )
        self._write(project, connection, table, payload, fetched_at)
        return meta

    def _read(self, project: str, connection: str, table: str) -> TableMeta | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fetched_at FROM table_meta"
                " WHERE project = ? AND connection = ? AND table_name = ?",
                (project, connection, table),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return TableMeta(
            table=table,
            columns=payload["columns"],
            indexes=payload["indexes"],
            primary_key=payload["primary_key"],
            row_estimate=payload.get("row_estimate"),
            fetched_at=row["fetched_at"],
        )

    def _write(
        self, project: str, connection: str, table: str, payload: dict, fetched_at: float
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO table_meta"
                " (project, connection, table_name, payload, fetched_at) VALUES (?, ?, ?, ?, ?)",
                (project, connection, table, json.dumps(payload, ensure_ascii=False), fetched_at),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
