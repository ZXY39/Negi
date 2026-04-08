from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import g3ku.shells.web as web_shell
from g3ku.agent.tools.base import Tool
from g3ku.providers.base import LLMModelAttempt, LLMResponse, ToolCallRequest
from main.api.internal_rest import router as internal_router
from main.api.rest import router as rest_router
from main.api.websocket_task import router as task_ws_router
from main.models import (
    NodeFinalResult,
    SpawnChildResult,
    SpawnChildSpec,
    TaskRecord,
    normalize_final_acceptance_metadata,
)
from main.protocol import now_iso
from main.runtime.node_runner import SKIPPED_CHECK_RESULT
from main.service.runtime_service import MainRuntimeService
from main.service.task_stall_callback import normalize_task_stall_payload
from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_TOKEN_ENV,
    TASK_TERMINAL_CALLBACK_URL_ENV,
    normalize_task_terminal_payload,
    save_task_terminal_callback_config,
)


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


class _SpawnReviewToolCallChatBackend:
    def __init__(self, *, arguments: dict[str, object]) -> None:
        self.calls: list[dict[str, object]] = []
        self._arguments = dict(arguments)

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call:spawn-review",
                    name="review_spawn_candidates",
                    arguments=dict(self._arguments),
                )
            ],
            finish_reason="tool_calls",
        )


class _SpawnReviewRetryChatBackend:
    def __init__(self, *, responses: list[LLMResponse]) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = list(responses)

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self._responses:
            raise AssertionError("spawn review retry backend exhausted")
        return self._responses.pop(0)


class _SpawnReviewExceptionChatBackend:
    def __init__(self, *, message: str) -> None:
        self.calls: list[dict[str, object]] = []
        self._message = str(message or "spawn review failed")

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        raise RuntimeError(self._message)


class _StaticTool(Tool):
    def __init__(self, name: str, result: str = "ok") -> None:
        self._name = name
        self._result = result

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        _ = kwargs
        return self._result


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(rest_router, prefix="/api")
    app.include_router(internal_router, prefix="/api")
    app.include_router(task_ws_router, prefix="/api")
    return app


class _HeartbeatRecorder:
    def __init__(self, *, reject_task_terminal_reason: str = "") -> None:
        self.payloads: list[dict[str, object]] = []
        self.stall_payloads: list[dict[str, object]] = []
        self.started = 0
        self._dedupe_keys: set[str] = set()
        self._reject_task_terminal_reason = str(reject_task_terminal_reason or "").strip()
        self._task_terminal_rejection_reasons: dict[str, str] = {}

    async def start(self) -> None:
        self.started += 1

    def enqueue_task_terminal_payload(self, payload: dict[str, object] | None) -> bool:
        normalized = dict(payload or {})
        dedupe_key = str(normalized.get("dedupe_key") or "").strip()
        if self._reject_task_terminal_reason:
            if dedupe_key:
                self._task_terminal_rejection_reasons[dedupe_key] = self._reject_task_terminal_reason
            return False
        if dedupe_key and dedupe_key in self._dedupe_keys:
            return False
        if dedupe_key:
            self._dedupe_keys.add(dedupe_key)
        self.payloads.append(normalized)
        return True

    def task_terminal_rejection_reason(self, dedupe_key: str) -> str:
        return self._task_terminal_rejection_reasons.get(str(dedupe_key or "").strip(), "")

    def enqueue_task_stall_payload(self, payload: dict[str, object] | None) -> bool:
        normalized = dict(payload or {})
        dedupe_key = str(normalized.get("dedupe_key") or "").strip()
        if dedupe_key and dedupe_key in self._dedupe_keys:
            return False
        if dedupe_key:
            self._dedupe_keys.add(dedupe_key)
        self.stall_payloads.append(normalized)
        return True


@pytest.fixture(autouse=True)
def _unlock_task_websocket_runtime(monkeypatch):
    class _Security:
        def is_unlocked(self) -> bool:
            return True

    monkeypatch.setattr("main.api.websocket_task.get_bootstrap_security_service", lambda: _Security())


def _mark_worker_online(service: MainRuntimeService, *, active_task_count: int = 0) -> None:
    _mark_worker_at(service, now_iso(), active_task_count=active_task_count)


def _receive_until_type(ws, expected_type: str, predicate=None) -> dict[str, object]:
    while True:
        payload = ws.receive_json()
        if payload.get("type") == expected_type and (predicate is None or bool(predicate(payload))):
            return payload


async def _create_web_task(service: MainRuntimeService):
    _mark_worker_online(service)
    return await service.create_task("test task", session_id="web:shared")


def _execution_policy(mode: str = "focus") -> dict[str, str]:
    return {"mode": mode}


def _mark_worker_at(
    service: MainRuntimeService,
    updated_at: str,
    *,
    status: str = "running",
    active_task_count: int = 0,
) -> None:
    item = {
        "worker_id": "worker:test",
        "role": "task_worker",
        "status": status,
        "updated_at": updated_at,
        "payload": {"execution_mode": "worker", "active_task_count": int(active_task_count)},
    }
    service.store.upsert_worker_status(
        worker_id=str(item["worker_id"]),
        role=str(item["role"]),
        status=str(item["status"]),
        updated_at=str(item["updated_at"]),
        payload=dict(item["payload"]),
    )
    service.publish_worker_status_event(item=item)


def _publish_task_live_patch(service: MainRuntimeService, task_id: str) -> None:
    task = service.get_task(task_id)
    assert task is not None
    service.log_service._publish_task_live_patch_locked(task=task)


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


