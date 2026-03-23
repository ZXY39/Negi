from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import g3ku.shells.web as web_shell
from g3ku.providers.base import LLMResponse, ToolCallRequest
from main.api.internal_rest import router as internal_router
from main.api.rest import router as rest_router
from main.api.websocket_task import router as task_ws_router
from main.models import NodeFinalResult, SpawnChildSpec, TaskRecord
from main.models import normalize_final_acceptance_metadata
from main.monitoring.models import TaskProjectionMetaRecord
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
        self._dedupe_keys: set[str] = set()

    async def start(self) -> None:
        self.started += 1

    def enqueue_task_terminal_payload(self, payload: dict[str, object] | None) -> bool:
        normalized = dict(payload or {})
        dedupe_key = str(normalized.get("dedupe_key") or "").strip()
        if dedupe_key and dedupe_key in self._dedupe_keys:
            return False
        if dedupe_key:
            self._dedupe_keys.add(dedupe_key)
        self.payloads.append(normalized)
        return True


def _mark_worker_online(service: MainRuntimeService, *, active_task_count: int = 0) -> None:
    service.store.upsert_worker_status(
        worker_id="worker:test",
        role="task_worker",
        status="running",
        updated_at=now_iso(),
        payload={"execution_mode": "worker", "active_task_count": int(active_task_count)},
    )


def _receive_until_type(ws, expected_type: str) -> dict[str, object]:
    while True:
        payload = ws.receive_json()
        if payload.get("type") == expected_type:
            return payload


async def _create_web_task(service: MainRuntimeService):
    _mark_worker_online(service)
    return await service.create_task("test task", session_id="web:shared")


def _mark_worker_at(
    service: MainRuntimeService,
    updated_at: str,
    *,
    status: str = "running",
    active_task_count: int = 0,
) -> None:
    service.store.upsert_worker_status(
        worker_id="worker:test",
        role="task_worker",
        status=status,
        updated_at=updated_at,
        payload={"execution_mode": "worker", "active_task_count": int(active_task_count)},
    )


def test_internal_task_terminal_callback_persists_pending_outbox_and_dedupes(tmp_path: Path, monkeypatch):
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
    assert entry["delivery_state"] == "pending"
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
    assert entry["delivery_state"] == "pending"
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


