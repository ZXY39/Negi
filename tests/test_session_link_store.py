"""Session-link storage contract: symmetric edges, never-reused aliases, per-recipient inbox."""

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


def test_alias_stable_and_roundtrip(store):
    alias = store.get_or_create_session_alias('web:ceo-abc', created_at='t0')
    assert alias == 's1'
    assert store.get_or_create_session_alias('web:ceo-abc', created_at='t1') == 's1'
    assert store.get_session_alias('web:ceo-abc') == 's1'
    assert store.resolve_session_alias('s1') == 'web:ceo-abc'
    assert store.resolve_session_alias('S1') == 'web:ceo-abc'
    other = store.get_or_create_session_alias('ext:bridge:hash', created_at='t0')
    assert other == 's2'
    assert store.resolve_session_alias('s9') == ''
    assert store.resolve_session_alias('node:abc') == ''
    assert store.resolve_session_alias('') == ''
    assert store.get_session_alias('web:missing') == ''


def test_alias_seq_never_reused_after_row_loss(store):
    store.get_or_create_session_alias('web:a', created_at='t0')
    store.get_or_create_session_alias('web:b', created_at='t0')
    # Simulate the worst case (row physically gone): AUTOINCREMENT still advances.
    with sqlite3.connect(str(store.path)) as conn:
        conn.execute("DELETE FROM session_aliases WHERE session_key = 'web:b'")
    assert store.get_or_create_session_alias('web:c', created_at='t0') == 's3'
    # The same key returning (ext forget/re-register) reclaiming its old row keeps seq.
    assert store.get_or_create_session_alias('web:a', created_at='t0') == 's1'


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


def test_service_replace_links_symmetric_diff_and_aliases(tmp_path, monkeypatch):
    from main.service.runtime_service import MainRuntimeService

    instance = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    service = MainRuntimeService.__new__(MainRuntimeService)
    service.store = instance
    try:
        result = service.replace_session_links('web:a', ['web:b', 'web:c'])
        assert result == {'added': ['web:b', 'web:c'], 'removed': []}
        # Symmetric: the peer side sees the edge without its own write.
        assert service.list_session_links('web:b') == ['web:a']
        assert service.get_session_alias('web:a') == 's1'
        assert service.get_session_alias('web:b') == 's2'
        assert service.get_session_alias('web:c') == 's3'
        result = service.replace_session_links('web:a', ['web:c', 'web:d'])
        assert result == {'added': ['web:d'], 'removed': ['web:b']}
        assert service.list_session_links('web:b') == []
        assert service.resolve_session_alias('s2') == 'web:b'
        # Self-targets are silently dropped, not edges.
        service.replace_session_links('web:a', ['web:a'])
        assert service.list_session_links('web:a') == []
        roster = service.reachable_session_roster('web:a')
        assert roster == []
    finally:
        instance.close()