def _install_allow_all_spawn_review(service: MainRuntimeService, monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    async def _allow_all_review(*, task, parent, specs, cache_key):
        return {
            "reviewed_at": now_iso(),
            "requested_specs": [
                service.node_runner._spawn_review_requested_spec_payload(index=index, spec=spec)
                for index, spec in enumerate(specs)
            ],
            "allowed_indexes": list(range(len(list(specs or [])))),
            "blocked_specs": [],
            "error_text": "",
        }

    if monkeypatch is not None:
        monkeypatch.setattr(service.node_runner, "_review_spawn_batch", _allow_all_review)
        return
    service.node_runner._review_spawn_batch = _allow_all_review  # type: ignore[method-assign]


def _record_enqueue_calls(target: list[str]):
    async def _enqueue(task_id: str) -> None:
        target.append(str(task_id))

    return _enqueue


def _create_pending_tool_round(
    service: MainRuntimeService,
    *,
    task_id: str,
    node_id: str,
    tool_calls: list[dict[str, object]],
    live_tool_calls: list[dict[str, object]] | None = None,
    content: str = "assistant tool turn",
) -> dict[str, object]:
    service.log_service.submit_next_stage(
        task_id,
        node_id,
        stage_goal="recovery test stage",
        tool_round_budget=1,
    )
    round_payload = service.log_service.record_execution_stage_round(
        task_id,
        node_id,
        tool_calls=tool_calls,
        created_at=now_iso(),
    )
    assert round_payload is not None
    service.log_service.append_node_output(
        task_id,
        node_id,
        content=content,
        tool_calls=tool_calls,
    )
    service.log_service.update_frame(
        task_id,
        node_id,
        lambda frame: {
            **frame,
            "node_id": node_id,
            "phase": "waiting_tool_results",
            "pending_tool_calls": [dict(item) for item in tool_calls],
            "tool_calls": [dict(item) for item in list(live_tool_calls or [])],
            "active_round_id": str(round_payload.get("round_id") or ""),
            "active_round_tool_call_ids": [
                str(item.get("id") or "") for item in tool_calls if str(item.get("id") or "").strip()
            ],
            "active_round_started_at": str(round_payload.get("created_at") or now_iso()),
        },
        publish_snapshot=False,
    )
    return round_payload


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


def test_internal_task_terminal_callback_records_rejected_enqueue_reason(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    heartbeat = _HeartbeatRecorder(reject_task_terminal_reason="manual_pause_waiting_reason")
    payload = normalize_task_terminal_payload(
        {
            "task_id": "task:demo-rejected",
            "session_id": "web:demo",
            "title": "demo",
            "status": "failed",
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
    assert response.json()["accepted"] is False
    assert response.json()["rejected_reason"] == "manual_pause_waiting_reason"

    entry = service.store.get_task_terminal_outbox(str(payload.get("dedupe_key") or ""))
    assert entry is not None
    assert entry["accepted"] is False
    assert entry["rejected_reason"] == "manual_pause_waiting_reason"
    assert heartbeat.payloads == []


def test_internal_task_stall_callback_persists_pending_outbox_and_dedupes(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    heartbeat = _HeartbeatRecorder()
    payload = normalize_task_stall_payload(
        {
            "task_id": "task:demo",
            "session_id": "web:demo",
            "title": "demo",
            "bucket_minutes": 10,
            "stalled_minutes": 12,
            "last_visible_output_at": now_iso(),
            "brief_text": "stalled",
            "latest_node_summary": "node waiting for output",
            "runtime_summary_excerpt": "root phase=waiting_tool_results tools=1/1",
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
        "/api/internal/task-stall",
        json=payload,
        headers={"x-g3ku-internal-token": "secret-token"},
    )
    assert response.status_code == 200
    assert response.json()["duplicate"] is False

    duplicate = client.post(
        "/api/internal/task-stall",
        json=payload,
        headers={"x-g3ku-internal-token": "secret-token"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    entry = service.store.get_task_stall_outbox(str(payload.get("dedupe_key") or ""))
    assert entry is not None
    assert entry["delivery_state"] == "pending"
    assert len(heartbeat.stall_payloads) == 1


def test_internal_task_event_callback_forwards_live_patch(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "secret-token")
    monkeypatch.setattr("main.api.internal_rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    async def _ensure_services(_agent=None) -> None:
        return None

    monkeypatch.setattr("main.api.internal_rest.ensure_web_runtime_services", _ensure_services)

    client = TestClient(_build_app())
    record = asyncio.run(_create_web_task(service))
    with client.websocket_connect(f"/api/ws/tasks/{record.task_id}?after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"

        payload = {
            "event_type": "task.live.patch",
            "session_id": record.session_id,
            "task_id": record.task_id,
            "data": {
                "task_id": record.task_id,
                "runtime_summary": {"active_node_ids": [record.root_node_id], "runnable_node_ids": [], "waiting_node_ids": [], "frames": []},
            },
        }
        response = client.post(
            "/api/internal/task-event",
            json=payload,
            headers={"x-g3ku-internal-token": "secret-token"},
        )
        assert response.status_code == 200
        assert response.json()["accepted"] is True

        pushed = _receive_until_type(ws, "task.live.patch")
        assert pushed["data"]["task_id"] == record.task_id


def test_internal_task_event_batch_callback_forwards_summary_patches(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "secret-token")
    monkeypatch.setattr("main.api.internal_rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    async def _ensure_services(_agent=None) -> None:
        return None

    monkeypatch.setattr("main.api.internal_rest.ensure_web_runtime_services", _ensure_services)

    client = TestClient(_build_app())
    first = asyncio.run(_create_web_task(service))
    second = asyncio.run(_create_web_task(service))
    with client.websocket_connect("/api/ws/tasks?session_id=all&after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "task.worker.status"

        payload = {
            "items": [
                {
                    "event_type": "task.summary.patch",
                    "session_id": first.session_id,
                    "task_id": first.task_id,
                    "data": {"task": {"task_id": first.task_id, "session_id": first.session_id, "title": first.title, "brief": "patched-one"}},
                },
                {
                    "event_type": "task.summary.patch",
                    "session_id": second.session_id,
                    "task_id": second.task_id,
                    "data": {"task": {"task_id": second.task_id, "session_id": second.session_id, "title": second.title, "brief": "patched-two"}},
                },
            ]
        }
        response = client.post(
            "/api/internal/task-event-batch",
            json=payload,
            headers={"x-g3ku-internal-token": "secret-token"},
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 2

        first_event = _receive_until_type(ws, "task.summary.patch")
        second_event = _receive_until_type(ws, "task.summary.patch")
        briefs = {
            str(first_event["data"]["task"]["task_id"]): str(first_event["data"]["task"]["brief"]),
            str(second_event["data"]["task"]["task_id"]): str(second_event["data"]["task"]["brief"]),
        }
        assert briefs[first.task_id] == "patched-one"
        assert briefs[second.task_id] == "patched-two"


def test_task_list_websocket_streams_token_patch_events(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    record = asyncio.run(_create_web_task(service))
    with client.websocket_connect("/api/ws/tasks?session_id=all&after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "task.worker.status"

        service.log_service.append_node_output(
            record.task_id,
            record.root_node_id,
            content="token update",
            tool_calls=[],
            usage_attempts=[
                LLMModelAttempt(
                    model_key="gpt-5.4",
                    provider_id="openai",
                    provider_model="gpt-5.4",
                    usage={"input_tokens": 7, "output_tokens": 3, "cache_hit_tokens": 1},
                )
            ],
        )

        token_event = _receive_until_type(ws, "task.token.patch")
        assert token_event["data"]["task_id"] == record.task_id
        assert token_event["data"]["token_usage"]["input_tokens"] >= 7
        assert token_event["data"]["token_usage"]["output_tokens"] >= 3


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
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    async def _skip_china(_agent=None) -> None:
        return None

    async def _start_heartbeat(_agent, _runtime_manager, **kwargs):
        if kwargs.get("replay_pending_outbox"):
            for entry in service.store.list_pending_task_terminal_outbox(limit=500):
                heartbeat.enqueue_task_terminal_payload(dict(entry.get("payload") or {}))
        await heartbeat.start()
        return heartbeat

    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)
    monkeypatch.setattr(web_shell, "_ensure_china_bridge_services", _skip_china)

    await web_shell.ensure_web_runtime_services(SimpleNamespace(main_task_service=service))

    entry = service.store.get_task_terminal_outbox(str(payload.get("dedupe_key") or ""))
    assert entry is not None
    assert entry["delivery_state"] == "pending"
    assert heartbeat.started == 1
    assert heartbeat.payloads == [payload]


@pytest.mark.asyncio
async def test_ensure_web_runtime_services_replays_pending_task_stall_outbox(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    heartbeat = _HeartbeatRecorder()
    payload = normalize_task_stall_payload(
        {
            "task_id": "task:replay-stall",
            "session_id": "web:replay",
            "title": "replay",
            "bucket_minutes": 20,
            "stalled_minutes": 24,
            "last_visible_output_at": now_iso(),
            "brief_text": "stalled",
            "latest_node_summary": "latest node",
            "runtime_summary_excerpt": "runtime excerpt",
        }
    )
    service.store.put_task_stall_outbox(
        dedupe_key=str(payload.get("dedupe_key") or ""),
        task_id=str(payload.get("task_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        created_at=str(payload.get("last_visible_output_at") or now_iso()),
        payload=payload,
    )
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    async def _skip_china(_agent=None) -> None:
        return None

    async def _start_heartbeat(_agent, _runtime_manager, **kwargs):
        if kwargs.get("replay_pending_outbox"):
            for entry in service.store.list_pending_task_stall_outbox(limit=500):
                heartbeat.enqueue_task_stall_payload(dict(entry.get("payload") or {}))
        await heartbeat.start()
        return heartbeat

    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)
    monkeypatch.setattr(web_shell, "_ensure_china_bridge_services", _skip_china)

    await web_shell.ensure_web_runtime_services(SimpleNamespace(main_task_service=service))

    entry = service.store.get_task_stall_outbox(str(payload.get("dedupe_key") or ""))
    assert entry is not None
    assert entry["delivery_state"] == "pending"
    assert heartbeat.started == 1
    assert heartbeat.stall_payloads == [payload]


@pytest.mark.asyncio
async def test_ensure_web_runtime_services_starts_managed_worker(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    heartbeat = _HeartbeatRecorder()
    worker_calls: list[tuple[MainRuntimeService, float]] = []

    async def _ensure_worker(current_service, *, wait_timeout_s: float = 5.0) -> bool:
        worker_calls.append((current_service, float(wait_timeout_s)))
        return True

    async def _skip_china(_agent=None) -> None:
        return None

    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    async def _start_heartbeat(_agent, _runtime_manager, **kwargs):
        _ = kwargs
        await heartbeat.start()
        return heartbeat

    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)
    monkeypatch.setattr(web_shell, "ensure_managed_task_worker", _ensure_worker)
    monkeypatch.setattr(web_shell, "_ensure_china_bridge_services", _skip_china)

    await web_shell.ensure_web_runtime_services(SimpleNamespace(main_task_service=service))

    assert worker_calls == [(service, 1.0)]
    assert heartbeat.started == 1


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


def test_worker_task_stall_emit_persists_outbox_and_schedules_delivery(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    scheduled: list[str] = []
    service._schedule_task_stall_delivery = lambda dedupe_key: scheduled.append(str(dedupe_key))
    payload = normalize_task_stall_payload(
        {
            "task_id": "task:demo-stall",
            "session_id": "web:demo",
            "title": "demo",
            "bucket_minutes": 5,
            "stalled_minutes": 6,
            "last_visible_output_at": now_iso(),
            "brief_text": "stalled",
            "latest_node_summary": "node summary",
            "runtime_summary_excerpt": "runtime summary",
        }
    )

    service.emit_task_stall(payload)

    pending = service.store.list_pending_task_stall_outbox(limit=10)
    assert len(pending) == 1
    assert pending[0]["task_id"] == "task:demo-stall"
    assert scheduled == [pending[0]["dedupe_key"]]


def test_worker_task_status_persists_outbox_and_schedules_delivery(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    scheduled: list[str] = []
    service._schedule_task_worker_status_delivery = lambda worker_id: scheduled.append(str(worker_id))

    payload = service.publish_worker_status_event(
        item={
            "worker_id": "worker:test",
            "role": "task_worker",
            "status": "running",
            "updated_at": now_iso(),
            "payload": {"execution_mode": "worker", "active_task_count": 0},
        }
    )

    pending = service.store.list_pending_task_worker_status_outbox(limit=10)
    assert len(pending) == 1
    assert pending[0]["worker_id"] == "worker:test"
    assert pending[0]["payload"]["event_type"] == "task.worker.status"
    assert pending[0]["payload"]["data"]["worker"]["worker_id"] == "worker:test"
    assert payload["worker_state"] == "online"
    assert scheduled == ["worker:test"]


def test_worker_task_status_outbox_keeps_latest_payload(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    service._schedule_task_worker_status_delivery = lambda _worker_id: None

    first_updated_at = now_iso()
    second_updated_at = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
    service.publish_worker_status_event(
        item={
            "worker_id": "worker:test",
            "role": "task_worker",
            "status": "running",
            "updated_at": first_updated_at,
            "payload": {"execution_mode": "worker", "active_task_count": 0},
        }
    )
    service.publish_worker_status_event(
        item={
            "worker_id": "worker:test",
            "role": "task_worker",
            "status": "running",
            "updated_at": second_updated_at,
            "payload": {"execution_mode": "worker", "active_task_count": 1},
        }
    )

    pending = service.store.list_pending_task_worker_status_outbox(limit=10)
    assert len(pending) == 1
    assert pending[0]["payload"]["data"]["worker"]["updated_at"] == second_updated_at
    assert pending[0]["payload"]["data"]["worker"]["payload"]["active_task_count"] == 1


def test_worker_task_summary_persists_outbox_and_schedules_delivery(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    scheduled: list[str] = []
    service._schedule_task_summary_delivery = lambda task_id=None: scheduled.append(str(task_id or ""))

    updated_at = now_iso()
    service._schedule_task_event_callback(
        {
            "event_type": "task.summary.patch",
            "session_id": "web:shared",
            "task_id": "task:demo-summary",
            "data": {
                "task": {
                    "task_id": "task:demo-summary",
                    "session_id": "web:shared",
                    "title": "demo",
                    "updated_at": updated_at,
                    "token_usage": {"tracked": True, "input_tokens": 12, "output_tokens": 4, "cache_hit_tokens": 2},
                }
            },
        }
    )

    pending = service.store.list_pending_task_summary_outbox(limit=10)
    assert len(pending) == 1
    assert pending[0]["task_id"] == "task:demo-summary"
    assert pending[0]["payload"]["event_type"] == "task.summary.patch"
    assert pending[0]["payload"]["data"]["task"]["updated_at"] == updated_at
    assert scheduled == ["task:demo-summary"]


def test_worker_task_summary_outbox_keeps_latest_payload(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    service._schedule_task_summary_delivery = lambda _task_id=None: None

    first_updated_at = now_iso()
    second_updated_at = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
    service._schedule_task_event_callback(
        {
            "event_type": "task.summary.patch",
            "session_id": "web:shared",
            "task_id": "task:demo-summary",
            "data": {
                "task": {
                    "task_id": "task:demo-summary",
                    "session_id": "web:shared",
                    "title": "demo",
                    "updated_at": first_updated_at,
                    "token_usage": {"tracked": True, "input_tokens": 3, "output_tokens": 1, "cache_hit_tokens": 0},
                }
            },
        }
    )
    service._schedule_task_event_callback(
        {
            "event_type": "task.summary.patch",
            "session_id": "web:shared",
            "task_id": "task:demo-summary",
            "data": {
                "task": {
                    "task_id": "task:demo-summary",
                    "session_id": "web:shared",
                    "title": "demo",
                    "updated_at": second_updated_at,
                    "token_usage": {"tracked": True, "input_tokens": 9, "output_tokens": 5, "cache_hit_tokens": 4},
                }
            },
        }
    )

    pending = service.store.list_pending_task_summary_outbox(limit=10)
    assert len(pending) == 1
    assert pending[0]["version"] == 2
    assert pending[0]["payload"]["data"]["task"]["updated_at"] == second_updated_at
    assert pending[0]["payload"]["data"]["task"]["token_usage"]["input_tokens"] == 9


@pytest.mark.asyncio
async def test_worker_task_summary_outbox_retries_and_marks_delivered(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    service._schedule_task_summary_delivery = lambda _task_id=None: None
    service._schedule_task_event_callback(
        {
            "event_type": "task.summary.patch",
            "session_id": "web:shared",
            "task_id": "task:demo-summary",
            "data": {
                "task": {
                    "task_id": "task:demo-summary",
                    "session_id": "web:shared",
                    "title": "demo",
                    "updated_at": now_iso(),
                    "token_usage": {"tracked": True, "input_tokens": 6, "output_tokens": 2, "cache_hit_tokens": 1},
                }
            },
        }
    )
    monkeypatch.setenv("G3KU_INTERNAL_CALLBACK_URL", "http://127.0.0.1:18790/api/internal/task-terminal")
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "secret-token")

    attempts: list[str] = []

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            return None

        async def post(self, url: str, json: dict | None = None, headers: dict | None = None, timeout: float | None = None):
            attempts.append(str(url))
            assert float(timeout or 0.0) == 2.0
            assert str(headers.get("x-g3ku-internal-token") or "") == "secret-token"
            assert str(url).endswith("/api/internal/task-event-batch")
            assert isinstance(json, dict)
            assert len(list(json.get("items") or [])) == 1
            if len(attempts) == 1:
                return httpx.Response(500, json={"error": "retry"})
            return httpx.Response(200, json={"ok": True})

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("main.service.runtime_service.httpx.AsyncClient", _AsyncClient)
    monkeypatch.setattr("main.service.runtime_service.asyncio.sleep", _no_sleep)

    await service._deliver_task_summary_outbox("task:demo-summary")

    entry = service.store.get_task_summary_outbox("task:demo-summary")
    assert entry is not None
    assert entry["delivery_state"] == "delivered"
    assert entry["attempts"] == 1
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_worker_task_summary_outbox_falls_back_to_file_callback_config_when_env_target_fails(
    tmp_path: Path,
    monkeypatch,
):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    service._schedule_task_summary_delivery = lambda _task_id=None: None
    service._schedule_task_event_callback(
        {
            "event_type": "task.summary.patch",
            "session_id": "web:shared",
            "task_id": "task:demo-summary",
            "data": {
                "task": {
                    "task_id": "task:demo-summary",
                    "session_id": "web:shared",
                    "title": "demo",
                    "updated_at": now_iso(),
                }
            },
        }
    )
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_URL_ENV, "http://127.0.0.1:19999/api/internal/task-terminal")
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "stale-token")
    save_task_terminal_callback_config(
        workspace=tmp_path,
        url="http://127.0.0.1:18790/api/internal/task-terminal",
        token="fresh-token",
    )
    monkeypatch.setattr("main.service.runtime_service.Path.cwd", lambda: tmp_path)

    attempts: list[tuple[str, str]] = []

    async def _post(url: str, *, payload: dict[str, object], headers: dict[str, str], timeout: float):
        attempts.append((str(url), str(headers.get("x-g3ku-internal-token") or "")))
        assert float(timeout) == 2.0
        if ":19999/" in str(url):
            raise httpx.ConnectError("stale callback target")
        assert str(url).endswith("/api/internal/task-event-batch")
        assert str(headers.get("x-g3ku-internal-token") or "") == "fresh-token"
        assert len(list((payload or {}).get("items") or [])) == 1
        return httpx.Response(200, json={"ok": True})

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(service, "_post_internal_callback", _post)
    monkeypatch.setattr("main.service.runtime_service.asyncio.sleep", _no_sleep)

    await service._deliver_task_summary_outbox("task:demo-summary")

    entry = service.store.get_task_summary_outbox("task:demo-summary")
    assert entry is not None
    assert entry["delivery_state"] == "delivered"
    assert attempts == [
        ("http://127.0.0.1:19999/api/internal/task-event-batch", "stale-token"),
        ("http://127.0.0.1:18790/api/internal/task-event-batch", "fresh-token"),
    ]


@pytest.mark.asyncio
async def test_worker_task_summary_batch_delivery_groups_multiple_items(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    service.store.put_task_summary_outbox(
        task_id="task:one",
        session_id="web:shared",
        created_at=now_iso(),
        payload={
            "event_type": "task.summary.patch",
            "session_id": "web:shared",
            "task_id": "task:one",
            "data": {"task": {"task_id": "task:one", "title": "one", "updated_at": now_iso()}},
        },
    )
    service.store.put_task_summary_outbox(
        task_id="task:two",
        session_id="web:shared",
        created_at=now_iso(),
        payload={
            "event_type": "task.summary.patch",
            "session_id": "web:shared",
            "task_id": "task:two",
            "data": {"task": {"task_id": "task:two", "title": "two", "updated_at": now_iso()}},
        },
    )
    monkeypatch.setenv("G3KU_INTERNAL_CALLBACK_URL", "http://127.0.0.1:18790/api/internal/task-terminal")
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "secret-token")

    posted: list[dict[str, object]] = []

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            return None

        async def post(self, url: str, json: dict | None = None, headers: dict | None = None, timeout: float | None = None):
            posted.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
            return httpx.Response(200, json={"ok": True})

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("main.service.runtime_service.httpx.AsyncClient", _AsyncClient)
    monkeypatch.setattr("main.service.runtime_service.asyncio.sleep", _no_sleep)

    await service._deliver_task_summary_batches()

    assert len(posted) == 1
    assert str(posted[0]["url"]).endswith("/api/internal/task-event-batch")
    assert len(list((posted[0]["json"] or {}).get("items") or [])) == 2
    assert service.store.get_task_summary_outbox("task:one")["delivery_state"] == "delivered"
    assert service.store.get_task_summary_outbox("task:two")["delivery_state"] == "delivered"


@pytest.mark.asyncio
async def test_worker_task_terminal_outbox_falls_back_to_file_callback_config_when_env_target_fails(
    tmp_path: Path,
    monkeypatch,
):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    service._schedule_task_terminal_delivery = lambda _dedupe_key: None
    payload = normalize_task_terminal_payload(
        {
            "task_id": "task:demo-terminal",
            "session_id": "web:shared",
            "title": "demo terminal task",
            "status": "failed",
            "brief_text": "acceptance failed",
            "failure_reason": "acceptance failed",
            "finished_at": now_iso(),
        }
    )
    dedupe_key = str(payload.get("dedupe_key") or "")
    service.store.put_task_terminal_outbox(
        dedupe_key=dedupe_key,
        task_id=str(payload.get("task_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        created_at=str(payload.get("finished_at") or now_iso()),
        payload=payload,
    )
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_URL_ENV, "http://127.0.0.1:19999/api/internal/task-terminal")
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "stale-token")
    save_task_terminal_callback_config(
        workspace=tmp_path,
        url="http://127.0.0.1:18790/api/internal/task-terminal",
        token="fresh-token",
    )
    monkeypatch.setattr("main.service.runtime_service.Path.cwd", lambda: tmp_path)

    attempts: list[tuple[str, str]] = []

    async def _post(url: str, *, payload: dict[str, object], headers: dict[str, str], timeout: float):
        attempts.append((str(url), str(headers.get("x-g3ku-internal-token") or "")))
        assert float(timeout) == 2.0
        if ":19999/" in str(url):
            raise httpx.ConnectError("stale callback target")
        assert str(url).endswith("/api/internal/task-terminal")
        assert str(headers.get("x-g3ku-internal-token") or "") == "fresh-token"
        assert str(payload.get("task_id") or "") == "task:demo-terminal"
        return httpx.Response(200, json={"ok": True})

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(service, "_post_internal_callback", _post)
    monkeypatch.setattr("main.service.runtime_service.asyncio.sleep", _no_sleep)

    await service._deliver_task_terminal_outbox(dedupe_key)

    entry = service.store.get_task_terminal_outbox(dedupe_key)
    assert entry is not None
    assert entry["delivery_state"] == "delivered"
    assert attempts == [
        ("http://127.0.0.1:19999/api/internal/task-terminal", "stale-token"),
        ("http://127.0.0.1:18790/api/internal/task-terminal", "fresh-token"),
    ]


@pytest.mark.asyncio
async def test_worker_task_status_outbox_retries_and_marks_delivered(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    service._schedule_task_worker_status_delivery = lambda _worker_id: None
    service.publish_worker_status_event(
        item={
            "worker_id": "worker:test",
            "role": "task_worker",
            "status": "running",
            "updated_at": now_iso(),
            "payload": {"execution_mode": "worker", "active_task_count": 0},
        }
    )
    monkeypatch.setenv("G3KU_INTERNAL_CALLBACK_URL", "http://127.0.0.1:18790/api/internal/task-terminal")
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "secret-token")

    attempts: list[str] = []

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            return None

        async def post(self, url: str, json: dict | None = None, headers: dict | None = None, timeout: float | None = None):
            attempts.append(str(url))
            assert float(timeout or 0.0) == 2.0
            assert str(headers.get("x-g3ku-internal-token") or "") == "secret-token"
            if len(attempts) == 1:
                return httpx.Response(500, json={"error": "retry"})
            return httpx.Response(200, json={"ok": True})

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("main.service.runtime_service.httpx.AsyncClient", _AsyncClient)
    monkeypatch.setattr("main.service.runtime_service.asyncio.sleep", _no_sleep)

    await service._deliver_task_worker_status_outbox("worker:test")

    entry = service.store.get_task_worker_status_outbox("worker:test")
    assert entry is not None
    assert entry["delivery_state"] == "delivered"
    assert entry["attempts"] == 1
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_worker_startup_replays_pending_worker_status_outbox(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    scheduled: list[str] = []
    service._start_worker_loops = lambda: None
    service._schedule_task_worker_status_delivery = lambda worker_id: scheduled.append(str(worker_id))
    service.store.put_task_worker_status_outbox(
        worker_id="worker:pending",
        created_at=now_iso(),
        payload={
            "event_type": "task.worker.status",
            "session_id": "all",
            "task_id": "",
            "data": {
                "worker": {
                    "worker_id": "worker:pending",
                    "role": "task_worker",
                    "status": "running",
                    "updated_at": now_iso(),
                    "payload": {"execution_mode": "worker", "active_task_count": 0},
                },
                "worker_online": True,
                "worker_state": "online",
                "worker_last_seen_at": now_iso(),
                "worker_control_available": True,
                "worker_stale_after_seconds": 15.0,
            },
        },
    )

    await service.startup()

    assert scheduled == ["worker:pending"]


@pytest.mark.asyncio
async def test_worker_startup_replays_pending_task_summary_outbox(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    scheduled: list[str] = []
    service._start_worker_loops = lambda: None
    service._schedule_task_summary_delivery = lambda task_id=None: scheduled.append(str(task_id or ""))
    service.store.put_task_summary_outbox(
        task_id="task:pending-summary",
        session_id="web:shared",
        created_at=now_iso(),
        payload={
            "event_type": "task.summary.patch",
            "session_id": "web:shared",
            "task_id": "task:pending-summary",
            "data": {
                "task": {
                    "task_id": "task:pending-summary",
                    "session_id": "web:shared",
                    "title": "demo",
                    "updated_at": now_iso(),
                    "token_usage": {"tracked": True, "input_tokens": 7, "output_tokens": 3, "cache_hit_tokens": 1},
                }
            },
        },
    )

    await service.startup()

    assert scheduled == ["task:pending-summary"]


@pytest.mark.asyncio
async def test_worker_startup_publishes_heartbeat_before_read_model_rebuild(tmp_path: Path, monkeypatch):
    seed_service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-seed",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    worker_service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-worker",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )

    original_sync = worker_service.log_service.sync_task_read_models

    def _sync_with_heartbeat_assertion(task_id: str, *args, **kwargs):
        deadline = time.time() + 1.0
        while time.time() < deadline and worker_service.latest_worker_status() is None:
            time.sleep(0.01)
        assert worker_service.latest_worker_status() is not None
        return original_sync(task_id, *args, **kwargs)

    try:
        record, root = seed_service._build_task_record(
            task="startup heartbeat ordering",
            session_id="web:shared",
            max_depth=None,
            title=None,
            metadata=None,
        )
        seed_service.log_service.initialize_task(record, root)
        monkeypatch.setattr(worker_service.log_service, "sync_task_read_models", _sync_with_heartbeat_assertion)
        worker_service.global_scheduler.enqueue_task = _noop_enqueue_task
        await worker_service.startup()
    finally:
        await worker_service.close()
        await seed_service.close()


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
    service.global_scheduler.enqueue_task = _record_enqueue_calls(started)

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
        assert ws.receive_json()["type"] == "task.worker.status"

        service._publish_task_list_patch_event(
            session_id=record.session_id,
            task_payload={"task_id": record.task_id, "session_id": record.session_id, "brief": "patched"},
        )

        patch_event = _receive_until_type(ws, "task.summary.patch")
        assert patch_event["type"] == "task.summary.patch"
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
        status_event = ws.receive_json()
        assert status_event["type"] == "task.worker.status"
        assert status_event["data"]["worker_online"] is False
        assert status_event["data"]["worker_state"] == "stale"
        assert float(status_event["data"]["worker_stale_after_seconds"]) > 0

        _mark_worker_online(service)

        worker_event = _receive_until_type(ws, "task.worker.status")
        assert worker_event["type"] == "task.worker.status"
        assert worker_event["data"]["worker_online"] is True
        assert worker_event["data"]["worker_state"] == "online"
        assert float(worker_event["data"]["worker_stale_after_seconds"]) > 0
        assert worker_event["data"]["worker"]["worker_id"] == "worker:test"


def test_tasks_rest_includes_worker_stale_after_seconds(tmp_path: Path, monkeypatch):
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
    asyncio.run(_create_web_task(service))
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    response = client.get("/api/tasks?session_id=all&scope=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert isinstance(payload["worker"], dict)
    assert payload["worker_online"] is True
    assert payload["worker_state"] == "online"
    assert payload["worker_control_available"] is True
    assert payload["worker_last_seen_at"]
    assert float(payload["worker_stale_after_seconds"]) > 0


def test_task_worker_status_rest_endpoint_returns_worker_metadata(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    _mark_worker_online(service)
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    response = client.get("/api/tasks/worker-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["worker_online"] is True
    assert payload["worker_state"] == "online"
    assert payload["worker_control_available"] is True
    assert payload["worker_last_seen_at"]


def test_worker_status_payload_surfaces_tool_pressure_diagnostics(tmp_path: Path):
    sample_at = now_iso()
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    service.store.upsert_worker_status(
        worker_id="worker:test",
        role="task_worker",
        status="running",
        updated_at=now_iso(),
        payload={
            "execution_mode": "worker",
            "active_task_count": 1,
            "tool_pressure_state": "throttled",
            "tool_pressure_target_limit": 1,
            "tool_pressure_running_count": 1,
            "tool_pressure_waiting_count": 3,
            "tool_pressure_event_loop_lag_ms": 321.5,
            "tool_pressure_writer_queue_depth": 77,
            "tool_pressure_process_cpu_ratio": 0.92,
            "tool_pressure_last_transition_at": "2026-03-30T00:00:00+08:00",
            "tool_pressure_throttled_since": "2026-03-30T00:00:00+08:00",
            "worker_execution_state": "throttled",
            "worker_execution_target_limit": 1,
            "worker_execution_running_count": 1,
            "worker_execution_waiting_count": 3,
            "worker_execution_oldest_wait_ms": 1250.0,
            "machine_pressure_available": True,
            "machine_pressure_cpu_percent": 91.0,
            "machine_pressure_memory_percent": 72.0,
            "machine_pressure_disk_busy_percent": 55.0,
            "pressure_sample_at": sample_at,
            "sqlite_write_wait_ms": 42.0,
            "sqlite_query_latency_ms": 18.0,
        },
    )

    payload = service.worker_status_payload()

    assert payload["tool_pressure_state"] == "throttled"
    assert payload["tool_pressure_target_limit"] == 1
    assert payload["tool_pressure_running_count"] == 1
    assert payload["tool_pressure_waiting_count"] == 3
    assert float(payload["tool_pressure_event_loop_lag_ms"]) == 321.5
    assert int(payload["tool_pressure_writer_queue_depth"]) == 77
    assert float(payload["tool_pressure_process_cpu_ratio"]) == 0.92
    assert payload["tool_pressure_last_transition_at"] == "2026-03-30T00:00:00+08:00"
    assert payload["tool_pressure_throttled_since"] == "2026-03-30T00:00:00+08:00"
    assert payload["worker_execution_state"] == "throttled"
    assert payload["worker_execution_target_limit"] == 1
    assert payload["worker_execution_running_count"] == 1
    assert payload["worker_execution_waiting_count"] == 3
    assert float(payload["worker_execution_oldest_wait_ms"]) == 1250.0
    assert payload["machine_pressure_available"] is True
    assert float(payload["machine_pressure_cpu_percent"]) == 91.0
    assert float(payload["machine_pressure_memory_percent"]) == 72.0
    assert float(payload["machine_pressure_disk_busy_percent"]) == 55.0
    assert payload["pressure_sample_at"] == sample_at
    assert float(payload["pressure_sample_age_ms"]) >= 0.0
    assert payload["pressure_snapshot_fresh"] is True
    assert float(payload["sqlite_write_wait_ms"]) == 42.0
    assert float(payload["sqlite_query_latency_ms"]) == 18.0


def test_worker_status_payload_preserves_easing_state_and_zero_target_limit(tmp_path: Path):
    sample_at = now_iso()
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    service.store.upsert_worker_status(
        worker_id="worker:test",
        role="task_worker",
        status="running",
        updated_at=now_iso(),
        payload={
            "execution_mode": "worker",
            "tool_pressure_state": "easing",
            "tool_pressure_target_limit": 0,
            "worker_execution_state": "easing",
            "worker_execution_target_limit": 0,
            "machine_pressure_available": True,
            "pressure_sample_at": sample_at,
        },
    )

    payload = service.worker_status_payload()

    assert payload["tool_pressure_state"] == "easing"
    assert payload["tool_pressure_target_limit"] == 0
    assert payload["worker_execution_state"] == "easing"
    assert payload["worker_execution_target_limit"] == 0


def test_adaptive_tool_budget_settings_default_safe_window_is_three() -> None:
    settings = MainRuntimeService._adaptive_tool_budget_settings(None)
    assert settings["safe_consecutive_samples"] == 3
    assert settings["machine_memory_safe_percent"] == 95.0


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
    assert service.worker_state() == "online"

    _mark_worker_at(service, definitely_stale)
    assert service.is_worker_online() is False
    assert service.worker_state() == "stale"


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
    assert service.worker_state() == "online"

    _mark_worker_at(service, definitely_stale_even_with_grace, active_task_count=1)
    assert service.is_worker_online() is False
    assert service.worker_state() == "stale"


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
    assert service.worker_state() == "stopped"


def test_web_mode_worker_state_reports_offline_without_worker_status(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    assert service.worker_state() == "offline"
    assert service.is_worker_online() is False


def test_web_mode_worker_state_reports_starting_for_recent_managed_worker(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    started_at = now_iso()
    monkeypatch.setattr(
        "main.service.runtime_service.managed_worker_snapshot",
        lambda starting_grace_s=10.0: {
            "pid": 123,
            "active": True,
            "auto_worker_enabled": True,
            "started_at": started_at,
            "starting": True,
            "starting_grace_seconds": starting_grace_s,
        },
    )

    assert service.worker_state() == "starting"
    assert service.is_worker_online() is False


@pytest.mark.parametrize(
    ("worker_state", "detail", "route"),
    [
        ("starting", "task_worker_starting", "/api/tasks/demo/pause"),
        ("stale", "task_worker_stale", "/api/tasks/demo/resume"),
        ("offline", "task_worker_offline", "/api/tasks/demo/cancel"),
    ],
)
def test_task_control_routes_surface_specific_worker_state_errors(
    tmp_path: Path,
    monkeypatch,
    worker_state: str,
    detail: str,
    route: str,
):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    monkeypatch.setattr(service, "worker_state", lambda **kwargs: worker_state)
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    response = client.post(route)

    assert response.status_code == 503
    assert response.json()["detail"] == detail


def test_global_tasks_websocket_does_not_replay_historical_patches_after_hello(tmp_path: Path, monkeypatch):
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
        event_type="task.summary.patch",
        created_at=now_iso(),
        payload={"task": {"task_id": record.task_id, "brief": "historical"}},
    )
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect("/api/ws/tasks?session_id=all&after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "task.worker.status"

        service._publish_task_list_patch_event(
            session_id=record.session_id,
            task_payload={"task_id": record.task_id, "session_id": record.session_id, "brief": "fresh"},
        )

        patch_event = _receive_until_type(ws, "task.summary.patch")
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

        service.log_service.replace_runtime_frames(
            record.task_id,
            frames=[],
            active_node_ids=[record.root_node_id],
            runnable_node_ids=[],
            waiting_node_ids=[],
        )
        _publish_task_live_patch(service, record.task_id)

        runtime_event = _receive_until_type(ws, "task.live.patch")
        assert runtime_event["type"] == "task.live.patch"
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
        event_type="task.live.patch",
        created_at=now_iso(),
        payload={"task_id": record.task_id, "runtime_summary": {"active_node_ids": ["historical"], "runnable_node_ids": [], "waiting_node_ids": [], "frames": []}},
    )
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/tasks/{record.task_id}?after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"

        service.log_service.replace_runtime_frames(
            record.task_id,
            frames=[],
            active_node_ids=[record.root_node_id],
            runnable_node_ids=[],
            waiting_node_ids=[],
        )
        _publish_task_live_patch(service, record.task_id)

        runtime_event = _receive_until_type(ws, "task.live.patch")
        assert runtime_event["data"]["runtime_summary"]["active_node_ids"] == [record.root_node_id]


def test_task_live_patch_history_persists_latest_payload_after_window(tmp_path: Path):
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
    existing_events = service.store.list_task_events(after_seq=0, task_id=record.task_id, limit=10_000)
    after_seq = max((int(item.get("seq") or 0) for item in existing_events), default=0)

    for index in range(20):
        service.log_service.update_frame(
            record.task_id,
            record.root_node_id,
            lambda frame, idx=index: {
                **frame,
                "node_id": record.root_node_id,
                "depth": 0,
                "node_kind": "execution",
                "phase": "before_model",
                "stage_goal": f"stage-{idx}",
            },
            publish_snapshot=True,
        )

    time.sleep(1.3)

    live_events = [
        item for item in service.store.list_task_events(after_seq=after_seq, task_id=record.task_id, limit=10_000)
        if item.get("event_type") == "task.live.patch"
    ]

    assert 1 <= len(live_events) <= 2
    assert live_events[-1]["payload"]["frame"]["stage_goal"] == "stage-19"


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
    service.store.append_task_model_call(
        task_id=record.task_id,
        node_id=record.root_node_id,
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
    assert payload["recent_model_calls"][0]["call_index"] == 3
    assert "progress" not in payload
    assert "tree_root" not in payload
    assert payload["runtime_summary"]["dispatch_limits"] == {"execution": 0, "inspection": 0}
    assert payload["runtime_summary"]["dispatch_running"] == {"execution": 0, "inspection": 0}
    assert payload["runtime_summary"]["dispatch_queued"] == {"execution": 0, "inspection": 0}
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    monkeypatch.setattr("main.api.websocket_task.get_agent", lambda: SimpleNamespace(main_task_service=service))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/tasks/{record.task_id}?after_seq=0") as ws:
        assert ws.receive_json()["type"] == "hello"

        service.store.append_task_model_call(
            task_id=record.task_id,
            node_id=record.root_node_id,
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
        service.log_service._dispatch_live_event_locked(
            task=service.get_task(record.task_id),
            event_type="task.model.call",
            data={
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


def test_task_tree_snapshot_payload_contains_root_and_child_nodes(tmp_path: Path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
    )
    service.log_service.update_node_status(
        record.task_id,
        child.node_id,
        status="success",
        final_output="child done",
    )

    payload = service.get_task_tree_snapshot_payload(record.task_id)

    assert payload is not None
    assert payload["root_node_id"] == root.node_id
    assert root.node_id in payload["nodes_by_id"]
    assert child.node_id in payload["nodes_by_id"]
    root_snapshot = payload["nodes_by_id"][root.node_id]
    assert root_snapshot["auxiliary_child_ids"] == [child.node_id]


def test_task_model_call_event_includes_cache_diagnostics(tmp_path: Path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    model_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]
    request_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt\n\nSystem note for this turn only:\nstage overlay"},
    ]
    service.log_service.append_node_output(
        record.task_id,
        record.root_node_id,
        content='{"status":"success"}',
        tool_calls=[],
        usage_attempts=[
            LLMModelAttempt(
                model_key="sub gpt-5.4",
                provider_id="openai",
                provider_model="gpt-5.4",
                usage={"input_tokens": 10, "output_tokens": 5, "cache_hit_tokens": 2},
            )
        ],
        model_messages=model_messages,
        request_messages=request_messages,
        prompt_cache_key="stable-cache-key",
        request_message_count=len(request_messages),
        request_message_chars=321,
    )

    events = service.store.list_task_events(task_id=record.task_id, limit=20)
    model_call = [item for item in events if item["event_type"] == "task.model.call"][-1]["payload"]

    assert model_call["prompt_cache_key_present"] is True
    assert str(model_call["prompt_cache_key_hash"]).strip()
    assert model_call["request_overlay_applied"] is True
    assert model_call["model_message_count"] == 2
    assert model_call["prepared_message_count"] == 2
    assert str(model_call["model_prefix_hash"]).strip()
    assert str(model_call["prepared_prefix_hash"]).strip()


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


def test_tool_result_batch_uses_canonical_output_ref_for_wrapped_content(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)

    assert root is not None

    inner = service.content_store.maybe_externalize_text(
        "alpha\nneedle\nomega\n",
        runtime={"task_id": record.task_id, "node_id": root.node_id},
        display_name="inner",
        source_kind="node_output",
        force=True,
    )

    assert inner is not None

    wrapped = json.dumps(inner.to_dict(), ensure_ascii=False)
    service.log_service.record_tool_result_batch(
        task_id=record.task_id,
        node_id=root.node_id,
        response_tool_calls=[SimpleNamespace(id="call:content", name="content", arguments={})],
        results=[
            {
                "tool_message": {
                    "tool_call_id": "call:content",
                    "name": "content",
                    "content": wrapped,
                    "status": "success",
                },
                "live_state": {"tool_call_id": "call:content", "tool_name": "content", "status": "success"},
            }
        ],
    )

    tool_results = service.store.list_task_node_tool_results(record.task_id, root.node_id)

    assert tool_results[-1].output_ref == inner.ref


def test_tool_result_batch_tolerates_non_mapping_tool_arguments(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)

    assert root is not None

    service.log_service.record_tool_result_batch(
        task_id=record.task_id,
        node_id=root.node_id,
        response_tool_calls=[
            ToolCallRequest(
                id="call:filesystem",
                name="filesystem",
                arguments=["oops"],
            )
        ],
        results=[
            {
                "tool_message": {
                    "tool_call_id": "call:filesystem",
                    "name": "filesystem",
                    "content": '{"ok":true}',
                    "status": "success",
                },
                "live_state": {
                    "tool_call_id": "call:filesystem",
                    "tool_name": "filesystem",
                    "status": "success",
                },
            }
        ],
    )

    tool_results = service.store.list_task_node_tool_results(record.task_id, root.node_id)

    assert tool_results[-1].tool_call_id == "call:filesystem"
    assert tool_results[-1].arguments_text == ""


def test_refresh_task_view_skips_upsert_when_semantically_unchanged(tmp_path: Path):
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
    calls: list[str] = []
    original = service.store.upsert_task

    def _wrapped(task):
        calls.append(str(task.task_id or ""))
        return original(task)

    service.store.upsert_task = _wrapped  # type: ignore[assignment]
    try:
        refreshed = service.log_service.refresh_task_view(record.task_id, mark_unread=False)
    finally:
        service.store.upsert_task = original  # type: ignore[assignment]

    assert refreshed is not None
    assert calls == []


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
        acceptance_prompt="鏍稿鏈€缁堢粨鏋滄槸鍚︽弧瓒宠姹傘€?",
        parent_node_id=root.node_id,
        metadata={"final_acceptance": True},
    )
    service.log_service.update_node_check_result(record.task_id, acceptance.node_id, "楠屾敹閫氳繃")
    service.log_service.update_node_status(
        record.task_id,
        acceptance.node_id,
        status="success",
        final_output="楠屾敹閫氳繃",
    )

    subtree = service.get_task_tree_subtree_payload(record.task_id, root.node_id)

    assert subtree is not None
    assert subtree["root_node_id"] == root.node_id
    assert acceptance.node_id in subtree["nodes_by_id"]
    assert subtree["nodes_by_id"][acceptance.node_id]["node_kind"] == "acceptance"


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
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
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
        acceptance_prompt="妫€鏌?child 杈撳嚭銆?",
        parent_node_id=child.node_id,
    )
    service.log_service.update_node_check_result(record.task_id, child.node_id, "child acceptance passed")
    service.log_service.update_node_check_result(record.task_id, acceptance.node_id, "楠屾敹閫氳繃")
    service.log_service.update_node_status(
        record.task_id,
        acceptance.node_id,
        status="success",
        final_output="楠屾敹閫氳繃",
    )

    subtree = service.get_task_tree_subtree_payload(record.task_id, child.node_id)

    assert subtree is not None
    assert subtree["root_node_id"] == child.node_id
    assert acceptance.node_id in subtree["nodes_by_id"]
    assert subtree["nodes_by_id"][acceptance.node_id]["node_kind"] == "acceptance"


def test_direct_child_creation_emits_parent_node_patch_with_children_fingerprint(tmp_path: Path):
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

    detail_before = service.get_node_detail_payload(record.task_id, root.node_id)
    fingerprint_before = str(detail_before["item"].get("children_fingerprint") or "")
    existing_events = service.store.list_task_events(after_seq=0, task_id=record.task_id, limit=10_000)
    after_seq = max((int(item.get("seq") or 0) for item in existing_events), default=0)

    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
    )

    patch_events = [
        item for item in service.store.list_task_events(after_seq=after_seq, task_id=record.task_id, limit=10_000)
        if item.get("event_type") == "task.node.patch"
    ]
    parent_patches = [
        item["payload"]["node"]
        for item in patch_events
        if str(((item.get("payload") or {}).get("node") or {}).get("node_id") or "").strip() == root.node_id
    ]

    assert child is not None
    assert parent_patches
    assert str(parent_patches[-1].get("children_fingerprint") or "").strip()
    assert str(parent_patches[-1].get("children_fingerprint") or "") != fingerprint_before


def test_task_node_patch_persists_only_once_when_only_updated_at_changes(tmp_path: Path):
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

    existing_events = service.store.list_task_events(after_seq=0, task_id=record.task_id, limit=10_000)
    after_seq = max((int(item.get("seq") or 0) for item in existing_events), default=0)

    service.log_service._publish_task_node_patch_locked(task=task, node=root)
    service.log_service._publish_task_node_patch_locked(
        task=task,
        node=root.model_copy(update={"updated_at": now_iso()}),
    )

    node_events = [
        item for item in service.store.list_task_events(after_seq=after_seq, task_id=record.task_id, limit=10_000)
        if item.get("event_type") == "task.node.patch"
        and str((((item.get("payload") or {}).get("node") or {}).get("node_id") or "")).strip() == root.node_id
    ]

    assert len(node_events) == 1


def test_child_status_updates_do_not_emit_parent_structure_patch(tmp_path: Path):
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
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
    )
    existing_events = service.store.list_task_events(after_seq=0, task_id=record.task_id, limit=10_000)
    after_seq = max((int(item.get("seq") or 0) for item in existing_events), default=0)

    service.log_service.update_node_status(
        record.task_id,
        child.node_id,
        status="success",
        final_output="child done",
    )

    parent_patches = [
        item for item in service.store.list_task_events(after_seq=after_seq, task_id=record.task_id, limit=10_000)
        if item.get("event_type") == "task.node.patch"
        and str((((item.get("payload") or {}).get("node") or {}).get("node_id") or "")).strip() == root.node_id
    ]

    assert parent_patches == []


def test_metadata_only_spawn_update_does_not_rewrite_task_node_detail(tmp_path: Path):
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

    detail_before = service.store.get_task_node_detail(root.node_id)
    node_before = service.store.get_task_node(root.node_id)

    assert detail_before is not None
    assert node_before is not None

    service.log_service.update_node_metadata(
        root.node_id,
        lambda metadata: {
            **metadata,
            "spawn_operations": {
                "call:test": {
                    "specs": [],
                    "entries": [],
                    "results": [],
                    "completed": False,
                    "created_at": now_iso(),
                }
            },
        },
    )

    detail_after = service.store.get_task_node_detail(root.node_id)
    node_after = service.store.get_task_node(root.node_id)
    rounds = [item for item in service.store.list_task_node_rounds(record.task_id) if item.parent_node_id == root.node_id]

    assert detail_after is not None
    assert node_after is not None
    assert detail_after.updated_at == detail_before.updated_at
    assert detail_after.payload == detail_before.payload
    assert node_after.children_fingerprint != node_before.children_fingerprint
    assert node_after.default_round_id != node_before.default_round_id
    assert node_after.round_options_count == 1
    assert rounds


def test_failed_acceptance_node_preserves_execution_child_status(tmp_path: Path):
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
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
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
        acceptance_prompt="妫€鏌?child 杈撳嚭銆?",
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
    assert latest_child.status == "success"
    assert latest_child.final_output == "child done"
    assert latest_child.failure_reason == ""
    assert latest_child.check_result == "child acceptance failed"


def test_failed_node_ids_follow_projection_tree_for_failed_acceptance(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
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
        acceptance_prompt="妫€鏌?child 杈撳嚭銆?",
        parent_node_id=child.node_id,
    )
    service.log_service.update_node_status(
        record.task_id,
        acceptance.node_id,
        status="failed",
        final_output="child acceptance failed",
        failure_reason="child acceptance failed",
    )

    assert service.failed_node_ids(record.task_id) == f'- {acceptance.node_id}'


@pytest.mark.asyncio
async def test_execution_policy_focus_propagates_to_task_payload_child_and_acceptance_prompt(tmp_path: Path):
    expected_core_requirement = "瀹屾垚涓€鐗堝彲鐩存帴浜や粯鐨勫彂甯冨叕鍛婂垵绋?"
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task(
            "甯垜鍐欎竴鐗堝彂甯冨叕鍛婂垵绋?",
            session_id="web:shared",
            metadata={
                "core_requirement": expected_core_requirement,
                "execution_policy": _execution_policy(),
            },
        )
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)

        assert task is not None
        assert root is not None
        assert task.metadata["execution_policy"] == {"mode": "focus"}
        assert root.metadata["execution_policy"] == {"mode": "focus"}

        messages = await service.node_runner._build_messages(task=task, node=root)
        payload = json.loads(messages[1]["content"])
        assert payload["core_requirement"] == expected_core_requirement
        assert payload["execution_policy"] == {"mode": "focus"}
        assert payload["prompt"] == root.prompt

        child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(
                goal="璧疯崏棣栫増鍏憡姝ｆ枃",
                prompt="杈撳嚭棣栫増鍙姝ｆ枃銆?",
                execution_policy=_execution_policy(),
            ),
        )
        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=child,
            goal="accept:draft",
            acceptance_prompt="妫€鏌ュ叕鍛婅崏绋挎槸鍚︽弧瓒充氦浠樿姹傘€?",
            parent_node_id=child.node_id,
        )

        assert child.metadata["execution_policy"] == {"mode": "focus"}
        assert child.prompt == "杈撳嚭棣栫増鍙姝ｆ枃銆?"
        assert acceptance.metadata["execution_policy"] == {"mode": "focus"}
        assert "(empty)" in acceptance.prompt
        assert "(none)" in acceptance.prompt
        assert "浣犳鍦ㄥ畬鎴愮殑浠诲姟鏄牳蹇冮渶姹傘€?" not in acceptance.prompt
        assert "core_requirement" not in child.metadata
        assert "core_requirement" not in acceptance.metadata

        child_messages = await service.node_runner._build_messages(task=task, node=child)
        child_payload = json.loads(child_messages[1]["content"])
        assert child_payload["core_requirement"] == expected_core_requirement
        assert child_payload["execution_policy"] == {"mode": "focus"}
        assert child_payload["prompt"] == child.prompt

        acceptance_messages = await service.node_runner._build_messages(task=task, node=acceptance)
        acceptance_payload = json.loads(acceptance_messages[1]["content"])
        assert acceptance_payload["core_requirement"] == expected_core_requirement
        assert acceptance_payload["execution_policy"] == {"mode": "focus"}
        assert acceptance_payload["prompt"] == acceptance.prompt
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_execution_policy_coverage_is_provided_via_payload_without_prompt_notice(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task(
            "鍏ㄩ潰姊崇悊椤圭洰瀵瑰鍙戝竷鏉愭枡",
            session_id="web:shared",
            metadata={
                "core_requirement": "绯荤粺瀹屾垚椤圭洰瀵瑰鍙戝竷鏉愭枡鐨勬暣鐞嗕笌琛ユ紡",
                "execution_policy": _execution_policy("coverage"),
            },
        )
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)

        assert task is not None
        assert root is not None

        messages = await service.node_runner._build_messages(task=task, node=root)
        payload = json.loads(messages[1]["content"])
        assert payload["core_requirement"] == "绯荤粺瀹屾垚椤圭洰瀵瑰鍙戝竷鏉愭枡鐨勬暣鐞嗕笌琛ユ紡"
        assert payload["execution_policy"] == {"mode": "coverage"}
        assert payload["prompt"] == root.prompt
        assert "浣犳鍦ㄥ畬鎴愮殑浠诲姟鏄牳蹇冮渶姹傘€?" not in payload["prompt"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_children_allows_execution_policy_mode_divergence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    _install_allow_all_spawn_review(service, monkeypatch)

    try:
        record = await service.create_task(
            "鏁寸悊闇€姹?",
            session_id="web:shared",
            metadata={
                "core_requirement": "瀹屾垚鑱氱劍鏁寸悊",
                "execution_policy": _execution_policy(),
            },
        )
        root = service.get_node(record.root_node_id)

        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary="child done",
                    answer="child done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[
                SpawnChildSpec(
                    goal="瑕嗙洊琛ユ紡",
                    prompt="琛ュ仛鎵€鏈夎竟缂樺垎鏀€?",
                    execution_policy=_execution_policy("coverage"),
                )
            ],
            call_id="divergent-policy",
        )

        assert len(results) == 1
        assert results[0].goal == "瑕嗙洊琛ユ紡"

        root_after = service.get_node(root.node_id)
        assert root_after is not None
        spawn_operations = dict((root_after.metadata or {}).get("spawn_operations") or {})
        entries = list((spawn_operations.get("divergent-policy") or {}).get("entries") or [])
        assert len(entries) == 1

        child_id = str(entries[0].get("child_node_id") or "").strip()
        child = service.get_node(child_id)
        assert child is not None
        assert (child.metadata or {}).get("execution_policy") == _execution_policy("coverage")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_children_only_surfaces_failure_info_for_failed_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)
    try:
        record = await _create_web_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)

        assert task is not None
        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            if node.goal == "bad child":
                return service.node_runner._mark_finished(
                    task_id,
                    node_id,
                    NodeFinalResult(
                        status="failed",
                        delivery_status="final",
                        summary="child failed summary",
                        answer="",
                        evidence=[],
                        remaining_work=["tighten child scope"],
                        blocking_reason="",
                    ),
                )
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary="good child done",
                    answer="good child done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[
                SpawnChildSpec(goal="bad child", prompt="bad prompt", execution_policy=_execution_policy()),
                SpawnChildSpec(goal="good child", prompt="good prompt", execution_policy=_execution_policy()),
            ],
            call_id="round-failure-info",
        )

        failed_result = next(item for item in results if item.goal == "bad child")
        success_result = next(item for item in results if item.goal == "good child")

        assert failed_result.failure_info is not None
        assert failed_result.failure_info.source == "execution"
        assert failed_result.failure_info.summary == "child failed summary"
        assert failed_result.failure_info.delivery_status == "final"
        assert failed_result.failure_info.blocking_reason == ""
        assert failed_result.failure_info.remaining_work == ["tighten child scope"]
        assert "failure_info" in failed_result.model_dump(mode="json", exclude_none=True)

        assert success_result.failure_info is None
        assert "failure_info" not in success_result.model_dump(mode="json", exclude_none=True)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_children_materializes_batch_children_before_pipeline_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)

    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)
        assert root is not None

        observed_child_ids: list[list[str]] = []

        async def _fake_run_node(task_id: str, node_id: str):
            root_after = service.get_node(root.node_id)
            assert root_after is not None
            entries = list(((root_after.metadata or {}).get("spawn_operations") or {}).get("batch-call", {}).get("entries") or [])
            observed_child_ids.append([
                str(item.get("child_node_id") or "").strip()
                for item in entries
            ])
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary=f"{node_id} done",
                    answer=f"{node_id} done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[
                SpawnChildSpec(goal="child 1", prompt="prompt 1", execution_policy=_execution_policy()),
                SpawnChildSpec(goal="child 2", prompt="prompt 2", execution_policy=_execution_policy()),
            ],
            call_id="batch-call",
        )

        root_after = service.get_node(root.node_id)
        assert root_after is not None
        entries = list(((root_after.metadata or {}).get("spawn_operations") or {}).get("batch-call", {}).get("entries") or [])
        child_ids = [str(item.get("child_node_id") or "").strip() for item in entries]

        assert len(results) == 2
        assert len(entries) == 2
        assert all(child_ids)
        assert len(service.store.list_children(root.node_id)) == 2
        assert observed_child_ids
        assert all(all(item for item in snapshot) for snapshot in observed_child_ids)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_children_surfaces_acceptance_failure_info_while_preserving_child_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)

        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            if node.node_kind == "acceptance":
                return service.node_runner._mark_finished(
                    task_id,
                    node_id,
                    NodeFinalResult(
                        status="failed",
                        delivery_status="final",
                        summary="acceptance failed summary",
                        answer="need stricter proof",
                        evidence=[],
                        remaining_work=["reopen cited lines"],
                        blocking_reason="",
                    ),
                )
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary="child done",
                    answer="child done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy(), acceptance_prompt="check child")],
            call_id="round-acceptance-failure",
        )

        result = results[0]

        assert result.node_output == "child done"
        assert result.node_output_summary == "child done"
        assert result.check_result == "acceptance failed summary"
        assert result.failure_info is not None
        assert result.failure_info.source == "acceptance"
        assert result.failure_info.summary == "acceptance failed summary"
        assert result.failure_info.delivery_status == "final"
        assert result.failure_info.remaining_work == ["reopen cited lines"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_children_surfaces_runtime_failure_info_for_pipeline_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)

        assert root is not None

        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(service.node_runner, "run_node", _boom)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy())],
            call_id="round-runtime-failure",
        )

        result = results[0]

        assert result.failure_info is not None
        assert result.failure_info.source == "runtime"
        assert result.failure_info.summary == "Error: RuntimeError: boom"
        assert result.failure_info.delivery_status == "blocked"
        assert result.failure_info.blocking_reason == "Error: RuntimeError: boom"
        assert result.failure_info.remaining_work == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_children_isolates_cancelled_child_without_failing_whole_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)

        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            if node.goal == "bad child":
                raise asyncio.CancelledError()
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary="good child done",
                    answer="good child done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[
                SpawnChildSpec(goal="bad child", prompt="bad prompt", execution_policy=_execution_policy()),
                SpawnChildSpec(goal="good child", prompt="good prompt", execution_policy=_execution_policy()),
            ],
            call_id="round-cancel-isolation",
        )

        bad_result = next(item for item in results if item.goal == "bad child")
        good_result = next(item for item in results if item.goal == "good child")

        assert len(results) == 2
        assert bad_result.failure_info is not None
        assert bad_result.failure_info.source == "runtime"
        assert bad_result.failure_info.summary == "Error: CancelledError"
        assert good_result.failure_info is None
        assert good_result.node_output == "good child done"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_children_empty_runtime_exception_includes_exception_class_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)

        assert root is not None

        async def _boom(*args, **kwargs):
            raise RuntimeError()

        monkeypatch.setattr(service.node_runner, "run_node", _boom)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy())],
            call_id="round-empty-runtime-error",
        )

        result = results[0]
        assert result.failure_info is not None
        assert result.failure_info.source == "runtime"
        assert result.failure_info.summary == "Error: RuntimeError"
        assert result.failure_info.blocking_reason == "Error: RuntimeError"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_new_spawn_round_supersedes_active_old_subtree_and_preserves_terminal_success_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)
    try:
        record = await _create_web_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)

        assert task is not None
        assert root is not None

        stale_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(goal="stale child", prompt="stale prompt", execution_policy=_execution_policy()),
        )
        stale_descendant = service.node_runner._create_execution_child(
            task=task,
            parent=stale_child,
            spec=SpawnChildSpec(goal="stale leaf", prompt="stale leaf prompt", execution_policy=_execution_policy()),
        )
        steady_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(goal="steady child", prompt="steady prompt", execution_policy=_execution_policy()),
        )
        service.node_runner._mark_finished(
            record.task_id,
            steady_child.node_id,
            NodeFinalResult(
                status="success",
                delivery_status="final",
                summary="steady child done",
                answer="steady child done",
                evidence=[],
                remaining_work=[],
                blocking_reason="",
            ),
        )

        steady_result = SpawnChildResult(
            goal="steady child",
            check_result=SKIPPED_CHECK_RESULT,
            node_output="steady child done",
            node_output_summary="steady child done",
            node_output_ref="",
        )

        def _mutate(metadata: dict[str, object]) -> dict[str, object]:
            metadata["spawn_operations"] = {
                "round-1": {
                    "specs": [
                        SpawnChildSpec(goal="stale child", prompt="stale prompt", execution_policy=_execution_policy()).model_dump(mode="json"),
                        SpawnChildSpec(goal="steady child", prompt="steady prompt", execution_policy=_execution_policy()).model_dump(mode="json"),
                    ],
                    "entries": [
                        {
                            "index": 0,
                            "goal": "stale child",
                            "prompt": "stale prompt",
                            "requires_acceptance": False,
                            "acceptance_prompt": "",
                            "status": "running",
                            "started_at": now_iso(),
                            "finished_at": "",
                            "child_node_id": stale_child.node_id,
                            "acceptance_node_id": "",
                            "check_status": "skipped",
                            "result": {},
                        },
                        {
                            "index": 1,
                            "goal": "steady child",
                            "prompt": "steady prompt",
                            "requires_acceptance": False,
                            "acceptance_prompt": "",
                            "status": "success",
                            "started_at": now_iso(),
                            "finished_at": now_iso(),
                            "child_node_id": steady_child.node_id,
                            "acceptance_node_id": "",
                            "check_status": "skipped",
                            "result": steady_result.model_dump(mode="json", exclude_none=True),
                        },
                    ],
                    "results": [steady_result.model_dump(mode="json", exclude_none=True)],
                    "completed": False,
                }
            }
            return metadata

        service.log_service.update_node_metadata(root.node_id, _mutate)

        async def _fake_run_node(task_id: str, node_id: str):
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary=f"{node_id} new round done",
                    answer=f"{node_id} new round done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        new_results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[SpawnChildSpec(goal="new child", prompt="new prompt", execution_policy=_execution_policy())],
            call_id="round-2",
        )

        root_after = service.get_node(root.node_id)
        stale_after = service.get_node(stale_child.node_id)
        stale_leaf_after = service.get_node(stale_descendant.node_id)
        steady_after = service.get_node(steady_child.node_id)

        assert root_after is not None
        assert stale_after is not None
        assert stale_leaf_after is not None
        assert steady_after is not None
        assert len(new_results) == 1
        assert stale_after.status == "failed"
        assert stale_leaf_after.status == "failed"
        assert "superseded by newer spawn round: round-2" in str(stale_after.failure_reason or "")
        assert "superseded by newer spawn round: round-2" in str(stale_leaf_after.failure_reason or "")
        assert steady_after.status == "success"

        spawn_operations = dict((root_after.metadata or {}).get("spawn_operations") or {})
        assert {"round-1", "round-2"} <= set(spawn_operations)
        stale_entry = spawn_operations["round-1"]["entries"][0]
        steady_entry = spawn_operations["round-1"]["entries"][1]
        assert stale_entry["status"] == "error"
        assert stale_entry["result"]["failure_info"]["summary"] == "superseded by newer spawn round: round-2"
        assert spawn_operations["round-1"]["completed"] is True
        assert steady_entry["status"] == "success"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_branch_respawn_creates_new_round_and_keeps_old_failed_subtree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)

        assert root is not None

        attempts = {"bad child": 0}

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            if node.goal == "bad child":
                attempts["bad child"] += 1
                if attempts["bad child"] == 1:
                    return service.node_runner._mark_finished(
                        task_id,
                        node_id,
                        NodeFinalResult(
                            status="failed",
                            delivery_status="final",
                            summary="first attempt failed",
                            answer="",
                            evidence=[],
                            remaining_work=["retry with refined prompt"],
                            blocking_reason="",
                        ),
                    )
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary="retry succeeded",
                    answer="retry succeeded",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        first_results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[SpawnChildSpec(goal="bad child", prompt="bad prompt", execution_policy=_execution_policy())],
            call_id="round-1",
        )
        second_results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[SpawnChildSpec(goal="bad child", prompt="bad prompt refined", execution_policy=_execution_policy())],
            call_id="round-2",
        )

        root_after = service.get_node(root.node_id)
        assert root_after is not None
        spawn_operations = dict((root_after.metadata or {}).get("spawn_operations") or {})
        first_child_id = spawn_operations["round-1"]["entries"][0]["child_node_id"]
        second_child_id = spawn_operations["round-2"]["entries"][0]["child_node_id"]

        assert len(first_results) == 1
        assert len(second_results) == 1
        assert first_results[0].failure_info is not None
        assert second_results[0].failure_info is None
        assert first_child_id != second_child_id

        default_subtree = service.get_task_tree_subtree_payload(record.task_id, root.node_id)
        first_round_subtree = service.get_task_tree_subtree_payload(record.task_id, root.node_id, round_id="round-1")

        assert default_subtree is not None
        assert first_round_subtree is not None
        default_root = default_subtree["nodes_by_id"][root.node_id]
        assert [item["round_id"] for item in default_root["rounds"]] == ["round-1", "round-2"]
        assert default_root["default_round_id"] == "round-2"
        assert second_child_id in default_subtree["nodes_by_id"]
        assert first_child_id in first_round_subtree["nodes_by_id"]
        visible_failed_ids = service.query_service.failed_node_ids(record.task_id)
        assert visible_failed_ids == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_duplicate_successful_spawn_reuses_completed_operation_without_new_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _install_allow_all_spawn_review(service, monkeypatch)
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)

        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary="child succeeded",
                    answer="child succeeded",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        specs = [
            SpawnChildSpec(
                goal="same child",
                prompt="same prompt",
                execution_policy=_execution_policy(),
            )
        ]
        first_results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=specs,
            call_id="round-1",
        )
        second_results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=specs,
            call_id="round-2",
        )

        root_after = service.get_node(root.node_id)
        assert root_after is not None
        spawn_operations = dict((root_after.metadata or {}).get("spawn_operations") or {})

        assert list(spawn_operations) == ["round-1"]
        assert "round-2" not in spawn_operations
        assert [item.model_dump(mode="json") for item in first_results] == [
            item.model_dump(mode="json") for item in second_results
        ]

        subtree = service.get_task_tree_subtree_payload(record.task_id, root.node_id)
        assert subtree is not None
        subtree_root = subtree["nodes_by_id"][root.node_id]
        assert [item["round_id"] for item in subtree_root["rounds"]] == ["round-1"]
        assert subtree_root["default_round_id"] == "round-1"
    finally:
        await service.close()


