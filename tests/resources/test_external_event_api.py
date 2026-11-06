from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import external_event_v1
from g3ku.runtime.event_subscriptions import EventSubscriptionManager
from g3ku.runtime.external_event_service import (
    ExternalEventService,
    set_external_event_service,
)
from main.storage.sqlite_store import SQLiteTaskStore


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
            "session_key": "web:ceo-test",
            "task_temp_dir": str(root),
        },
        name="comment-watch",
        instruction="Inspect the new comment and decide whether to answer it.",
        credential_file=str(root / "credentials.json"),
    )
    token = json.loads((root / "credentials.json").read_text(encoding="utf-8"))["token"]
    return created, token


@pytest.fixture()
def event_service(store, monkeypatch):
    service = ExternalEventService(
        store=store,
        task_service=None,
        heartbeat_getter=lambda: None,
    )
    monkeypatch.setattr(service, "_schedule_dispatch", lambda event_id: None)
    set_external_event_service(service)
    yield service
    set_external_event_service(None)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        external_event_v1,
        "get_runtime_config",
        lambda force=False: (SimpleNamespace(external_api=SimpleNamespace(enabled=True)), None),
    )
    app = FastAPI()
    app.include_router(external_event_v1.router, prefix="/api/v1")
    return TestClient(app)


def _post(client: TestClient, subscription_id: str, token: str, **overrides):
    payload = {
        "type": "comment.created",
        "occurred_at": "2026-11-06T12:00:00+08:00",
        "text": "new comment",
        "data": {"comment_id": "123"},
    }
    payload.update(overrides.pop("payload", {}))
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "comment-123",
    }
    headers.update(overrides.pop("headers", {}))
    return client.post(
        f"/api/v1/event-subscriptions/{subscription_id}/events",
        json=payload,
        headers=headers,
    )


def test_event_post_accepts_and_deduplicates(
    client: TestClient,
    event_service: ExternalEventService,
    subscription,
) -> None:
    created, token = subscription
    first = _post(client, created["subscription_id"], token)
    assert first.status_code == 202
    body = first.json()
    assert body["duplicate"] is False
    assert body["status"] == "pending"
    event_id = body["event_id"]
    stored = event_service.store.get_external_event(event_id)
    assert stored["payload"]["text"] == "new comment"
    assert stored["payload"]["data"] == {"comment_id": "123"}

    second = _post(client, created["subscription_id"], token)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["event_id"] == event_id
    assert len(event_service.store.list_external_events_for_subscription(created["subscription_id"])) == 1


def test_event_post_requires_valid_subscription_credential(
    client: TestClient,
    event_service: ExternalEventService,
    subscription,
) -> None:
    created, token = subscription
    missing = client.post(
        f"/api/v1/event-subscriptions/{created['subscription_id']}/events",
        json={"text": "x"},
        headers={"Idempotency-Key": "k"},
    )
    assert missing.status_code == 401
    wrong = _post(client, created["subscription_id"], "nev_wrong")
    assert wrong.status_code == 401
    unknown = _post(client, "sub-unknown", token)
    assert unknown.status_code == 404


def test_disabled_and_external_api_switch(
    client: TestClient,
    event_service: ExternalEventService,
    subscription,
    monkeypatch,
) -> None:
    created, token = subscription
    event_service.manager.set_enabled(
        runtime_context={
            "actor_role": "ceo",
            "session_key": "web:ceo-test",
            "task_temp_dir": str(Path(created["credential_file"]).parent),
        },
        subscription_id=created["subscription_id"],
        enabled=False,
    )
    disabled = _post(client, created["subscription_id"], token)
    assert disabled.status_code == 403

    monkeypatch.setattr(
        external_event_v1,
        "get_runtime_config",
        lambda force=False: (SimpleNamespace(external_api=SimpleNamespace(enabled=False)), None),
    )
    switched_off = _post(client, created["subscription_id"], token)
    assert switched_off.status_code == 403


def test_event_body_validation(
    client: TestClient,
    event_service: ExternalEventService,
    subscription,
) -> None:
    created, token = subscription
    missing_key = _post(
        client,
        created["subscription_id"],
        token,
        headers={"Idempotency-Key": ""},
    )
    assert missing_key.status_code == 400
    empty = _post(
        client,
        created["subscription_id"],
        token,
        payload={"text": "", "data": {}},
        headers={"Idempotency-Key": "empty"},
    )
    assert empty.status_code == 400
    non_object = _post(
        client,
        created["subscription_id"],
        token,
        payload={"data": ["not", "object"]},
        headers={"Idempotency-Key": "shape"},
    )
    assert non_object.status_code == 400
    too_large = _post(
        client,
        created["subscription_id"],
        token,
        payload={"text": "x" * (256 * 1024 + 1)},
        headers={"Idempotency-Key": "large"},
    )
    assert too_large.status_code == 413


def test_event_service_unavailable_returns_503(client: TestClient) -> None:
    set_external_event_service(None)
    response = client.post(
        "/api/v1/event-subscriptions/sub-x/events",
        json={"text": "x"},
        headers={"Authorization": "Bearer nev_x", "Idempotency-Key": "k"},
    )
    assert response.status_code == 503