def test_global_tasks_websocket_pushes_worker_status_recovery(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    stale = (datetime.now(timezone.utc) - timedelta(seconds=30)).replace(microsecond=0).isoformat()
    _mark_worker_at(service, stale)
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect("/api/ws/tasks?session_id=all&after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "task.list.snapshot"
        assert snapshot["data"]["worker_online"] is False

        _mark_worker_online(service)

        worker_event = _receive_until_type(ws, "task.worker.status")
        assert worker_event["type"] == "task.worker.status"
        assert worker_event["data"]["worker_online"] is True
        assert worker_event["data"]["worker"]["worker_id"] == "worker:test"


def test_web_mode_worker_online_uses_relaxed_stale_window(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    recent_but_not_tiny = (datetime.now(timezone.utc) - timedelta(seconds=10)).replace(microsecond=0).isoformat()
    definitely_stale = (datetime.now(timezone.utc) - timedelta(seconds=20)).replace(microsecond=0).isoformat()

    _mark_worker_at(service, recent_but_not_tiny)
    assert service.is_worker_online() is True

    _mark_worker_at(service, definitely_stale)
    assert service.is_worker_online() is False


def test_web_mode_worker_online_extends_stale_window_for_active_tasks(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    active_but_still_recent = (datetime.now(timezone.utc) - timedelta(seconds=45)).replace(microsecond=0).isoformat()
    definitely_stale_even_with_grace = (datetime.now(timezone.utc) - timedelta(seconds=75)).replace(microsecond=0).isoformat()

    _mark_worker_at(service, active_but_still_recent, active_task_count=1)
    assert service.is_worker_online() is True

    _mark_worker_at(service, definitely_stale_even_with_grace, active_task_count=1)
    assert service.is_worker_online() is False


def test_web_mode_worker_online_treats_stopped_status_as_offline(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    _mark_worker_at(service, now_iso(), status="stopped")
    assert service.is_worker_online() is False


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


def test_task_detail_payload_and_websocket_include_model_call_events(tmp_path: Path, monkeypatch):
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
        event_type="task.model.call",
        created_at=now_iso(),
        payload={
            "task_id": record.task_id,
            "node_id": record.root_node_id,
            "call_index": 3,
            "prepared_message_count": 9,
            "prepared_message_chars": 1234,
            "response_tool_call_count": 2,
            "delta_usage": {
                "tracked": True,
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_hit_tokens": 40,
                "call_count": 1,
                "calls_with_usage": 1,
                "calls_without_usage": 0,
                "is_partial": False,
            },
            "delta_usage_by_model": [],
        },
    )
    payload = service.get_task_detail_payload(record.task_id, mark_read=False)

    assert payload is not None
    assert payload["progress"]["model_calls"][0]["call_index"] == 3
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/tasks/{record.task_id}?after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "task.snapshot"
        assert snapshot["data"]["progress"]["model_calls"][0]["call_index"] == 3

        service.store.append_task_event(
            task_id=record.task_id,
            session_id=record.session_id,
            event_type="task.model.call",
            created_at=now_iso(),
            payload={
                "task_id": record.task_id,
                "node_id": record.root_node_id,
                "call_index": 4,
                "prepared_message_count": 10,
                "prepared_message_chars": 1400,
                "response_tool_call_count": 0,
                "delta_usage": {
                    "tracked": True,
                    "input_tokens": 50,
                    "output_tokens": 5,
                    "cache_hit_tokens": 25,
                    "call_count": 1,
                    "calls_with_usage": 1,
                    "calls_without_usage": 0,
                    "is_partial": False,
                },
                "delta_usage_by_model": [],
            },
        )

        event = _receive_until_type(ws, "task.model.call")
        assert event["data"]["call_index"] == 4


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


def test_task_snapshot_preserves_auxiliary_acceptance_children(tmp_path: Path):
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
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=root,
        goal=f"最终验收:{root.goal}",
        acceptance_prompt="核对最终结果是否满足要求。",
        parent_node_id=root.node_id,
        metadata={"final_acceptance": True},
    )
    service.log_service.update_node_check_result(record.task_id, acceptance.node_id, "验收通过")
    service.log_service.update_node_status(
        record.task_id,
        acceptance.node_id,
        status="success",
        final_output="验收通过",
    )

    snapshot = service.get_task_detail_payload(record.task_id, mark_read=False)

    assert snapshot is not None
    tree_root = snapshot["tree_root"]
    auxiliary_children = tree_root["auxiliary_children"]

    assert [item["node_id"] for item in auxiliary_children] == [acceptance.node_id]
    assert auxiliary_children[0]["node_kind"] == "acceptance"
    assert acceptance.node_id in [item["node_id"] for item in tree_root["children"]]


def test_task_snapshot_preserves_nested_child_acceptance_children(tmp_path: Path):
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
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt"),
    )
    service.log_service.update_node_status(
        record.task_id,
        child.node_id,
        status="success",
        final_output="child done",
    )

    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=child,
        goal="accept:child goal",
        acceptance_prompt="检查 child 输出。",
        parent_node_id=child.node_id,
    )
    service.log_service.update_node_check_result(record.task_id, child.node_id, "child acceptance passed")
    service.log_service.update_node_check_result(record.task_id, acceptance.node_id, "验收通过")
    service.log_service.update_node_status(
        record.task_id,
        acceptance.node_id,
        status="success",
        final_output="验收通过",
    )

    snapshot = service.get_task_detail_payload(record.task_id, mark_read=False)

    assert snapshot is not None
    tree_root = snapshot["tree_root"]
    child_item = next(item for item in tree_root["children"] if item["node_id"] == child.node_id)

    assert [item["node_id"] for item in child_item["auxiliary_children"]] == [acceptance.node_id]
    assert child_item["auxiliary_children"][0]["node_kind"] == "acceptance"
    assert acceptance.node_id in [item["node_id"] for item in child_item["children"]]


def test_failed_acceptance_node_marks_execution_child_failed(tmp_path: Path):
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
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt"),
    )
    service.log_service.update_node_status(
        record.task_id,
        child.node_id,
        status="success",
        final_output="child done",
    )

    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=child,
        goal="accept:child goal",
        acceptance_prompt="检查 child 输出。",
        parent_node_id=child.node_id,
    )
    service.log_service.update_node_status(
        record.task_id,
        acceptance.node_id,
        status="failed",
        final_output="child acceptance failed",
        failure_reason="child acceptance failed",
    )

    latest_child = service.get_node(child.node_id)

    assert latest_child is not None
    assert latest_child.status == "failed"
    assert latest_child.final_output == "child done"
    assert latest_child.failure_reason == "child acceptance failed"