def test_node_detail_returns_matching_artifacts_for_node(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)

    assert root is not None

    matching = service.artifact_store.create_text_artifact(
        task_id=record.task_id,
        node_id=root.node_id,
        kind="report",
        title="Root Artifact",
        content="root artifact content",
    )
    service.artifact_store.create_text_artifact(
        task_id=record.task_id,
        node_id="node:other",
        kind="report",
        title="Other Artifact",
        content="other artifact content",
    )

    payload = service.node_detail(record.task_id, root.node_id)

    assert isinstance(payload, dict)
    assert payload["ok"] is True
    assert payload["task_id"] == record.task_id
    assert payload["node_id"] == root.node_id
    assert payload["item"]["node_id"] == root.node_id
    assert payload["item"]["detail_level"] == "summary"
    assert payload["artifact_count"] == 1
    assert payload["artifacts_preview"][0]["artifact_id"] == matching.artifact_id
    assert payload["artifacts_preview"][0]["node_id"] == root.node_id
    assert payload["artifacts_preview"][0]["ref"] == f'artifact:{matching.artifact_id}'
    assert "artifacts" not in payload


def test_rest_node_detail_reports_real_artifact_metadata_for_summary_and_full(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)

    assert root is not None

    matching = service.artifact_store.create_text_artifact(
        task_id=record.task_id,
        node_id=root.node_id,
        kind="report",
        title="Root Artifact",
        content="root artifact content",
    )

    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    client = TestClient(_build_app())

    summary_response = client.get(f"/api/tasks/{record.task_id}/nodes/{root.node_id}")
    full_response = client.get(
        f"/api/tasks/{record.task_id}/nodes/{root.node_id}",
        params={"detail_level": "full"},
    )

    assert summary_response.status_code == 200
    assert full_response.status_code == 200

    summary_payload = summary_response.json()
    assert summary_payload["artifact_count"] == 1
    assert summary_payload["artifacts_preview"][0]["artifact_id"] == matching.artifact_id
    assert summary_payload["item"]["artifact_count"] == 1
    assert summary_payload["item"]["artifacts_preview"][0]["artifact_id"] == matching.artifact_id

    full_payload = full_response.json()
    assert full_payload["artifact_count"] == 1
    assert full_payload["artifacts"][0]["artifact_id"] == matching.artifact_id
    assert full_payload["item"]["artifact_count"] == 1
    assert full_payload["item"]["artifacts_preview"] == []


