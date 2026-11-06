from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime.event_subscriptions import EventSubscriptionManager
from main.storage.sqlite_store import SQLiteTaskStore


class _FakeTaskService:
    def __init__(self, tasks=None, nodes=None):
        self.tasks = dict(tasks or {})
        self.nodes = dict(nodes or {})

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


@pytest.fixture()
def store(tmp_path: Path):
    instance = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def _ceo_context(root: Path) -> dict[str, str]:
    return {
        "actor_role": "ceo",
        "session_key": "web:ceo-test",
        "task_temp_dir": str(root),
    }


def test_create_writes_credential_without_returning_plaintext(store, tmp_path: Path) -> None:
    root = tmp_path / "temp"
    manager = EventSubscriptionManager(store)
    credential_file = root / "connector" / "credentials.json"

    created = manager.create(
        runtime_context=_ceo_context(root),
        name="desktop-import",
        instruction="Read the newly exported spreadsheet and continue the analysis.",
        condition_note="A new xlsx file is complete.",
        credential_file=str(credential_file),
    )

    assert created["subscription_id"].startswith("sub-")
    assert created["endpoint_path"].endswith("/events")
    assert created["credential_file"] == str(credential_file.resolve())
    assert "secret_hash" not in created
    assert "token" not in json.dumps(created)
    payload = json.loads(credential_file.read_text(encoding="utf-8"))
    assert payload["subscription_id"] == created["subscription_id"]
    assert payload["token"].startswith("nev_")

    record, reason = manager.authenticate(created["subscription_id"], payload["token"])
    assert reason == "ok"
    assert record is not None
    assert record["target_kind"] == "session"
    assert record["session_key"] == "web:ceo-test"


def test_credential_file_must_stay_inside_allowed_root(store, tmp_path: Path) -> None:
    root = tmp_path / "temp"
    root.mkdir()
    manager = EventSubscriptionManager(store)

    with pytest.raises(ValueError, match="credential_file_outside_allowed_root"):
        manager.create(
            runtime_context=_ceo_context(root),
            name="outside",
            instruction="wake",
            credential_file=str(tmp_path / "outside" / "credential.json"),
        )


def test_rotate_enable_disable_and_remove(store, tmp_path: Path) -> None:
    root = tmp_path / "temp"
    manager = EventSubscriptionManager(store)
    credential_file = root / "credentials.json"
    created = manager.create(
        runtime_context=_ceo_context(root),
        name="watch",
        instruction="handle the event",
        credential_file=str(credential_file),
    )
    old_token = json.loads(credential_file.read_text(encoding="utf-8"))["token"]

    rotated = manager.rotate_credential(
        runtime_context=_ceo_context(root),
        subscription_id=created["subscription_id"],
    )
    new_token = json.loads(credential_file.read_text(encoding="utf-8"))["token"]
    assert new_token != old_token
    assert rotated["credential_hint"] != created["credential_hint"]
    assert manager.authenticate(created["subscription_id"], old_token)[1] == "invalid_token"
    assert manager.authenticate(created["subscription_id"], new_token)[1] == "ok"

    manager.set_enabled(
        runtime_context=_ceo_context(root),
        subscription_id=created["subscription_id"],
        enabled=False,
    )
    assert manager.authenticate(created["subscription_id"], new_token)[1] == "disabled"

    manager.set_enabled(
        runtime_context=_ceo_context(root),
        subscription_id=created["subscription_id"],
        enabled=True,
    )
    assert manager.authenticate(created["subscription_id"], new_token)[1] == "ok"

    manager.remove(
        runtime_context=_ceo_context(root),
        subscription_id=created["subscription_id"],
    )
    assert manager.authenticate(created["subscription_id"], new_token)[1] == "not_found"
    assert manager.list(runtime_context=_ceo_context(root)) == []


