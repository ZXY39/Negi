from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane
from g3ku.heartbeat.session_service import WebSessionHeartbeatService
from g3ku.runtime.event_subscriptions import EventSubscriptionManager
from g3ku.runtime.external_event_service import ExternalEventService
from main.storage.sqlite_store import SQLiteTaskStore

SESSION_KEY = "web:ceo-test"


class _FakeHeartbeat:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.payloads: list[dict] = []

    def enqueue_external_event(self, payload: dict) -> bool:
        if not self.accept:
            return False
        self.payloads.append(dict(payload))
        return True


@pytest.fixture()
def store(tmp_path: Path):
    instance = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def subscription(store, tmp_path: Path):
    root = tmp_path / "temp"
    manager = EventSubscriptionManager(store)
    created = manager.create(
        runtime_context={
            "actor_role": "ceo",
            "session_key": SESSION_KEY,
            "task_temp_dir": str(root),
        },
        name="file-ready",
        instruction="Read the exported spreadsheet and continue the task.",
        condition_note="The spreadsheet finished writing.",
        credential_file=str(root / "credentials.json"),
    )
    return store.get_event_subscription(created["subscription_id"])


def _service(store, heartbeat, monkeypatch):
    service = ExternalEventService(
        store=store,
        task_service=None,
        heartbeat_getter=lambda: heartbeat,
    )
    monkeypatch.setattr(service, "_schedule_dispatch", lambda event_id: None)
    return service


def test_session_dispatch_enqueues_and_restart_replays_pending(
    store,
    subscription,
    monkeypatch,
) -> None:
    heartbeat = _FakeHeartbeat()
    service = _service(store, heartbeat, monkeypatch)

    event, inserted = service.accept(
        subscription=subscription,
        idempotency_key="file-1",
        event_type="file.ready",
        occurred_at="2026-11-06T15:00:00+08:00",
        text="spreadsheet ready",
        data={"path": "D:/Desktop/book.xlsx"},
    )
    assert inserted is True
    assert event["status"] == "pending"
    assert heartbeat.payloads == []

    import asyncio

    asyncio.run(service._dispatch_event(event["event_id"]))
    assert len(heartbeat.payloads) == 1
    payload = heartbeat.payloads[0]
    assert payload["session_id"] == SESSION_KEY
    assert payload["subscription_id"] == subscription["subscription_id"]
    assert payload["instruction"].startswith("Read the exported")
    assert payload["text"] == "spreadsheet ready"
    stored = store.get_external_event(event["event_id"])
    assert stored["status"] == "pending"
    assert stored["delivery_detail"]["stage"] == "heartbeat_enqueued"

    # A process restart rebuilds the service from the same durable rows.
    replay_heartbeat = _FakeHeartbeat()
    replay_service = _service(store, replay_heartbeat, monkeypatch)
    asyncio.run(replay_service._dispatch_event(event["event_id"]))
    assert replay_heartbeat.payloads == heartbeat.payloads


def test_disabled_subscription_keeps_event_pending(
    store,
    subscription,
    monkeypatch,
) -> None:
    store.set_event_subscription_enabled(
        subscription["subscription_id"],
        enabled=False,
        updated_at="2026-11-06T15:01:00",
    )
    heartbeat = _FakeHeartbeat()
    service = _service(store, heartbeat, monkeypatch)
    event, _ = service.accept(
        subscription=store.get_event_subscription(subscription["subscription_id"]),
        idempotency_key="disabled-1",
        event_type="file.ready",
        occurred_at="",
        text="ready",
        data={},
    )

    import asyncio

    asyncio.run(service._dispatch_event(event["event_id"]))
    assert heartbeat.payloads == []
    stored = store.get_external_event(event["event_id"])
    assert stored["status"] == "pending"
    assert stored["delivery_detail"]["stage"] == "subscription_disabled"


def test_heartbeat_injection_marks_event_injected(
    store,
    subscription,
    monkeypatch,
    tmp_path: Path,
) -> None:
    heartbeat = _FakeHeartbeat()
    event_service = _service(store, heartbeat, monkeypatch)
    event, _ = event_service.accept(
        subscription=subscription,
        idempotency_key="inject-1",
        event_type="file.ready",
        occurred_at="",
        text="ready",
        data={"path": "x"},
    )
    import asyncio

    asyncio.run(event_service._dispatch_event(event["event_id"]))
    payload = heartbeat.payloads[0]

    inferred = SimpleNamespace()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=inferred,
        runtime_manager=inferred,
        main_task_service=SimpleNamespace(registry=None, store=store),
        session_manager=inferred,
    )
    queued = service._events.enqueue(
        session_id=SESSION_KEY,
        source="external_api",
        reason="external_event",
        dedupe_key=f"external-event:{subscription['subscription_id']}:{event['event_id']}",
        payload=payload,
    )
    assert queued is not None
    service._consume_heartbeat_events_at_injection(
        SESSION_KEY,
        [queued],
        SimpleNamespace(metadata={}, messages=[], updated_at=""),
    )

    stored = store.get_external_event(event["event_id"])
    assert stored["status"] == "injected"
    assert stored["delivery_detail"]["stage"] == "injected"
    assert service._events.peek(SESSION_KEY) == []


def test_duplicate_enqueue_is_rejected() -> None:
    service = object.__new__(WebSessionHeartbeatService)
    from g3ku.heartbeat.session_events import SessionHeartbeatEventQueue

    service._events = SessionHeartbeatEventQueue()
    service._started = False
    payload = {
        "session_id": SESSION_KEY,
        "subscription_id": "sub-1",
        "event_id": "evt-1",
    }
    assert service.enqueue_external_event(payload) is True
    assert service.enqueue_external_event(payload) is False


def test_external_event_prompt_lane_keeps_instruction_and_untrusted_boundary() -> None:
    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[
            {
                "reason": "external_event",
                "event_id": "evt-1",
                "subscription_id": "sub-1",
                "subscription_name": "comment-watch",
                "event_type": "comment.created",
                "occurred_at": "2026-11-06T12:00:00+08:00",
                "instruction": "Inspect the comment and answer only if it asks a real question.",
                "condition_note": "A new comment matched the filter.",
                "text": "How do I install this?",
                "data": {"author": "user-1"},
                "target_kind": "session",
            }
        ],
    )
    text = lane.event_bundle_text
    assert "External event: comment-watch" in text
    assert "evt-1" in text and "sub-1" in text
    assert "Inspect the comment and answer only if it asks a real question." in text
    assert "How do I install this?" in text
    assert "untrusted external data" in text
    assert "call `silent`" in text
    assert "artifact:" not in lane.retrieval_query