def test_get_node_detail_payload_uses_summary_mode_and_execution_trace_ref(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    payload = service.get_node_detail_payload(record.task_id, record.root_node_id)

    assert payload is not None
    assert payload["item"]["detail_level"] == "summary"
    assert payload["item"]["execution_trace_ref"].startswith("artifact:")
    assert "execution_trace_summary" in payload["item"]
    assert "execution_trace" not in payload["item"]


@pytest.mark.asyncio
async def test_worker_startup_reuses_existing_execution_trace_refs_without_reexternalizing(tmp_path: Path, monkeypatch):
    seed_service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-seed",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    worker_service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-worker",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )

    try:
        record = await _create_web_task(seed_service)
        detail_before = seed_service.store.get_task_node_detail(record.root_node_id)
        assert detail_before is not None
        assert detail_before.execution_trace_ref.startswith("artifact:")

        async def _noop_enqueue(_task_id: str) -> None:
            return None

        def _forbid_externalize(*args, **kwargs):
            raise AssertionError("startup should not re-externalize execution traces")

        worker_service.global_scheduler.enqueue_task = _noop_enqueue
        worker_service._start_worker_loops = lambda: None
        worker_service.task_stall_notifier.bootstrap_running_tasks = lambda: None
        monkeypatch.setattr(worker_service.content_store, "maybe_externalize_text", _forbid_externalize)

        await worker_service.startup()

        detail_after = worker_service.store.get_task_node_detail(record.root_node_id)
        assert detail_after is not None
        assert detail_after.execution_trace_ref == detail_before.execution_trace_ref
    finally:
        await worker_service.close()
        await seed_service.close()


