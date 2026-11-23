"""Session-link storage contract: symmetric edges, frozen short ids, per-recipient inbox."""

from __future__ import annotations

import sqlite3

import pytest

from main.models import FrontdoorSessionMessage
from main.storage.sqlite_store import SQLiteTaskStore


@pytest.fixture()
def store(tmp_path):
    instance = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    yield instance
    instance.close()


def test_tables_idempotent_on_reopen(tmp_path):
    path = tmp_path / 'runtime.sqlite3'
    first = SQLiteTaskStore(path)
    first.add_session_link('web:a', 'web:b', created_at='2026-11-18T00:00:00')
    first.close()
    second = SQLiteTaskStore(path)
    assert second.list_session_links('web:a') == ['web:b']
    second.close()


def test_link_edge_is_symmetric_and_normalized(store):
    assert store.add_session_link('web:b', 'web:a', created_at='t0') is True
    # Same edge spelled the other way is a duplicate, not a second row.
    assert store.add_session_link('web:a', 'web:b', created_at='t1') is False
    assert store.has_session_link('web:a', 'web:b') is True
    assert store.has_session_link('web:b', 'web:a') is True
    assert store.list_session_links('web:a') == ['web:b']
    assert store.list_session_links('web:b') == ['web:a']
    assert store.remove_session_link('web:b', 'web:a') is True
    assert store.has_session_link('web:a', 'web:b') is False


def test_link_rejects_self_and_empty(store):
    assert store.add_session_link('web:a', 'web:a', created_at='t0') is False
    assert store.add_session_link('', 'web:b', created_at='t0') is False
    assert store.list_session_links('') == []


def test_delete_links_for_clears_both_directions(store):
    store.add_session_link('web:a', 'web:b', created_at='t0')
    store.add_session_link('web:c', 'web:a', created_at='t0')
    store.add_session_link('web:b', 'web:c', created_at='t0')
    assert store.delete_session_links_for('web:a') == 2
    assert store.list_session_links('web:a') == []
    assert store.list_session_links('web:b') == ['web:c']


def test_short_id_stable_and_roundtrip(store):
    short_id = store.get_or_create_session_short_id('web:ceo-3f9a1b2c4d5e', created_at='t0')
    assert short_id == '3f9a1b2c'
    assert store.get_or_create_session_short_id('web:ceo-3f9a1b2c4d5e', created_at='t1') == '3f9a1b2c'
    assert store.get_session_short_id('web:ceo-3f9a1b2c4d5e') == '3f9a1b2c'
    assert store.resolve_session_short_id('3f9a1b2c') == 'web:ceo-3f9a1b2c4d5e'
    assert store.resolve_session_short_id('3F9A1B2C') == 'web:ceo-3f9a1b2c4d5e'
    other = store.get_or_create_session_short_id('ext:bridge:77aa0011ff', created_at='t0')
    assert other == '77aa0011'
    assert store.resolve_session_short_id('nosuchid') == ''
    assert store.resolve_session_short_id('') == ''
    assert store.get_session_short_id('web:missing') == ''
    # The legacy s{seq} read path still answers for rows that carry one.
    assert store.get_session_alias('web:ceo-3f9a1b2c4d5e') == 's1'
    assert store.resolve_session_alias('s1') == 'web:ceo-3f9a1b2c4d5e'
    assert store.resolve_session_alias('S1') == 'web:ceo-3f9a1b2c4d5e'
    assert store.resolve_session_alias('s9') == ''
    assert store.resolve_session_alias('node:abc') == ''


def test_short_id_extends_on_collision_and_never_changes(store):
    first = store.get_or_create_session_short_id('web:ceo-3f9a1b2c4d5e', created_at='t0')
    # Same 8-char prefix, different key: the newcomer extends, the incumbent keeps its id.
    second = store.get_or_create_session_short_id('web:ceo-3f9a1b2c9999', created_at='t0')
    assert first == '3f9a1b2c'
    assert second == '3f9a1b2c9999'
    # A third namespace with the same tail extends past the full tail.
    third = store.get_or_create_session_short_id('ext:qq:3f9a1b2c4d5e', created_at='t0')
    assert third not in {first, second}
    assert store.resolve_session_short_id(third) == 'ext:qq:3f9a1b2c4d5e'
    assert store.get_or_create_session_short_id('web:ceo-3f9a1b2c4d5e', created_at='t9') == first


def test_short_id_survives_row_loss_for_the_same_key(store):
    first = store.get_or_create_session_short_id('web:ceo-aaaa1111bbbb', created_at='t0')
    second = store.get_or_create_session_short_id('web:ceo-bbbb2222cccc', created_at='t0')
    # 标识行生产上永不删；这里模拟物理丢行：同 key 回归拿回原号，别的会话不受影响。
    with sqlite3.connect(str(store.path)) as conn:
        conn.execute("DELETE FROM session_aliases WHERE session_key = 'web:ceo-bbbb2222cccc'")
    assert store.get_or_create_session_short_id('web:ceo-bbbb2222cccc', created_at='t1') == second
    assert store.get_or_create_session_short_id('web:ceo-aaaa1111bbbb', created_at='t1') == first


