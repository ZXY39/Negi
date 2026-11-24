"""Session-link addressing and delivery: short ids, lazy edges, ACL, dedupe, chain/hop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.external_sessions import reset_external_session_registry
from g3ku.session.manager import SessionManager
from main.models import FrontdoorSessionMessage
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore

A = 'web:ceo-aaaa1111bbbb'
B = 'web:ceo-bbbb2222cccc'
C = 'web:ceo-cccc3333dddd'
D = 'web:ceo-dddd4444eeee'


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, 'workspace_path', lambda: tmp_path)
    reset_external_session_registry()
    manager = SessionManager(tmp_path)
    for key in (A, B, C, D):
        session = manager.get_or_create(key)
        session.messages.append({'role': 'user', 'content': '一句', 'timestamp': '2026-11-23T10:00:00'})
        manager.save(session)
    instance = MainRuntimeService.__new__(MainRuntimeService)
    instance.store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    instance.replace_session_links(A, [B, C])
    yield instance
    instance.store.close()
    reset_external_session_registry()


def _send(service, sender, to, content='hello', **kwargs):
    return asyncio.run(
        service.deliver_frontdoor_node_message(
            session_id=sender, to=to, content=content, **kwargs
        )
    )


def test_bare_short_id_and_explicit_prefix_resolve(service):
    short_b = service.session_short_id(B)
    assert short_b == 'bbbb2222'
    for target in (short_b, f'session:{short_b}', f'session:{B}'):
        result = _send(service, A, target, content=f'to {target}')
        assert result['ok'] is True, result
        item = result['results'][0]
        assert item['status'] == 'delivered'
        assert item['target_kind'] == 'session'
        assert item['resolved_session_key'] == B
        assert item['wake'] == 'session_inbox'
        assert 'auto_linked' not in item


def test_one_to_many_list_per_target_receipts(service):
    result = _send(service, A, [service.session_short_id(B), service.session_short_id(C)])
    assert result['ok'] is True
    assert [item['resolved_session_key'] for item in result['results']] == [B, C]
    assert len(service.store.list_pending_frontdoor_session_messages()) == 2


def test_unlinked_owner_target_is_linked_on_delivery(service):
    """懒创建：owner 会话向一个可关联 id 投递即建边，双向可见。"""
    assert service.list_session_links(D) == []
    result = _send(service, D, service.session_short_id(B), content='first contact')
    item = result['results'][0]
    assert result['ok'] is True
    assert item['status'] == 'delivered'
    assert item['auto_linked'] is True
    assert service.has_session_link(D, B) is True
    assert service.has_session_link(B, D) is True
    # Both endpoints hold a short id, and the new peer shows up in legal targets.
    assert service.session_short_id(D) == 'dddd4444'
    assert service.session_short_id(B) in service._frontdoor_legal_targets(D)[0]
    # The recipient can answer without any operator action.
    reply = _send(service, B, service.session_short_id(D), content='answer')
    assert reply['results'][0]['status'] == 'delivered'
    assert 'auto_linked' not in reply['results'][0]


def test_lazy_link_survives_operator_unlink(service):
    """解除关联后再发消息会重新建边——这是懒创建的定义，不是残留。"""
    service.replace_session_links(A, [C])
    assert service.has_session_link(A, B) is False
    result = _send(service, A, service.session_short_id(B), content='again')
    assert result['results'][0]['auto_linked'] is True
    assert service.has_session_link(A, B) is True


def test_third_party_sender_still_needs_operator_link(service):
    stranger = 'ext:qq-official:stranger'
    result = _send(service, stranger, service.session_short_id(B))
    assert result['ok'] is False
    assert result['results'][0]['reason'] == 'session_not_linked'
    assert service.has_session_link(stranger, B) is False


def test_unlinkable_and_missing_targets_do_not_create_edges(service):
    result = _send(service, D, 'session:internal:persona_photo')
    assert result['results'][0]['reason'] == 'session_not_linkable'
    result = _send(service, D, 'session:web:ceo-zzzz9999yyyy')
    assert result['results'][0]['reason'] == 'session_missing'
    assert service.list_session_links(D) == []
    # A rejection surfaces the sender's reachable short ids (none for D).
    assert result['legal_targets'] == []
    linked = _send(service, A, 'session:web:ceo-zzzz9999yyyy')
    assert service.session_short_id(B) in linked['legal_targets']


def test_unknown_short_id_and_bad_shape_rejected(service):
    result = _send(service, A, 's999')
    assert result['results'][0]['reason'] == 'session_missing'
    result = _send(service, A, 'session:not-a-key')
    assert result['results'][0]['reason'] == 'session_missing'
    # A bare token that names nothing keeps the node/task rejection code.
    result = _send(service, A, 'deadbeef0000')
    assert result['results'][0]['reason'] == 'target_not_in_task'


def test_self_send_rejected(service):
    result = _send(service, A, service.session_short_id(A))
    assert result['results'][0]['reason'] == 'session_not_linkable'
    assert service.list_session_links(A) == [B, C]


def test_legacy_alias_still_addresses(service):
    """存量记忆/来信里的 s{seq} 仍可寻址（只读解析，不再新分配）。"""
    legacy_a = service.store.get_session_alias(A)
    assert legacy_a.startswith('s')
    result = _send(service, B, legacy_a, content='legacy address')
    assert result['results'][0]['status'] == 'delivered'
    assert result['results'][0]['resolved_session_key'] == A


def test_reply_to_inherits_chain_and_hop(service):
    first = _send(service, A, service.session_short_id(B))['results'][0]
    assert first['hop'] == 0
    assert first['chain_id'] == first['message_id']
    reply = _send(
        service, B, service.session_short_id(A), content='answer', reply_to=first['message_id']
    )['results'][0]
    assert reply['status'] == 'delivered'
    assert reply['hop'] == 1
    assert reply['chain_id'] == first['chain_id']
    stored = service.store.get_frontdoor_session_message(reply['message_id'])
    assert stored.payload['reply_to'] == first['message_id']
    assert stored.payload['wakeable'] is True


def test_reply_to_must_be_own_incoming_message(service):
    first = _send(service, A, service.session_short_id(B))['results'][0]
    # c never received that message (it went to b).
    result = _send(service, C, service.session_short_id(A), reply_to=first['message_id'])
    assert result['results'][0]['reason'] == 'reply_to_unknown'
    result = _send(service, B, service.session_short_id(A), reply_to='smsg:missing')
    assert result['results'][0]['reason'] == 'reply_to_unknown'


def test_hop_cap_lands_without_wake(service):
    service.store.upsert_frontdoor_session_message(
        FrontdoorSessionMessage(
            message_id='smsg:deep',
            session_id=B,
            source_session_key=A,
            content='deep',
            created_at='t0',
            payload={'chain_id': 'chain:x', 'hop': 7, 'fingerprint': 'fpdeep'},
        )
    )
    reply = _send(
        service, B, service.session_short_id(A), content='cap', reply_to='smsg:deep'
    )['results'][0]
    assert reply['status'] == 'delivered'
    assert reply['hop'] == 8
    assert reply['wake'] == 'session_inbox_no_wake'
    assert 'wake budget' in reply['notice']
    stored = service.store.get_frontdoor_session_message(reply['message_id'])
    assert stored.payload['wakeable'] is False
    # A fresh message without reply_to starts a new chain at hop 0.
    fresh = _send(service, B, service.session_short_id(A), content='new topic')['results'][0]
    assert fresh['hop'] == 0
    assert fresh['wake'] == 'session_inbox'


def test_fingerprint_dedupe_and_consumed_resend(service):
    short_b = service.session_short_id(B)
    first = _send(service, A, short_b, content='same')['results'][0]
    again = _send(service, A, short_b, content='same')['results'][0]
    assert again['status'] == 'deduplicated'
    assert again['message_id'] == first['message_id']
    service.store.mark_frontdoor_session_message_consumed(first['message_id'], consumed_at='t1')
    third = _send(service, A, short_b, content='same')['results'][0]
    assert third['status'] == 'delivered'
    assert third['message_id'] != first['message_id']


def test_sender_short_id_stamped_in_payload(service):
    result = _send(service, A, service.session_short_id(B))['results'][0]
    stored = service.store.get_frontdoor_session_message(result['message_id'])
    assert stored.payload['source_short_id'] == service.session_short_id(A)
    assert stored.source_session_key == A


def test_scanner_signs_legacy_inbox_row_with_short_id(service):
    """存量信箱行盖的是 source_alias=s{seq}：扫描器富化成短标识。"""
    from g3ku.heartbeat.session_message_scanner import SessionMessageScanner

    service.store.upsert_frontdoor_session_message(
        FrontdoorSessionMessage(
            message_id='smsg:legacy',
            session_id=B,
            source_session_key=A,
            content='old row',
            created_at='t0',
            payload={
                'chain_id': 'smsg:legacy',
                'hop': 0,
                'fingerprint': 'fplegacy',
                'source_alias': service.store.get_session_alias(A),
            },
        )
    )
    enqueued: list[tuple[str, dict]] = []
    scanner = SessionMessageScanner(
        main_task_service=service,
        heartbeat=SimpleNamespace(
            enqueue_session_message_payload=lambda session_id, payload: enqueued.append(
                (session_id, payload)
            ) or True
        ),
        runtime_manager=SimpleNamespace(get=lambda _key: None),
        session_manager=SimpleNamespace(workspace=service.store.path.parent),
    )
    assert asyncio.run(scanner.scan_once()) == 1
    assert enqueued[0][0] == B
    assert enqueued[0][1]['source_short_id'] == service.session_short_id(A)