def test_get_node_detail_payload_rebuilds_missing_execution_trace_ref_on_demand(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    detail_before = service.store.get_task_node_detail(record.root_node_id)

    assert detail_before is not None
    assert detail_before.execution_trace_ref.startswith("artifact:")

    blank_payload = {
        **dict(detail_before.payload or {}),
        "execution_trace_ref": "",
    }
    service.store.upsert_task_node_detail(
        detail_before.model_copy(
            update={
                "execution_trace_ref": "",
                "payload": blank_payload,
            }
        )
    )

    payload = service.get_node_detail_payload(record.task_id, record.root_node_id)

    assert payload is not None
    assert payload["item"]["execution_trace_ref"].startswith("artifact:")
    detail_after = service.store.get_task_node_detail(record.root_node_id)
    assert detail_after is not None
    assert detail_after.execution_trace_ref.startswith("artifact:")


def test_get_node_detail_payload_summary_mode_rebuilds_flattened_execution_trace_summary_with_rounds(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    service.log_service.submit_next_stage(
        record.task_id,
        record.root_node_id,
        stage_goal="inspect repository structure",
        tool_round_budget=2,
    )
    service.log_service.record_execution_stage_round(
        record.task_id,
        record.root_node_id,
        tool_calls=[{"id": "call:1", "name": "filesystem", "arguments": {"action": "list", "path": str(tmp_path)}}],
        created_at=now_iso(),
    )
    service.log_service.sync_node_read_model(record.task_id, record.root_node_id, externalize_execution_trace=True)
    detail_before = service.store.get_task_node_detail(record.root_node_id)

    assert detail_before is not None
    assert detail_before.execution_trace_ref.startswith("artifact:")

    legacy_summary = {
        "stages": [
            {
                "stage_goal": "inspect repository structure",
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
                "tool_calls": [
                    {
                        "tool_call_id": "call:1",
                        "tool_name": "filesystem",
                        "arguments_text": '{"action":"list","path":"legacy"}',
                        "output_text": "legacy listing",
                        "status": "success",
                    }
                ],
            }
        ]
    }
    service.store.upsert_task_node_detail(
        detail_before.model_copy(
            update={
                "payload": {
                    **dict(detail_before.payload or {}),
                    "execution_trace_summary": legacy_summary,
                },
            }
        )
    )

    payload = service.get_node_detail_payload(record.task_id, record.root_node_id)

    assert payload is not None
    summary = payload["item"]["execution_trace_summary"]
    assert summary["stages"][0]["rounds"][0]["tools"][0]["tool_name"] == "filesystem"
    assert summary["stages"][0]["tool_calls"][0]["tool_name"] == "filesystem"


def test_get_node_detail_payload_summary_mode_uses_previews_instead_of_full_inline_text(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)

    assert root is not None

    input_text = "\n".join(f"input line {index:03d}" for index in range(160))
    output_text = "\n".join(f"output line {index:03d}" for index in range(180))
    check_result = "\n".join(f"check line {index:03d}" for index in range(170))
    final_output = "\n".join(f"final line {index:03d}" for index in range(200))

    service.log_service.update_node_input(record.task_id, root.node_id, input_text)
    service.log_service.append_node_output(record.task_id, root.node_id, content=output_text)
    service.log_service.update_node_check_result(record.task_id, root.node_id, check_result)
    service.log_service.update_node_status(
        record.task_id,
        root.node_id,
        status="success",
        final_output=final_output,
    )

    payload = service.get_node_detail_payload(record.task_id, root.node_id)

    assert payload is not None
    item = payload["item"]
    assert item["detail_level"] == "summary"
    assert item["input"] == ""
    assert item["output"] == ""
    assert item["check_result"] == ""
    assert item["final_output"] == ""
    assert item["input_preview"]
    assert item["output_preview"]
    assert item["check_result_preview"]
    assert item["final_output_preview"]
    assert item["input_preview"] != input_text
    assert item["output_preview"] != output_text
    assert item["check_result_preview"] != check_result
    assert item["final_output_preview"] != final_output
    assert len(item["input_preview"]) < len(input_text)
    assert len(item["output_preview"]) < len(output_text)
    assert len(item["check_result_preview"]) < len(check_result)
    assert len(item["final_output_preview"]) < len(final_output)
    assert item["input_ref"].startswith("artifact:")
    assert item["output_ref"].startswith("artifact:")
    assert item["check_result_ref"].startswith("artifact:")
    assert item["final_output_ref"].startswith("artifact:")


def test_node_detail_summary_compacts_tool_payloads_from_trace(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)
    assert root is not None
    _create_pending_tool_round(
        service,
        task_id=record.task_id,
        node_id=root.node_id,
        tool_calls=[
            {
                "id": "call-1",
                "name": "filesystem",
                "arguments": {"path": str(tmp_path)},
            }
        ],
        live_tool_calls=[{"tool_call_id": "call-1", "tool_name": "filesystem", "status": "running"}],
        content="pending compaction regression",
    )
    service.log_service.upsert_synthetic_tool_result(
        task_id=record.task_id,
        node_id=root.node_id,
        tool_call_id="call-1",
        tool_name="filesystem",
        status="success",
        arguments_text=json.dumps({"path": str(tmp_path)}, ensure_ascii=False),
        output_text="very long inline output",
        output_ref="artifact:artifact:tool-output",
        started_at=now_iso(),
        finished_at=now_iso(),
    )
    service.log_service.sync_node_read_model(record.task_id, root.node_id, externalize_execution_trace=True)
    payload = service.get_node_detail_payload(record.task_id, root.node_id)

    assert payload is not None
    summary = payload["item"]["execution_trace_summary"]
    stages = summary.get("stages") or []
    assert stages
    rounds = stages[0].get("rounds") or []
    assert rounds
    tools = rounds[0].get("tools") or []
    assert tools
    tool = tools[0]

    assert tool["tool_call_id"] == "call-1"
    assert tool["tool_name"] == "filesystem"
    assert tool["arguments_preview"]
    assert '"path"' in tool["arguments_preview"]
    assert str(tmp_path) in tool["arguments_preview"]
    assert tool.get("output_preview")
    assert tool["output_ref"] == "artifact:artifact:tool-output"
    assert "arguments_text" not in tool
    assert "output_text" not in tool


def test_node_detail_resolves_full_final_and_acceptance_text_from_refs(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)

    assert root is not None

    final_output = "\n".join(f"final line {index:03d}" for index in range(200))
    acceptance_result = "\n".join(f"acceptance line {index:03d}" for index in range(180))

    service.log_service.update_node_status(
        record.task_id,
        root.node_id,
        status="success",
        final_output=final_output,
    )
    service.log_service.update_node_check_result(
        record.task_id,
        root.node_id,
        acceptance_result,
    )
    service.record_node_file_change(record.task_id, root.node_id, path=str((tmp_path / "created.txt").resolve()), change_type="created")

    payload = service.get_node_detail_payload(record.task_id, root.node_id, detail_level="full")

    assert payload is not None
    item = payload["item"]
    assert item["final_output"] == final_output
    assert item["check_result"] == acceptance_result
    assert item["execution_trace"]["final_output"] == final_output
    assert item["execution_trace"]["acceptance_result"] == acceptance_result
    assert item["final_output_ref"].startswith("artifact:")
    assert item["check_result_ref"].startswith("artifact:")
    assert item["tool_file_changes"] == [
        {
            "path": str((tmp_path / "created.txt").resolve()),
            "change_type": "created",
        }
    ]


def test_node_latest_context_uses_singleton_runtime_frame_artifact_and_freezes_on_remove(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)

    assert root is not None

    service.log_service.upsert_frame(
        record.task_id,
        {
            "node_id": root.node_id,
            "depth": root.depth,
            "node_kind": root.node_kind,
            "phase": "before_model",
            "messages": [{"role": "user", "content": "first context"}],
        },
        publish_snapshot=False,
    )
    first_frame = service.store.get_task_runtime_frame(record.task_id, root.node_id)
    assert first_frame is not None
    first_ref = str((first_frame.payload or {}).get("messages_ref") or "")
    assert first_ref.startswith("artifact:")

    service.log_service.update_frame(
        record.task_id,
        root.node_id,
        lambda frame: {
            **frame,
            "messages": [{"role": "user", "content": "second context"}],
        },
        publish_snapshot=False,
    )
    second_frame = service.store.get_task_runtime_frame(record.task_id, root.node_id)
    assert second_frame is not None
    second_ref = str((second_frame.payload or {}).get("messages_ref") or "")
    assert second_ref == first_ref

    runtime_artifacts = [
        artifact
        for artifact in service.list_artifacts(record.task_id)
        if artifact.kind == "task_runtime_messages" and artifact.node_id == root.node_id
    ]
    assert len(runtime_artifacts) == 1
    assert "second context" in Path(runtime_artifacts[0].path).read_text(encoding="utf-8")

    service.log_service.remove_frame(record.task_id, root.node_id, publish_snapshot=False)
    assert service.store.get_task_runtime_frame(record.task_id, root.node_id) is None

    payload = service.get_node_latest_context_payload(record.task_id, root.node_id)

    assert payload is not None
    assert payload["ref"] == second_ref
    assert "second context" in payload["content"]


def test_latest_context_route_returns_payload(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    root = service.get_node(record.root_node_id)

    assert root is not None

    service.log_service.upsert_frame(
        record.task_id,
        {
            "node_id": root.node_id,
            "depth": root.depth,
            "node_kind": root.node_kind,
            "phase": "before_model",
            "messages": [{"role": "user", "content": "route context"}],
        },
        publish_snapshot=False,
    )

    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    client = TestClient(_build_app())

    response = client.get(f"/api/tasks/{record.task_id}/nodes/{root.node_id}/latest-context")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["task_id"] == record.task_id
    assert payload["node_id"] == root.node_id
    assert payload["ref"].startswith("artifact:")
    assert "route context" in payload["content"]


def test_node_detail_and_latest_context_repair_legacy_mojibake(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = asyncio.run(_create_web_task(service))
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=root,
        goal=f"鏈€缁堥獙鏀?{root.goal}",
        acceptance_prompt="鏍稿鏈€缁堢粨鏋滄槸鍚︽弧瓒宠姹傘€?",
        parent_node_id=root.node_id,
        metadata={"final_acceptance": True},
    )
    service.log_service.update_node_check_result(record.task_id, acceptance.node_id, "楠屾敹閫氳繃")
    service.log_service.upsert_frame(
        record.task_id,
        {
            "node_id": acceptance.node_id,
            "depth": acceptance.depth,
            "node_kind": acceptance.node_kind,
            "phase": "before_model",
            "messages": [{"role": "user", "content": "legacy context"}],
        },
        publish_snapshot=False,
    )

    detail_payload = service.get_node_detail_payload(record.task_id, acceptance.node_id)
    latest_context = service.get_node_latest_context_payload(record.task_id, acceptance.node_id)

    assert detail_payload is not None
    assert latest_context is not None
    assert detail_payload["item"]["goal"] == f"最终验收:{root.goal}"
    assert detail_payload["item"]["check_result"] == "验收通过"
    assert latest_context["title"] == f"最终验收:{root.goal}"


def test_rest_node_detail_accepts_full_detail_level_query_parameter(tmp_path: Path, monkeypatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    captured: dict[str, str] = {}

    def _node_detail(task_id: str, node_id: str, detail_level: str = "summary"):
        captured["task_id"] = task_id
        captured["node_id"] = node_id
        captured["detail_level"] = detail_level
        return {
            "ok": True,
            "task_id": task_id,
            "node_id": node_id,
            "item": {
                "task_id": task_id,
                "node_id": node_id,
                "detail_level": detail_level,
                "execution_trace": {"stages": []},
            },
        }

    service.node_detail = _node_detail
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    client = TestClient(_build_app())

    response = client.get("/api/tasks/task:demo/nodes/node:demo?detail_level=full")

    assert response.status_code == 200
    assert captured == {
        "task_id": "task:demo",
        "node_id": "node:demo",
        "detail_level": "full",
    }
    assert response.json()["item"]["detail_level"] == "full"


def test_failed_final_acceptance_node_preserves_root_status_and_marks_task_business_unpassed(tmp_path: Path):
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
                    "prompt": "鏍稿鏈€缁堢粨鏋滄槸鍚︽弧瓒宠姹傘€?",
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
        acceptance_prompt="鏍稿鏈€缁堢粨鏋滄槸鍚︽弧瓒宠姹傘€?",
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
    assert latest_root.status == "success"
    assert latest_root.final_output == "root deliverable"
    assert latest_root.failure_reason == ""
    assert latest_root.check_result == "final acceptance failed"
    assert latest_task.status == "success"
    assert latest_task.failure_reason == "final acceptance failed"
    assert final_acceptance is not None
    assert final_acceptance.status == "failed"
    assert latest_task.metadata.get("failure_class") == "business_unpassed"
    assert latest_task.metadata.get("final_execution_output") == "root deliverable"
    items = service.query_service.get_tasks("web:shared", 1)
    assert len(items) == 1
    assert items[0].failure_class == "business_unpassed"
    assert items[0].final_acceptance.get("status") == "failed"


@pytest.mark.asyncio
async def test_retry_task_reopens_same_task_in_place_for_engine_failure(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task("retry me in place", session_id="web:shared")
        root = service.get_node(record.root_node_id)

        assert root is not None

        service.log_service.update_node_status(
            record.task_id,
            root.node_id,
            status="failed",
            final_output="engine failure",
            failure_reason="engine failure",
        )

        failed_task = service.get_task(record.task_id)
        assert failed_task is not None
        assert failed_task.status == "failed"
        assert failed_task.metadata.get("failure_class") == "engine_failure"

        retried = await service.retry_task(record.task_id)
        latest_task = service.get_task(record.task_id)
        latest_root = service.get_node(record.root_node_id)
        runtime_frame = service.log_service.read_runtime_frame(record.task_id, record.root_node_id)

        assert retried is not None
        assert retried.task_id == record.task_id
        assert latest_task is not None
        assert latest_root is not None
        assert len(service.store.list_tasks()) == 1
        assert latest_task.status == "in_progress"
        assert latest_task.finished_at is None
        assert latest_task.failure_reason == ""
        assert list(latest_task.metadata.get("retry_history") or [])
        assert latest_root.status == "in_progress"
        assert latest_root.final_output == ""
        assert latest_root.failure_reason == ""
        assert runtime_frame is not None
        assert str(runtime_frame.get("phase") or "").strip() == "before_model"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_retry_task_rejects_business_unpassed_task(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task(
            "final acceptance retry blocked",
            session_id="web:shared",
            metadata={
                "final_acceptance": {
                    "required": True,
                    "prompt": "verify the final answer",
                }
            },
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

        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=root,
            goal="final acceptance",
            acceptance_prompt="verify the final answer",
            parent_node_id=root.node_id,
            metadata={"final_acceptance": True},
        )
        service.log_service.update_node_status(
            record.task_id,
            acceptance.node_id,
            status="failed",
            final_output="acceptance failed",
            failure_reason="acceptance failed",
        )

        with pytest.raises(ValueError, match="task_not_retryable"):
            await service.retry_task(record.task_id)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_continue_task_recreate_cancels_old_task_then_creates_new_task(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task("continue by recreating", session_id="web:shared")

        result = await service.continue_task(
            mode="recreate",
            target_task_id=record.task_id,
            continuation_instruction="continue with recovered context",
            reason="heartbeat_stall",
            source="heartbeat",
        )

        original = service.get_task(record.task_id)
        continuation = result.get("continuation_task")

        assert result["status"] == "completed"
        assert result["mode"] == "recreate"
        assert original is not None
        assert original.status == "failed"
        assert original.failure_reason == "canceled"
        assert continuation is not None
        assert continuation.task_id != record.task_id
        assert continuation.metadata.get("continuation_of_task_id") == record.task_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_continue_task_retry_in_place_reopens_same_task_after_terminalizing(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task("continue by retrying", session_id="web:shared")
        root = service.get_node(record.root_node_id)

        assert root is not None

        service.log_service.update_node_status(
            record.task_id,
            root.node_id,
            status="failed",
            final_output="engine failure",
            failure_reason="engine failure",
        )

        result = await service.continue_task(
            mode="retry_in_place",
            target_task_id=record.task_id,
            continuation_instruction="retry safely in place",
            reason="engine_failure",
            source="api",
        )

        latest = service.get_task(record.task_id)

        assert result["status"] == "completed"
        assert result["mode"] == "retry_in_place"
        assert result.get("resumed_task") is not None
        assert result["resumed_task"].task_id == record.task_id
        assert latest is not None
        assert latest.status == "in_progress"
        assert len(service.store.list_tasks()) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_continue_task_retry_in_place_rejects_business_unpassed(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task(
            "continue blocked for unpassed task",
            session_id="web:shared",
            metadata={
                "final_acceptance": {
                    "required": True,
                    "prompt": "verify the final answer",
                }
            },
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

        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=root,
            goal="final acceptance",
            acceptance_prompt="verify the final answer",
            parent_node_id=root.node_id,
            metadata={"final_acceptance": True},
        )
        service.log_service.update_node_status(
            record.task_id,
            acceptance.node_id,
            status="failed",
            final_output="acceptance failed",
            failure_reason="acceptance failed",
        )

        with pytest.raises(ValueError, match="task_not_retryable"):
            await service.continue_task(
                mode="retry_in_place",
                target_task_id=record.task_id,
                continuation_instruction="retry",
                reason="manual",
                source="api",
            )
    finally:
        await service.close()


def test_live_tree_payload_keeps_acceptance_node_kind(tmp_path: Path):
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
        acceptance_prompt="妫€鏌ユ渶缁堢粨鏋滄槸鍚︽弧瓒宠姹傘€?",
        parent_node_id=root.node_id,
        metadata={"final_acceptance": True},
    )

    payload = service.get_task_tree_subtree_payload(record.task_id, root.node_id)

    assert payload is not None
    assert acceptance.node_id in payload["nodes_by_id"]
    assert payload["nodes_by_id"][acceptance.node_id]["node_kind"] == "acceptance"


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
        stage_goal="鏍归樁娈电洰鏍?",
        tool_round_budget=1,
    )
    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
    )
    service.log_service.submit_next_stage(
        record.task_id,
        child.node_id,
        stage_goal="瀛愰樁娈电洰鏍?",
        tool_round_budget=1,
    )
    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=child,
        goal="accept child",
        acceptance_prompt="妫€鏌?child 杈撳嚭銆?",
        parent_node_id=child.node_id,
    )

    text = service.view_progress(record.task_id, mark_read=False)

    assert text.startswith("Task status: in_progress\n")
    assert root.node_id in text
    assert child.node_id in text
    assert acceptance.node_id in text
    assert "Latest node output" not in text
    assert "Active parallel work:" not in text


def test_view_progress_tree_text_shows_acceptance_stage_goal_when_present(tmp_path: Path):
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
        goal="accept root",
        acceptance_prompt="verify root output",
        parent_node_id=root.node_id,
    )
    service.log_service.submit_next_stage(
        record.task_id,
        acceptance.node_id,
        stage_goal="validate cited evidence before final verdict",
        tool_round_budget=1,
    )

    progress = service.query_service.view_progress(record.task_id, mark_read=False)
    acceptance_detail = service.get_node_detail_payload(record.task_id, acceptance.node_id, detail_level="full")

    assert progress is not None
    assert acceptance_detail is not None
    assert acceptance.node_id in progress.tree_text
    assert "validate cited evidence before final verdict" in progress.tree_text
    assert acceptance_detail["item"]["execution_trace"]["stages"][0]["stage_goal"] == "validate cited evidence before final verdict"


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
        stage_goal="鏃ч樁娈电洰鏍?",
        tool_round_budget=1,
    )
    service.log_service.replace_runtime_frames(
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
                "stage_mode": "鑷富鎵ц",
                "stage_status": "杩涜涓?",
                "stage_goal": "鏈€鏂伴樁娈电洰鏍?",
                "stage_total_steps": 1,
                "tool_calls": [],
                "child_pipelines": [],
            }
        ],
    )

    progress = service.query_service.view_progress(record.task_id, mark_read=False)

    assert progress is not None
    assert progress.tree_text == f"({root.node_id},in_progress,鏈€鏂伴樁娈电洰鏍?)"


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

    progress = service.query_service.view_progress(record.task_id, mark_read=False)
    node_payload = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level="full")

    assert progress is not None
    assert node_payload is not None
    assert node_payload["item"]["output"] == "still working on the task"
    assert node_payload["item"]["final_output"] == ""
    assert node_payload["item"]["execution_trace"]["final_output"] == ""

    root_progress_node = next(
        item for item in progress.nodes if item["node_id"] == record.root_node_id
    )
    assert root_progress_node["execution_trace"]["final_output"] == ""


