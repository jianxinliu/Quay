"""管理后台的站内通知收件箱：SQLite `notification` 表 + Notifier 适配。

设计要点：
- 内推是默认渠道（`InboxNotifier` 始终启用，不可关），发一条即写一行
- 保留策略：7 天自动清（housekeep_once 里跑）
- SSE 推给已认证的后台页面（铃铛角标 + 下拉列表）
- 与审批/审计/片段共用 dbm.sqlite3，独立表
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .notify import Notifier

DEFAULT_RETENTION_DAYS = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- approval_created / connection_exhausted / …
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    meta        TEXT NOT NULL,      -- JSON: {change_id, project, connection, risk_level, ...}
    read_at     TEXT                -- NULL 表示未读
);
CREATE INDEX IF NOT EXISTS idx_notification_read ON notification (read_at);
CREATE INDEX IF NOT EXISTS idx_notification_created ON notification (created_at);
"""


@dataclass
class Notification:
    id: int
    created_at: str
    kind: str
    title: str
    body: str
    meta: dict
    read_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class InboxStore:
    """SQLite 存储 + 内存 fan-out（SSE 订阅者）。"""

    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # SSE 订阅：每个订阅者一个内存队列；有新通知时并发投递
        self._subs: set[queue.Queue] = set()
        self._subs_lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- CRUD ----------

    def add(self, kind: str, title: str, body: str, meta: dict | None = None) -> Notification:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO notification (created_at, kind, title, body, meta)"
                " VALUES (?, ?, ?, ?, ?)",
                (now, kind, title, body, meta_json),
            )
            self._conn.commit()
            nid = int(cur.lastrowid or 0)
        n = Notification(id=nid, created_at=now, kind=kind,
                         title=title, body=body, meta=meta or {})
        self._fanout(n)
        return n

    def list_recent(self, limit: int = 20, unread_only: bool = False) -> list[Notification]:
        clause = "WHERE read_at IS NULL " if unread_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM notification {clause}ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_notification(r) for r in rows]

    def unread_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM notification WHERE read_at IS NULL"
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_read(self, ids: list[int]) -> int:
        """把指定 id 标已读，返回受影响行数。空列表则 no-op。"""
        if not ids:
            return 0
        now = datetime.now(UTC).isoformat(timespec="seconds")
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE notification SET read_at = ?"
                f" WHERE id IN ({placeholders}) AND read_at IS NULL",
                (now, *ids),
            )
            self._conn.commit()
        return cur.rowcount

    def mark_all_read(self) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE notification SET read_at = ? WHERE read_at IS NULL", (now,))
            self._conn.commit()
        return cur.rowcount

    def purge_old(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM notification WHERE created_at < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        # 关订阅：给每个队列投一个 sentinel 让 SSE 环退出
        with self._subs_lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(None)
                except Exception:  # noqa: BLE001
                    pass
            self._subs.clear()
        with self._lock:
            self._conn.close()

    # ---------- SSE 订阅 ----------

    def subscribe(self) -> queue.Queue:
        """SSE 处理器订阅：返回一个队列，之后 `.get()` 会拿到新通知（或 None sentinel 退出）。

        队列容量 100 条，满了丢最老（避免慢消费者拖爆内存）。
        """
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._subs_lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            self._subs.discard(q)

    def _fanout(self, n: Notification) -> None:
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(n)
            except queue.Full:
                # 慢消费者：丢一条最老的再塞新的
                try:
                    q.get_nowait()
                    q.put_nowait(n)
                except Exception:  # noqa: BLE001
                    pass

    def _row_to_notification(self, r: sqlite3.Row) -> Notification:
        try:
            meta = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return Notification(
            id=int(r["id"]),
            created_at=str(r["created_at"]),
            kind=str(r["kind"]),
            title=str(r["title"]),
            body=str(r["body"]),
            meta=meta,
            read_at=str(r["read_at"] or ""),
        )


class InboxNotifier(Notifier):
    """把通知写进 InboxStore（同时触发 SSE fan-out）。

    与其他 Notifier 一样，send() 不抛异常；异常吞掉写 stderr。
    """

    def __init__(self, store: InboxStore):
        self._store = store

    def send(self, title: str, body: str, meta: dict | None = None) -> None:
        kind = (meta or {}).get("kind") or "info"
        try:
            self._store.add(kind=str(kind), title=title, body=body, meta=meta or {})
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("inbox add failed")
