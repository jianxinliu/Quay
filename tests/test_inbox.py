"""InboxStore + InboxNotifier：CRUD、未读计数、SSE 订阅 fan-out、7 天保留。"""

from __future__ import annotations

import queue
from datetime import UTC, datetime, timedelta

import pytest

from dbmcp.inbox import InboxNotifier, InboxStore


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "inbox.sqlite3")
    yield s
    s.close()


class TestBasicCRUD:
    def test_add_and_list(self, store):
        n1 = store.add("approval_created", "T1", "B1", {"change_id": 42})
        assert n1.id > 0 and n1.title == "T1"
        n2 = store.add("connection_exhausted", "T2", "B2")
        items = store.list_recent(limit=10)
        assert [n.id for n in items] == [n2.id, n1.id]  # 倒序

    def test_unread_count(self, store):
        store.add("k", "a", "b")
        store.add("k", "c", "d")
        assert store.unread_count() == 2

    def test_mark_read_updates_count(self, store):
        n1 = store.add("k", "a", "b")
        n2 = store.add("k", "c", "d")
        updated = store.mark_read([n1.id])
        assert updated == 1
        assert store.unread_count() == 1
        # 再标同一条不会再动
        assert store.mark_read([n1.id]) == 0
        assert store.mark_read([n2.id]) == 1
        assert store.unread_count() == 0

    def test_mark_all_read(self, store):
        for i in range(5):
            store.add("k", f"t{i}", "b")
        assert store.mark_all_read() == 5
        assert store.unread_count() == 0

    def test_list_unread_only(self, store):
        n1 = store.add("k", "a", "b")
        store.add("k", "c", "d")
        store.mark_read([n1.id])
        items = store.list_recent(limit=10, unread_only=True)
        assert len(items) == 1 and items[0].title == "c"

    def test_meta_roundtrip(self, store):
        store.add("approval_created", "t", "b", {"change_id": 7, "risk_level": "HIGH"})
        n = store.list_recent(1)[0]
        assert n.meta == {"change_id": 7, "risk_level": "HIGH"}


class TestPurge:
    def test_purge_old_removes_beyond_retention(self, store):
        # 手工把一条改成 10 天前
        n = store.add("k", "old", "b")
        with store._lock:
            old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
            store._conn.execute("UPDATE notification SET created_at = ? WHERE id = ?",
                                (old_ts, n.id))
            store._conn.commit()
        store.add("k", "new", "b")
        removed = store.purge_old(retention_days=7)
        assert removed == 1
        # new 还在
        assert [x.title for x in store.list_recent(10)] == ["new"]


class TestSubscribe:
    def test_fanout_delivers_to_subscribers(self, store):
        q = store.subscribe()
        store.add("k", "hello", "world")
        got = q.get(timeout=1)
        assert got.title == "hello"

    def test_multiple_subscribers_all_get(self, store):
        q1 = store.subscribe()
        q2 = store.subscribe()
        store.add("k", "x", "y")
        assert q1.get(timeout=1).title == "x"
        assert q2.get(timeout=1).title == "x"

    def test_unsubscribe_stops_delivery(self, store):
        q = store.subscribe()
        store.unsubscribe(q)
        store.add("k", "x", "y")
        with pytest.raises(queue.Empty):
            q.get(timeout=0.05)

    def test_close_sends_sentinel_to_subs(self, tmp_path):
        s = InboxStore(tmp_path / "i.sqlite3")
        q = s.subscribe()
        s.close()
        # sentinel = None
        assert q.get(timeout=1) is None


class TestInboxNotifier:
    def test_notifier_send_writes_row(self, store):
        n = InboxNotifier(store)
        n.send("Title", "Body", meta={"kind": "approval_created", "change_id": 1})
        items = store.list_recent(10)
        assert len(items) == 1
        assert items[0].kind == "approval_created"
        assert items[0].meta["change_id"] == 1

    def test_kind_falls_back_to_info(self, store):
        n = InboxNotifier(store)
        n.send("t", "b")  # 无 meta
        assert store.list_recent(1)[0].kind == "info"

    def test_notifier_never_raises(self, store):
        n = InboxNotifier(store)
        store.close()  # 让底层写失败
        # 不应抛
        n.send("t", "b", meta={"kind": "x"})