def test_view_progress_nodes_are_compact_summaries(tmp_path: Path):
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
    progress = service.query_service.view_progress(record.task_id, mark_read=False)

    assert root is not None
    assert progress is not None

    root_progress_node = next(
        item for item in progress.nodes if item["node_id"] == record.root_node_id
    )

    assert root_progress_node["goal"] == root.goal
    assert root_progress_node["title"] == root.goal
    assert root_progress_node["status"] == root.status
    assert root_progress_node["execution_trace"]["tool_steps"] == []
    assert root_progress_node["execution_trace"]["stages"] == []
    assert "prompt" not in root_progress_node
    assert "input" not in root_progress_node
    assert "metadata" not in root_progress_node
    assert "root" not in progress.model_dump(mode="json")


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

    service.pause_task = _pause
    service.cancel_task = _cancel

    await service._process_worker_command({"command_type": "pause_task", "task_id": "demo"})
    await service._process_worker_command({"command_type": "cancel_task", "task_id": "demo"})

    assert paused == ["task:demo"]
    assert cancelled == ["task:demo"]


@pytest.mark.asyncio
async def test_request_worker_runtime_refresh_waits_for_worker_ack(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("main.service.runtime_service.get_config_path", lambda: config_path)

    worker_service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-worker",
        artifact_dir=tmp_path / "artifacts-worker",
        governance_store_path=tmp_path / "governance-worker.sqlite3",
        execution_mode="worker",
    )
    web_service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-web",
        artifact_dir=tmp_path / "artifacts-web",
        governance_store_path=tmp_path / "governance-web.sqlite3",
        execution_mode="web",
    )
    captured: dict[str, object] = {}

    def _ensure_runtime_config_current(*, force: bool = False, reason: str = "runtime") -> bool:
        captured["force"] = force
        captured["reason"] = reason
        return True

    worker_service.ensure_runtime_config_current = _ensure_runtime_config_current

    try:
        await worker_service.startup()

        result = await web_service.request_worker_runtime_refresh(reason="test-refresh", timeout_s=2.0)

        assert captured == {"force": True, "reason": "test-refresh"}
        assert result["worker_refresh_acked"] is True
        assert result["changed"] is True
        assert result["worker_id"] == worker_service.worker_id
        assert result["applied_config_mtime_ns"] == config_path.stat().st_mtime_ns
    finally:
        await web_service.close()
        await worker_service.close()


