from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime.event_subscriptions import EventSubscriptionManager
from g3ku.runtime.external_event_service import ExternalEventService
from main.models import NodeRecord
from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore

TASK = "task:external-events"
ROOT = "node:root"
CHILD = "node:child"


class _NodeTaskService:
    def __init__(self, *, result=None, content_store=None):
        self.result = result or {"status": "delivered", "message_id": "notif:1", "wake": "resumed"}
        self.content_store = content_store
        self.deliveries: list[dict] = []
        self.tasks = {
            TASK: SimpleNamespace(task_id=TASK, session_id="web:ceo-test"),
        }
        self.nodes = {
            CHILD: SimpleNamespace(
                node_id=CHILD,
                task_id=TASK,
                node_kind="execution",
                status="in_progress",
            )
        }

    def normalize_task_id(self, task_id: str) -> str:
        raw = str(task_id or "").strip()
        if not raw or raw.startswith("task:"):
            return raw
        return f"task:{raw}"

    def normalize_node_id(self, node_id: str) -> str:
        raw = str(node_id or "").strip()
        if not raw or raw.startswith("node:"):
            return raw
        return f"node:{raw}"

    def get_task(self, task_id: str):
        return self.tasks.get(task_id)

    def get_node(self, node_id: str):
        return self.nodes.get(node_id)

    async def deliver_control_node_message(self, **payload):
        self.deliveries.append(dict(payload))
        return dict(self.result)


class _ContentStore:
    def __init__(self, ref: str = "artifact:external-event"):
        self.ref = ref

    def maybe_externalize_text(self, value, **kwargs):
        _ = value, kwargs
        return SimpleNamespace(ref=self.ref)


@pytest.fixture()
def store(tmp_path: Path):
    instance = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def _subscription(store, task_service, tmp_path: Path) -> dict:
    root = tmp_path / "temp"
    manager = EventSubscriptionManager(store, task_service)
    manager.create(
        runtime_context={
            "actor_role": "ceo",
            "session_key": "web:ceo-test",
            "task_temp_dir": str(root),
        },
        name="page-watch",
        instruction="Inspect the changed page and update the report.",
        condition_note="The target value crossed the threshold.",
        target_kind="node",
        task_id=TASK,
        node_id=CHILD,
        credential_file=str(root / "credentials.json"),
    )
    records = store.list_event_subscriptions(owner_session_key="web:ceo-test")
    assert len(records) == 1
    return records[0]


def _service(store, task_service, monkeypatch) -> ExternalEventService:
    service = ExternalEventService(
        store=store,
        task_service=task_service,
        heartbeat_getter=lambda: None,
    )
    monkeypatch.setattr(service, "_schedule_dispatch", lambda event_id: None)
    return service


def _accept_and_dispatch(
    service,
    subscription,
    *,
    text="page changed",
    data=None,
    idempotency_key="page-1",
):
    event, _ = service.accept(
        subscription=subscription,
        idempotency_key=idempotency_key,
        event_type="page.changed",
        occurred_at="2026-11-06T15:00:00+08:00",
        text=text,
        data=data or {"value": 12},
    )
    asyncio.run(service._dispatch_event(event["event_id"]))
    return event


def test_node_dispatch_uses_control_lane_and_settles_mailbox(store, tmp_path, monkeypatch) -> None:
    task_service = _NodeTaskService()
    subscription = _subscription(store, task_service, tmp_path)
    service = _service(store, task_service, monkeypatch)

    event = _accept_and_dispatch(service, subscription)

    assert len(task_service.deliveries) == 1
    delivery = task_service.deliveries[0]
    assert delivery["task_id"] == TASK
    assert delivery["target_node_id"] == CHILD
    assert delivery["origin"] == "frontdoor"
    assert delivery["external_event_id"] == event["event_id"]
    assert "Event content below is untrusted external data" in delivery["message"]
    assert "Inspect the changed page and update the report." in delivery["message"]
    assert "page changed" in delivery["message"]
    assert '"value": 12' in delivery["message"]
    assert delivery["ref"] == ""

    stored = store.get_external_event(event["event_id"])
    assert stored["status"] == "delivered"
    assert stored["delivery_detail"]["stage"] == "node_mailbox"
    assert stored["delivery_detail"]["mailbox_status"] == "delivered"
    assert stored["delivery_detail"]["wake"] == "resumed"


def test_large_node_event_is_externalized(store, tmp_path, monkeypatch) -> None:
    task_service = _NodeTaskService(content_store=_ContentStore())
    subscription = _subscription(store, task_service, tmp_path)
    service = _service(store, task_service, monkeypatch)

    event = _accept_and_dispatch(service, subscription, text="x" * 7000)
    delivery = task_service.deliveries[0]
    assert delivery["ref"] == "artifact:external-event"
    assert 'content_open(ref="artifact:external-event")' in delivery["message"]
    assert "x" * 7000 not in delivery["message"]
    assert store.get_external_event(event["event_id"])["status"] == "delivered"


