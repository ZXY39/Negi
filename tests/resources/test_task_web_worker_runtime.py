from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import g3ku.shells.web as web_shell
from main.api.internal_rest import router as internal_router
from main.api.rest import router as rest_router
from main.api.websocket_task import router as task_ws_router
from main.models import TaskRecord
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService
from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_TOKEN_ENV,
    normalize_task_terminal_payload,
)


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(rest_router, prefix="/api")
    app.include_router(internal_router, prefix="/api")
    app.include_router(task_ws_router, prefix="/api")
    return app


class _HeartbeatRecorder:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.started = 0

    async def start(self) -> None:
        self.started += 1

    def enqueue_task_terminal_payload(self, payload: dict[str, object] | None) -> bool:
        self.payloads.append(dict(payload or {}))
        return True


def _mark_worker_online(service: MainRuntimeService) -> None:
    service.store.upsert_worker_status(
        worker_id="worker:test",
        role="task_worker",
        status="running",
        updated_at=now_iso(),
        payload={"execution_mode": "worker"},
    )


def _receive_until_type(ws, expected_type: str) -> dict[str, object]:
    while True:
        payload = ws.receive_json()
        if payload.get("type") == expected_type:
            return payload


async def _create_web_task(service: MainRuntimeService):
    _mark_worker_online(service)
    return await service.create_task("test task", session_id="web:shared")