@pytest.mark.asyncio
async def test_worker_startup_rejects_second_logical_worker(tmp_path: Path):
    first = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-first",
        artifact_dir=tmp_path / "artifacts-first",
        governance_store_path=tmp_path / "governance-first.sqlite3",
        execution_mode="worker",
    )
    second = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-second",
        artifact_dir=tmp_path / "artifacts-second",
        governance_store_path=tmp_path / "governance-second.sqlite3",
        execution_mode="worker",
    )

    try:
        await first.startup()
        with pytest.raises(RuntimeError, match="worker_lease_unavailable"):
            await second.startup()
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_worker_startup_takes_over_stale_lease(tmp_path: Path):
    stale_owner = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-stale",
        artifact_dir=tmp_path / "artifacts-stale",
        governance_store_path=tmp_path / "governance-stale.sqlite3",
        execution_mode="worker",
    )
    recovered = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks-recovered",
        artifact_dir=tmp_path / "artifacts-recovered",
        governance_store_path=tmp_path / "governance-recovered.sqlite3",
        execution_mode="worker",
    )
    stale_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).astimezone().isoformat(timespec="seconds")

    stale_owner.store.acquire_worker_lease(
        role="task_worker",
        worker_id="worker:stale",
        holder_pid=99999,
        acquired_at=stale_at,
        heartbeat_at=stale_at,
        expires_at=stale_at,
        payload={"workspace": str(tmp_path)},
    )

    try:
        await recovered.startup()
        lease = recovered.store.get_worker_lease("task_worker")
        assert recovered._worker_lease_takeover is True
        assert lease is not None
        assert lease["worker_id"] == recovered.worker_id
    finally:
        await recovered.close()
        stale_owner.store.close()


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
        assert service.global_scheduler.is_active(record.task_id) is False

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
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:final",
                        name="submit_final_result",
                        arguments={
                            "status": "success",
                            "delivery_status": "final",
                            "summary": "done",
                            "answer": "done",
                            "evidence": [{"kind": "artifact", "note": "resume path completed"}],
                            "remaining_work": [],
                            "blocking_reason": "",
                        },
                    )
                ],
                finish_reason="tool_calls",
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
        assert service.global_scheduler.is_active(record.task_id) is False

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
            if current is not None and current.is_paused and not service.global_scheduler.is_active(record.task_id):
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
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    _install_allow_all_spawn_review(service)

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
            spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
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
            spec=SpawnChildSpec(goal="bad child", prompt="bad prompt", execution_policy=_execution_policy()),
        )
        nested_success = service.node_runner._create_execution_child(
            task=task,
            parent=in_progress_child,
            spec=SpawnChildSpec(goal="nested child", prompt="nested prompt", execution_policy=_execution_policy()),
        )
        service.log_service.update_node_status(
            record.task_id,
            nested_success.node_id,
            status="success",
            final_output="nested done",
        )
        service.log_service.update_task_runtime_meta(
            record.task_id,
            last_visible_output_at=now_iso(),
            last_stall_notice_bucket_minutes=0,
        )
        service.log_service.replace_runtime_frames(
            record.task_id,
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
    restarted.global_scheduler.enqueue_task = _record_enqueue_calls(started)
    _install_allow_all_spawn_review(restarted)

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
        assert recovered_root.output
        assert preserved_child is not None
        assert preserved_child.status == "success"
        preserved_in_progress_child = restarted.get_node(in_progress_child.node_id)
        preserved_nested_success = restarted.get_node(nested_success.node_id)
        assert preserved_in_progress_child is not None
        assert preserved_in_progress_child.status == "in_progress"
        assert preserved_nested_success is not None
        assert preserved_nested_success.status == "success"

        runtime_state = restarted.log_service.read_runtime_state(record.task_id)
        assert runtime_state is not None
        projected_node_ids = {item.node_id for item in restarted.store.list_task_nodes(record.task_id)}
        assert projected_node_ids == {
            record.root_node_id,
            success_child.node_id,
            in_progress_child.node_id,
            nested_success.node_id,
        }
        tree_snapshot = restarted.query_service.get_tree_snapshot(record.task_id)
        assert tree_snapshot is not None
        assert set(tree_snapshot.nodes_by_id) == {
            record.root_node_id,
            success_child.node_id,
            in_progress_child.node_id,
            nested_success.node_id,
        }
        assert started == [record.task_id]
        assert "Recovery: 本任务遇到异常停止，已回退到稳定步骤继续。" not in restarted.view_progress(record.task_id, mark_read=False)

        before_child_ids = [node.node_id for node in restarted.store.list_children(record.root_node_id)]
        results = await restarted.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=record.root_node_id,
            specs=[SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy())],
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


