"""SQL 片段库存储测试：CRUD + 校验。"""

import pytest

from dbmcp.snippets import SnippetError, SnippetStore


@pytest.fixture
def store(tmp_path):
    s = SnippetStore(tmp_path / "snip.sqlite3")
    yield s
    s.close()


def test_create_and_get(store):
    snip = store.create("日活统计", "SELECT count(*) FROM users", note="每日跑", connection="demo/main")
    assert snip.id > 0
    assert snip.title == "日活统计" and snip.note == "每日跑"
    assert snip.connection == "demo/main"
    got = store.get(snip.id)
    assert got.sql == "SELECT count(*) FROM users"
    assert got.created_at and got.updated_at


def test_title_and_sql_required(store):
    with pytest.raises(SnippetError, match="标题"):
        store.create("  ", "SELECT 1")
    with pytest.raises(SnippetError, match="SQL"):
        store.create("t", "   ")


def test_list_orders_by_updated_desc(store):
    a = store.create("a", "SELECT 1")
    b = store.create("b", "SELECT 2")
    ids = [s.id for s in store.list()]
    # 最新创建/更新的排前面
    assert ids[0] == b.id and ids[1] == a.id


def test_update_changes_fields_and_touches_updated_at(store):
    snip = store.create("旧标题", "SELECT 1", note="old")
    updated = store.update(snip.id, "新标题", "SELECT 2", note="new", connection="p/c")
    assert updated.title == "新标题" and updated.sql == "SELECT 2"
    assert updated.note == "new" and updated.connection == "p/c"
    assert store.get(snip.id).title == "新标题"


def test_update_missing_raises(store):
    with pytest.raises(SnippetError, match="不存在"):
        store.update(999, "t", "SELECT 1")


def test_delete(store):
    snip = store.create("t", "SELECT 1")
    store.delete(snip.id)
    assert store.list() == []
    with pytest.raises(SnippetError, match="不存在"):
        store.get(snip.id)


def test_delete_missing_raises(store):
    with pytest.raises(SnippetError, match="不存在"):
        store.delete(12345)