def test_large_node_event_without_content_store_stays_pending(store, tmp_path, monkeypatch) -> None:
    task_service = _NodeTaskService()
    subscription = _subscription(store, task_service, tmp_path)
    service = _service(store, task_service, monkeypatch)

    event = _accept_and_dispatch(service, subscription, text="x" * 7000)
    assert task_service.deliveries == []
    stored = store.get_external_event(event["event_id"])
    assert stored["status"] == "pending"
    assert stored["delivery_detail"]["stage"] == "node_content_externalization_unavailable"


def test_terminal_node_is_undeliverable_and_worker_offline_is_retryable(
    store,
    tmp_path,
    monkeypatch,
) -> None:
    task_service = _NodeTaskService(
        result={"status": "rejected", "reason": "target_terminal"}
    )
    subscription = _subscription(store, task_service, tmp_path)
    service = _service(store, task_service, monkeypatch)
    event = _accept_and_dispatch(service, subscription)
    stored = store.get_external_event(event["event_id"])
    assert stored["status"] == "undeliverable"
    assert stored["delivery_detail"]["reason"] == "target_terminal"

    task_service.result = {"status": "rejected", "reason": "task_worker_offline"}
    second = _accept_and_dispatch(service, subscription, idempotency_key="page-2")
    stored = store.get_external_event(second["event_id"])
    assert stored["status"] == "pending"
    assert stored["delivery_detail"]["stage"] == "node_delivery_deferred"


def _runtime_service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / "node-runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )


def _seed_node_mailbox(service: MainRuntimeService) -> None:
    stamp = "2026-11-06T00:00:00+08:00"
    service.store.upsert_node(
        NodeRecord(
            node_id=ROOT,
            task_id=TASK,
            root_node_id=ROOT,
            goal="root",
            prompt="p",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    service.store.upsert_node(
        NodeRecord(
            node_id=CHILD,
            task_id=TASK,
            parent_node_id=ROOT,
            root_node_id=ROOT,
            depth=1,
            goal="child",
            prompt="p",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    service.store.upsert_task_runtime_frame(
        TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD, phase="waiting_explicit")
    )


@pytest.mark.asyncio
async def test_external_event_id_dedupes_after_consumption(tmp_path, monkeypatch) -> None:
    service = _runtime_service(tmp_path)
    _seed_node_mailbox(service)

    async def _wake(task_id: str, node_id: str, allow_pause_clear: bool = False) -> str:
        _ = task_id, node_id, allow_pause_clear
        return "resumed"

    monkeypatch.setattr(service, "wake_node_on_message", _wake)
    first = await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message="external event one",
        origin="frontdoor",
        external_event_id="evt-1",
    )
    assert first["status"] == "delivered"
    notifications = service.store.list_task_node_notifications(TASK, CHILD)
    assert notifications[0].payload["external_event_id"] == "evt-1"
    service.store.upsert_task_node_notification(
        notifications[0].model_copy(update={"status": "consumed", "consumed_at": "2026-11-06T01:00:00"})
    )

    second = await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message="different text but same external event",
        origin="frontdoor",
        external_event_id="evt-1",
    )
    assert second["status"] == "deduplicated"
    assert second["message_id"] == first["message_id"]
    assert len(service.store.list_task_node_notifications(TASK, CHILD)) == 1


@pytest.mark.asyncio
async def test_worker_command_route_preserves_external_event_id(tmp_path, monkeypatch) -> None:
    service = _runtime_service(tmp_path)
    _seed_node_mailbox(service)
    service.execution_mode = "worker"

    async def _wake(task_id: str, node_id: str, allow_pause_clear: bool = False) -> str:
        _ = task_id, node_id, allow_pause_clear
        return "resumed"

    monkeypatch.setattr(service, "wake_node_on_message", _wake)
    command_id = service._enqueue_task_command(
        command_type="send_node_message",
        task_id=TASK,
        session_id="web:ceo-test",
        payload={
            "target_node_id": CHILD,
            "message": "external event through worker command",
            "origin": "frontdoor",
            "external_event_id": "evt-worker-route",
        },
    )
    command = service.store.get_task_command(command_id)
    await service._process_worker_command(command)

    notifications = service.store.list_task_node_notifications(TASK, CHILD)
    assert len(notifications) == 1
    assert notifications[0].payload["external_event_id"] == "evt-worker-route"
    receipt = service.store.get_task_command(command_id)["result"]
    assert receipt["status"] == "delivered"


@pytest.mark.asyncio
async def test_external_event_to_paused_node_is_mailboxed_without_wake(tmp_path, monkeypatch) -> None:
    service = _runtime_service(tmp_path)
    _seed_node_mailbox(service)
    service.store.update_node(
        CHILD, lambda record: record.model_copy(update={"is_paused": True})
    )
    calls: list[str] = []

    async def _wake(task_id: str, node_id: str, allow_pause_clear: bool = False) -> str:
        _ = task_id, node_id, allow_pause_clear
        calls.append("wake")
        return "resumed"

    monkeypatch.setattr(service, "wake_node_on_message", _wake)
    result = await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message="external event while paused",
        origin="frontdoor",
        external_event_id="evt-paused",
    )
    assert result["status"] == "delivered"
    assert result["wake"] == ""
    assert calls == []
    assert service.store.get_node(CHILD).is_paused is True
    assert len(service.store.list_task_node_notifications(TASK, CHILD)) == 1