def test_short_id_column_migrates_on_legacy_database(tmp_path):
    """存量库没有 short_id 列：补列后按行懒分配，s{seq} 读口不受影响。"""
    path = tmp_path / 'runtime.sqlite3'
    first = SQLiteTaskStore(path)
    first.get_or_create_session_short_id('web:ceo-aaaa1111bbbb', created_at='t0')
    first.close()
    with sqlite3.connect(str(path)) as conn:
        conn.execute('ALTER TABLE session_aliases RENAME TO legacy_aliases')
        conn.execute(
            'CREATE TABLE session_aliases (seq INTEGER PRIMARY KEY AUTOINCREMENT, '
            'session_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)'
        )
        conn.execute(
            'INSERT INTO session_aliases (seq, session_key, created_at) '
            'SELECT seq, session_key, created_at FROM legacy_aliases'
        )
        conn.execute('DROP TABLE legacy_aliases')
    second = SQLiteTaskStore(path)
    try:
        assert second.get_session_short_id('web:ceo-aaaa1111bbbb') == ''
        assert second.resolve_session_alias('s1') == 'web:ceo-aaaa1111bbbb'
        assert second.get_or_create_session_short_id('web:ceo-aaaa1111bbbb', created_at='t1') == 'aaaa1111'
        assert second.resolve_session_short_id('aaaa1111') == 'web:ceo-aaaa1111bbbb'
    finally:
        second.close()


def _message(message_id: str, *, session_id: str = 'web:b', source: str = 'web:a',
             fingerprint: str = 'fp1', status: str = 'pending') -> FrontdoorSessionMessage:
    return FrontdoorSessionMessage(
        message_id=message_id,
        session_id=session_id,
        source_session_key=source,
        content='hello',
        status=status,
        created_at='2026-11-18T00:00:00',
        payload={'fingerprint': fingerprint, 'chain_id': message_id, 'hop': 0},
    )


def test_inbox_crud_and_pending_scan(store):
    store.upsert_frontdoor_session_message(_message('sm:1'))
    store.upsert_frontdoor_session_message(_message('sm:2', fingerprint='fp2'))
    assert store.get_frontdoor_session_message('sm:1').content == 'hello'
    assert store.get_frontdoor_session_message('sm:missing') is None
    assert [m.message_id for m in store.list_pending_frontdoor_session_messages()] == ['sm:1', 'sm:2']
    assert [m.message_id for m in store.list_frontdoor_session_messages('web:b')] == ['sm:1', 'sm:2']
    assert store.list_frontdoor_session_messages('web:b', status='consumed') == []
    store.mark_frontdoor_session_message_consumed('sm:1', consumed_at='t1')
    assert store.get_frontdoor_session_message('sm:1').status == 'consumed'
    assert [m.message_id for m in store.list_pending_frontdoor_session_messages()] == ['sm:2']
    # Marking an unknown id is a no-op, not an error.
    store.mark_frontdoor_session_message_consumed('sm:missing', consumed_at='t1')


def test_fingerprint_dedupe_only_absorbs_unconsumed(store):
    store.upsert_frontdoor_session_message(_message('sm:1'))
    hit = store.find_unconsumed_session_message_by_fingerprint('web:b', 'web:a', 'fp1')
    assert hit is not None and hit.message_id == 'sm:1'
    store.mark_frontdoor_session_message_consumed('sm:1', consumed_at='t1')
    # Consumed repeat is a new intent: the dedupe arm no longer sees it.
    assert store.find_unconsumed_session_message_by_fingerprint('web:b', 'web:a', 'fp1') is None
    # Different recipient or source does not share the dedupe space.
    assert store.find_unconsumed_session_message_by_fingerprint('web:c', 'web:a', 'fp1') is None


def test_delete_messages_for_clears_inbox_and_sent(store):
    store.upsert_frontdoor_session_message(_message('sm:1'))
    store.upsert_frontdoor_session_message(_message('sm:2', session_id='web:c', source='web:b'))
    assert store.delete_frontdoor_session_messages_for('web:b') == 2
    assert store.list_pending_frontdoor_session_messages() == []


def test_service_replace_links_symmetric_diff_and_short_ids(tmp_path, monkeypatch):
    from main.service.runtime_service import MainRuntimeService

    instance = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    service = MainRuntimeService.__new__(MainRuntimeService)
    service.store = instance
    try:
        result = service.replace_session_links('web:ceo-aaaa1111bbbb', ['web:ceo-bbbb2222cccc', 'web:ceo-cccc3333dddd'])
        assert result == {'added': ['web:ceo-bbbb2222cccc', 'web:ceo-cccc3333dddd'], 'removed': []}
        # Symmetric: the peer side sees the edge without its own write.
        assert service.list_session_links('web:ceo-bbbb2222cccc') == ['web:ceo-aaaa1111bbbb']
        assert service.session_short_id('web:ceo-aaaa1111bbbb') == 'aaaa1111'
        assert service.session_short_id('web:ceo-bbbb2222cccc') == 'bbbb2222'
        assert service.session_short_id('web:ceo-cccc3333dddd') == 'cccc3333'
        result = service.replace_session_links('web:ceo-aaaa1111bbbb', ['web:ceo-cccc3333dddd', 'web:ceo-dddd4444eeee'])
        assert result == {'added': ['web:ceo-dddd4444eeee'], 'removed': ['web:ceo-bbbb2222cccc']}
        assert service.list_session_links('web:ceo-bbbb2222cccc') == []
        # Unlinking keeps the identifier: the row is never deleted.
        assert service.resolve_session_address('bbbb2222') == 'web:ceo-bbbb2222cccc'
        # Self-targets are silently dropped, not edges.
        service.replace_session_links('web:ceo-aaaa1111bbbb', ['web:ceo-aaaa1111bbbb'])
        assert service.list_session_links('web:ceo-aaaa1111bbbb') == []
        roster = service.reachable_session_roster('web:ceo-aaaa1111bbbb')
        assert roster == []
    finally:
        instance.close()