def test_failed_final_acceptance_node_marks_root_failed(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    import asyncio

    _mark_worker_online(service)
    record = asyncio.run(
        service.create_task(
            "root final acceptance",
            session_id="web:shared",
            metadata={
                "final_acceptance": {
                    "required": True,
                    "prompt": "核对最终结果是否满足要求。",
                }
            },
        )
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    service.log_service.update_node_status(
        record.task_id,
        root.node_id,
        status="success",
        final_output="root deliverable",
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=root,
        goal=f"最终验收:{root.goal}",
        acceptance_prompt="核对最终结果是否满足要求。",
        parent_node_id=root.node_id,
        metadata={"final_acceptance": True},
    )
    service.log_service.update_node_status(
        record.task_id,
        acceptance.node_id,
        status="failed",
        final_output="final acceptance failed",
        failure_reason="final acceptance failed",
    )

    latest_task = service.get_task(record.task_id)
    latest_root = service.get_node(record.root_node_id)
    final_acceptance = normalize_final_acceptance_metadata((latest_task.metadata or {}).get("final_acceptance")) if latest_task is not None else None

    assert latest_task is not None
    assert latest_root is not None
    assert latest_root.status == "failed"
    assert latest_root.final_output == "root deliverable"
    assert latest_root.failure_reason == "final acceptance failed"
    assert latest_task.status == "failed"
    assert latest_task.failure_reason == "final acceptance failed"
    assert final_acceptance is not None
    assert final_acceptance.status == "failed"
    assert latest_task.metadata.get("final_execution_output") == "root deliverable"


def test_view_progress_text_contains_only_status_and_stage_goal_tree(tmp_path: Path):
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
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    service.log_service.submit_next_stage(
        record.task_id,
        root.node_id,
        stage_goal="根阶段目标",
        tool_round_budget=1,
    )
    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt"),
    )
    service.log_service.submit_next_stage(
        record.task_id,
        child.node_id,
        stage_goal="子阶段目标",
        tool_round_budget=1,
    )
    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=child,
        goal="accept child",
        acceptance_prompt="检查 child 输出。",
        parent_node_id=child.node_id,
    )

    snapshot = service.get_task_detail_payload(record.task_id, mark_read=False)
    text = service.view_progress(record.task_id, mark_read=False)

    assert snapshot is not None
    assert text == (
        "Task status: in_progress\n"
        f"({root.node_id},in_progress,根阶段目标)\n"
        f"|-({child.node_id},in_progress,子阶段目标)\n"
        f"  |-({acceptance.node_id},in_progress,检验中)"
    )
    assert "Latest node output" not in text
    assert "Active parallel work:" not in text


def test_view_progress_tree_text_prefers_live_stage_goal_over_historical_goal(tmp_path: Path):
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
    root = service.get_node(record.root_node_id)

    assert root is not None

    service.log_service.submit_next_stage(
        record.task_id,
        root.node_id,
        stage_goal="旧阶段目标",
        tool_round_budget=1,
    )
    service.log_service.update_runtime_state(
        record.task_id,
        active_node_ids=[root.node_id],
        runnable_node_ids=[],
        waiting_node_ids=[],
        frames=[
            {
                "node_id": root.node_id,
                "depth": 0,
                "node_kind": "execution",
                "phase": "execution",
                "stage_mode": "自主执行",
                "stage_status": "进行中",
                "stage_goal": "最新阶段目标",
                "stage_total_steps": 1,
                "tool_calls": [],
                "child_pipelines": [],
            }
        ],
    )

    snapshot = service.get_task_detail_payload(record.task_id, mark_read=False)

    assert snapshot is not None
    assert snapshot["progress"]["tree_text"] == f"({root.node_id},in_progress,最新阶段目标)"


def test_running_node_output_does_not_pollute_final_output_in_projection(tmp_path: Path):
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
    service.log_service.append_node_output(
        record.task_id,
        record.root_node_id,
        content="still working on the task",
    )

    snapshot = service.get_task_detail_payload(record.task_id, mark_read=False)
    node_payload = service.get_node_detail_payload(record.task_id, record.root_node_id)

    assert snapshot is not None
    assert node_payload is not None
    assert node_payload["item"]["output"] == "still working on the task"
    assert node_payload["item"]["final_output"] == ""
    assert node_payload["item"]["execution_trace"]["final_output"] == ""

    root_progress_node = next(
        item for item in snapshot["progress"]["nodes"] if item["node_id"] == record.root_node_id
    )
    assert root_progress_node["execution_trace"]["final_output"] == ""


