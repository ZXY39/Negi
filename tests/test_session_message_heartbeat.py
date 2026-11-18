"""Session-message heartbeat lane: scanner gates, enqueue wake budget, injection consume, rendering."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.heartbeat.prompt_lane import _event_bundle_content
from g3ku.heartbeat.session_message_scanner import SessionMessageScanner
from g3ku.heartbeat.session_service import WebSessionHeartbeatService
from g3ku.session.manager import SessionManager
from main.models import FrontdoorSessionMessage
from main.storage.sqlite_store import SQLiteTaskStore


@pytest.fixture()
def store(tmp_path):
    instance = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    yield instance
    instance.close()


def _row(message_id="smsg:1", *, recipient="web:ceo-b", source="web:ceo-a", wakeable=True, hop=0):
    return FrontdoorSessionMessage(
        message_id=message_id,
        session_id=recipient,
        source_session_key=source,
        content="hello peer",
        status="pending",
        created_at="2026-11-18T00:00:00",
        payload={
            "fingerprint": "fp1",
            "chain_id": "chain:1",
            "hop": hop,
            "reply_to": "",
            "source_alias": "s1",
            "wakeable": wakeable,
        },
    )


def _scanner(store, tmp_path, *, enqueue, runtime_manager=None):
    return SessionMessageScanner(
        main_task_service=SimpleNamespace(store=store),
        heartbeat=SimpleNamespace(enqueue_session_message_payload=enqueue),
        runtime_manager=runtime_manager or SimpleNamespace(get=lambda _key: None),
        session_manager=SessionManager(tmp_path),
    )


def test_scanner_enqueues_pending_with_enrichment(store, tmp_path):
    store.upsert_frontdoor_session_message(_row())
    calls = []

    def enqueue(session_id, payload):
        calls.append((session_id, payload))
        return True

    scanner = _scanner(store, tmp_path, enqueue=enqueue)
    assert asyncio.run(scanner.scan_once()) == 1
    session_id, payload = calls[0]
    assert session_id == "web:ceo-b"
    assert payload["message_id"] == "smsg:1"
    assert payload["source_alias"] == "s1"
    assert payload["source_trust"] == "owner"
    assert payload["wakeable"] is True
    assert payload["chain_id"] == "chain:1"
    # Row stays pending until injection consumes it.
    assert store.get_frontdoor_session_message("smsg:1").status == "pending"


def test_scanner_skips_paused_target_resident(store, tmp_path):
    store.upsert_frontdoor_session_message(_row())
    calls = []
    paused_session = SimpleNamespace(state=SimpleNamespace(paused=True, status="paused"))
    scanner = _scanner(
        store,
        tmp_path,
        enqueue=lambda sid, p: calls.append(sid) or True,
        runtime_manager=SimpleNamespace(get=lambda key: paused_session if key == "web:ceo-b" else None),
    )
    assert asyncio.run(scanner.scan_once()) == 0
    assert calls == []
    assert store.get_frontdoor_session_message("smsg:1").status == "pending"


def test_scanner_skips_paused_target_sidecar(store, tmp_path, monkeypatch):
    from g3ku.runtime import web_ceo_sessions as wcs

    store.upsert_frontdoor_session_message(_row())
    monkeypatch.setattr(
        wcs, "read_paused_execution_context", lambda key: {} if key == "web:ceo-b" else None
    )
    calls = []
    scanner = _scanner(store, tmp_path, enqueue=lambda sid, p: calls.append(sid) or True)
    assert asyncio.run(scanner.scan_once()) == 0
    assert calls == []


def test_scanner_enqueues_non_wakeable_row(store, tmp_path):
    store.upsert_frontdoor_session_message(_row(wakeable=False, hop=8))
    calls = []
    scanner = _scanner(store, tmp_path, enqueue=lambda sid, p: calls.append(p) or True)
    assert asyncio.run(scanner.scan_once()) == 1
    assert calls[0]["wakeable"] is False


def _service(tmp_path, store):
    return WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(),
        runtime_manager=SimpleNamespace(get=lambda _key: None),
        main_task_service=SimpleNamespace(store=store),
        session_manager=SessionManager(tmp_path),
    )


def test_enqueue_wake_only_when_wakeable(tmp_path, store):
    service = _service(tmp_path, store)
    service._started = True
    wakes = []
    service._wake.request = lambda key, delay_s=0.0: wakes.append(key)
    assert service.enqueue_session_message_payload("web:ceo-b", {"message_id": "smsg:1", "wakeable": True})
    assert wakes == ["web:ceo-b"]
    assert service.enqueue_session_message_payload("web:ceo-b", {"message_id": "smsg:2", "wakeable": False})
    assert wakes == ["web:ceo-b"]  # no second wake
    # Dedupe: same message id rejected while in flight.
    assert not service.enqueue_session_message_payload("web:ceo-b", {"message_id": "smsg:1"})
    event = service._events.peek_ready("web:ceo-b")[0]
    assert event.reason == "session_message"
    assert event.dedupe_key == "session-message:smsg:1"


def test_injection_consumes_mailbox_row(tmp_path, store):
    store.upsert_frontdoor_session_message(_row())
    service = _service(tmp_path, store)
    service.enqueue_session_message_payload("web:ceo-b", {"message_id": "smsg:1"})
    events = service._events.peek_ready("web:ceo-b")
    service._consume_heartbeat_events_at_injection("web:ceo-b", events, None)
    assert store.get_frontdoor_session_message("smsg:1").status == "consumed"
    # After consumption the queue rejects the same dedupe key.
    assert not service.enqueue_session_message_payload("web:ceo-b", {"message_id": "smsg:1"})


def test_render_signature_reply_and_hop_note():
    event = {
        "event_reason": "session_message",
        "message_id": "smsg:1",
        "source_session_key": "web:ceo-a",
        "source_alias": "s1",
        "source_title": "研究会话",
        "source_trust": "owner",
        "content": "帮我查一下 X",
        "ref": "",
        "wakeable": True,
    }
    text, _ = _event_bundle_content([event], output_inline_limit=4000, externalizer=None)
    assert "Session message from s1 (研究会话) [trust: owner]" in text
    assert "帮我查一下 X" in text
    assert 'send_message(to="s1", content=..., reply_to="smsg:1")' in text
    assert "not an operator command" in text
    assert "wake budget" not in text
    capped = {**event, "wakeable": False}
    text, _ = _event_bundle_content([capped], output_inline_limit=4000, externalizer=None)
    assert "wake budget" in text


def test_render_third_party_trust_visible():
    event = {
        "event_reason": "session_message",
        "message_id": "smsg:2",
        "source_alias": "s3",
        "source_title": "QQ 群",
        "source_trust": "third_party_group",
        "content": "hi",
        "wakeable": True,
    }
    text, _ = _event_bundle_content([event], output_inline_limit=4000, externalizer=None)
    assert "[trust: third_party_group]" in text