def test_event_idempotency_is_persistent_and_scoped_by_subscription(store, tmp_path: Path) -> None:
    root = tmp_path / "temp"
    manager = EventSubscriptionManager(store)
    first = manager.create(
        runtime_context=_ceo_context(root),
        name="first",
        instruction="first",
        credential_file=str(root / "first.json"),
    )
    second = manager.create(
        runtime_context=_ceo_context(root),
        name="second",
        instruction="second",
        credential_file=str(root / "second.json"),
    )

    record, inserted = store.insert_external_event(
        {
            "event_id": "evt-first",
            "subscription_id": first["subscription_id"],
            "idempotency_key": "same-key",
            "event_type": "file.ready",
            "payload": {"path": "a.txt"},
            "status": "pending",
            "created_at": "2026-11-06T10:00:00",
            "updated_at": "2026-11-06T10:00:00",
        }
    )
    assert inserted is True
    assert record["event_id"] == "evt-first"

    duplicate, inserted_again = store.insert_external_event(
        {
            "event_id": "evt-second",
            "subscription_id": first["subscription_id"],
            "idempotency_key": "same-key",
            "event_type": "file.ready",
            "payload": {"path": "b.txt"},
            "status": "pending",
            "created_at": "2026-11-06T10:01:00",
            "updated_at": "2026-11-06T10:01:00",
        }
    )
    assert inserted_again is False
    assert duplicate["event_id"] == "evt-first"

    other, other_inserted = store.insert_external_event(
        {
            "event_id": "evt-other",
            "subscription_id": second["subscription_id"],
            "idempotency_key": "same-key",
            "event_type": "file.ready",
            "payload": {"path": "c.txt"},
            "status": "pending",
            "created_at": "2026-11-06T10:02:00",
            "updated_at": "2026-11-06T10:02:00",
        }
    )
    assert other_inserted is True
    assert other["event_id"] == "evt-other"


def test_node_target_must_be_execution_node_in_owner_scope(store, tmp_path: Path) -> None:
    root = tmp_path / "temp"
    task = SimpleNamespace(task_id="task:1", session_id="web:ceo-test")
    execution = SimpleNamespace(
        node_id="node:exec",
        task_id="task:1",
        node_kind="execution",
        status="in_progress",
    )
    acceptance = SimpleNamespace(
        node_id="node:check",
        task_id="task:1",
        node_kind="acceptance",
        status="in_progress",
    )
    other_task = SimpleNamespace(task_id="task:2", session_id="web:other")
    other_node = SimpleNamespace(
        node_id="node:other",
        task_id="task:2",
        node_kind="execution",
        status="in_progress",
    )
    service = _FakeTaskService(
        tasks={"task:1": task, "task:2": other_task},
        nodes={
            "node:exec": execution,
            "node:check": acceptance,
            "node:other": other_node,
        },
    )
    manager = EventSubscriptionManager(store, service)

    created = manager.create(
        runtime_context=_ceo_context(root),
        name="node-watch",
        instruction="continue node work",
        target_kind="node",
        task_id="1",
        node_id="exec",
        credential_file=str(root / "node.json"),
    )
    assert created["target_kind"] == "node"
    assert created["task_id"] == "task:1"
    assert created["node_id"] == "node:exec"

    with pytest.raises(ValueError, match="acceptance_node_target_not_allowed"):
        manager.create(
            runtime_context=_ceo_context(root),
            name="acceptance",
            instruction="x",
            target_kind="node",
            task_id="task:1",
            node_id="node:check",
            credential_file=str(root / "acceptance.json"),
        )
    with pytest.raises(ValueError, match="target_outside_session"):
        manager.create(
            runtime_context=_ceo_context(root),
            name="other",
            instruction="x",
            target_kind="node",
            node_id="node:other",
            credential_file=str(root / "other.json"),
        )

    execution_context = {
        "actor_role": "execution",
        "session_key": "web:ceo-test",
        "task_id": "task:1",
        "node_id": "node:exec",
        "task_temp_dir": str(root / "task-temp"),
    }
    self_target = manager.create(
        runtime_context=execution_context,
        name="self-target",
        instruction="wake this node",
        target_kind="node",
        credential_file=str(root / "task-temp" / "self.json"),
    )
    assert self_target["task_id"] == "task:1"
    assert self_target["node_id"] == "node:exec"

    with pytest.raises(ValueError, match="target_outside_task"):
        manager.create(
            runtime_context=execution_context,
            name="cross-task",
            instruction="x",
            target_kind="node",
            task_id="task:2",
            node_id="node:other",
            credential_file=str(root / "task-temp" / "cross.json"),
        )


def test_inspection_role_cannot_create_subscription(store, tmp_path: Path) -> None:
    manager = EventSubscriptionManager(store)
    with pytest.raises(ValueError, match="event_subscription_role_not_allowed"):
        manager.create(
            runtime_context={
                "actor_role": "inspection",
                "session_key": "web:ceo-test",
                "task_temp_dir": str(tmp_path),
            },
            name="no",
            instruction="x",
            credential_file=str(tmp_path / "credential.json"),
        )