def test_internal_task_terminal_callback_marks_outbox_delivered_and_dedupes(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    heartbeat = _HeartbeatRecorder()
    payload = normalize_task_terminal_payload(
        {
            "task_id": "task:demo",
            "session_id": "web:demo",
            "title": "demo",
            "status": "success",
            "brief_text": "done",
            "finished_at": now_iso(),
        }
    )
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "secret-token")
    monkeypatch.setattr("main.api.internal_rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.internal_rest.get_web_heartbeat_service", lambda _agent=None: heartbeat)

    async def _ensure_services(_agent=None) -> None:
        return None

    monkeypatch.setattr("main.api.internal_rest.ensure_web_runtime_services", _ensure_services)

    client = TestClient(_build_app())
    response = client.post(
        "/api/internal/task-terminal",
        json=payload,
        headers={"x-g3ku-internal-token": "secret-token"},
    )
    assert response.status_code == 200
    assert response.json()["duplicate"] is False

    duplicate = client.post(
        "/api/internal/task-terminal",
        json=payload,
        headers={"x-g3ku-internal-token": "secret-token"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    entry = service.store.get_task_terminal_outbox(str(payload.get("dedupe_key") or ""))
    assert entry is not None
    assert entry["delivery_state"] == "delivered"
    assert len(heartbeat.payloads) == 1


@pytest.mark.asyncio
async def test_ensure_web_runtime_services_replays_pending_task_terminal_outbox(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    heartbeat = _HeartbeatRecorder()
    payload = normalize_task_terminal_payload(
        {
            "task_id": "task:replay",
            "session_id": "web:replay",
            "title": "replay",
            "status": "success",
            "brief_text": "replayed",
            "finished_at": now_iso(),
        }
    )
    service.store.put_task_terminal_outbox(
        dedupe_key=str(payload.get("dedupe_key") or ""),
        task_id=str(payload.get("task_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        created_at=str(payload.get("finished_at") or now_iso()),
        payload=payload,
    )
    monkeypatch.setattr(web_shell, "get_web_heartbeat_service", lambda _agent=None: heartbeat)

    await web_shell.ensure_web_runtime_services(SimpleNamespace(main_task_service=service))

    entry = service.store.get_task_terminal_outbox(str(payload.get("dedupe_key") or ""))
    assert entry is not None
    assert entry["delivery_state"] == "delivered"
    assert heartbeat.started == 1
    assert heartbeat.payloads == [payload]


def test_worker_task_terminal_listener_persists_outbox_and_schedules_delivery(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    scheduled: list[str] = []
    service._schedule_task_terminal_delivery = lambda dedupe_key: scheduled.append(str(dedupe_key))
    finished_at = now_iso()
    task = TaskRecord(
        task_id="task:demo",
        session_id="web:demo",
        title="demo",
        user_request="demo",
        status="success",
        root_node_id="node:root",
        max_depth=0,
        cancel_requested=False,
        pause_requested=False,
        is_paused=False,
        is_unread=True,
        brief_text="done",
        created_at=finished_at,
        updated_at=finished_at,
        finished_at=finished_at,
        final_output="",
        failure_reason="",
        metadata={},
    )

    service._enqueue_task_terminal_callback(task)

    pending = service.store.list_pending_task_terminal_outbox(limit=10)
    assert len(pending) == 1
    assert pending[0]["task_id"] == "task:demo"
    assert scheduled == [pending[0]["dedupe_key"]]


def test_web_mode_create_task_enqueues_command_without_running(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    started: list[str] = []
    service.task_runner.start_background = lambda task_id: started.append(str(task_id))

    import asyncio

    record = asyncio.run(_create_web_task(service))
    commands = service.store.claim_pending_task_commands(
        worker_id="worker:claim",
        claimed_at=now_iso(),
        limit=10,
    )

    assert started == []
    assert record.task_id.startswith("task:")
    assert [item["command_type"] for item in commands] == ["create_task"]
    assert commands[0]["task_id"] == record.task_id


def test_global_tasks_websocket_reads_sqlite_events(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    import asyncio

    record = asyncio.run(_create_web_task(service))
    existing_events = service.store.list_task_events(after_seq=0, limit=100)
    after_seq = max((int(item.get("seq") or 0) for item in existing_events), default=0)
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/tasks?session_id=all&after_seq={after_seq}") as ws:
        assert ws.receive_json()["type"] == "hello"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "task.list.snapshot"
        assert snapshot["data"]["items"][0]["task_id"] == record.task_id

        service.store.append_task_event(
            task_id=record.task_id,
            session_id=record.session_id,
            event_type="task.list.patch",
            created_at=now_iso(),
            payload={"task": {"task_id": record.task_id, "brief": "patched"}},
        )

        patch_event = _receive_until_type(ws, "task.list.patch")
        assert patch_event["type"] == "task.list.patch"
        assert patch_event["data"]["task"]["brief"] == "patched"


def test_global_tasks_websocket_does_not_replay_historical_patches_after_snapshot(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    import asyncio

    record = asyncio.run(_create_web_task(service))
    service.store.append_task_event(
        task_id=record.task_id,
        session_id=record.session_id,
        event_type="task.list.patch",
        created_at=now_iso(),
        payload={"task": {"task_id": record.task_id, "brief": "historical"}},
    )
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect("/api/ws/tasks?session_id=all&after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "task.list.snapshot"

        service.store.append_task_event(
            task_id=record.task_id,
            session_id=record.session_id,
            event_type="task.list.patch",
            created_at=now_iso(),
            payload={"task": {"task_id": record.task_id, "brief": "fresh"}},
        )

        patch_event = _receive_until_type(ws, "task.list.patch")
        assert patch_event["data"]["task"]["brief"] == "fresh"


def test_task_detail_websocket_streams_runtime_updates(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    import asyncio

    record = asyncio.run(_create_web_task(service))
    existing_events = service.store.list_task_events(after_seq=0, task_id=record.task_id, limit=100)
    after_seq = max((int(item.get("seq") or 0) for item in existing_events), default=0)
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/tasks/{record.task_id}?after_seq={after_seq}") as ws:
        assert ws.receive_json()["type"] == "hello"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "task.snapshot"
        assert snapshot["data"]["task"]["task_id"] == record.task_id

        service.store.append_task_event(
            task_id=record.task_id,
            session_id=record.session_id,
            event_type="task.runtime.updated",
            created_at=now_iso(),
            payload={"task_id": record.task_id, "runtime_summary": {"active_node_ids": [record.root_node_id], "runnable_node_ids": [], "waiting_node_ids": [], "frames": []}},
        )

        runtime_event = _receive_until_type(ws, "task.runtime.updated")
        assert runtime_event["type"] == "task.runtime.updated"
        assert runtime_event["data"]["runtime_summary"]["active_node_ids"] == [record.root_node_id]


def test_task_detail_websocket_does_not_replay_historical_runtime_updates_after_snapshot(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    import asyncio

    record = asyncio.run(_create_web_task(service))
    service.store.append_task_event(
        task_id=record.task_id,
        session_id=record.session_id,
        event_type="task.runtime.updated",
        created_at=now_iso(),
        payload={"task_id": record.task_id, "runtime_summary": {"active_node_ids": ["historical"], "runnable_node_ids": [], "waiting_node_ids": [], "frames": []}},
    )
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/tasks/{record.task_id}?after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "task.snapshot"

        service.store.append_task_event(
            task_id=record.task_id,
            session_id=record.session_id,
            event_type="task.runtime.updated",
            created_at=now_iso(),
            payload={"task_id": record.task_id, "runtime_summary": {"active_node_ids": [record.root_node_id], "runnable_node_ids": [], "waiting_node_ids": [], "frames": []}},
        )

        runtime_event = _receive_until_type(ws, "task.runtime.updated")
        assert runtime_event["data"]["runtime_summary"]["active_node_ids"] == [record.root_node_id]


def test_task_projection_tables_are_populated_and_used_for_node_detail(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    import asyncio

    record = asyncio.run(_create_web_task(service))
    snapshot = service.get_task_detail_payload(record.task_id, mark_read=False)

    assert snapshot is not None
    assert service.store.list_task_nodes(record.task_id)
    assert service.store.list_task_node_details(record.task_id)
    assert service.store.list_task_runtime_frames(record.task_id)

    detail_record = service.store.get_task_node_detail(record.root_node_id)
    assert detail_record is not None
    payload = dict(detail_record.payload or {})
    payload["output_text"] = "projection-output"
    payload["execution_trace"] = {"final_output": "projection-output", "tool_steps": []}
    detail_record = detail_record.model_copy(update={"output_text": "projection-output", "payload": payload})
    service.store.replace_task_node_details(record.task_id, [detail_record])

    node_payload = service.get_node_detail_payload(record.task_id, record.root_node_id)

    assert node_payload is not None
    assert node_payload["item"]["output"] == "projection-output"


def test_task_projection_backfills_when_rows_are_missing(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    import asyncio

    record = asyncio.run(_create_web_task(service))
    initial = service.get_task_detail_payload(record.task_id, mark_read=False)
    assert initial is not None

    with service.store._lock, service.store._conn:
        service.store._conn.execute("DELETE FROM task_nodes WHERE task_id = ?", (record.task_id,))
        service.store._conn.execute("DELETE FROM task_node_details WHERE task_id = ?", (record.task_id,))
        service.store._conn.execute("DELETE FROM task_runtime_frames WHERE task_id = ?", (record.task_id,))

    restored = service.get_task_detail_payload(record.task_id, mark_read=False)

    assert restored is not None
    assert service.store.list_task_nodes(record.task_id)
    assert service.store.list_task_node_details(record.task_id)


@pytest.mark.asyncio
async def test_worker_commands_call_pause_and_cancel_handlers(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )

    paused: list[str] = []
    cancelled: list[str] = []

    async def _pause(task_id: str) -> None:
        paused.append(task_id)

    async def _cancel(task_id: str) -> None:
        cancelled.append(task_id)

    service.task_runner.pause = _pause
    service.task_runner.cancel = _cancel

    await service._process_worker_command({"command_type": "pause_task", "task_id": "demo"})
    await service._process_worker_command({"command_type": "cancel_task", "task_id": "demo"})

    assert paused == ["task:demo"]
    assert cancelled == ["task:demo"]


@pytest.mark.asyncio
async def test_pause_task_cancels_active_background_run_without_marking_failed(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )

    started = asyncio.Event()
    blocker = asyncio.Event()

    async def _blocking_run_node(task_id: str, node_id: str):
        started.set()
        await blocker.wait()
        raise AssertionError("pause should cancel the background run before unblock")

    service.node_runner.run_node = _blocking_run_node

    try:
        record = await service.create_task("pause me", session_id="web:shared")
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await service.pause_task(record.task_id)

        paused = service.get_task(record.task_id)
        assert paused is not None
        assert paused.status == "in_progress"
        assert paused.is_paused is True
        assert paused.pause_requested is True
        assert service.task_runner.is_active(record.task_id) is False

        root = service.get_node(paused.root_node_id)
        assert root is not None
        assert root.status == "in_progress"
        assert root.failure_reason == ""
    finally:
        blocker.set()
        await service.close()