@pytest.mark.asyncio
async def test_resume_waiting_children_turn_replays_incomplete_spawn_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    _install_allow_all_spawn_review(service, monkeypatch)

    try:
        record = await service.create_task("recover waiting children", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)

        assert task is not None
        assert root is not None

        spec = SpawnChildSpec(
            goal="child goal",
            prompt="child prompt",
            execution_policy=_execution_policy(),
        )
        child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=spec,
        )

        service.log_service.append_node_output(
            record.task_id,
            root.node_id,
            content="spawning child before shutdown",
            tool_calls=[
                {
                    "id": "call:spawn-round",
                    "name": "spawn_child_nodes",
                    "arguments": {
                        "children": [spec.model_dump(mode="json")],
                    },
                }
            ],
        )

        def _mutate(metadata: dict[str, object]) -> dict[str, object]:
            metadata["spawn_operations"] = {
                "call:spawn-round": {
                    "specs": [spec.model_dump(mode="json")],
                    "entries": [
                        {
                            "index": 0,
                            "goal": "child goal",
                            "prompt": "child prompt",
                            "requires_acceptance": False,
                            "acceptance_prompt": "",
                            "status": "running",
                            "started_at": now_iso(),
                            "finished_at": "",
                            "child_node_id": child.node_id,
                            "acceptance_node_id": "",
                            "check_status": "skipped",
                            "result": {},
                        }
                    ],
                    "results": [],
                    "completed": False,
                }
            }
            return metadata

        service.log_service.update_node_metadata(root.node_id, _mutate)
        service.log_service.replace_runtime_frames(
            record.task_id,
            active_node_ids=[root.node_id],
            runnable_node_ids=[root.node_id],
            waiting_node_ids=[root.node_id],
            frames=[
                {
                    **service.log_service._default_frame(
                        node_id=root.node_id,
                        depth=root.depth,
                        node_kind=root.node_kind,
                        phase="waiting_children",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                }
            ],
            publish_snapshot=False,
        )
        root = service.get_node(root.node_id)
        assert root is not None

        async def _finish_child(task_id: str, node_id: str):
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary="child done",
                    answer="child done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "_run_nested_node", _finish_child)

        history = await service.node_runner._react_loop._resume_waiting_children_turn_if_needed(
            task=task,
            node=root,
            message_history=[],
            tools=service.node_runner._build_tools(task=task, node=root),
            runtime_context={"task_id": record.task_id, "node_id": root.node_id, "node_kind": root.node_kind},
        )

        assert history is not None

        root_after = service.get_node(root.node_id)
        child_after = service.get_node(child.node_id)
        assert root_after is not None
        assert child_after is not None

        spawn_operations = dict((root_after.metadata or {}).get("spawn_operations") or {})
        operation = dict(spawn_operations.get("call:spawn-round") or {})
        entries = list(operation.get("entries") or [])

        assert operation.get("completed") is True
        assert entries[0]["status"] == "success"
        assert entries[0]["result"]["node_output"] == "child done"
        assert child_after.status == "success"

        tool_results = service.store.list_task_node_tool_results(record.task_id, root.node_id)
        assert any(item.tool_name == "spawn_child_nodes" and item.status == "success" for item in tool_results)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resume_pending_tool_turn_uses_recovery_check_for_verified_done_write(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    _install_allow_all_spawn_review(service)

    try:
        record = await service.create_task("recover write", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)

        assert task is not None
        assert root is not None

        target = tmp_path / "write-target.txt"
        target.write_text("expected body", encoding="utf-8")

        round_payload = _create_pending_tool_round(
            service,
            task_id=record.task_id,
            node_id=root.node_id,
            tool_calls=[
                {
                    "id": "call:write",
                    "name": "filesystem",
                    "arguments": {
                        "action": "write",
                        "path": str(target),
                        "content": "expected body",
                    },
                }
            ],
            live_tool_calls=[{"tool_call_id": "call:write", "tool_name": "filesystem", "status": "running"}],
            content="write pending before shutdown",
        )

        history = await service.node_runner._react_loop._resume_pending_tool_turn_if_needed(
            task=task,
            node=root,
            message_history=[],
            tools={},
            runtime_context={"task_temp_dir": str(tmp_path)},
        )

        assert history is not None
        tool_results = service.store.list_task_node_tool_results(record.task_id, root.node_id)
        result_statuses = {item.tool_call_id: item.status for item in tool_results}
        assert result_statuses["call:write"] == "success"
        assert any(item.tool_name == "recovery_check" for item in tool_results)

        detail = service.get_node_detail_payload(record.task_id, root.node_id)
        assert detail is not None
        round_tools = detail["item"]["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]
        assert [item["tool_name"] for item in round_tools] == ["recovery_check", "filesystem"]
        recovery_step = round_tools[0]
        assert recovery_step["status"] == "success"
        assert recovery_step["tool_call_id"] == f"recovery_check:{round_payload['round_id']}"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resume_pending_tool_turn_marks_exec_round_for_model_decide_when_side_effect_uncertain(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    _install_allow_all_spawn_review(service)

    try:
        record = await service.create_task("recover exec", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)

        assert task is not None
        assert root is not None

        _create_pending_tool_round(
            service,
            task_id=record.task_id,
            node_id=root.node_id,
            tool_calls=[
                {
                    "id": "call:exec",
                    "name": "exec",
                    "arguments": {"command": "git apply patch.diff"},
                }
            ],
            live_tool_calls=[{"tool_call_id": "call:exec", "tool_name": "exec", "status": "running"}],
            content="exec pending before shutdown",
        )

        history = await service.node_runner._react_loop._resume_pending_tool_turn_if_needed(
            task=task,
            node=root,
            message_history=[],
            tools={"exec": _StaticTool("exec", result="should not run")},
            runtime_context={"task_temp_dir": str(tmp_path)},
        )

        assert history is not None
        tool_results = service.store.list_task_node_tool_results(record.task_id, root.node_id)
        result_statuses = {item.tool_call_id: item.status for item in tool_results}
        assert result_statuses["call:exec"] == "interrupted"
        assert any(item.tool_name == "recovery_check" for item in tool_results)

        detail = service.get_node_detail_payload(record.task_id, root.node_id)
        assert detail is not None
        round_tools = detail["item"]["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]
        assert [item["tool_name"] for item in round_tools] == ["recovery_check", "exec"]
        assert round_tools[0]["status"] == "warning"
        assert round_tools[1]["status"] == "interrupted"
    finally:
        await service.close()


def test_terminal_task_clears_runtime_frames_and_rejects_late_runtime_updates(tmp_path: Path):
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
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
    )
    service.log_service.replace_runtime_frames(
        record.task_id,
        active_node_ids=[root.node_id, child.node_id],
        runnable_node_ids=[root.node_id, child.node_id],
        waiting_node_ids=[],
        frames=[
            service.log_service._default_frame(node_id=root.node_id, depth=root.depth, node_kind=root.node_kind, phase="before_model"),
            service.log_service._default_frame(node_id=child.node_id, depth=child.depth, node_kind=child.node_kind, phase="before_model"),
        ],
    )

    service.log_service.update_node_status(
        record.task_id,
        root.node_id,
        status="failed",
        failure_reason="root failed",
    )

    runtime_state = service.log_service.read_runtime_state(record.task_id)
    latest_task = service.get_task(record.task_id)

    assert latest_task is not None
    assert latest_task.status == "failed"
    assert runtime_state is not None
    assert runtime_state["active_node_ids"] == []
    assert runtime_state["runnable_node_ids"] == []
    assert runtime_state["waiting_node_ids"] == []
    assert runtime_state["frames"] == []

    service.log_service.replace_runtime_frames(
        record.task_id,
        active_node_ids=[child.node_id],
        runnable_node_ids=[child.node_id],
        waiting_node_ids=[],
        frames=[
            service.log_service._default_frame(node_id=child.node_id, depth=child.depth, node_kind=child.node_kind, phase="before_model"),
        ],
    )

    runtime_state = service.log_service.read_runtime_state(record.task_id)

    assert runtime_state is not None
    assert runtime_state["active_node_ids"] == []
    assert runtime_state["runnable_node_ids"] == []
    assert runtime_state["waiting_node_ids"] == []
    assert runtime_state["frames"] == []


def test_terminal_event_emits_once_even_if_late_node_updates_arrive(tmp_path: Path):
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
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
    )

    service.log_service.update_node_status(
        record.task_id,
        root.node_id,
        status="failed",
        failure_reason="root failed",
    )

    terminal_events = [
        item
        for item in service.store.list_task_events(after_seq=0, task_id=record.task_id, limit=10_000)
        if item.get("event_type") == "task.terminal"
    ]
    assert len(terminal_events) == 1

    service.log_service.update_node_status(
        record.task_id,
        child.node_id,
        status="failed",
        failure_reason="late child update",
    )

    terminal_events = [
        item
        for item in service.store.list_task_events(after_seq=0, task_id=record.task_id, limit=10_000)
        if item.get("event_type") == "task.terminal"
    ]
    assert len(terminal_events) == 1


async def test_run_node_short_circuits_when_task_is_already_terminal(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    record = await _create_web_task(service)
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy=_execution_policy()),
    )

    service.log_service.update_node_status(
        record.task_id,
        root.node_id,
        status="failed",
        failure_reason="root failed",
    )

    result = await service.node_runner.run_node(record.task_id, child.node_id)
    latest_child = service.get_node(child.node_id)

    assert result.status == "failed"
    assert latest_child is not None
    assert latest_child.status == "failed"
    assert latest_child.failure_reason == "root failed"


@pytest.mark.asyncio
async def test_spawn_children_prefilters_specs_and_preserves_result_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = _SpawnReviewToolCallChatBackend(
        arguments={
            "allowed_indexes": [1],
            "blocked_specs": [
                {
                    "index": 0,
                    "reason": "拆分过细，偏离当前父节点目标",
                    "suggestion": "请由父节点直接执行，或收缩为更聚焦的单一派生",
                }
            ],
        }
    )
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)

        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary=f"{node.goal} done",
                    answer=f"{node.goal} done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[
                SpawnChildSpec(goal="blocked branch", prompt="blocked prompt", execution_policy=_execution_policy()),
                SpawnChildSpec(goal="allowed branch", prompt="allowed prompt", execution_policy=_execution_policy("coverage")),
            ],
            call_id="spawn-review-batch",
        )

        root_after = service.get_node(root.node_id)
        assert root_after is not None
        spawn_operation = dict((root_after.metadata or {}).get("spawn_operations") or {}).get("spawn-review-batch") or {}
        entries = list(spawn_operation.get("entries") or [])
        spawn_review = dict(spawn_operation.get("spawn_review") or {})

        assert [item.goal for item in results] == ["blocked branch", "allowed branch"]
        assert results[0].failure_info is None
        assert results[0].check_result == "派生已被拦截"
        assert "拆分过细" in results[0].node_output
        assert "请由父节点直接执行" in results[0].node_output
        assert results[0].node_output_summary == "派生拦截：拆分过细，偏离当前父节点目标"
        assert "请由父节点直接执行" not in results[0].node_output_summary
        assert results[1].node_output == "allowed branch done"
        assert len(service.store.list_children(root.node_id)) == 1
        assert len(entries) == 2
        assert entries[0]["review_decision"] == "blocked"
        assert entries[0]["blocked_reason"] == "拆分过细，偏离当前父节点目标"
        assert entries[0]["blocked_suggestion"] == "请由父节点直接执行，或收缩为更聚焦的单一派生"
        assert entries[0]["synthetic_result_summary"] == "派生拦截：拆分过细，偏离当前父节点目标"
        assert entries[1]["review_decision"] == "allowed"
        assert spawn_review["allowed_indexes"] == [1]
        assert spawn_review["blocked_specs"][0]["index"] == 0
        assert backend.calls
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_review_request_uses_root_to_parent_path_tree_and_stage_goals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = _SpawnReviewToolCallChatBackend(
        arguments={"allowed_indexes": [0], "blocked_specs": []}
    )
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task("spawn review path tree", session_id="web:shared", max_depth=3)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        parent = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(goal="parent branch", prompt="parent prompt", execution_policy=_execution_policy()),
        )
        existing_child = service.node_runner._create_execution_child(
            task=task,
            parent=parent,
            spec=SpawnChildSpec(goal="existing child", prompt="existing child prompt", execution_policy=_execution_policy()),
        )
        service.log_service.submit_next_stage(record.task_id, root.node_id, stage_goal="root stage goal", tool_round_budget=1)
        service.log_service.submit_next_stage(record.task_id, parent.node_id, stage_goal="parent stage goal", tool_round_budget=1)

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary=f"{node.goal} done",
                    answer=f"{node.goal} done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=parent.node_id,
            specs=[SpawnChildSpec(goal="new child", prompt="new child prompt", execution_policy=_execution_policy())],
            call_id="path-tree-review",
        )

        assert backend.calls
        call = backend.calls[-1]
        payload = json.loads(str(call["messages"][1]["content"]))
        assert payload["parent_node_id"] == parent.node_id
        assert payload["spawn_request"]["requested_specs"][0]["goal"] == "new child"
        assert "root stage goal" in payload["path_tree_text"]
        assert "parent stage goal" in payload["path_tree_text"]
        assert root.node_id in payload["path_tree_text"]
        assert parent.node_id in payload["path_tree_text"]
        assert existing_child.node_id not in payload["path_tree_text"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_spawn_review_retries_invalid_output_and_defaults_to_block_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    retry_backend = _SpawnReviewRetryChatBackend(
        responses=[
            LLMResponse(content="not-json", finish_reason="stop"),
            LLMResponse(
                content=json.dumps({"allowed_indexes": [0], "blocked_specs": []}),
                finish_reason="stop",
            ),
        ]
    )
    service = MainRuntimeService(
        chat_backend=retry_backend,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task("spawn review retry", session_id="web:shared")
        root = service.get_node(record.root_node_id)
        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary=f"{node.goal} done",
                    answer=f"{node.goal} done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[SpawnChildSpec(goal="retry child", prompt="retry prompt", execution_policy=_execution_policy())],
            call_id="retry-review",
        )

        assert len(results) == 1
        assert results[0].node_output == "retry child done"
        assert len(retry_backend.calls) == 2
        assert retry_backend.calls[0].get("tool_choice") is None
        second_messages = list(retry_backend.calls[1].get("messages") or [])
        assert second_messages[-1]["role"] == "user"
        assert "上一轮检验派生回复无效" in str(second_messages[-1]["content"] or "")
    finally:
        await service.close()

    exception_backend = _SpawnReviewExceptionChatBackend(message="inspection chain unavailable")
    exception_service = MainRuntimeService(
        chat_backend=exception_backend,
        store_path=tmp_path / "runtime-2.sqlite3",
        files_base_dir=tmp_path / "tasks-2",
        artifact_dir=tmp_path / "artifacts-2",
        governance_store_path=tmp_path / "governance-2.sqlite3",
        execution_mode="embedded",
    )
    exception_service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await exception_service.create_task("spawn review exception", session_id="web:shared")
        root = exception_service.get_node(record.root_node_id)
        assert root is not None

        async def _unexpected_run_node(*args, **kwargs):
            raise AssertionError("run_node should not be called when spawn review blocks all specs")

        monkeypatch.setattr(exception_service.node_runner, "run_node", _unexpected_run_node)

        results = await exception_service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[SpawnChildSpec(goal="blocked by exception", prompt="blocked prompt", execution_policy=_execution_policy())],
            call_id="exception-review",
        )

        root_after = exception_service.get_node(root.node_id)
        assert root_after is not None
        spawn_operation = dict((root_after.metadata or {}).get("spawn_operations") or {}).get("exception-review") or {}
        spawn_review = dict(spawn_operation.get("spawn_review") or {})

        assert len(results) == 1
        assert results[0].failure_info is None
        assert "RuntimeError: inspection chain unavailable" in results[0].node_output
        assert len(exception_service.store.list_children(root.node_id)) == 0
        assert "RuntimeError: inspection chain unavailable" in spawn_review["error_text"]
    finally:
        await exception_service.close()


@pytest.mark.asyncio
async def test_task_snapshot_excludes_governance_and_node_detail_includes_spawn_review_rounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = _SpawnReviewToolCallChatBackend(
        arguments={
            "allowed_indexes": [1],
            "blocked_specs": [
                {
                    "index": 0,
                    "reason": "信息价值不足",
                    "suggestion": "请由父节点直接执行",
                }
            ],
        }
    )
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)
        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary=f"{node.goal} done",
                    answer=f"{node.goal} done",
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[
                SpawnChildSpec(goal="blocked detail", prompt="blocked prompt", execution_policy=_execution_policy()),
                SpawnChildSpec(goal="allowed detail", prompt="allowed prompt", execution_policy=_execution_policy()),
            ],
            call_id="detail-review",
        )

        task_payload = service.get_task_detail_payload(record.task_id, mark_read=False)
        node_payload = service.get_node_detail_payload(record.task_id, root.node_id, detail_level="summary")

        assert task_payload is not None
        assert node_payload is not None
        assert "governance" not in task_payload
        assert "governance" not in task_payload["task"]
        assert "spawn_review_rounds" in node_payload["item"]
        assert len(node_payload["item"]["spawn_review_rounds"]) == 1
        assert node_payload["item"]["spawn_review_rounds"][0]["round_id"] == "detail-review"
        assert node_payload["item"]["spawn_review_rounds"][0]["blocked_specs"][0]["reason"] == "信息价值不足"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_tree_snapshot_excludes_fully_blocked_spawn_rounds_but_node_detail_keeps_spawn_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _SpawnReviewToolCallChatBackend(
        arguments={
            "allowed_indexes": [],
            "blocked_specs": [
                {
                    "index": 0,
                    "reason": "信息价值不足",
                    "suggestion": "请由父节点直接执行",
                },
                {
                    "index": 1,
                    "reason": "拆分过细",
                    "suggestion": "请合并为单个批次",
                },
            ],
        }
    )
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)
        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            raise AssertionError("blocked specs should not dispatch child nodes")

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        results = await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[
                SpawnChildSpec(goal="blocked detail 1", prompt="blocked prompt 1", execution_policy=_execution_policy()),
                SpawnChildSpec(goal="blocked detail 2", prompt="blocked prompt 2", execution_policy=_execution_policy()),
            ],
            call_id="detail-review-all-blocked",
        )

        subtree = service.get_task_tree_subtree_payload(record.task_id, root.node_id)
        node_payload = service.get_node_detail_payload(record.task_id, root.node_id, detail_level="summary")

        assert [item.check_result for item in results] == ["派生已被拦截", "派生已被拦截"]
        assert subtree is not None
        assert node_payload is not None
        subtree_root = subtree["nodes_by_id"][root.node_id]
        assert subtree_root["rounds"] == []
        assert subtree_root["default_round_id"] == ""
        assert "spawn_review_rounds" in node_payload["item"]
        assert len(node_payload["item"]["spawn_review_rounds"]) == 1
        assert node_payload["item"]["spawn_review_rounds"][0]["round_id"] == "detail-review-all-blocked"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_node_detail_includes_latest_direct_child_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = _SpawnReviewToolCallChatBackend(
        arguments={"allowed_indexes": [0, 1], "blocked_specs": []}
    )
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    try:
        record = await _create_web_task(service)
        root = service.get_node(record.root_node_id)
        assert root is not None

        async def _fake_run_node(task_id: str, node_id: str):
            node = service.get_node(node_id)
            assert node is not None
            return service.node_runner._mark_finished(
                task_id,
                node_id,
                NodeFinalResult(
                    status="success",
                    delivery_status="final",
                    summary=f"{node.goal} done",
                    answer=f"{node.goal} done\n" + ("z" * 1400),
                    evidence=[],
                    remaining_work=[],
                    blocking_reason="",
                ),
            )

        monkeypatch.setattr(service.node_runner, "run_node", _fake_run_node)

        await service.node_runner._spawn_children(
            task_id=record.task_id,
            parent_node_id=root.node_id,
            specs=[
                SpawnChildSpec(goal="child 1", prompt="prompt 1", execution_policy=_execution_policy()),
                SpawnChildSpec(goal="child 2", prompt="prompt 2", execution_policy=_execution_policy()),
            ],
            call_id="detail-child-results",
        )

        node_payload = service.get_node_detail_payload(record.task_id, root.node_id, detail_level="summary")
        assert node_payload is not None
        assert node_payload["item"]["latest_spawn_round_id"] == "detail-child-results"
        direct_child_results = list(node_payload["item"]["direct_child_results"])
        assert [item["goal"] for item in direct_child_results] == ["child 1", "child 2"]
        assert all(str(item["child_node_id"] or "").startswith("node:") for item in direct_child_results)
        assert all(item["check_result"] == SKIPPED_CHECK_RESULT for item in direct_child_results)
        assert all(str(item["node_output_summary"] or "").strip() for item in direct_child_results)
        assert all(str(item["node_output_ref"] or "").startswith("artifact:") for item in direct_child_results)
    finally:
        await service.close()