def test_task_projection_backfills_when_projection_version_is_stale(tmp_path: Path):
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

    detail_record = service.store.get_task_node_detail(record.root_node_id)
    assert detail_record is not None
    payload = dict(detail_record.payload or {})
    payload["execution_trace"] = {"final_output": "stale-final-output", "tool_steps": []}
    stale_detail = detail_record.model_copy(
        update={
            "final_output": "stale-final-output",
            "payload": payload,
        }
    )
    service.store.replace_task_node_details(record.task_id, [stale_detail])
    service.store.upsert_task_projection_meta(
        TaskProjectionMetaRecord(
            task_id=record.task_id,
            version=0,
            updated_at=now_iso(),
        )
    )

    restored = service.get_node_detail_payload(record.task_id, record.root_node_id)

    assert restored is not None
    assert restored["item"]["final_output"] == ""
    assert restored["item"]["execution_trace"]["final_output"] == ""


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


@pytest.mark.asyncio
async def test_pause_during_model_call_keeps_task_resumable_and_resume_finishes_same_task(tmp_path: Path):
    class _PauseableChatBackend:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.call_count = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.call_count += 1
            if self.call_count == 1:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
            if self.call_count == 2:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="call:stage",
                            name="submit_next_stage",
                            arguments={"stage_goal": "resume after pause", "tool_round_budget": 1},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage={"input_tokens": 8, "output_tokens": 4},
                )
            return LLMResponse(
                content='{"status":"success","delivery_status":"final","summary":"done","answer":"done","evidence":[{"kind":"artifact","note":"resume path completed"}],"remaining_work":[],"blocking_reason":""}',
                tool_calls=[],
                finish_reason="stop",
                usage={"input_tokens": 8, "output_tokens": 4},
            )

    backend = _PauseableChatBackend()
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )

    try:
        record = await service.create_task("pause and resume me", session_id="web:shared")
        await asyncio.wait_for(backend.started.wait(), timeout=1.0)

        await service.pause_task(record.task_id)

        paused = service.get_task(record.task_id)
        assert paused is not None
        assert paused.task_id == record.task_id
        assert paused.status == "in_progress"
        assert paused.is_paused is True
        assert paused.pause_requested is True
        assert backend.call_count == 1
        assert backend.cancelled.is_set() is True
        assert service.task_runner.is_active(record.task_id) is False

        root = service.get_node(record.root_node_id)
        assert root is not None
        assert root.status == "in_progress"
        assert root.failure_reason == ""

        await service.resume_task(record.task_id)
        finished = await asyncio.wait_for(service.wait_for_task(record.task_id), timeout=2.0)
        assert finished is not None
        assert finished.task_id == record.task_id
        assert finished.status == "success"
        assert finished.is_paused is False
        assert finished.pause_requested is False
        assert finished.failure_reason == ""
        assert backend.call_count == 3

        latest_root = service.get_node(record.root_node_id)
        assert latest_root is not None
        assert latest_root.status == "success"
        assert latest_root.failure_reason == ""
        assert len(service.store.list_tasks()) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pause_requested_after_valid_result_flushes_node_output_before_task_pauses(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )

    async def _pause_after_valid_result(**kwargs):
        task = kwargs["task"]
        service.log_service.set_pause_state(task.task_id, pause_requested=True, is_paused=True)
        return NodeFinalResult(
            status="success",
            delivery_status="final",
            summary="done",
            answer="done",
            evidence=[{"kind": "artifact", "note": "pause flush completed"}],
            remaining_work=[],
            blocking_reason="",
        )

    service.node_runner._react_loop.run = _pause_after_valid_result

    try:
        record = await service.create_task("pause after valid result", session_id="web:shared")

        for _ in range(100):
            current = service.get_task(record.task_id)
            if current is not None and current.is_paused and not service.task_runner.is_active(record.task_id):
                break
            await asyncio.sleep(0.01)

        paused = service.get_task(record.task_id)
        assert paused is not None
        assert paused.status in {"in_progress", "success"}
        assert paused.is_paused is True
        assert paused.pause_requested is True

        root = service.get_node(record.root_node_id)
        assert root is not None
        assert root.status == "success"
        assert root.final_output == "done"
        assert root.failure_reason == ""
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_startup_recovery_preserves_success_nodes_and_reuses_them_for_spawn(tmp_path: Path):
    store_path = tmp_path / "runtime.sqlite3"
    tasks_dir = tmp_path / "tasks"
    artifacts_dir = tmp_path / "artifacts"
    governance_path = tmp_path / "governance.sqlite3"

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=store_path,
        files_base_dir=tasks_dir,
        artifact_dir=artifacts_dir,
        governance_store_path=governance_path,
        execution_mode="embedded",
    )
    service.task_runner.start_background = lambda task_id: None

    try:
        record = await service.create_task("recover me", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)

        assert task is not None
        assert root is not None

        service.log_service.append_node_output(
            record.task_id,
            root.node_id,
            content="stale root output before crash",
        )

        success_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(goal="child goal", prompt="child prompt"),
        )
        service.log_service.update_node_status(
            record.task_id,
            success_child.node_id,
            status="success",
            final_output="child done",
        )

        in_progress_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(goal="bad child", prompt="bad prompt"),
        )
        nested_success = service.node_runner._create_execution_child(
            task=task,
            parent=in_progress_child,
            spec=SpawnChildSpec(goal="nested child", prompt="nested prompt"),
        )
        service.log_service.update_node_status(
            record.task_id,
            nested_success.node_id,
            status="success",
            final_output="nested done",
        )
        service.log_service.update_runtime_state(
            record.task_id,
            root_node_id=root.node_id,
            paused=False,
            pause_requested=False,
            cancel_requested=False,
            active_node_ids=[root.node_id, in_progress_child.node_id],
            runnable_node_ids=[root.node_id, in_progress_child.node_id],
            waiting_node_ids=[],
            frames=[
                service.log_service._default_frame(node_id=root.node_id, depth=root.depth, node_kind=root.node_kind, phase="before_model"),
                service.log_service._default_frame(
                    node_id=in_progress_child.node_id,
                    depth=in_progress_child.depth,
                    node_kind=in_progress_child.node_kind,
                    phase="before_model",
                ),
            ],
            publish_snapshot=False,
        )
    finally:
        await service.close()

    restarted = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=store_path,
        files_base_dir=tasks_dir,
        artifact_dir=artifacts_dir,
        governance_store_path=governance_path,
        execution_mode="embedded",
    )
    started: list[str] = []
    restarted.task_runner.start_background = lambda task_id: started.append(str(task_id))

    try:
        await restarted.startup()

        recovered_task = restarted.get_task(record.task_id)
        recovered_root = restarted.get_node(record.root_node_id)
        preserved_child = restarted.get_node(success_child.node_id)

        assert recovered_task is not None
        assert recovered_task.status == "in_progress"
        assert recovered_task.metadata.get("recovery_notice") == "本任务遇到异常停止，已回退到稳定步骤继续。"
        assert recovered_root is not None
        assert recovered_root.status == "in_progress"
        assert recovered_root.output == []
        assert recovered_root.final_output == ""
        assert recovered_root.failure_reason == ""
        assert preserved_child is not None
        assert preserved_child.status == "success"
        assert restarted.get_node(in_progress_child.node_id) is None
        assert restarted.get_node(nested_success.node_id) is None

        runtime_state = restarted.log_service.read_runtime_state(record.task_id)
        assert runtime_state is not None
        assert runtime_state["active_node_ids"] == [record.root_node_id]
        assert len(runtime_state["frames"]) == 1
        assert runtime_state["frames"][0]["node_id"] == record.root_node_id
        assert started == [record.task_id]
        assert "Recovery: 本任务遇到异常停止，已回退到稳定步骤继续。" not in restarted.view_progress(record.task_id, mark_read=False)

        before_child_ids = [node.node_id for node in restarted.store.list_children(record.root_node_id)]
        results = await restarted.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=record.root_node_id,
            specs=[SpawnChildSpec(goal="child goal", prompt="child prompt")],
            call_id="recovery-call",
        )
        after_child_ids = [node.node_id for node in restarted.store.list_children(record.root_node_id)]
        root_after_spawn = restarted.get_node(record.root_node_id)
        spawn_operations = dict((root_after_spawn.metadata or {}).get("spawn_operations") or {})
        recovery_entry = spawn_operations["recovery-call"]["entries"][0]

        assert len(results) == 1
        assert before_child_ids == after_child_ids
        assert recovery_entry["child_node_id"] == success_child.node_id
        assert "child done" in results[0].node_output
    finally:
        await restarted.close()
