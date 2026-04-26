from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.agent.tools.base import Tool
from g3ku.content import ContentNavigationService, parse_content_envelope
from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.session_service import HEARTBEAT_OK, WebSessionHeartbeatService
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.resources.models import ResourceKind, ToolResourceDescriptor
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.api import ceo_sessions, websocket_ceo
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import ceo_agent_middleware
from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted, CeoPendingInterrupt
from g3ku.runtime.manager import SessionRuntimeManager
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import SessionManager
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore


class _Registry:
    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self.published: list[tuple[str, dict[str, object]]] = []
        self.global_published: list[dict[str, object]] = []

    async def subscribe_ceo(self, session_id: str):
        _ = session_id
        return asyncio.Queue()

    async def subscribe_global_ceo(self):
        return asyncio.Queue()

    async def unsubscribe_ceo(self, session_id: str, queue) -> None:
        _ = session_id, queue

    async def unsubscribe_global_ceo(self, queue) -> None:
        _ = queue

    def next_ceo_seq(self, session_id: str) -> int:
        key = str(session_id or "")
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    def publish_ceo(self, session_id: str, envelope: dict[str, object]) -> None:
        self.published.append((str(session_id or ""), dict(envelope)))

    def publish_global_ceo(self, envelope: dict[str, object]) -> None:
        self.global_published.append(dict(envelope))


class _TaskService:
    def __init__(self) -> None:
        self.registry = _Registry()
        self.delivered: list[tuple[str, str]] = []
        self.store = SimpleNamespace(mark_task_terminal_outbox_delivered=self._mark_task_terminal_outbox_delivered)
        self.tasks: dict[str, object] = {}
        self.node_details: dict[tuple[str, str], dict[str, object]] = {}
        self.retry_calls: list[str] = []
        self.continue_calls: list[dict[str, object]] = []

    async def startup(self) -> None:
        return None

    def _mark_task_terminal_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        self.delivered.append((str(dedupe_key or ""), str(delivered_at or "")))

    def get_task(self, task_id: str):
        return self.tasks.get(str(task_id or "").strip())

    def get_node_detail_payload(self, task_id: str, node_id: str):
        key = (str(task_id or "").strip(), str(node_id or "").strip())
        return self.node_details.get(key)


    async def retry_task(self, task_id: str):
        normalized = str(task_id or "").strip()
        self.retry_calls.append(normalized)
        current = self.tasks.get(normalized)
        if current is None:
            return None
        current.status = "in_progress"
        return current

    async def continue_task(self, **kwargs):
        self.continue_calls.append(dict(kwargs))
        task_id = str(kwargs.get("target_task_id") or "").strip()
        current = self.tasks.get(task_id)
        if current is None:
            return None
        current.status = "in_progress"
        return {
            "status": "completed",
            "mode": str(kwargs.get("mode") or "").strip(),
            "target_task_id": task_id,
            "target_task": current,
            "target_task_terminal_status": "failed",
            "target_task_finished_at": "",
            "continuation_task": None,
            "resumed_task": current,
            "reused_existing": False,
            "message": "retried_in_place",
        }


def test_ceo_tool_status_treats_ok_false_payload_as_error() -> None:
    assert CeoFrontDoorSupport._tool_status('{"ok": false, "error": "bad args"}') == "error"
    assert CeoFrontDoorSupport._tool_status({"ok": False, "error": "bad args"}) == "error"


class _RuntimeManager:
    def __init__(self, session) -> None:
        self._session = session

    def get_or_create(self, **kwargs):
        _ = kwargs
        return self._session


class _FakeLiveSession:
    def __init__(self) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self._listeners = set()
        self._final_trace = {
            "active_stage_id": "",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_index": 1,
                    "stage_goal": "install the requested skill",
                    "tool_round_budget": 5,
                    "tool_rounds_used": 1,
                    "status": "completed",
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-1:round-1",
                            "round_index": 1,
                            "tool_names": ["skill-installer"],
                            "tool_call_ids": ["skill-installer:1"],
                            "budget_counted": True,
                            "tools": [
                                {
                                    "tool_call_id": "skill-installer:1",
                                    "tool_name": "skill-installer",
                                    "status": "success",
                                }
                            ],
                        }
                    ],
                }
            ],
        }

    def subscribe(self, listener):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    def state_dict(self) -> dict[str, object]:
        return {"status": self.state.status, "is_running": self.state.is_running}

    def inflight_turn_snapshot(self):
        return None

    def _frontdoor_execution_trace_summary_snapshot(self):
        return dict(self._final_trace)

    async def _emit(self, event_type: str, **payload) -> None:
        event = AgentEvent(type=event_type, timestamp="2026-03-18T12:00:00", payload=payload)
        for listener in list(self._listeners):
            result = listener(event)
            if hasattr(result, "__await__"):
                await result

    async def prompt(self, user_message) -> SimpleNamespace:
        _ = user_message
        self.state.status = "running"
        self.state.is_running = True
        await self._emit("state_snapshot", state=self.state_dict())
        await self._emit("message_end", role="assistant", text="I will keep waiting for the install.")
        self.state.status = "completed"
        self.state.is_running = False
        await self._emit("state_snapshot", state=self.state_dict())
        return SimpleNamespace(output="I will keep waiting for the install.")


class _FakeErrorSession:
    def __init__(self) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self._listeners = set()
        self._snapshot = {
            "status": "error",
            "source": "user",
            "user_message": {"content": "Open bilibili"},
            "last_error": {"message": "CEO frontdoor exceeded maximum iterations"},
        }

    def subscribe(self, listener):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    def state_dict(self) -> dict[str, object]:
        return {"status": self.state.status, "is_running": self.state.is_running}

    def inflight_turn_snapshot(self):
        return copy.deepcopy(self._snapshot)

    async def _emit(self, event_type: str, **payload) -> None:
        event = AgentEvent(type=event_type, timestamp="2026-03-18T12:00:00", payload=payload)
        for listener in list(self._listeners):
            result = listener(event)
            if hasattr(result, "__await__"):
                await result

    async def prompt(self, user_message) -> SimpleNamespace:
        _ = user_message
        self.state.status = "running"
        self.state.is_running = True
        await self._emit("state_snapshot", state=self.state_dict())
        self.state.status = "error"
        self.state.is_running = False
        raise RuntimeError("CEO frontdoor exceeded maximum iterations")


class _FakeHeartbeatSession:
    def __init__(
        self,
        *,
        output: str = HEARTBEAT_OK,
        outputs: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompts: list[UserInputMessage] = []
        self.persist_transcript_flags: list[bool] = []
        self._listeners = set()
        self._outputs = [str(item or "") for item in list(outputs or [output])]
        self.turn_id = "turn-heartbeat-default"

    def subscribe(self, listener):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    async def _emit(self, event_type: str, **payload) -> None:
        event = AgentEvent(type=event_type, timestamp="2026-03-18T12:00:00", payload=payload)
        for listener in list(self._listeners):
            result = listener(event)
            if hasattr(result, "__await__"):
                await result

    async def prompt(self, user_message, persist_transcript: bool = False) -> SimpleNamespace:
        self.persist_transcript_flags.append(bool(persist_transcript))
        self.prompts.append(user_message)
        output = self._outputs.pop(0) if self._outputs else ""
        heartbeat_reason = str((getattr(user_message, "metadata", None) or {}).get("heartbeat_reason") or "").strip()
        if output:
            await self._emit(
                "message_end",
                role="assistant",
                text=str(output),
                source="heartbeat",
                heartbeat_internal=True,
                heartbeat_reason=heartbeat_reason,
                turn_id=self.turn_id,
            )
        return SimpleNamespace(output=output)


class _FakeHeartbeatFinalSession(_FakeHeartbeatSession):
    def __init__(self, *, output: str = "Background task finished.") -> None:
        super().__init__(output=output)
        self.turn_id = "turn-heartbeat-final"
        self._preserved_snapshot: dict[str, object] | None = {
            "turn_id": "turn-user-preserved",
            "status": "paused",
            "user_message": {"content": "Install the skill"},
            "assistant_text": "Still working on it...",
            "execution_trace_summary": {
                "active_stage_id": "inflight-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "inflight-stage-1",
                        "stage_index": 1,
                        "stage_goal": "",
                        "tool_round_budget": 0,
                        "tool_rounds_used": 1,
                        "status": "active",
                        "mode": "自主执行",
                        "stage_kind": "normal",
                        "system_generated": True,
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "archive_ref": "",
                        "archive_stage_index_start": 0,
                        "archive_stage_index_end": 0,
                        "rounds": [
                            {
                                "round_index": 1,
                                "tools": [
                                    {
                                        "status": "running",
                                        "tool_name": "skill-installer",
                                        "text": "install still running",
                                        "tool_call_id": "skill-installer:1",
                                    }
                                ],
                            }
                        ],
                        "created_at": "",
                        "finished_at": "",
                    }
                ],
            },
        }
        self.clear_calls = 0

    def inflight_turn_snapshot(self):
        return copy.deepcopy(self._preserved_snapshot)

    def clear_preserved_inflight_turn(self) -> None:
        self.clear_calls += 1
        self._preserved_snapshot = None


class _PersistingHeartbeatSession(_FakeHeartbeatSession):
    def __init__(
        self,
        *,
        session_manager: SessionManager,
        session_id: str,
        output: str = "Background install finished successfully.",
    ) -> None:
        super().__init__(output=output)
        self._session_manager = session_manager
        self._session_id = session_id

    async def prompt(self, user_message, persist_transcript: bool = False) -> SimpleNamespace:
        result = await super().prompt(user_message, persist_transcript=persist_transcript)
        if persist_transcript:
            persisted = self._session_manager.get_or_create(self._session_id)
            metadata = dict(getattr(user_message, "metadata", None) or {})
            turn_id = self.turn_id
            base_metadata = {
                "_transcript_turn_id": turn_id,
                "_transcript_state": "completed",
            }
            stable_rules_text = str(metadata.get("heartbeat_stable_rules_text") or "").strip()
            event_bundle_text = str(metadata.get("heartbeat_event_bundle_text") or user_message.content or "").strip()
            if stable_rules_text:
                persisted.add_message(
                    "system",
                    stable_rules_text,
                    metadata={
                        **base_metadata,
                        "source": "heartbeat",
                        "prompt_visible": True,
                        "ui_visible": False,
                        "internal_prompt_kind": "heartbeat_rule",
                    },
                )
            if event_bundle_text:
                persisted.add_message(
                    "user",
                    event_bundle_text,
                    metadata={
                        **base_metadata,
                        "source": "heartbeat",
                        "heartbeat_internal": True,
                        "prompt_visible": True,
                        "ui_visible": False,
                        "internal_prompt_kind": "heartbeat_event_bundle",
                    },
                )
            output_text = str(getattr(result, "output", "") or "").strip()
            if output_text and output_text != HEARTBEAT_OK:
                persisted.add_message(
                    "assistant",
                    output_text,
                    turn_id=turn_id,
                    metadata={
                        "source": "heartbeat",
                        "prompt_visible": True,
                        "ui_visible": True,
                    },
                )
            self._session_manager.save(persisted)
        return result


class _HeartbeatRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def enqueue_tool_background(self, *, session_id: str, payload: dict[str, object]) -> None:
        self.calls.append((session_id, dict(payload)))


class _HeartbeatController:
    def __init__(self) -> None:
        self.clear_calls: list[str] = []

    def clear_session(self, session_id: str) -> None:
        self.clear_calls.append(str(session_id or ""))


class _HeartbeatReplayRecorder:
    def __init__(self) -> None:
        self.replay_calls: list[str] = []

    def replay_pending_outbox(self, *, session_id: str | None = None, limit: int = 500) -> dict[str, int]:
        _ = limit
        self.replay_calls.append(str(session_id or ""))
        return {"task_terminal": 0, "task_stall": 0}


class _FakeToolExecutionManager:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self._results = [dict(item) for item in results]
        self.calls: list[tuple[str, float]] = []

    async def wait_execution(self, execution_id: str, *, wait_seconds: float = 20.0, **kwargs) -> dict[str, object]:
        _ = kwargs
        self.calls.append((str(execution_id or ""), float(wait_seconds)))
        if self._results:
            return self._results.pop(0)
        return {
            "status": "background_running",
            "execution_id": str(execution_id or ""),
            "tool_name": "skill-installer",
            "elapsed_seconds": 0.0,
            "recommended_wait_seconds": 0.05,
            "runtime_snapshot": {"summary_text": "still running"},
        }


def _sample_frontdoor_stage_state() -> dict[str, object]:
    return {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_goal": "inspect repository",
                "tool_round_budget": 3,
                "tool_rounds_used": 1,
                "status": "active",
                "mode": "自主执行",
                "stage_kind": "normal",
                "system_generated": False,
                "completed_stage_summary": "",
                "key_refs": [],
                "archive_ref": "",
                "archive_stage_index_start": 0,
                "archive_stage_index_end": 0,
                "rounds": [
                    {
                        "round_index": 1,
                        "tool_names": ["filesystem"],
                        "tool_calls": [{"name": "filesystem", "arguments": {"path": "."}}],
                    }
                ],
                "created_at": "2026-04-01T10:00:00Z",
                "finished_at": "",
            }
        ],
    }


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")
    return app


def _recv_until(ws, predicate, *, limit: int = 20):
    seen: list[dict[str, object]] = []
    for _ in range(limit):
        payload = ws.receive_json()
        seen.append(payload)
        if predicate(payload):
            return payload, seen
    raise AssertionError(f"Did not receive expected websocket payload. Seen: {seen!r}")


@pytest.fixture(autouse=True)
def _unlock_websocket_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        websocket_ceo,
        "get_bootstrap_security_service",
        lambda: SimpleNamespace(is_unlocked=lambda: True),
    )


def _mock_workspace(monkeypatch, workspace: Path) -> None:
    monkeypatch.setattr(websocket_ceo, "workspace_path", lambda: workspace)
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: workspace)
    monkeypatch.setattr(
        web_ceo_sessions,
        "load_config",
        lambda: SimpleNamespace(
            china_bridge=SimpleNamespace(
                channels=SimpleNamespace(
                    qqbot=SimpleNamespace(enabled=False, accounts={}),
                    dingtalk=SimpleNamespace(enabled=False, accounts={}),
                    wecom=SimpleNamespace(enabled=False, accounts={}),
                    wecom_app=SimpleNamespace(enabled=False, accounts={}),
                    feishu_china=SimpleNamespace(enabled=False, accounts={}),
                )
            )
        ),
    )


@pytest.mark.asyncio
async def test_runtime_agent_session_keeps_background_running_tool_result_as_update() -> None:
    heartbeat = _HeartbeatRecorder()
    loop = SimpleNamespace(model="gpt-test", reasoning_effort=None, web_session_heartbeat=heartbeat)
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    await session._handle_progress(
        "skill-installer started",
        event_kind="tool_start",
        event_data={"tool_name": "skill-installer"},
    )
    await session._handle_progress(
        json.dumps({"status": "background_running", "execution_id": "tool-exec:1"}, ensure_ascii=False),
        event_kind="tool_result",
        event_data={"tool_name": "skill-installer"},
    )

    tool_events = [event for event in events if event.type.startswith("tool_execution")]
    assert [event.type for event in tool_events] == ["tool_execution_start", "tool_execution_update"]
    assert tool_events[-1].payload["kind"] == "tool_background"
    assert session.state.pending_tool_calls == {"skill-installer:1"}
    assert heartbeat.calls == [
        (
            "web:shared",
            {"status": "background_running", "execution_id": "tool-exec:1", "tool_name": "skill-installer"},
        )
    ]


@pytest.mark.asyncio
async def test_runtime_agent_session_merges_wait_tool_execution_back_into_original_tool_step() -> None:
    heartbeat = _HeartbeatRecorder()
    loop = SimpleNamespace(model="gpt-test", reasoning_effort=None, web_session_heartbeat=heartbeat)
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    await session._handle_progress(
        "skill-installer started",
        event_kind="tool_start",
        event_data={"tool_name": "skill-installer"},
    )
    await session._handle_progress(
        json.dumps(
            {
                "status": "background_running",
                "execution_id": "tool-exec:1",
                "tool_name": "skill-installer",
                "recommended_wait_seconds": 60,
            },
            ensure_ascii=False,
        ),
        event_kind="tool_result",
        event_data={"tool_name": "skill-installer"},
    )
    await session._handle_progress(
        "wait_tool_execution started",
        event_kind="tool_start",
        event_data={"tool_name": "wait_tool_execution"},
    )
    await session._handle_progress(
        json.dumps(
            {
                "status": "background_running",
                "execution_id": "tool-exec:1",
                "tool_name": "skill-installer",
                "recommended_wait_seconds": 240,
                "runtime_snapshot": {"summary_text": "still fetching remote repository"},
            },
            ensure_ascii=False,
        ),
        event_kind="tool_result",
        event_data={"tool_name": "wait_tool_execution"},
    )

    tool_events = [event for event in events if event.type.startswith("tool_execution")]
    assert [event.type for event in tool_events] == [
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_update",
    ]
    assert [event.payload["tool_name"] for event in tool_events] == [
        "skill-installer",
        "skill-installer",
        "skill-installer",
    ]
    assert [event.payload["tool_call_id"] for event in tool_events] == [
        "skill-installer:1",
        "skill-installer:1",
        "skill-installer:1",
    ]
    assert json.loads(tool_events[-1].payload["text"])["recommended_wait_seconds"] == 240


@pytest.mark.asyncio
async def test_runtime_agent_session_parallel_same_name_tools_distinct_call_ids() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "stage_goal": "fetch references",
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
                "status": "active",
                "rounds": [
                    {
                        "round_index": 1,
                        "tool_call_ids": ["web_fetch-call-1", "web_fetch-call-2"],
                        "tool_names": ["web_fetch", "web_fetch"],
                    }
                ],
            }
        ],
    }

    await session._handle_progress(
        "first fetch started",
        event_kind="tool_start",
        event_data={"tool_name": "web_fetch", "tool_call_id": "web_fetch-call-1"},
    )
    await session._handle_progress(
        "second fetch started",
        event_kind="tool_start",
        event_data={"tool_name": "web_fetch", "tool_call_id": "web_fetch-call-2"},
    )
    await session._handle_progress(
        '{"summary":"first done"}',
        event_kind="tool_result",
        event_data={"tool_name": "web_fetch", "tool_call_id": "web_fetch-call-1"},
    )
    await session._handle_progress(
        '{"summary":"second done"}',
        event_kind="tool_result",
        event_data={"tool_name": "web_fetch", "tool_call_id": "web_fetch-call-2"},
    )

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    round_item = snapshot["execution_trace_summary"]["stages"][0]["rounds"][0]
    assert round_item["tool_call_ids"] == ["web_fetch-call-1", "web_fetch-call-2"]
    assert round_item["tool_names"] == ["web_fetch", "web_fetch"]
    assert round_item["tools"] == []
    tool_end_events = [event for event in session._event_log if event["type"] == "tool_execution_end"]
    assert [event["payload"]["tool_call_id"] for event in tool_end_events] == [
        "web_fetch-call-1",
        "web_fetch-call-2",
    ]
    assert session.state.pending_tool_calls == set()


@pytest.mark.asyncio
async def test_runtime_agent_session_progress_resolution_precedence_prefers_tool_call_id_over_conflicting_name() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    await session._handle_progress(
        "install started",
        event_kind="tool_start",
        event_data={"tool_name": "skill-installer", "tool_call_id": "skill-installer:1"},
    )
    await session._handle_progress(
        "fetch started",
        event_kind="tool_start",
        event_data={"tool_name": "web_fetch", "tool_call_id": "web_fetch:1"},
    )

    await session._handle_progress(
        "install still running",
        event_kind="tool",
        event_data={"tool_name": "web_fetch", "tool_call_id": "skill-installer:1"},
    )

    tool_events = [event for event in events if event.type.startswith("tool_execution")]
    assert [event.type for event in tool_events] == [
        "tool_execution_start",
        "tool_execution_start",
        "tool_execution_update",
    ]
    assert tool_events[-1].payload["tool_name"] == "skill-installer"
    assert tool_events[-1].payload["tool_call_id"] == "skill-installer:1"
    assert session._resolve_progress_tool_target({"tool_name": "web_fetch", "tool_call_id": "skill-installer:1"}) == (
        "skill-installer",
        "skill-installer:1",
    )
    assert session._resolve_progress_tool_target({"tool_name": "web_fetch"}) == ("web_fetch", "web_fetch:1")


@pytest.mark.asyncio
async def test_runtime_agent_session_cancel_clears_pending_and_background_tool_indexes() -> None:
    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    session = RuntimeAgentSession(
        SimpleNamespace(
            model="gpt-test",
            reasoning_effort=None,
            cancel_session_tasks=_cancel_session_tasks,
        ),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"

    await session._handle_progress(
        "first fetch started",
        event_kind="tool_start",
        event_data={"tool_name": "web_fetch", "tool_call_id": "web_fetch-call-1"},
    )
    await session._handle_progress(
        "second fetch started",
        event_kind="tool_start",
        event_data={"tool_name": "web_fetch", "tool_call_id": "web_fetch-call-2"},
    )

    assert session.state.pending_tool_calls == {"web_fetch-call-1", "web_fetch-call-2"}
    assert session._pending_tool_call_names == {
        "web_fetch-call-1": "web_fetch",
        "web_fetch-call-2": "web_fetch",
    }
    assert list(session._pending_tool_name_calls["web_fetch"]) == [
        "web_fetch-call-1",
        "web_fetch-call-2",
    ]
    session._remember_background_tool_target(
        execution_id="tool-exec:2",
        tool_name="web_fetch",
        tool_call_id="web_fetch-call-2",
    )

    resolved_tool_name, resolved_call_id, resolved_execution_id = session._resolve_control_tool_target(
        tool_name="wait_tool_execution",
        payload={"execution_id": "tool-exec:2"},
    )

    assert (resolved_tool_name, resolved_call_id, resolved_execution_id) == (
        "web_fetch",
        "web_fetch-call-2",
        "tool-exec:2",
    )

    await session.cancel()

    assert session.state.pending_tool_calls == set()
    assert session._pending_tool_call_names == {}
    assert session._pending_tool_name_calls == {}
    assert session._background_tool_targets == {}
    assert session._resolve_control_tool_target(
        tool_name="wait_tool_execution",
        payload={"execution_id": "tool-exec:2"},
    ) == ("wait_tool_execution", "", "tool-exec:2")


@pytest.mark.asyncio
async def test_runtime_agent_session_analysis_progress_updates_inflight_snapshot() -> None:
    loop = SimpleNamespace(model="gpt-test", reasoning_effort=None)
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session._state.is_running = True
    session._state.status = "running"
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    await session._handle_progress(
        "正在请求 CEO 模型生成下一步响应...",
        event_kind="analysis",
    )

    snapshot = session.inflight_turn_snapshot()
    assert snapshot is not None
    assert snapshot["assistant_text"] == "正在请求 CEO 模型生成下一步响应..."
    assert any(event.type == "state_snapshot" for event in events)


@pytest.mark.asyncio
async def test_runtime_agent_session_emits_lightweight_assistant_stream_events_without_state_snapshot() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._active_turn_id = "turn-stream-1"
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    await session._handle_assistant_text_delta("O")
    await session._handle_assistant_text_delta("K")

    assert session.state.latest_message == "OK"
    stream_events = [event for event in events if event.type == "assistant_stream_delta"]
    assert stream_events
    assert stream_events[-1].payload["turn_id"] == "turn-stream-1"
    assert stream_events[-1].payload["text"] == "OK"
    assert not any(event.type == "state_snapshot" for event in events)


@pytest.mark.asyncio
async def test_runtime_agent_session_marks_heartbeat_message_end(tmp_path: Path, monkeypatch) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            return HEARTBEAT_OK

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    await session.prompt(
        UserInputMessage(
            content="heartbeat",
            metadata={"heartbeat_internal": True, "heartbeat_reason": "tool_background"},
        ),
        persist_transcript=False,
    )

    message_end = next(event for event in events if event.type == "message_end")
    assert message_end.payload["text"] == HEARTBEAT_OK
    assert message_end.payload["heartbeat_internal"] is True


@pytest.mark.asyncio
async def test_runtime_agent_session_converts_frontdoor_interrupt_into_paused_state(tmp_path, monkeypatch) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _InterruptingRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            raise CeoFrontdoorInterrupted(
                interrupts=[
                    CeoPendingInterrupt(
                        interrupt_id="interrupt-1",
                        value={"kind": "frontdoor_tool_approval", "tool_calls": [{"name": "create_async_task"}]},
                    )
                ],
                values={
                    "tool_call_payloads": [{"name": "create_async_task"}],
                    "frontdoor_stage_state": {
                        "active_stage_id": "frontdoor-stage-1",
                        "transition_required": False,
                        "stages": [
                            {
                                "stage_id": "frontdoor-stage-1",
                                "stage_index": 1,
                                "stage_goal": "inspect repository",
                                "status": "active",
                                "rounds": [{"round_index": 1, "tools": [{"tool_name": "filesystem"}]}],
                            }
                        ],
                    },
                    "compression_state": {"status": "running", "text": "上下文压缩中", "source": "user"},
                },
            )

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_InterruptingRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")

    result = await session.prompt("create a task")

    assert result.output == ""
    assert session.state.status == "paused"
    assert session.state.paused is True
    assert session.state.pending_interrupts == [
        {
            "id": "interrupt-1",
            "value": {"kind": "frontdoor_tool_approval", "tool_calls": [{"name": "create_async_task"}]},
        }
    ]
    paused = session.paused_execution_context_snapshot()
    assert paused["interrupts"][0]["id"] == "interrupt-1"
    assert paused["execution_trace_summary"]["stages"][0]["stage_goal"] == "inspect repository"
    assert paused["compression"]["status"] == "running"
    inflight = session.inflight_turn_snapshot()
    assert inflight is not None
    assert inflight["execution_trace_summary"]["active_stage_id"] == "frontdoor-stage-1"
    assert inflight["compression"]["text"] == "上下文压缩中"


@pytest.mark.asyncio
async def test_runtime_agent_session_preserves_previewed_round_through_real_middleware_order_interrupt_pause(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
    from g3ku.runtime.frontdoor.prompt_cache_contract import FrontdoorPromptContract
    from g3ku.runtime import web_ceo_sessions

    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _BackendRecorder:
        def __init__(self, responses: list[LLMResponse]) -> None:
            self.responses = list(responses)

        async def chat(self, **kwargs):
            _ = kwargs
            return self.responses.pop(0)

    class _FakeToolRegistry:
        def __init__(self, tools: list[Tool]) -> None:
            self._tools = {tool.name: tool for tool in list(tools)}
            self.tool_names = sorted(self._tools)

        def get(self, name: str):
            return self._tools.get(str(name or "").strip())

        def push_runtime_context(self, context: dict[str, object]):
            _ = context
            return object()

        def pop_runtime_context(self, token) -> None:
            _ = token

    class _ContinuationTaskTool(Tool):
        @property
        def name(self) -> str:
            return "create_async_task"

        @property
        def description(self) -> str:
            return "dispatch async task"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "core_requirement": {"type": "string"},
                    "execution_policy": {"type": "object"},
                },
                "required": ["task", "core_requirement", "execution_policy"],
            }

        async def execute(
            self,
            task: str,
            core_requirement: str,
            execution_policy: dict[str, object],
            **kwargs,
        ) -> str:
            _ = task, core_requirement, execution_policy, kwargs
            return json.dumps({"ok": True}, ensure_ascii=False)

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    async def _startup() -> None:
        return None

    backend = _BackendRecorder(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-stage-1",
                        name="submit_next_stage",
                        arguments={
                            "stage_goal": "Inspect the repository structure",
                            "tool_round_budget": 2,
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-task-1",
                        name="create_async_task",
                        arguments={
                            "task": "Inspect the repository structure",
                            "core_requirement": "Inspect the repository structure",
                            "execution_policy": {"mode": "focus"},
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        _ensure_checkpointer_ready=lambda: None,
        _checkpointer=InMemorySaver(),
        _store=None,
        main_task_service=SimpleNamespace(startup=_startup),
        tools=_FakeToolRegistry([_ContinuationTaskTool()]),
        max_iterations=8,
        resource_manager=None,
        tool_execution_manager=None,
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                frontdoor_interrupt_approval_enabled=True,
                frontdoor_interrupt_tool_names=["create_async_task"],
            )
        ),
    )
    runner = CeoFrontDoorRunner(loop=loop)
    loop.multi_agent_runner = runner

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["create_async_task"]}

    async def _build_for_ceo(**kwargs):
        seed_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = kwargs.get("user_content")
        return SimpleNamespace(
            system_prompt="SYSTEM PROMPT",
            recent_history=[],
            tool_names=["create_async_task"],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["create_async_task"],
                    "visible_skill_ids": [],
                },
            },
            model_messages=[*seed_messages, {"role": "user", "content": user_content}],
            stable_messages=[*seed_messages, {"role": "user", "content": user_content}],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    def _fake_build_frontdoor_prompt_contract(**kwargs):
        return FrontdoorPromptContract(
            request_messages=list(kwargs.get("stable_messages") or []),
            prompt_cache_key="frontdoor-cache-key",
            diagnostics={"stable_prompt_signature": "frontdoor-sig"},
            stable_prefix_hash="stable-hash",
            dynamic_appendix_hash="dynamic-hash",
            stable_messages=list(kwargs.get("stable_messages") or []),
            dynamic_appendix_messages=list(kwargs.get("dynamic_appendix_messages") or []),
            diagnostic_dynamic_messages=[],
            cache_family_revision="frontdoor:v1",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: backend)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai:gpt-4.1"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda model_refs: {
            "model_key": "openai:gpt-4.1",
            "provider_model": "openai:gpt-4.1",
            "context_window_tokens": 128000,
        },
    )
    monkeypatch.setattr(ceo_runtime_ops, "build_frontdoor_prompt_contract", _fake_build_frontdoor_prompt_contract)

    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")

    result = await session.prompt("create a task")

    assert result.output == ""
    assert session.state.status == "paused"
    assert len(session.state.pending_interrupts) == 1
    pending_interrupt = session.state.pending_interrupts[0]
    assert str(pending_interrupt["id"]).strip()
    assert pending_interrupt["value"]["kind"] == "frontdoor_tool_approval_batch"
    assert str(pending_interrupt["value"]["batch_id"]).startswith("batch:")
    assert pending_interrupt["value"]["tool_calls"] == [
        {
            "id": "call-task-1",
            "name": "create_async_task",
            "arguments": {
                "task": "Inspect the repository structure",
                "core_requirement": "Inspect the repository structure",
                "execution_policy": {"mode": "focus"},
            },
        }
    ]
    paused = session.paused_execution_context_snapshot()
    assert paused["interrupts"][0]["id"] == pending_interrupt["id"]
    assert paused["execution_trace_summary"]["active_stage_id"] == "frontdoor-stage-1"
    paused_stage = paused["execution_trace_summary"]["stages"][0]
    assert paused_stage["stage_goal"] == "Inspect the repository structure"
    assert paused_stage["tool_round_budget"] == 2
    assert [round_item["tool_names"] for round_item in paused_stage["rounds"]] == [["create_async_task"]]
    assert "tool_events" not in paused
    assert paused["compression"] == {}
    inflight = session.inflight_turn_snapshot()
    assert inflight is not None
    assert inflight["execution_trace_summary"]["active_stage_id"] == "frontdoor-stage-1"
    assert [round_item["tool_names"] for round_item in inflight["execution_trace_summary"]["stages"][0]["rounds"]] == [
        ["create_async_task"]
    ]
    assert inflight["compression"] == {}


@pytest.mark.asyncio
async def test_runtime_agent_session_preserves_pre_synced_frontdoor_state_when_interrupt_values_are_sparse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _SparseInterruptRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, on_progress
            session._frontdoor_stage_state = _sample_frontdoor_stage_state()
            session._compression_state = {"status": "running", "text": "compressing", "source": "user"}
            raise CeoFrontdoorInterrupted(
                interrupts=[
                    CeoPendingInterrupt(
                        interrupt_id="interrupt-sparse-1",
                        value={"kind": "frontdoor_tool_approval"},
                    )
                ],
                values={"approval_request": {"kind": "frontdoor_tool_approval"}},
            )

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_SparseInterruptRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")

    result = await session.prompt("create a task")

    assert result.output == ""
    assert session.state.status == "paused"
    paused = session.paused_execution_context_snapshot()
    assert paused is not None
    assert paused["execution_trace_summary"]["active_stage_id"] == "frontdoor-stage-1"
    assert paused["execution_trace_summary"]["stages"][0]["stage_goal"] == "inspect repository"
    assert paused["compression"]["status"] == "running"
    inflight = session.inflight_turn_snapshot()
    assert inflight is not None
    assert inflight["execution_trace_summary"]["active_stage_id"] == "frontdoor-stage-1"
    assert inflight["compression"]["text"] == "compressing"


@pytest.mark.asyncio
async def test_runtime_agent_session_resume_frontdoor_interrupt_clears_pending_interrupts(
    tmp_path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _Runner:
        async def resume_turn(self, *, session, resume_value, on_progress):
            _ = session, resume_value, on_progress
            return "approved reply"

    loop = SimpleNamespace(
        multi_agent_runner=_Runner(),
        model="gpt-test",
        reasoning_effort=None,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session.state.pending_interrupts = [{"id": "interrupt-1", "value": {"kind": "frontdoor_tool_approval"}}]
    session.state.paused = True
    session.state.status = "paused"

    result = await session.resume_frontdoor_interrupt(resume_value={"approved": True})

    assert result.output == "approved reply"
    assert session.state.pending_interrupts == []
    assert session.state.status == "completed"


@pytest.mark.asyncio
async def test_runtime_agent_session_resume_frontdoor_interrupt_keeps_repair_required_lists_available_to_runner(
    tmp_path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)
    captured: dict[str, object] = {}

    class _Runner:
        async def resume_turn(self, *, session, resume_value, on_progress):
            _ = resume_value, on_progress
            paused_snapshot = session.paused_execution_context_snapshot()
            captured["paused_snapshot"] = copy.deepcopy(paused_snapshot)
            return "approved reply"

    loop = SimpleNamespace(
        multi_agent_runner=_Runner(),
        model="gpt-test",
        reasoning_effort=None,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session.state.pending_interrupts = [{"id": "interrupt-1", "value": {"kind": "frontdoor_tool_approval"}}]
    session.state.paused = True
    session.state.status = "paused"
    session._set_paused_execution_context(
        {
            "status": "paused",
            "interrupts": [{"id": "interrupt-1", "value": {"kind": "frontdoor_tool_approval"}}],
            "repair_required_tool_items": [
                {
                    "tool_id": "agent_browser",
                    "description": "Browser automation",
                    "reason": "missing required paths",
                }
            ],
            "repair_required_skill_items": [
                {
                    "skill_id": "writing-skills",
                    "description": "Skill maintenance workflow",
                    "reason": "missing required bins",
                }
            ],
        }
    )

    result = await session.resume_frontdoor_interrupt(resume_value={"approved": True})

    assert result.output == "approved reply"
    paused_snapshot = dict(captured["paused_snapshot"] or {})
    assert paused_snapshot["repair_required_tool_items"] == [
        {
            "tool_id": "agent_browser",
            "description": "Browser automation",
            "reason": "missing required paths",
        }
    ]
    assert paused_snapshot["repair_required_skill_items"] == [
        {
            "skill_id": "writing-skills",
            "description": "Skill maintenance workflow",
            "reason": "missing required bins",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_agent_session_resume_frontdoor_interrupt_reenters_paused_state_on_interrupt(
    tmp_path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _Runner:
        async def resume_turn(self, *, session, resume_value, on_progress):
            _ = session, resume_value, on_progress
            raise CeoFrontdoorInterrupted(
                interrupts=[
                    CeoPendingInterrupt(
                        interrupt_id="interrupt-2",
                        value={"kind": "frontdoor_tool_approval", "tool_calls": [{"name": "create_async_task"}]},
                    )
                ],
                values={"tool_call_payloads": [{"name": "create_async_task"}]},
            )

    loop = SimpleNamespace(
        multi_agent_runner=_Runner(),
        model="gpt-test",
        reasoning_effort=None,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session.state.pending_interrupts = [{"id": "interrupt-1", "value": {"kind": "frontdoor_tool_approval"}}]
    session.state.paused = True
    session.state.status = "paused"

    result = await session.resume_frontdoor_interrupt(resume_value={"approved": True})

    assert result.output == ""
    assert session.state.status == "paused"
    assert session.state.paused is True
    assert session.state.pending_interrupts == [
        {
            "id": "interrupt-2",
            "value": {"kind": "frontdoor_tool_approval", "tool_calls": [{"name": "create_async_task"}]},
        }
    ]


@pytest.mark.asyncio
async def test_inflight_snapshot_preserves_paused_user_turn_across_heartbeat_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = session, on_progress
            metadata = dict(getattr(user_input, "metadata", None) or {})
            if bool(metadata.get("heartbeat_internal")):
                return HEARTBEAT_OK
            return "normal reply"

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session._last_prompt = UserInputMessage(content="Install the weather skill")
    session._event_log = [
        {
            "type": "tool_execution_start",
            "timestamp": "2026-03-18T12:00:00",
            "payload": {
                "tool_name": "skill-installer",
                "text": "skill-installer started",
                "tool_call_id": "skill-installer:1",
            },
        }
    ]
    session._state.paused = True
    session._state.is_running = False
    session._state.status = "paused"
    session._state.latest_message = "Still installing dependencies..."

    before = session.inflight_turn_snapshot()

    assert before is not None
    assert before["status"] == "paused"
    assert isinstance(before["turn_id"], str) and before["turn_id"]
    assert before["user_message"]["content"] == "Install the weather skill"

    await session.prompt(
        UserInputMessage(
            content="heartbeat",
            metadata={"heartbeat_internal": True, "heartbeat_reason": "tool_background"},
        ),
        persist_transcript=False,
    )

    snapshot = session.inflight_turn_snapshot()

    assert session.state.status == "completed"
    assert snapshot is not None
    assert snapshot["status"] == "paused"
    assert snapshot["turn_id"] == before["turn_id"]
    assert snapshot["user_message"]["content"] == "Install the weather skill"
    assert snapshot["assistant_text"] == "Still installing dependencies..."
    assert "tool_events" not in snapshot


def test_runtime_agent_session_separates_current_heartbeat_snapshot_from_preserved_visible_turn() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._state.latest_message = "heartbeat is processing"
    session._last_prompt = UserInputMessage(
        content="heartbeat",
        metadata={"heartbeat_internal": True, "heartbeat_reason": "tool_background"},
    )
    session._preserved_inflight_turn = {
        "source": "user",
        "turn_id": "turn-user-preserved",
        "status": "running",
        "user_message": {"content": "Install the skill"},
        "assistant_text": "Still working on the previous turn",
        "execution_trace_summary": {
            "stages": [
                {
                    "stage_id": "frontdoor-stage-user",
                    "stage_goal": "install skill",
                    "rounds": [],
                }
            ]
        },
    }

    current_snapshot = session.inflight_turn_snapshot()
    preserved_snapshot = session.preserved_inflight_turn_snapshot()

    assert current_snapshot is not None
    assert current_snapshot["source"] == "heartbeat"
    assert current_snapshot["status"] == "running"
    assert current_snapshot["assistant_text"] == "heartbeat is processing"
    assert current_snapshot.get("turn_id") != "turn-user-preserved"
    assert preserved_snapshot is not None
    assert preserved_snapshot["source"] == "user"
    assert preserved_snapshot["turn_id"] == "turn-user-preserved"
    assert preserved_snapshot["execution_trace_summary"]["stages"][0]["stage_id"] == "frontdoor-stage-user"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_input", "expected_source"),
    [
        pytest.param("new prompt", None, id="user-turn"),
        pytest.param(
            UserInputMessage(
                content="heartbeat prompt",
                metadata={"heartbeat_internal": True, "heartbeat_reason": "tool_background"},
            ),
            "heartbeat",
            id="heartbeat-turn",
        ),
        pytest.param(
            UserInputMessage(
                content="cron prompt",
                metadata={"cron_internal": True, "cron_job_id": "job-77"},
            ),
            "cron",
            id="cron-turn",
        ),
    ],
)
async def test_turn_start_clears_stale_frontdoor_stage_and_compression_before_first_running_snapshot(
    tmp_path: Path,
    monkeypatch,
    prompt_input: str | UserInputMessage,
    expected_source: str | None,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            return "ok"

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session._frontdoor_stage_state = _sample_frontdoor_stage_state()
    session._compression_state = {"status": "running", "text": "旧压缩状态", "source": "user"}
    session._frontdoor_selection_debug = {"selected_tool_names": ["filesystem_write"]}
    captured: dict[str, object] = {}

    async def _listener(event: AgentEvent) -> None:
        if event.type != "state_snapshot":
            return
        state = event.payload.get("state") if isinstance(event.payload, dict) else {}
        if str((state or {}).get("status") or "") != "running":
            return
        if "snapshot" not in captured:
            captured["snapshot"] = session.inflight_turn_snapshot()

    session.subscribe(_listener)
    await session.prompt(prompt_input)

    snapshot = captured.get("snapshot")
    assert isinstance(snapshot, dict)
    assert snapshot["execution_trace_summary"]["active_stage_id"] == "frontdoor-stage-1"
    assert snapshot["compression"]["text"] == "旧压缩状态"
    assert "frontdoor_selection_debug" not in snapshot
    if expected_source is None:
        assert "source" not in snapshot
    else:
        assert snapshot.get("source") == expected_source


@pytest.mark.asyncio
async def test_runtime_agent_session_persists_hidden_cron_prompt_messages_and_visible_reply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    captured: dict[str, object] = {}

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input
            await on_progress(
                "cron started",
                event_kind="tool_start",
                event_data={"tool_name": "cron"},
            )
            captured["snapshot"] = session.inflight_turn_snapshot()
            return "Scheduled progress update."

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    await session.prompt(
        UserInputMessage(
            content="Please query task 27255d28379d and report progress.",
            metadata={
                "cron_internal": True,
                "cron_job_id": "job-77",
                "cron_max_runs": 3,
                "cron_delivery_index": 2,
                "cron_delivered_runs": 1,
                "cron_reminder_text": "Please query task 27255d28379d and report progress.",
                "cron_scheduled_run_at_ms": 1_777_000_000_000,
                "cron_last_delivered_at_ms": 1_776_999_000_000,
            },
        )
    )

    inflight = captured["snapshot"]
    assert isinstance(inflight, dict)
    assert inflight["source"] == "cron"
    assert "user_message" not in inflight
    assert inflight["execution_trace_summary"] == {}

    persisted = loop.sessions.get_or_create("web:shared")
    assert [message["role"] for message in persisted.messages] == ["system", "system", "assistant"]
    assert persisted.messages[0]["content"].startswith("你接收到了之前你定时的任务，如下：")
    assert persisted.messages[0]["metadata"]["source"] == "cron"
    assert persisted.messages[0]["metadata"]["cron_job_id"] == "job-77"
    assert persisted.messages[0]["metadata"]["prompt_visible"] is True
    assert persisted.messages[0]["metadata"]["ui_visible"] is False
    assert persisted.messages[0]["metadata"]["internal_prompt_kind"] == "cron_rule"
    assert persisted.messages[1]["content"].startswith("[CRON INTERNAL EVENT]")
    assert persisted.messages[1]["metadata"]["source"] == "cron"
    assert persisted.messages[1]["metadata"]["cron_job_id"] == "job-77"
    assert persisted.messages[1]["metadata"]["prompt_visible"] is True
    assert persisted.messages[1]["metadata"]["ui_visible"] is False
    assert persisted.messages[1]["metadata"]["internal_prompt_kind"] == "cron_event_bundle"
    assert persisted.messages[2]["content"] == "Scheduled progress update."
    assert str(persisted.messages[2]["turn_id"]).strip()
    assert persisted.messages[2]["metadata"]["source"] == "cron"
    assert persisted.messages[2]["metadata"]["cron_job_id"] == "job-77"
    assert persisted.messages[2]["metadata"]["prompt_visible"] is True
    assert persisted.messages[2]["metadata"]["ui_visible"] is True
    assert web_ceo_sessions.transcript_messages(persisted) == [
        {
            "role": "assistant",
            "content": "Scheduled progress update.",
            "timestamp": persisted.messages[2]["timestamp"],
            "turn_id": persisted.messages[2]["turn_id"],
            "metadata": {
                "source": "cron",
                "cron_job_id": "job-77",
                "prompt_visible": True,
                "ui_visible": True,
            },
        }
    ]
    assert [message["role"] for message in web_ceo_sessions.prompt_history_messages(persisted)] == [
        "system",
        "system",
        "assistant",
    ]

    message_end = next(event for event in events if event.type == "message_end")
    assert message_end.payload["source"] == "cron"
    assert message_end.payload["heartbeat_internal"] is False

@pytest.mark.asyncio
async def test_runtime_agent_session_persists_hidden_heartbeat_prompt_messages_and_visible_reply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, on_progress
            session._last_verified_task_ids = ["task:demo-heartbeat"]
            return "Background task completed."

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")

    heartbeat_rules_text = (
        "This is a background heartbeat. Do not explain internal mechanics.\n"
        "If no user-facing update is needed, reply with exactly HEARTBEAT_OK."
    )
    heartbeat_event_bundle = "[SESSION EVENTS]\n## EVENT BUNDLE\n- Task demo completed"

    await session.prompt(
        UserInputMessage(
            content=heartbeat_event_bundle,
            metadata={
                "heartbeat_internal": True,
                "heartbeat_reason": "tool_background",
                "heartbeat_stable_rules_text": heartbeat_rules_text,
                "heartbeat_event_bundle_text": heartbeat_event_bundle,
            },
        )
    )

    persisted = loop.sessions.get_or_create("web:shared")
    assert [message["role"] for message in persisted.messages] == ["system", "user", "assistant"]
    assert persisted.messages[0]["content"] == heartbeat_rules_text
    assert persisted.messages[0]["metadata"]["source"] == "heartbeat"
    assert persisted.messages[0]["metadata"]["prompt_visible"] is True
    assert persisted.messages[0]["metadata"]["ui_visible"] is False
    assert persisted.messages[0]["metadata"]["internal_prompt_kind"] == "heartbeat_rule"
    assert persisted.messages[1]["content"] == heartbeat_event_bundle
    assert persisted.messages[1]["metadata"]["source"] == "heartbeat"
    assert persisted.messages[1]["metadata"]["prompt_visible"] is True
    assert persisted.messages[1]["metadata"]["ui_visible"] is False
    assert persisted.messages[1]["metadata"]["internal_prompt_kind"] == "heartbeat_event_bundle"
    assert persisted.messages[2]["content"] == "Background task completed."
    assert str(persisted.messages[2]["turn_id"]).strip()
    assert persisted.messages[2]["metadata"]["source"] == "heartbeat"
    assert persisted.messages[2]["metadata"]["prompt_visible"] is True
    assert persisted.messages[2]["metadata"]["ui_visible"] is True
    assert persisted.messages[2]["metadata"]["task_ids"] == ["task:demo-heartbeat"]
    assert web_ceo_sessions.transcript_messages(persisted) == [
        {
            "role": "assistant",
            "content": "Background task completed.",
            "timestamp": persisted.messages[2]["timestamp"],
            "turn_id": persisted.messages[2]["turn_id"],
            "metadata": {
                "source": "heartbeat",
                "prompt_visible": True,
                "ui_visible": True,
                "task_ids": ["task:demo-heartbeat"],
            },
        }
    ]
    assert [message["role"] for message in web_ceo_sessions.prompt_history_messages(persisted)] == [
        "system",
        "user",
        "assistant",
    ]
    assert persisted.metadata["last_task_memory"] == {
        "version": web_ceo_sessions.TASK_MEMORY_VERSION,
        "task_ids": ["task:demo-heartbeat"],
        "source": "heartbeat",
        "reason": "",
        "updated_at": persisted.messages[2]["timestamp"],
        "task_results": [],
    }


@pytest.mark.asyncio
async def test_runtime_agent_session_pause_emits_single_pause_ack_and_snapshot(monkeypatch) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    started = asyncio.Event()
    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}

    class _BlockingRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session
            await on_progress(
                "skill-installer started",
                event_kind="tool_start",
                event_data={"tool_name": "skill-installer"},
            )
            started.set()
            await asyncio.Future()

    async def _cancel_session_tasks(_session_key: str) -> int:
        task = turn_task_ref.get("task")
        if task is None:
            return 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return 1

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        multi_agent_runner=_BlockingRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:pause-dedupe", channel="web", chat_id="pause-dedupe")
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    turn_task = asyncio.create_task(
        session.prompt(UserInputMessage(content="Please pause me"), persist_transcript=False)
    )
    turn_task_ref["task"] = turn_task

    await started.wait()
    await session.pause()

    with pytest.raises(asyncio.CancelledError):
        await turn_task

    pause_acks = [
        event for event in events
        if event.type == "control_ack" and str(event.payload.get("action") or "") == "pause"
    ]
    paused_snapshots = [
        event for event in events
        if event.type == "state_snapshot"
        and str((event.payload.get("state") or {}).get("status") or "") == "paused"
    ]

    assert len(pause_acks) == 1
    assert len(paused_snapshots) == 1


@pytest.mark.asyncio
async def test_runtime_agent_session_manual_pause_freezes_heartbeat_and_persists_follow_up(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    started = asyncio.Event()
    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}
    heartbeat = _HeartbeatController()

    class _BlockingRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            started.set()
            await asyncio.Future()

    async def _cancel_session_tasks(_session_key: str) -> int:
        task = turn_task_ref.get("task")
        if task is None:
            return 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return 1

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_BlockingRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        web_session_heartbeat=heartbeat,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key="web:pause-manual", channel="web", chat_id="pause-manual")
    events: list[AgentEvent] = []

    async def _listener(event: AgentEvent) -> None:
        events.append(event)

    session.subscribe(_listener)
    turn_task = asyncio.create_task(session.prompt(UserInputMessage(content="Pause and wait"), persist_transcript=False))
    turn_task_ref["task"] = turn_task

    await started.wait()
    await session.pause(manual=True)

    with pytest.raises(asyncio.CancelledError):
        await turn_task

    pause_ack = next(
        event
        for event in events
        if event.type == "control_ack" and str(event.payload.get("action") or "") == "pause"
    )
    completed_state = next(
        event
        for event in events
        if event.type == "state_snapshot"
        and str((event.payload.get("state") or {}).get("status") or "") == "completed"
    )
    assert pause_ack.payload["source"] == "user"
    assert all(event.type != "message_end" for event in events)
    assert heartbeat.clear_calls == []
    assert session.manual_pause_waiting_reason() is False
    assert str((completed_state.payload.get("state") or {}).get("stop_reason") or "") == "user_pause"
    assert session.inflight_turn_snapshot() is None
    assert web_ceo_sessions.read_inflight_turn_snapshot("web:pause-manual") is None
    assert session.paused_execution_context_snapshot() is None
    assert web_ceo_sessions.read_paused_execution_context("web:pause-manual") is None
    continuity = web_ceo_sessions.read_completed_continuity_snapshot("web:pause-manual")
    assert continuity is not None
    assert continuity["source_reason"] == "manual_stop"

    reloaded = SessionManager(tmp_path).get_or_create("web:pause-manual")
    assert [message["role"] for message in reloaded.messages] == ["user", "assistant"]
    assert reloaded.messages[0]["content"] == "Pause and wait"
    assert reloaded.messages[1]["content"] == "已暂停"
    assert reloaded.messages[1]["status"] == "paused"
    normalized_metadata = web_ceo_sessions.normalize_ceo_metadata(reloaded.metadata, session_key="web:pause-manual")
    assert "manual_pause_waiting_reason" not in normalized_metadata


@pytest.mark.asyncio
async def test_runtime_agent_session_manual_pause_dedupes_pending_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    started = asyncio.Event()
    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}
    heartbeat = _HeartbeatController()

    class _BlockingRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            started.set()
            await asyncio.Future()

    async def _cancel_session_tasks(_session_key: str) -> int:
        task = turn_task_ref.get("task")
        if task is None:
            return 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return 1

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_BlockingRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        web_session_heartbeat=heartbeat,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session_id = "web:pause-manual-dedupe"
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="pause-manual-dedupe")
    turn_task = asyncio.create_task(session.prompt(UserInputMessage(content="Pause without duplicate transcript")))
    turn_task_ref["task"] = turn_task

    await started.wait()
    await session.pause(manual=True)

    with pytest.raises(asyncio.CancelledError):
        await turn_task

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded.messages] == ["user", "assistant"]
    assert reloaded.messages[0]["content"] == "Pause without duplicate transcript"
    assert reloaded.messages[0]["metadata"]["_transcript_state"] == "paused"
    assert reloaded.messages[1]["content"] == "已暂停"
    assert web_ceo_sessions.read_completed_continuity_snapshot(session_id) is not None


@pytest.mark.asyncio
async def test_runtime_agent_session_prompt_batch_after_manual_pause_preserves_paused_request_and_starts_fresh_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    started = asyncio.Event()
    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}
    heartbeat = _HeartbeatController()

    class _BlockingRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            started.set()
            await asyncio.Future()

    captured_inputs: list[str] = []

    class _AnswerRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            captured_inputs.append(session._history_text(user_input.content))
            return "Because this follow-up only needed a direct explanation."

    async def _cancel_session_tasks(_session_key: str) -> int:
        task = turn_task_ref.get("task")
        if task is None:
            return 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return 1

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_BlockingRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        web_session_heartbeat=heartbeat,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session_id = "web:pause-follow-up-turn"
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="pause-follow-up-turn")

    turn_task = asyncio.create_task(session.prompt("Original paused request"))
    turn_task_ref["task"] = turn_task

    await started.wait()
    await session.pause(manual=True)

    with pytest.raises(asyncio.CancelledError):
        await turn_task

    loop.multi_agent_runner = _AnswerRunner()
    result = await session.prompt_batch(
        [
            UserInputMessage(content="Please keep the original request context"),
            UserInputMessage(content="Why no async task?"),
        ]
    )

    assert result.output == "Because this follow-up only needed a direct explanation."
    assert len(captured_inputs) == 1
    assert captured_inputs[0] == "Why no async task?"

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded.messages] == ["user", "assistant", "user", "user", "assistant"]
    assert reloaded.messages[0]["content"] == "Original paused request"
    assert reloaded.messages[0]["metadata"]["_transcript_state"] == "paused"
    assert reloaded.messages[1]["content"] == "已暂停"
    assert reloaded.messages[1]["status"] == "paused"
    assert reloaded.messages[1]["turn_id"] == reloaded.messages[0]["metadata"]["_transcript_turn_id"]
    assert reloaded.messages[1]["metadata"]["history_visible"] is False
    assert reloaded.messages[1]["metadata"]["source"] == "manual_pause_archive"
    assert reloaded.messages[2]["content"] == "Please keep the original request context"
    assert reloaded.messages[3]["content"] == "Why no async task?"
    assert reloaded.messages[2]["metadata"]["_transcript_state"] == "completed"
    assert reloaded.messages[3]["metadata"]["_transcript_state"] == "completed"
    assert reloaded.messages[2]["metadata"]["_transcript_batch_id"] == reloaded.messages[3]["metadata"]["_transcript_batch_id"]
    assert [message["role"] for message in web_ceo_sessions.transcript_messages(reloaded)] == [
        "user",
        "user",
        "user",
        "assistant",
    ]
    assert "补充要求" not in "".join(str(message["content"]) for message in reloaded.messages)
    assert reloaded.messages[4]["content"] == "Because this follow-up only needed a direct explanation."


@pytest.mark.asyncio
async def test_runtime_agent_session_new_user_turn_clears_persisted_manual_pause_and_replays_pending_outbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _AnswerRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            return "Manual pause state was cleared before this turn."

    session_id = "web:pause-cleared-by-user-turn"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    persisted.metadata = web_ceo_sessions.normalize_ceo_metadata(
        {},
        session_key=session_id,
    )
    session_manager.save(persisted)

    heartbeat = _HeartbeatReplayRecorder()
    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=session_manager,
        multi_agent_runner=_AnswerRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        web_session_heartbeat=heartbeat,
        main_task_service=SimpleNamespace(store=SimpleNamespace()),
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=lambda _session_key: 0,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="pause-cleared")

    result = await session.prompt("User resumed the conversation")

    assert result.output == "Manual pause state was cleared before this turn."
    assert heartbeat.replay_calls == []
    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    normalized_metadata = web_ceo_sessions.normalize_ceo_metadata(reloaded.metadata, session_key=session_id)
    assert "manual_pause_waiting_reason" not in normalized_metadata


def test_runtime_agent_session_can_clear_preserved_inflight_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._preserved_inflight_turn = {
        "status": "paused",
        "user_message": {"content": "Install the weather skill"},
    }
    session._sync_persisted_inflight_turn()

    assert session.inflight_turn_snapshot() is not None
    assert web_ceo_sessions.read_inflight_turn_snapshot("web:shared") is not None

    session.clear_preserved_inflight_turn()

    assert session.inflight_turn_snapshot() is None
    assert web_ceo_sessions.read_inflight_turn_snapshot("web:shared") is None


def test_runtime_agent_session_restores_paused_execution_context_from_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    web_ceo_sessions.write_paused_execution_context(
        "web:shared",
        {
            "status": "paused",
            "user_message": {"content": "Resume the browser automation flow"},
            "assistant_text": "I already created task task:resume-1 and was about to query its next node.",
        },
    )
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )

    snapshot = session.paused_execution_context_snapshot()

    assert snapshot is not None
    assert snapshot["status"] == "paused"
    assert snapshot["user_message"]["content"] == "Resume the browser automation flow"


def test_runtime_agent_session_can_clear_paused_execution_context_explicitly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    web_ceo_sessions.write_paused_execution_context(
        "web:shared",
        {
            "status": "paused",
            "user_message": {"content": "Resume the browser automation flow"},
        },
    )
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )

    assert session.paused_execution_context_snapshot() is not None

    session.clear_paused_execution_context()

    assert session.paused_execution_context_snapshot() is None
    assert web_ceo_sessions.read_paused_execution_context("web:shared") is None


def test_web_ceo_continuity_snapshot_round_trip_and_clear(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")

    payload = {
        "frontdoor_request_body_messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "hello"},
        ],
        "frontdoor_history_shrink_reason": "",
        "frontdoor_actual_request_path": str(request_path),
        "frontdoor_actual_request_history": [{"path": str(request_path), "turn_id": "turn-1"}],
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "frontdoor_canonical_context": {"active_stage_id": "", "transition_required": False, "stages": []},
        "compression_state": {"status": "ready", "text": "ok", "source": "semantic"},
        "semantic_context_state": {"summary_text": "summary", "needs_refresh": False},
        "hydrated_tool_names": ["web_fetch"],
        "capability_snapshot_exposure_revision": "exp:demo",
        "visible_tool_ids": ["exec", "web_fetch"],
        "visible_skill_ids": ["web-access"],
        "provider_tool_schema_names": ["exec", "web_fetch"],
        "source_reason": "finalize",
    }

    web_ceo_sessions.write_completed_continuity_snapshot("web:shared", payload)

    restored = web_ceo_sessions.read_completed_continuity_snapshot("web:shared")

    assert isinstance(restored, dict)
    assert restored["frontdoor_request_body_messages"] == payload["frontdoor_request_body_messages"]
    assert restored["provider_tool_schema_names"] == ["exec", "web_fetch"]
    assert restored["source_reason"] == "finalize"

    web_ceo_sessions.clear_completed_continuity_snapshot("web:shared")

    assert web_ceo_sessions.read_completed_continuity_snapshot("web:shared") is None


def test_web_ceo_continuity_snapshot_strips_multimodal_blocks_from_frontdoor_request_body_messages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")

    web_ceo_sessions.write_completed_continuity_snapshot(
        "web:shared",
        {
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Please inspect this image"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                },
            ],
            "frontdoor_history_shrink_reason": "",
            "frontdoor_actual_request_path": str(request_path),
            "frontdoor_actual_request_history": [{"path": str(request_path), "turn_id": "turn-1"}],
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "frontdoor_canonical_context": {"active_stage_id": "", "transition_required": False, "stages": []},
            "compression_state": {},
            "semantic_context_state": {},
            "hydrated_tool_names": [],
            "capability_snapshot_exposure_revision": "exp:demo",
            "visible_tool_ids": ["exec"],
            "visible_skill_ids": [],
            "provider_tool_schema_names": ["exec"],
            "source_reason": "finalize",
        },
    )

    restored = web_ceo_sessions.read_completed_continuity_snapshot("web:shared")

    assert isinstance(restored, dict)
    assert restored["frontdoor_request_body_messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Please inspect this image"},
    ]


def test_completed_continuity_snapshot_round_trips_token_preflight_diagnostics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")

    payload = {
        "frontdoor_request_body_messages": [{"role": "system", "content": "SYSTEM"}],
        "frontdoor_history_shrink_reason": "token_compression",
        "frontdoor_actual_request_path": str(request_path),
        "frontdoor_actual_request_history": [{"path": str(request_path), "turn_id": "turn-1"}],
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "frontdoor_canonical_context": {"active_stage_id": "", "transition_required": False, "stages": []},
        "compression_state": {"status": "ready", "text": "ok", "source": "semantic"},
        "semantic_context_state": {"summary_text": "summary", "needs_refresh": False},
        "frontdoor_token_preflight_diagnostics": {
            "applied": True,
            "final_request_tokens": 28000,
            "trigger_tokens": 20000,
            "estimate_source": "usage_plus_delta",
            "effective_input_tokens": 24000,
        },
        "hydrated_tool_names": ["web_fetch"],
        "capability_snapshot_exposure_revision": "exp:demo",
        "visible_tool_ids": ["exec", "web_fetch"],
        "visible_skill_ids": ["web-access"],
        "provider_tool_schema_names": ["exec", "web_fetch"],
        "source_reason": "finalize",
    }

    web_ceo_sessions.write_completed_continuity_snapshot("web:shared", payload)

    restored = web_ceo_sessions.read_completed_continuity_snapshot("web:shared")

    assert isinstance(restored, dict)
    assert restored["frontdoor_history_shrink_reason"] == "token_compression"
    assert restored["frontdoor_token_preflight_diagnostics"]["final_request_tokens"] == 28000
    assert restored["frontdoor_token_preflight_diagnostics"]["estimate_source"] == "usage_plus_delta"
    assert restored["frontdoor_token_preflight_diagnostics"]["effective_input_tokens"] == 24000


def test_clear_web_ceo_session_artifacts_clears_completed_continuity_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")
    web_ceo_sessions.write_completed_continuity_snapshot(
        "web:shared",
        {
            "frontdoor_request_body_messages": [{"role": "user", "content": "hello"}],
            "frontdoor_history_shrink_reason": "",
            "frontdoor_actual_request_path": str(request_path),
            "frontdoor_actual_request_history": [{"path": str(request_path), "turn_id": "turn-1"}],
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "frontdoor_canonical_context": {"active_stage_id": "", "transition_required": False, "stages": []},
            "compression_state": {},
            "semantic_context_state": {},
            "hydrated_tool_names": [],
            "capability_snapshot_exposure_revision": "exp:demo",
            "visible_tool_ids": ["exec"],
            "visible_skill_ids": [],
            "provider_tool_schema_names": ["exec"],
            "source_reason": "finalize",
        },
    )

    assert web_ceo_sessions.read_completed_continuity_snapshot("web:shared") is not None

    web_ceo_sessions.clear_web_ceo_session_artifacts(session_id="web:shared")

    assert web_ceo_sessions.read_completed_continuity_snapshot("web:shared") is None


def test_runtime_agent_session_restores_completed_continuity_snapshot_from_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    record = web_ceo_sessions.persist_frontdoor_actual_request(
        "web:shared",
        payload={
            "turn_id": "turn-1",
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "Resume from continuity"},
            ],
            "prompt_cache_key_hash": "family-continuity",
            "actual_request_hash": "continuity-hash",
            "actual_request_message_count": 2,
            "actual_tool_schema_hash": "tool-continuity",
            "provider_model": "responses:gpt-test",
        },
    )
    web_ceo_sessions.write_completed_continuity_snapshot(
        "web:shared",
        {
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "Resume from continuity"},
            ],
            "frontdoor_history_shrink_reason": "stage_compaction",
            "frontdoor_actual_request_path": record["path"],
            "frontdoor_actual_request_history": [record],
            "frontdoor_stage_state": {"active_stage_id": "stage-1", "transition_required": False, "stages": []},
            "frontdoor_canonical_context": {"active_stage_id": "", "transition_required": False, "stages": []},
            "compression_state": {"status": "ready", "text": "ok", "source": "semantic"},
            "semantic_context_state": {"summary_text": "summary", "needs_refresh": False},
            "hydrated_tool_names": ["web_fetch"],
            "capability_snapshot_exposure_revision": "exp:demo",
            "visible_tool_ids": ["exec", "web_fetch"],
            "visible_skill_ids": ["web-access"],
            "provider_tool_schema_names": ["exec", "web_fetch"],
            "source_reason": "finalize",
        },
    )
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, sessions=SessionManager(tmp_path)),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )

    assert session._frontdoor_request_body_messages == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Resume from continuity"},
    ]
    assert session._frontdoor_history_shrink_reason == "stage_compaction"
    assert session._frontdoor_actual_request_path == record["path"]
    assert session._frontdoor_actual_request_history == [record]
    assert session._frontdoor_stage_state == {"active_stage_id": "stage-1", "transition_required": False, "stages": []}
    assert session._compression_state == {"status": "ready", "text": "ok", "source": "semantic"}
    assert session._semantic_context_state == {"summary_text": "summary", "needs_refresh": False}
    assert session._frontdoor_hydrated_tool_names == ["web_fetch"]


def test_runtime_agent_session_execution_context_snapshot_strips_multimodal_frontdoor_baseline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, sessions=SessionManager(tmp_path)),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_request_body_messages = [
        {"role": "system", "content": "SYSTEM"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please inspect this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ]

    snapshot = session._build_execution_context_snapshot()

    assert snapshot["frontdoor_request_body_messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Please inspect this image"},
    ]


def test_ceo_session_pending_interrupts_fall_back_to_paused_disk_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, "workspace_path", lambda: tmp_path)
    web_ceo_sessions.write_paused_execution_context(
        "web:shared",
        {
            "status": "paused",
            "interrupts": [{"id": "interrupt-disk-1", "value": {"kind": "frontdoor_tool_approval"}}],
        },
    )
    session_manager = SessionManager(tmp_path)
    session_manager.save(session_manager.get_or_create("web:shared"))
    live_session = SimpleNamespace(
        state=SimpleNamespace(status="paused", is_running=False, pending_interrupts=[]),
    )

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")

    monkeypatch.setattr(ceo_sessions, "get_agent", lambda: SimpleNamespace(sessions=session_manager))
    monkeypatch.setattr(
        ceo_sessions,
        "get_runtime_manager",
        lambda _agent: SimpleNamespace(get=lambda _session_id: live_session),
    )

    client = TestClient(app)
    response = client.get("/api/ceo/sessions/web:shared/pending-interrupts")

    assert response.status_code == 200
    assert response.json()["items"] == [
        {"id": "interrupt-disk-1", "value": {"kind": "frontdoor_tool_approval"}}
    ]


def test_ceo_session_pending_batch_interrupts_fall_back_to_paused_disk_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, "workspace_path", lambda: tmp_path)
    web_ceo_sessions.write_paused_execution_context(
        "web:shared",
        {
            "status": "paused",
            "interrupts": [
                {
                    "id": "interrupt-disk-1",
                    "value": {
                        "kind": "frontdoor_tool_approval_batch",
                        "batch_id": "batch:123",
                        "review_items": [
                            {"tool_call_id": "call-1", "name": "exec", "risk_level": "high", "arguments": {"command": "echo hi"}}
                        ],
                    },
                }
            ],
        },
    )
    session_manager = SessionManager(tmp_path)
    session_manager.save(session_manager.get_or_create("web:shared"))
    live_session = SimpleNamespace(
        state=SimpleNamespace(status="paused", is_running=False, pending_interrupts=[]),
    )

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")

    monkeypatch.setattr(ceo_sessions, "get_agent", lambda: SimpleNamespace(sessions=session_manager))
    monkeypatch.setattr(
        ceo_sessions,
        "get_runtime_manager",
        lambda _agent: SimpleNamespace(get=lambda _session_id: live_session),
    )

    client = TestClient(app)
    response = client.get("/api/ceo/sessions/web:shared/pending-interrupts")

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "id": "interrupt-disk-1",
            "value": {
                "kind": "frontdoor_tool_approval_batch",
                "batch_id": "batch:123",
                "review_items": [
                    {"tool_call_id": "call-1", "name": "exec", "risk_level": "high", "arguments": {"command": "echo hi"}}
                ],
            },
        }
    ]


def test_ceo_session_pending_interrupts_do_not_fall_back_to_stale_paused_disk_state_while_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, "workspace_path", lambda: tmp_path)
    web_ceo_sessions.write_paused_execution_context(
        "web:shared",
        {
            "status": "paused",
            "interrupts": [
                {
                    "id": "interrupt-disk-1",
                    "value": {
                        "kind": "frontdoor_tool_approval_batch",
                        "batch_id": "batch:123",
                        "review_items": [
                            {"tool_call_id": "call-1", "name": "exec", "risk_level": "high", "arguments": {"command": "echo hi"}}
                        ],
                    },
                }
            ],
        },
    )
    session_manager = SessionManager(tmp_path)
    session_manager.save(session_manager.get_or_create("web:shared"))
    live_session = SimpleNamespace(
        state=SimpleNamespace(status="running", is_running=True, pending_interrupts=[]),
    )

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")

    monkeypatch.setattr(ceo_sessions, "get_agent", lambda: SimpleNamespace(sessions=session_manager))
    monkeypatch.setattr(
        ceo_sessions,
        "get_runtime_manager",
        lambda _agent: SimpleNamespace(get=lambda _session_id: live_session),
    )

    client = TestClient(app)
    response = client.get("/api/ceo/sessions/web:shared/pending-interrupts")

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_ceo_session_resume_interrupt_recreates_runtime_from_persisted_pause(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, "workspace_path", lambda: tmp_path)
    web_ceo_sessions.write_paused_execution_context(
        "web:shared",
        {
            "status": "paused",
            "interrupts": [{"id": "interrupt-disk-1", "value": {"kind": "frontdoor_tool_approval"}}],
            "user_message": {"content": "create the task"},
        },
    )
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create("web:shared")
    persisted.metadata = web_ceo_sessions.normalize_ceo_metadata(
        {"memory_scope": {"channel": "memory-web", "chat_id": "memory-shared"}},
        session_key="web:shared",
    )
    session_manager.save(persisted)

    class _RecreatedSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(
                status="paused",
                is_running=False,
                pending_interrupts=[{"id": "interrupt-disk-1", "value": {"kind": "frontdoor_tool_approval"}}],
            )
            self.resume_payloads: list[object] = []

        async def resume_frontdoor_interrupt(self, *, resume_value):
            self.resume_payloads.append(resume_value)
            self.state.status = "completed"
            self.state.pending_interrupts = []
            return SimpleNamespace(output="approved reply")

        def state_dict(self) -> dict[str, object]:
            return {
                "status": self.state.status,
                "is_running": self.state.is_running,
                "pending_interrupts": list(self.state.pending_interrupts),
            }

    created: list[dict[str, object]] = []
    recreated = _RecreatedSession()

    class _RuntimeManager:
        def get(self, _session_id: str):
            return None

        def get_or_create(self, **kwargs):
            created.append(dict(kwargs))
            return recreated

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")

    monkeypatch.setattr(ceo_sessions, "get_agent", lambda: SimpleNamespace(sessions=session_manager))
    monkeypatch.setattr(ceo_sessions, "get_runtime_manager", lambda _agent: _RuntimeManager())

    client = TestClient(app)
    response = client.post(
        "/api/ceo/sessions/web:shared/resume-interrupt",
        json={"resume": {"approved": True}},
    )

    assert response.status_code == 200
    assert created == [
        {
            "session_key": "web:shared",
            "channel": "web",
            "chat_id": "shared",
            "memory_channel": "memory-web",
            "memory_chat_id": "memory-shared",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_agent_session_resume_frontdoor_interrupt_restores_turn_id_from_paused_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    web_ceo_sessions.write_paused_execution_context(
        "web:shared",
        {
            "status": "paused",
            "source": "approval",
            "turn_id": "turn-from-disk",
            "user_message": {"content": "create the task"},
            "interrupts": [{"id": "interrupt-disk-1", "value": {"kind": "frontdoor_tool_approval_batch"}}],
        },
    )

    events: list[AgentEvent] = []

    class _Runner:
        async def resume_turn(self, *, session, resume_value, on_progress):
            _ = resume_value, on_progress
            paused_snapshot = session.paused_execution_context_snapshot()
            assert paused_snapshot is not None
            assert paused_snapshot["turn_id"] == "turn-from-disk"
            return "approved reply"

    loop = SimpleNamespace(
        multi_agent_runner=_Runner(),
        model="gpt-test",
        reasoning_effort=None,
    )
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session.state.pending_interrupts = [{"id": "interrupt-disk-1", "value": {"kind": "frontdoor_tool_approval_batch"}}]
    session.state.paused = True
    session.state.status = "paused"

    session.subscribe(lambda event: events.append(event))

    result = await session.resume_frontdoor_interrupt(resume_value={"approved": True})

    assert result.output == "approved reply"
    message_end = next(event for event in events if event.type == "message_end")
    assert message_end.payload["turn_id"] == "turn-from-disk"


def test_read_inflight_turn_snapshot_ignores_terminal_error_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    web_ceo_sessions.write_inflight_turn_snapshot(
        "web:ceo-stale-error",
        {
            "status": "error",
            "assistant_text": "运行出错：CEO frontdoor exceeded maximum iterations",
            "last_error": {"message": "CEO frontdoor exceeded maximum iterations"},
        },
    )

    assert web_ceo_sessions.read_inflight_turn_snapshot("web:ceo-stale-error") is None
    assert "web:ceo-stale-error" not in web_ceo_sessions.list_inflight_web_ceo_sessions()


def test_inflight_snapshot_skips_watchdog_progress_updates() -> None:
    loop = SimpleNamespace(model="gpt-test", reasoning_effort=None)
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session._state.is_running = True
    session._state.status = "running"
    session._event_log = [
        {
            "type": "tool_execution_start",
            "timestamp": "2026-03-18T12:00:00",
            "payload": {
                "tool_name": "skill-installer",
                "text": "skill-installer started",
                "tool_call_id": "skill-installer:1",
            },
        },
        {
            "type": "tool_execution_update",
            "timestamp": "2026-03-18T12:00:05",
            "payload": {
                "tool_name": "skill-installer",
                "text": "watchdog synthetic update",
                "tool_call_id": "skill-installer:1",
                "kind": "tool",
                "data": {"tool_name": "skill-installer", "watchdog": True},
            },
        },
        {
            "type": "tool_execution_update",
            "timestamp": "2026-03-18T12:00:30",
            "payload": {
                "tool_name": "skill-installer",
                "text": json.dumps(
                    {
                        "status": "background_running",
                        "execution_id": "tool-exec:1",
                        "runtime_snapshot": {"summary_text": "still fetching files"},
                    },
                    ensure_ascii=False,
                ),
                "tool_call_id": "skill-installer:1",
                "kind": "tool_background",
                "data": {"tool_name": "skill-installer"},
            },
        },
    ]

    tools = session._interaction_flow_snapshot()

    assert any(item["kind"] == "tool_background" for item in tools)
    assert all(item["text"] != "watchdog synthetic update" for item in tools)


def test_inflight_turn_snapshot_prefers_stage_trace_summary_over_flat_tool_events() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.status = "running"
    session._frontdoor_stage_state = _sample_frontdoor_stage_state()

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    assert "execution_trace_summary" in snapshot
    assert snapshot["execution_trace_summary"]["stages"][0]["stage_goal"] == "inspect repository"
    assert "tool_events" not in snapshot


def test_inflight_snapshot_without_real_stage_state_omits_tool_flow_snapshot() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {}
    session._event_log = [
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:00:00Z",
            "payload": {
                "tool_name": "load_tool_context",
                "text": "load_tool_context started",
                "tool_call_id": "load_tool_context:1",
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-08T08:00:02Z",
            "payload": {
                "tool_name": "load_tool_context",
                "text": "load_tool_context done",
                "tool_call_id": "load_tool_context:1",
                "is_error": False,
            },
        },
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:00:03Z",
            "payload": {
                "tool_name": "memory_note",
                "text": "memory_note started",
                "tool_call_id": "memory_note:2",
            },
        },
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is None


def test_inflight_snapshot_with_empty_stage_list_omits_tool_flow_snapshot() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {"stages": []}
    session._event_log = [
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:10:00Z",
            "payload": {
                "tool_name": "load_tool_context",
                "text": "load_tool_context started",
                "tool_call_id": "load_tool_context:1",
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-08T08:10:02Z",
            "payload": {
                "tool_name": "load_tool_context",
                "text": "load_tool_context done",
                "tool_call_id": "load_tool_context:1",
                "is_error": False,
            },
        },
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:10:03Z",
            "payload": {
                "tool_name": "memory_note",
                "text": "memory_note started",
                "tool_call_id": "memory_note:2",
            },
        },
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is None


def test_inflight_snapshot_with_real_stage_state_preserves_goal_budget_and_round_boundaries() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:ceo-da58bee7f1ca",
        channel="web",
        chat_id="ceo-da58bee7f1ca",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_goal": "查看当前可检索的长期记忆，并向用户按类别清晰汇总我已记住的内容。",
                "tool_round_budget": 3,
                "tool_rounds_used": 2,
                    "status": "active",
                    "mode": "自主执行",
                    "stage_kind": "normal",
                    "rounds": [
                        {
                            "round_index": 1,
                            "tool_call_ids": ["load_tool_context:1"],
                            "tool_names": ["load_tool_context"],
                        },
                        {
                            "round_index": 2,
                            "tool_call_ids": ["memory_note:2"],
                            "tool_names": ["memory_note"],
                        },
                    ],
                }
            ],
        }
    session._event_log = [
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:01:00Z",
            "payload": {
                "tool_name": "load_tool_context",
                "text": "round 1 started",
                "tool_call_id": "load_tool_context:1",
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-08T08:01:01Z",
            "payload": {
                "tool_name": "load_tool_context",
                "text": "round 1 done",
                "tool_call_id": "load_tool_context:1",
                "is_error": False,
            },
        },
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:01:10Z",
            "payload": {
                "tool_name": "memory_note",
                "text": "round 2 started",
                "tool_call_id": "memory_note:2",
            },
        },
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    trace = snapshot["execution_trace_summary"]
    assert trace["active_stage_id"] == "frontdoor-stage-1"
    assert len(trace["stages"]) == 1
    stage = trace["stages"][0]
    assert stage["stage_id"] == "frontdoor-stage-1"
    assert stage["stage_goal"] == "查看当前可检索的长期记忆，并向用户按类别清晰汇总我已记住的内容。"
    assert stage["tool_round_budget"] == 3
    assert [len(round_item["tools"]) for round_item in stage["rounds"]] == [0, 0]
    assert [round_item["tool_names"] for round_item in stage["rounds"]] == [["load_tool_context"], ["memory_note"]]
    assert [round_item["tool_call_ids"] for round_item in stage["rounds"]] == [
        ["load_tool_context:1"],
        ["memory_note:2"],
    ]


def test_snapshot_includes_compression_state_when_frontdoor_archive_is_running() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.status = "running"
    session._compression_state = {"status": "running", "text": "上下文压缩中", "source": "user"}

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    assert snapshot["compression"]["status"] == "running"
    assert snapshot["compression"]["text"] == "上下文压缩中"


def test_stage_trace_round_enrichment_uses_latest_tool_event_status() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_goal": "inspect repository",
                "status": "active",
                "rounds": [
                    {
                        "round_index": 1,
                        "tool_call_ids": ["skill-installer:1"],
                        "tool_names": ["skill-installer"],
                        "tools": [
                            {
                                "tool_call_id": "skill-installer:1",
                                "tool_name": "skill-installer",
                                "status": "success",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    session._event_log = [
        {
            "type": "tool_execution_start",
            "timestamp": "2026-03-18T12:00:00",
            "payload": {
                "tool_name": "skill-installer",
                "text": "started",
                "tool_call_id": "skill-installer:1",
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-03-18T12:00:10",
            "payload": {
                "tool_name": "skill-installer",
                "text": "completed",
                "tool_call_id": "skill-installer:1",
                "is_error": False,
            },
        },
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    tool = snapshot["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"][0]
    assert tool["tool_call_id"] == "skill-installer:1"
    assert tool["tool_name"] == "skill-installer"
    assert tool["status"] == "success"


@pytest.mark.asyncio
async def test_runtime_agent_session_persists_execution_trace_summary_into_session_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session
            await on_progress(
                "skill-installer started",
                event_kind="tool_start",
                event_data={"tool_name": "skill-installer"},
            )
            await on_progress(
                "installed weather",
                event_kind="tool_result",
                event_data={"tool_name": "skill-installer"},
            )
            session._frontdoor_stage_state = {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Install the weather skill",
                        "status": "active",
                        "rounds": [
                            {
                                "round_index": 1,
                                "tool_names": ["skill-installer"],
                                "tool_call_ids": ["skill-installer:1"],
                                "tools": [
                                    {
                                        "tool_call_id": "skill-installer:1",
                                        "tool_name": "skill-installer",
                                        "status": "success",
                                        "output_text": "installed weather",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return "The weather skill has been installed."

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session_id = "web:ceo-persist-tool-events"
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="ceo-persist-tool-events")

    result = await session.prompt("Install the weather skill")

    assert result.output == "The weather skill has been installed."
    reloaded_session = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded_session.messages] == ["user", "assistant"]
    assert reloaded_session.messages[1]["content"] == "The weather skill has been installed."
    canonical_context = reloaded_session.messages[1]["canonical_context"]
    tool = canonical_context["stages"][0]["rounds"][0]["tools"][0]
    assert tool["status"] == "success"
    assert tool["tool_name"] == "skill-installer"

    recent_history = web_ceo_sessions.extract_live_raw_tail(reloaded_session, turn_limit=4)
    assert recent_history[-2] == {"role": "user", "content": "Install the weather skill"}
    assert "Recent tool results:" in recent_history[-1]["content"]
    assert "skill-installer (success): installed weather" in recent_history[-1]["content"]


def test_inflight_execution_trace_summary_compacts_tool_payloads() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "stage_goal": "inspect repository",
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
                "status": "active",
                "rounds": [
                    {
                        "round_id": "frontdoor-stage-1:round-1",
                        "round_index": 1,
                        "created_at": "2026-04-08T12:00:00+08:00",
                        "budget_counted": True,
                        "tool_call_ids": ["call-1"],
                        "tool_names": ["load_tool_context"],
                        "tools": [
                            {
                                "tool_call_id": "call-1",
                                "tool_name": "load_tool_context",
                                "status": "success",
                                "arguments_text": '{"tool_id": "filesystem"}',
                                "output_text": "very long inline tool output",
                                "output_preview_text": "preview preserved",
                                "output_ref": "artifact:artifact:tool-output",
                                "started_at": "2026-04-08T12:00:00+08:00",
                                "finished_at": "2026-04-08T12:00:05+08:00",
                                "elapsed_seconds": 5.0,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    session._event_log = [
        {
            "timestamp": "2026-04-08T12:00:01+08:00",
            "type": "tool_execution_start",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "load_tool_context",
                "text": "started",
                "data": {
                    "arguments_text": '{"tool_id": "filesystem"}',
                    "started_at": "2026-04-08T12:00:00+08:00",
                },
            },
        },
        {
            "timestamp": "2026-04-08T12:00:05+08:00",
            "type": "tool_execution_end",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "load_tool_context",
                "text": "very long inline tool output",
                "is_error": False,
                "data": {
                    "arguments_text": '{"tool_id": "filesystem"}',
                    "output_text": "very long inline tool output",
                    "output_ref": "artifact:artifact:tool-output",
                    "started_at": "2026-04-08T12:00:00+08:00",
                    "finished_at": "2026-04-08T12:00:05+08:00",
                    "elapsed_seconds": 5.0,
                    "recovery_decision": "retry",
                    "related_tool_call_ids": ["call-0"],
                    "attempted_tools": ["filesystem"],
                    "evidence": [{"kind": "artifact", "ref": "artifact:artifact:tool-output"}],
                    "lost_result_summary": "preview preserved",
                },
            },
        }
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    tools = snapshot["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]

    assert tools[0]["arguments_text"] == '{"tool_id": "filesystem"}'
    assert tools[0]["output_preview_text"] == "preview preserved"
    assert tools[0]["output_ref"] == "artifact:artifact:tool-output"
    assert tools[0]["started_at"] == "2026-04-08T12:00:00+08:00"
    assert tools[0]["finished_at"] == "2026-04-08T12:00:05+08:00"
    assert tools[0]["elapsed_seconds"] == 5.0
    assert "recovery_decision" not in tools[0]
    assert "related_tool_call_ids" not in tools[0]
    assert "attempted_tools" not in tools[0]
    assert "evidence" not in tools[0]
    assert "lost_result_summary" not in tools[0]


def test_ceo_snapshot_summary_keeps_old_tool_details_only_as_preview_and_ref() -> None:
    raw_output = "short raw result"
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "stage_goal": "inspect repository",
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
                "status": "active",
                "rounds": [
                        {
                            "round_id": "frontdoor-stage-1:round-1",
                            "round_index": 1,
                            "created_at": "2026-04-08T12:00:00+08:00",
                            "budget_counted": True,
                            "tool_call_ids": ["call-1"],
                            "tool_names": ["load_tool_context"],
                            "tools": [
                                {
                                    "tool_call_id": "call-1",
                                    "tool_name": "load_tool_context",
                                    "status": "success",
                                    "output_preview_text": "short raw result",
                                    "output_ref": "artifact:artifact:tool-output",
                                }
                            ],
                        }
                    ],
                }
        ],
    }
    session._event_log = [
        {
            "timestamp": "2026-04-08T12:00:01+08:00",
            "type": "tool_execution_start",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "load_tool_context",
                "text": "started",
            },
        },
        {
            "timestamp": "2026-04-08T12:00:05+08:00",
            "type": "tool_execution_end",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "load_tool_context",
                "text": raw_output,
                "is_error": False,
                "data": {
                    "output_text": raw_output,
                    "output_ref": "artifact:artifact:tool-output",
                    "finished_at": "2026-04-08T12:00:05+08:00",
                },
            },
        }
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    tools = snapshot["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]

    assert tools[0]["output_preview_text"]
    assert tools[0]["output_preview_text"] == raw_output
    assert tools[0]["output_ref"] == "artifact:artifact:tool-output"


def test_ceo_snapshot_summary_preserves_falsy_event_payload_values() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "stage_goal": "inspect repository",
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
                "status": "active",
                "rounds": [
                    {
                        "round_id": "frontdoor-stage-1:round-1",
                        "round_index": 1,
                        "created_at": "2026-04-08T12:00:00+08:00",
                        "budget_counted": True,
                        "tool_call_ids": ["call-1"],
                        "tool_names": ["calculator"],
                        "tools": [
                            {
                                "tool_call_id": "call-1",
                                "tool_name": "calculator",
                                "status": "success",
                                "arguments_text": "0",
                                "output_text": "False",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    session._event_log = [
        {
            "timestamp": "2026-04-08T12:00:01+08:00",
            "type": "tool_execution_end",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "calculator",
                "text": "finished",
                "is_error": False,
                "data": {
                    "arguments_text": 0,
                    "output_text": False,
                },
            },
        }
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    tool = snapshot["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"][0]

    assert tool["arguments_text"] == "0"
    assert tool["output_text"] == "False"


def test_ceo_snapshot_summary_falls_back_to_tool_result_text_when_output_text_missing() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "stage_goal": "inspect skill workflow",
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
                "status": "active",
                "rounds": [
                    {
                        "round_id": "frontdoor-stage-1:round-1",
                        "round_index": 1,
                        "created_at": "2026-04-08T12:00:00+08:00",
                        "budget_counted": True,
                        "tool_call_ids": ["call-1"],
                        "tool_names": ["load_skill_context"],
                        "tools": [
                            {
                                "tool_call_id": "call-1",
                                "tool_name": "load_skill_context",
                                "status": "success",
                                "arguments_text": "load_skill_context (skill_id=find-skills)",
                                "output_preview_text": "loaded full skill body",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    session._event_log = [
        {
            "timestamp": "2026-04-08T12:00:01+08:00",
            "type": "tool_execution_start",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "load_skill_context",
                "text": "load_skill_context (skill_id=find-skills)",
                "data": {
                    "arguments_text": "load_skill_context (skill_id=find-skills)",
                },
            },
        },
        {
            "timestamp": "2026-04-08T12:00:05+08:00",
            "type": "tool_execution_end",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "load_skill_context",
                "text": '{"result_text":"loaded full skill body","status":"success"}',
                "is_error": False,
                "data": {
                    "output_text": "",
                },
            },
        },
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    tool = snapshot["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"][0]

    assert tool["arguments_text"] == "load_skill_context (skill_id=find-skills)"
    assert tool["output_preview_text"] == "loaded full skill body"


def test_ceo_frontdoor_support_extracts_output_ref_for_progress_events() -> None:
    payload = json.dumps(
        {
            "summary": "tool output stored externally",
            "ref": "artifact:artifact:tool-output",
            "resolved_ref": "artifact:artifact:tool-output",
        },
        ensure_ascii=False,
    )

    data = CeoFrontDoorSupport._tool_result_progress_event_data(
        tool_name="filesystem",
        result_text=payload,
    )

    assert data["tool_name"] == "filesystem"
    assert data["output_ref"] == "artifact:artifact:tool-output"
    assert data["output_preview_text"] == "tool output stored externally"


def test_ceo_exec_output_ref_payload_does_not_degrade_to_preview_only() -> None:
    payload = json.dumps(
        {
            "summary": "exec output stored externally",
            "output_ref": "artifact:artifact:exec-output",
        },
        ensure_ascii=False,
    )

    data = CeoFrontDoorSupport._tool_result_progress_event_data(
        tool_name="exec",
        result_text=payload,
        tool_call_id="call:exec",
    )

    assert data["tool_name"] == "exec"
    assert data["tool_call_id"] == "call:exec"
    assert data["output_ref"] == "artifact:artifact:exec-output"
    assert data["output_preview_text"] == "exec output stored externally"


def test_ceo_frontdoor_support_preserves_tool_call_id_for_progress_events() -> None:
    data = CeoFrontDoorSupport._tool_result_progress_event_data(
        tool_name="filesystem",
        result_text='{"summary":"ok"}',
        tool_call_id="filesystem-call-2",
    )

    assert data["tool_name"] == "filesystem"
    assert data["tool_call_id"] == "filesystem-call-2"


@pytest.mark.asyncio
async def test_ceo_large_non_inline_tool_result_emits_output_ref(tmp_path: Path) -> None:
    class _LargeTool(Tool):
        @property
        def name(self) -> str:
            return "large_tool"

        @property
        def description(self) -> str:
            return "Return a large payload that should be externalized."

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: object) -> object:
            _ = kwargs
            return {
                "ok": True,
                "stdout": "\n".join(f"line {index:03d} " + ("x" * 32) for index in range(180)),
            }

    class _ToolRuntimeStack:
        def push_runtime_context(self, runtime_context: dict[str, object]) -> None:
            _ = runtime_context
            return None

        def pop_runtime_context(self, token: object) -> None:
            _ = token

        def get(self, name: str) -> None:
            _ = name
            return None

    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    tool = _LargeTool()
    tool._descriptor = ToolResourceDescriptor(
        kind=ResourceKind.TOOL,
        name=tool.name,
        description=tool.description,
        root=tmp_path,
        manifest_path=tmp_path / "large_tool.yaml",
        fingerprint="large-tool",
        metadata={"tool_result_inline_full": False},
        tool_result_inline_full=False,
    )
    loop = SimpleNamespace(
        tools=_ToolRuntimeStack(),
        resource_manager=None,
        tool_execution_manager=None,
        main_task_service=SimpleNamespace(content_store=content_store),
    )
    support = CeoFrontDoorSupport(loop=loop)

    try:
        result_text, status, _started_at, _finished_at, _elapsed_seconds = await support._execute_tool_call(
            tool=tool,
            tool_name=tool.name,
            arguments={},
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "ceo"},
            on_progress=None,
            tool_call_id="call-large-1",
        )
    finally:
        store.close()

    envelope = parse_content_envelope(result_text)

    assert status == "success"
    assert envelope is not None
    assert envelope.ref.startswith("artifact:")
    progress_data = support._tool_result_progress_event_data(
        tool_name=tool.name,
        result_text=result_text,
        tool_call_id="call-large-1",
    )
    assert progress_data["tool_name"] == tool.name
    assert progress_data["tool_call_id"] == "call-large-1"
    assert progress_data["output_ref"] == envelope.ref
    assert progress_data["output_preview_text"]


@pytest.mark.asyncio
async def test_ceo_direct_load_tool_result_stays_inline_even_when_large(tmp_path: Path) -> None:
    class _LargeDirectLoadTool(Tool):
        @property
        def name(self) -> str:
            return "load_tool_context"

        @property
        def description(self) -> str:
            return "Return a large direct-load context body that must stay inline."

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: object) -> object:
            _ = kwargs
            return {
                "ok": True,
                "level": "l2",
                "uri": "g3ku://resource/tool/filesystem",
                "l0": "Filesystem tool context",
                "l1": "Full tool instructions for filesystem operations.",
                "content": "\n".join(f"context line {index:03d} " + ("x" * 40) for index in range(180)),
            }

    class _ToolRuntimeStack:
        def push_runtime_context(self, runtime_context: dict[str, object]) -> None:
            _ = runtime_context
            return None

        def pop_runtime_context(self, token: object) -> None:
            _ = token

        def get(self, name: str) -> None:
            _ = name
            return None

    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    tool = _LargeDirectLoadTool()
    loop = SimpleNamespace(
        tools=_ToolRuntimeStack(),
        resource_manager=None,
        tool_execution_manager=None,
        main_task_service=SimpleNamespace(content_store=content_store),
    )
    support = CeoFrontDoorSupport(loop=loop)

    try:
        result_text, status, _started_at, _finished_at, _elapsed_seconds = await support._execute_tool_call(
            tool=tool,
            tool_name=tool.name,
            arguments={},
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "ceo"},
            on_progress=None,
            tool_call_id="call-direct-load-1",
        )
    finally:
        store.close()

    assert status == "success"
    assert parse_content_envelope(result_text) is None
    payload = json.loads(result_text)
    assert payload["ok"] is True
    assert payload["level"] == "l2"
    assert payload["uri"] == "g3ku://resource/tool/filesystem"
    assert payload["content"].startswith("context line 000")
    assert len(payload["content"]) > 1200


@pytest.mark.asyncio
async def test_ceo_manifest_tool_result_inline_full_stays_inline_even_when_large(tmp_path: Path) -> None:
    class _LargeInlineManifestTool(Tool):
        @property
        def name(self) -> str:
            return "inline_manifest_tool"

        @property
        def description(self) -> str:
            return "Return a large manifest-backed payload that must stay inline."

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: object) -> object:
            _ = kwargs
            return {
                "ok": True,
                "stdout": "\n".join(f"inline line {index:03d} " + ("x" * 40) for index in range(180)),
            }

    class _ToolRuntimeStack:
        def push_runtime_context(self, runtime_context: dict[str, object]) -> None:
            _ = runtime_context
            return None

        def pop_runtime_context(self, token: object) -> None:
            _ = token

        def get(self, name: str) -> None:
            _ = name
            return None

    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    tool = _LargeInlineManifestTool()
    tool._descriptor = ToolResourceDescriptor(
        kind=ResourceKind.TOOL,
        name=tool.name,
        description=tool.description,
        root=tmp_path,
        manifest_path=tmp_path / "inline_manifest_tool.yaml",
        fingerprint="inline-manifest-tool",
        metadata={"tool_result_inline_full": True},
        tool_result_inline_full=True,
    )
    loop = SimpleNamespace(
        tools=_ToolRuntimeStack(),
        resource_manager=None,
        tool_execution_manager=None,
        main_task_service=SimpleNamespace(content_store=content_store),
    )
    support = CeoFrontDoorSupport(loop=loop)

    try:
        result_text, status, _started_at, _finished_at, _elapsed_seconds = await support._execute_tool_call(
            tool=tool,
            tool_name=tool.name,
            arguments={},
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "ceo"},
            on_progress=None,
            tool_call_id="call-inline-full-1",
        )
    finally:
        store.close()

    assert status == "success"
    assert parse_content_envelope(result_text) is None
    payload = json.loads(result_text)
    assert payload["ok"] is True
    assert payload["stdout"].startswith("inline line 000")
    assert len(payload["stdout"].splitlines()) == 180


@pytest.mark.asyncio
async def test_ceo_slow_generic_tool_stays_inline_and_never_detaches() -> None:
    class _SlowExecTool(Tool):
        @property
        def name(self) -> str:
            return "exec"

        @property
        def description(self) -> str:
            return "Return a slow but inline CEO tool result."

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: object) -> object:
            _ = kwargs
            await asyncio.sleep(0.16)
            return {"ok": True, "stdout": "done"}

    class _ToolRuntimeStack:
        def push_runtime_context(self, runtime_context: dict[str, object]) -> None:
            _ = runtime_context
            return None

        def pop_runtime_context(self, token: object) -> None:
            _ = token

        def get(self, name: str) -> None:
            _ = name
            return None

    class _HeartbeatTerminalRecorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def enqueue_tool_terminal(self, *, session_id: str, payload: dict[str, object]) -> None:
            self.calls.append((str(session_id or ""), dict(payload)))

    heartbeat = _HeartbeatTerminalRecorder()
    loop = SimpleNamespace(
        tools=_ToolRuntimeStack(),
        resource_manager=None,
        tool_execution_manager=None,
        web_session_heartbeat=heartbeat,
        main_task_service=SimpleNamespace(content_store=None),
    )
    support = CeoFrontDoorSupport(loop=loop)
    progress_events: list[tuple[str, str, dict[str, object] | None]] = []

    async def _on_progress(
        text: str,
        *,
        tool_hint: bool = False,
        event_kind: str | None = None,
        event_data: dict[str, object] | None = None,
    ) -> None:
        _ = tool_hint
        progress_events.append((str(event_kind or ""), str(text or ""), dict(event_data or {}) if isinstance(event_data, dict) else None))

    result_text, status, _started_at, _finished_at, elapsed_seconds = await support._execute_tool_call(
        tool=_SlowExecTool(),
        tool_name="exec",
        arguments={},
        runtime_context={
            "actor_role": "ceo",
            "session_key": "web:shared",
            "tool_watchdog": {
                "enabled": True,
                "poll_interval_seconds": 0.05,
                "handoff_after_seconds": 0.1,
            },
        },
        on_progress=_on_progress,
        tool_call_id="call-slow-exec-1",
    )

    assert status == "success"
    assert json.loads(result_text) == {"ok": True, "stdout": "done"}
    assert "background_running" not in result_text
    assert "execution_id" not in result_text
    assert heartbeat.calls == []
    assert any(event_kind == "tool_start" for event_kind, _text, _data in progress_events)
    assert elapsed_seconds is not None and elapsed_seconds >= 0.1


def test_stage_trace_call_id_fallback_does_not_reuse_same_tool_result_across_rounds() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "stage_goal": "inspect repository",
                "tool_round_budget": 3,
                "tool_rounds_used": 2,
                "status": "active",
                "rounds": [
                    {"round_index": 1, "tool_call_ids": ["filesystem:1"], "tool_names": ["filesystem"]},
                    {"round_index": 2, "tool_call_ids": ["filesystem:2"], "tool_names": ["filesystem"]},
                ],
            }
        ],
    }
    session._event_log = [
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:00:00Z",
            "payload": {
                "tool_name": "filesystem",
                "text": "round 1 started",
                "tool_call_id": "filesystem:1",
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-08T08:00:02Z",
            "payload": {
                "tool_name": "filesystem",
                "text": "round 1 done",
                "tool_call_id": "filesystem:1",
                "is_error": False,
            },
        },
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:00:03Z",
            "payload": {
                "tool_name": "filesystem",
                "text": "round 2 started",
                "tool_call_id": "filesystem:2",
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-08T08:00:04Z",
            "payload": {
                "tool_name": "filesystem",
                "text": "round 2 done",
                "tool_call_id": "filesystem:2",
                "is_error": False,
            },
        },
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    rounds = snapshot["execution_trace_summary"]["stages"][0]["rounds"]
    assert [round_item["tool_call_ids"] for round_item in rounds] == [["filesystem:1"], ["filesystem:2"]]
    assert [round_item["tool_names"] for round_item in rounds] == [["filesystem"], ["filesystem"]]
    assert [round_item["tools"] for round_item in rounds] == [[], []]


def test_stage_trace_with_mixed_same_name_rounds_does_not_pull_future_exec_into_prior_round() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    exec_round_1 = "call_yng5IS4S1QGJ7gGa35BArtLt|fc_036f236b3fb53d5b0169de312be2f48191934a3b4ca58f9b80"
    load_round_1 = "call_lftc3j1B076UIrNCmn4kyyrZ|fc_036f236b3fb53d5b0169de312be30081918fc4c8ce00206cc9"
    exec_round_2 = "call_4TQeCNWdG1PeIjqrgRLm8NkG|fc_036f236b3fb53d5b0169de312f5e1c8191ab2796363eb60754"
    exec_round_3 = "call_7HkcgF1xR6v670xKw298IF1m|fc_036f236b3fb53d5b0169de3139173c8191a267d514c2c85d1d"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": True,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "stage_goal": "write markdown file on desktop",
                "tool_round_budget": 4,
                "tool_rounds_used": 3,
                "status": "active",
                "created_at": "2026-04-14T20:20:54+08:00",
                "rounds": [
                    {
                        "round_id": "frontdoor-stage-1:round-1",
                        "round_index": 1,
                        "created_at": "2026-04-14T20:21:01+08:00",
                        "tool_call_ids": [exec_round_1, load_round_1],
                        "tool_names": ["exec", "load_tool_context"],
                    },
                    {
                        "round_id": "frontdoor-stage-1:round-2",
                        "round_index": 2,
                        "created_at": "2026-04-14T20:21:08+08:00",
                        "tool_call_ids": [exec_round_2],
                        "tool_names": ["exec"],
                    },
                    {
                        "round_id": "frontdoor-stage-1:round-3",
                        "round_index": 3,
                        "created_at": "2026-04-14T20:21:18+08:00",
                        "tool_call_ids": [exec_round_3],
                        "tool_names": ["exec"],
                    },
                ],
            }
        ],
    }
    session._event_log = [
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-14T20:21:01.642584",
            "payload": {
                "tool_name": "exec",
                "text": '{"status":"success","head_preview":"env"}',
                "tool_call_id": exec_round_1,
                "is_error": False,
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-14T20:21:00.822205",
            "payload": {
                "tool_name": "load_tool_context",
                "text": '{"ok":true,"tool_id":"filesystem_write"}',
                "tool_call_id": load_round_1,
                "is_error": False,
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-14T20:21:08.273269",
            "payload": {
                "tool_name": "exec",
                "text": '{"status":"error","head_preview":"blocked"}',
                "tool_call_id": exec_round_2,
                "is_error": False,
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-14T20:21:18.232596",
            "payload": {
                "tool_name": "exec",
                "text": '{"status":"error","head_preview":"retry"}',
                "tool_call_id": exec_round_3,
                "is_error": False,
            },
        },
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    rounds = snapshot["execution_trace_summary"]["stages"][0]["rounds"]
    assert [round_item["tool_call_ids"] for round_item in rounds] == [
        [exec_round_1, load_round_1],
        [exec_round_2],
        [exec_round_3],
    ]
    assert [round_item["tool_names"] for round_item in rounds] == [
        ["exec", "load_tool_context"],
        ["exec"],
        ["exec"],
    ]
    assert [round_item["tools"] for round_item in rounds] == [[], [], []]


def test_stage_trace_uses_existing_round_tools_without_event_log_reassignment() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="gpt-test", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "stage_goal": "inspect repository",
                "tool_round_budget": 2,
                "tool_rounds_used": 2,
                "status": "active",
                "rounds": [
                    {
                        "round_id": "frontdoor-stage-1:round-1",
                        "round_index": 1,
                        "created_at": "2026-04-08T08:00:00Z",
                        "tool_call_ids": ["filesystem:1"],
                        "tool_names": ["filesystem"],
                        "tools": [
                            {
                                "tool_call_id": "filesystem:1",
                                "tool_name": "filesystem",
                                "status": "success",
                                "arguments_text": "filesystem (path=/tmp/a.txt)",
                                "output_text": "round 1 done",
                                "output_ref": "",
                                "timestamp": "2026-04-08T08:00:02Z",
                                "kind": "tool_result",
                                "source": "user",
                            }
                        ],
                    },
                    {
                        "round_id": "frontdoor-stage-1:round-2",
                        "round_index": 2,
                        "created_at": "2026-04-08T08:00:03Z",
                        "tool_call_ids": ["filesystem:2"],
                        "tool_names": ["filesystem"],
                        "tools": [
                            {
                                "tool_call_id": "filesystem:2",
                                "tool_name": "filesystem",
                                "status": "success",
                                "arguments_text": "filesystem (path=/tmp/b.txt)",
                                "output_text": "round 2 done",
                                "output_ref": "",
                                "timestamp": "2026-04-08T08:00:04Z",
                                "kind": "tool_result",
                                "source": "user",
                            }
                        ],
                    },
                ],
            }
        ],
    }
    session._event_log = [
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-08T08:00:05Z",
            "payload": {
                "tool_name": "filesystem",
                "text": "future stray tool",
                "tool_call_id": "filesystem:3",
                "is_error": False,
            },
        }
    ]

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    rounds = snapshot["execution_trace_summary"]["stages"][0]["rounds"]
    assert [tool["tool_call_id"] for tool in rounds[0]["tools"]] == ["filesystem:1"]
    assert [tool["tool_call_id"] for tool in rounds[1]["tools"]] == ["filesystem:2"]


@pytest.mark.parametrize(
    "stages",
    [
        [None],
        [{}],
        [{"foo": "bar"}],
    ],
)
def test_inflight_snapshot_with_malformed_stage_state_keeps_legacy_tool_flow(stages: list[object]) -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {"stages": stages}
    session._event_log = [
        {
            "type": "tool_execution_start",
            "timestamp": "2026-04-08T08:20:00Z",
            "payload": {
                "tool_name": "load_tool_context",
                "text": "load_tool_context started",
                "tool_call_id": "load_tool_context:1",
            },
        },
        {
            "type": "tool_execution_end",
            "timestamp": "2026-04-08T08:20:02Z",
            "payload": {
                "tool_name": "load_tool_context",
                "text": "load_tool_context done",
                "tool_call_id": "load_tool_context:1",
                "is_error": False,
            },
        },
    ]

    snapshot = session.inflight_turn_snapshot()

    if stages == [None]:
        assert snapshot is None
        return

    assert snapshot is not None
    summary = snapshot.get("execution_trace_summary") or {}
    assert summary["stages"][0]["stage_id"] == "frontdoor-stage-1"
    assert summary["stages"][0]["stage_goal"] == ""
    assert summary["stages"][0]["rounds"] == []
    assert "tool_events" not in snapshot


@pytest.mark.asyncio
async def test_runtime_agent_session_persists_pending_user_turn_before_cancellation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    started = asyncio.Event()

    class _BlockingRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
            started.set()
            await asyncio.Future()

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_BlockingRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session_id = "web:ceo-persist-pending-turn"
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="ceo-persist-pending-turn")

    turn_task = asyncio.create_task(session.prompt("Keep this request after cancellation"))
    await started.wait()
    turn_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await turn_task

    reloaded_session = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded_session.messages] == ["user"]
    assert reloaded_session.messages[0]["content"] == "Keep this request after cancellation"
    assert reloaded_session.messages[0]["metadata"]["_transcript_state"] == "pending"
    assert str(reloaded_session.messages[0]["metadata"]["_transcript_turn_id"]).strip()

    recent_history = web_ceo_sessions.extract_live_raw_tail(reloaded_session, turn_limit=4)
    assert recent_history == [{"role": "user", "content": "Keep this request after cancellation"}]


@pytest.mark.asyncio
async def test_runtime_agent_session_archives_running_assistant_before_consumed_follow_up(
    tmp_path: Path,
) -> None:
    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=None,
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: None,
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=lambda _session_key: 0,
        _use_rag_memory=lambda: False,
    )
    session_id = "web:ceo-follow-up-archive"
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="ceo-follow-up-archive")

    original = UserInputMessage(content="Original request")
    session._configure_user_batch([original], batch_id="batch-original")
    session._last_prompt = original
    session._state.latest_message = "Inspecting repo"
    session._state.is_running = True
    await session._persist_pending_user_messages(user_inputs=[original])

    await session.queue_follow_up_batch([UserInputMessage(content="Queued follow-up")], persist_transcript=True)
    drained = await session.take_follow_up_batch_for_call_model()

    assert [websocket_ceo._history_text(item.content) for item in drained] == ["Queued follow-up"]

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded.messages] == ["user", "assistant", "user"]
    assert reloaded.messages[1]["content"] == "Inspecting repo"
    assert reloaded.messages[1]["metadata"]["source"] == "follow_up_archive"
    assert reloaded.messages[1]["metadata"]["prompt_visible"] is False
    assert reloaded.messages[1]["metadata"]["ui_visible"] is True
    assert reloaded.messages[2]["content"] == "Queued follow-up"
    assert reloaded.messages[2]["metadata"]["_transcript_state"] == "pending"


@pytest.mark.asyncio
async def test_runtime_agent_session_archives_running_assistant_before_chained_follow_up_turn(
    tmp_path: Path,
) -> None:
    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=None,
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: None,
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=lambda _session_key: 0,
        _use_rag_memory=lambda: False,
    )
    session_id = "web:ceo-follow-up-chain-archive"
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="ceo-follow-up-chain-archive")

    original = UserInputMessage(content="Original request")
    session._configure_user_batch([original], batch_id="batch-original")
    session._last_prompt = original
    session._state.latest_message = "Inspecting repo"
    session._state.is_running = True
    await session._persist_pending_user_messages(user_inputs=[original])

    queued = await session.queue_follow_up_batch([UserInputMessage(content="Queued follow-up")], persist_transcript=True)
    assert [websocket_ceo._history_text(item.content) for item in queued] == ["Queued follow-up"]

    pending_turn_ids = {
        str((item.metadata or {}).get("_transcript_turn_id") or "").strip()
        for item in queued
    }
    await session.archive_follow_up_chain_transition(pending_follow_up_turn_ids=pending_turn_ids)

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded.messages] == ["user", "assistant", "user"]
    assert reloaded.messages[1]["content"] == "Inspecting repo"
    assert reloaded.messages[1]["metadata"]["source"] == "follow_up_archive"
    assert reloaded.messages[2]["content"] == "Queued follow-up"
    assert reloaded.messages[2]["metadata"]["_transcript_state"] == "pending"


@pytest.mark.asyncio
async def test_runtime_agent_session_persists_failed_turn_for_follow_up_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input
            await on_progress(
                "agent_browser started",
                event_kind="tool_start",
                event_data={"tool_name": "agent_browser"},
            )
            raise RuntimeError("CEO frontdoor exceeded maximum iterations")

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session_id = "web:ceo-persist-failed-turn"
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="ceo-persist-failed-turn")

    with pytest.raises(RuntimeError, match="CEO frontdoor exceeded maximum iterations"):
        await session.prompt("Open bilibili")

    reloaded_session = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded_session.messages] == ["user", "assistant"]
    assert reloaded_session.messages[0]["content"] == "Open bilibili"
    assert reloaded_session.messages[1]["content"] == "运行出错：CEO frontdoor exceeded maximum iterations"
    assert reloaded_session.messages[1]["metadata"] == {
        "source": "runtime_error",
        "error_code": "legacy_session_error",
        "error_message": "CEO frontdoor exceeded maximum iterations",
        "recoverable": True,
    }
    assert "execution_trace_summary" not in reloaded_session.messages[1]
    assert "tool_events" not in reloaded_session.messages[1]

    recent_history = web_ceo_sessions.extract_live_raw_tail(reloaded_session, turn_limit=4)
    assert recent_history[-2] == {"role": "user", "content": "Open bilibili"}
    assert "运行出错：CEO frontdoor exceeded maximum iterations" in recent_history[-1]["content"]


@pytest.mark.asyncio
async def test_runtime_agent_session_recovers_dispatched_async_task_after_internal_runtime_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input
            session._frontdoor_stage_state = _sample_frontdoor_stage_state()
            await on_progress(
                "create_async_task started",
                event_kind="tool_start",
                event_data={"tool_name": "create_async_task"},
            )
            await on_progress(
                "创建任务成功task:demo-123",
                event_kind="tool_result",
                event_data={"tool_name": "create_async_task"},
            )
            raise RuntimeError("no active connection")

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session_id = "web:ceo-recover-dispatched-task"
    session = RuntimeAgentSession(loop, session_key=session_id, channel="web", chat_id="ceo-recover-dispatched-task")

    result = await session.prompt("Analyze the repository in background")

    assert result.output == "后台任务已经建立，任务号 `task:demo-123`。当前回写遇到暂时异常，但后台任务仍在运行，完成后会继续同步结果。"
    assert session.state.status == "completed"
    assert session.state.last_error is None

    reloaded_session = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded_session.messages] == ["user", "assistant"]
    assert reloaded_session.messages[1]["content"] == result.output
    assert reloaded_session.messages[1]["metadata"] == {
        "task_ids": ["task:demo-123"],
        "reason": "async_dispatch_runtime_recovered",
    }
    summary = reloaded_session.messages[1]["canonical_context"]
    assert summary["active_stage_id"] == ""
    stage = summary["stages"][0]
    assert stage["status"] == "completed"
    assert stage["completed_stage_summary"] == result.output
    assert stage["finished_at"]

    recent_history = web_ceo_sessions.extract_live_raw_tail(reloaded_session, turn_limit=4)
    assert recent_history[-2] == {"role": "user", "content": "Analyze the repository in background"}
    assert recent_history[-1]["role"] == "assistant"
    assert str(recent_history[-1]["content"]).startswith(result.output)


@pytest.mark.asyncio
async def test_runtime_agent_session_logs_traceback_for_recovered_async_dispatch_runtime_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    captured: dict[str, object] = {}

    class _FakeBoundLogger:
        def error(self, message: str, *args) -> None:
            captured["message"] = message
            captured["args"] = args

    class _FakeLogger:
        def opt(self, *, exception=None):
            captured["exception"] = exception
            return _FakeBoundLogger()

    monkeypatch.setattr("g3ku.runtime.session_agent.logger", _FakeLogger())

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _FakeRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input
            session._frontdoor_stage_state = _sample_frontdoor_stage_state()
            await on_progress(
                "create_async_task started",
                event_kind="tool_start",
                event_data={"tool_name": "create_async_task"},
            )
            await on_progress(
                "创建任务成功task:demo-456",
                event_kind="tool_result",
                event_data={"tool_name": "create_async_task"},
            )
            raise RuntimeError("no active connection")

    async def _cancel_session_tasks(session_key: str) -> int:
        _ = session_key
        return 0

    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=_FakeRunner(),
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(
        loop,
        session_key="web:ceo-recover-dispatched-task-logging",
        channel="web",
        chat_id="ceo-recover-dispatched-task-logging",
    )

    result = await session.prompt("Analyze the repository in background")

    assert result.output.startswith("后台任务已经建立，任务号 `task:demo-456`")
    assert isinstance(captured.get("exception"), RuntimeError)
    assert str(captured["exception"]) == "no active connection"
    assert "Recovered async dispatch turn after internal runtime error" in str(captured.get("message"))
    assert any("task:demo-456" in str(arg) for arg in list(captured.get("args") or []))


def test_ceo_websocket_forwards_message_end_as_final_reply(tmp_path: Path, monkeypatch) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)
    session_id = "web:ceo-live-final"
    session_manager = SessionManager(tmp_path)
    live_session = _FakeLiveSession()
    agent = SimpleNamespace(
        sessions=session_manager,
        main_task_service=_TaskService(),
    )
    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: _RuntimeManager(live_session))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "snapshot.ceo"
        assert ws.receive_json()["type"] == "ceo.state"

        ws.send_json({"type": "client.user_message", "text": "Install the skill"})

        messages = []
        for _ in range(6):
            payload = ws.receive_json()
            messages.append(payload)
            if payload.get("type") == "ceo.reply.final":
                break

    final_events = [item for item in messages if item["type"] == "ceo.reply.final"]
    assert len(final_events) == 1
    assert final_events[0]["data"]["text"] == "I will keep waiting for the install."


def test_ceo_websocket_final_reply_includes_current_turn_user_messages(tmp_path: Path, monkeypatch) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)
    session_id = "web:ceo-live-final-user-batch"

    class _FakeUserBatchFinalSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(status="idle", is_running=False)
            self._listeners = set()

        def subscribe(self, listener):
            self._listeners.add(listener)

            def _unsubscribe() -> None:
                self._listeners.discard(listener)

            return _unsubscribe

        def state_dict(self) -> dict[str, object]:
            return {"status": self.state.status, "is_running": self.state.is_running}

        def inflight_turn_snapshot(self):
            return {
                "turn_id": "turn-batch-final",
                "source": "user",
                "status": "running",
                "user_message": {"content": "Queued follow-up"},
                "user_messages": [
                    {"role": "user", "content": "Original request"},
                    {"role": "user", "content": "Queued follow-up"},
                ],
            }

        async def _emit(self, event_type: str, **payload) -> None:
            event = AgentEvent(type=event_type, timestamp="2026-04-21T12:00:00", payload=payload)
            for listener in list(self._listeners):
                result = listener(event)
                if hasattr(result, "__await__"):
                    await result

        async def prompt(self, user_message) -> SimpleNamespace:
            _ = user_message
            self.state.status = "running"
            self.state.is_running = True
            await self._emit("state_snapshot", state=self.state_dict())
            await self._emit(
                "message_end",
                role="assistant",
                text="Final answer",
                source="user",
                turn_id="turn-batch-final",
            )
            self.state.status = "completed"
            self.state.is_running = False
            await self._emit("state_snapshot", state=self.state_dict())
            return SimpleNamespace(output="Final answer")

    session_manager = SessionManager(tmp_path)
    live_session = _FakeUserBatchFinalSession()
    agent = SimpleNamespace(
        sessions=session_manager,
        main_task_service=_TaskService(),
    )
    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: _RuntimeManager(live_session))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "snapshot.ceo"
        assert ws.receive_json()["type"] == "ceo.state"

        ws.send_json({"type": "client.user_message", "text": "Original request"})

        final_payload, _seen = _recv_until(
            ws,
            lambda payload: payload.get("type") == "ceo.reply.final",
        )

    assert final_payload["data"]["text"] == "Final answer"
    assert [item["content"] for item in final_payload["data"]["user_messages"]] == [
        "Original request",
        "Queued follow-up",
    ]


def test_ceo_websocket_forwards_cron_heartbeat_ok_as_internal_ack(tmp_path: Path, monkeypatch) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    class _FakeCronAckSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(status="idle", is_running=False)
            self._listeners = set()

        def subscribe(self, listener):
            self._listeners.add(listener)

            def _unsubscribe() -> None:
                self._listeners.discard(listener)

            return _unsubscribe

        def state_dict(self) -> dict[str, object]:
            return {"status": self.state.status, "is_running": self.state.is_running}

        def inflight_turn_snapshot(self):
            return None

        async def _emit(self, event_type: str, **payload) -> None:
            event = AgentEvent(type=event_type, timestamp="2026-03-18T12:00:00", payload=payload)
            for listener in list(self._listeners):
                result = listener(event)
                if hasattr(result, "__await__"):
                    await result

        async def prompt(self, user_message) -> SimpleNamespace:
            _ = user_message
            self.state.status = "running"
            self.state.is_running = True
            await self._emit("state_snapshot", state=self.state_dict())
            await self._emit(
                "message_end",
                role="assistant",
                text=HEARTBEAT_OK,
                source="cron",
                turn_id="turn-cron-ack",
            )
            self.state.status = "completed"
            self.state.is_running = False
            await self._emit("state_snapshot", state=self.state_dict())
            return SimpleNamespace(output=HEARTBEAT_OK)

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)
    session_id = "web:ceo-cron-ack"
    session_manager = SessionManager(tmp_path)
    live_session = _FakeCronAckSession()
    agent = SimpleNamespace(
        sessions=session_manager,
        main_task_service=_TaskService(),
    )
    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: _RuntimeManager(live_session))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "snapshot.ceo"
        assert ws.receive_json()["type"] == "ceo.state"

        ws.send_json({"type": "client.user_message", "text": "run cron turn"})

        messages = []
        for _ in range(6):
            payload = ws.receive_json()
            messages.append(payload)
            if payload.get("type") == "ceo.internal.ack":
                break

    ack_events = [item for item in messages if item["type"] == "ceo.internal.ack"]
    assert len(ack_events) == 1
    assert ack_events[0]["data"]["source"] == "cron"
    assert ack_events[0]["data"]["reason"] == "heartbeat_ok"
    assert ack_events[0]["data"]["turn_id"] == "turn-cron-ack"


def test_ceo_websocket_resume_interrupt_forwards_resume_payload(tmp_path, monkeypatch) -> None:
    class _ResumeSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(
                status="paused",
                is_running=False,
                pending_interrupts=[{"id": "interrupt-1", "value": {"kind": "frontdoor_tool_approval"}}],
            )
            self.resume_payloads: list[object] = []
            self._listeners = set()

        def subscribe(self, listener):
            self._listeners.add(listener)
            return lambda: self._listeners.discard(listener)

        def state_dict(self) -> dict[str, object]:
            return {
                "status": self.state.status,
                "is_running": self.state.is_running,
                "pending_interrupts": list(self.state.pending_interrupts),
            }

        def inflight_turn_snapshot(self):
            return {"status": "paused", "interrupts": list(self.state.pending_interrupts)}

        async def resume_frontdoor_interrupt(self, *, resume_value):
            self.resume_payloads.append(resume_value)
            self.state.status = "completed"
            self.state.pending_interrupts = []
            return SimpleNamespace(output="")

    live_session = _ResumeSession()
    session_manager = SessionManager(tmp_path)
    session_manager.get_or_create("web:shared")
    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")

    monkeypatch.setattr(
        websocket_ceo,
        "get_agent",
        lambda: SimpleNamespace(
            sessions=session_manager,
            main_task_service=SimpleNamespace(
                startup=lambda: None,
                registry=SimpleNamespace(
                    subscribe_ceo=lambda _session_id: asyncio.Queue(),
                    subscribe_global_ceo=lambda: asyncio.Queue(),
                    unsubscribe_ceo=lambda _session_id, _queue: None,
                    unsubscribe_global_ceo=lambda _queue: None,
                    next_ceo_seq=lambda _session_id: 1,
                    publish_global_ceo=lambda _envelope: None,
                ),
            ),
        ),
    )
    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", lambda _agent: None)
    monkeypatch.setattr(
        websocket_ceo,
        "get_runtime_manager",
        lambda _agent: SimpleNamespace(get_or_create=lambda **kwargs: live_session, get=lambda _key: live_session),
    )
    monkeypatch.setattr(websocket_ceo, "workspace_path", lambda: tmp_path)

    client = TestClient(app)
    with client.websocket_connect("/api/ws/ceo?session_id=web:shared") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "client.resume_interrupt", "resume": {"approved": True}})

    assert live_session.resume_payloads == [{"approved": True}]


def test_ceo_websocket_user_message_after_manual_pause_starts_fresh_turn(tmp_path, monkeypatch) -> None:
    class _PausedResumeSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(status="paused", is_running=False, pending_interrupts=[])
            self.resume_payloads: list[dict[str, object]] = []
            self.prompt_payloads: list[object] = []
            self.prompt_batch_payloads: list[list[object]] = []
            self._listeners = set()

        def subscribe(self, listener):
            self._listeners.add(listener)
            return lambda: self._listeners.discard(listener)

        def state_dict(self) -> dict[str, object]:
            return {
                "status": self.state.status,
                "is_running": self.state.is_running,
                "pending_interrupts": list(self.state.pending_interrupts),
            }

        def inflight_turn_snapshot(self):
            return {
                "status": "paused",
                "source": "user",
                "turn_id": "turn-paused-original",
                "user_message": {"content": "整理介绍今天GitHub上最热门项目给我"},
            }

        def paused_execution_context_snapshot(self):
            return {
                "status": "paused",
                "source": "user",
                "turn_id": "turn-paused-original",
                "user_message": {"content": "整理介绍今天GitHub上最热门项目给我"},
            }

        async def resume(self, *, additional_context: str | None = None, replan: bool = False):
            self.resume_payloads.append({
                "additional_context": additional_context,
                "replan": replan,
            })
            return SimpleNamespace(output="继续处理原请求")

        async def prompt(self, user_message):
            self.prompt_payloads.append(user_message)
            return SimpleNamespace(output="正确地新开了一轮")

        async def prompt_batch(self, user_messages):
            self.prompt_batch_payloads.append(list(user_messages or []))
            return SimpleNamespace(output="正确地批量新开了一轮")

    live_session = _PausedResumeSession()
    session_manager = SessionManager(tmp_path)
    session_manager.get_or_create("web:shared")
    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")

    monkeypatch.setattr(
        websocket_ceo,
        "get_agent",
        lambda: SimpleNamespace(
            sessions=session_manager,
            main_task_service=SimpleNamespace(
                startup=lambda: None,
                registry=SimpleNamespace(
                    subscribe_ceo=lambda _session_id: asyncio.Queue(),
                    subscribe_global_ceo=lambda: asyncio.Queue(),
                    unsubscribe_ceo=lambda _session_id, _queue: None,
                    unsubscribe_global_ceo=lambda _queue: None,
                    next_ceo_seq=lambda _session_id: 1,
                    publish_global_ceo=lambda _envelope: None,
                ),
            ),
        ),
    )
    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", lambda _agent: None)
    monkeypatch.setattr(
        websocket_ceo,
        "get_runtime_manager",
        lambda _agent: SimpleNamespace(get_or_create=lambda **kwargs: live_session, get=lambda _key: live_session),
    )
    monkeypatch.setattr(websocket_ceo, "workspace_path", lambda: tmp_path)

    client = TestClient(app)
    with client.websocket_connect("/api/ws/ceo?session_id=web:shared") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "client.user_message", "text": "前10个"})

    assert live_session.resume_payloads == []
    assert len(live_session.prompt_payloads) == 1
    assert websocket_ceo._history_text(live_session.prompt_payloads[0].content) == "前10个"
    assert live_session.prompt_batch_payloads == []


def test_ceo_websocket_blocks_user_message_while_tool_approval_batch_pending(tmp_path, monkeypatch) -> None:
    class _ApprovalPendingSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(
                status="paused",
                is_running=False,
                pending_interrupts=[
                    {
                        "id": "interrupt-batch-1",
                        "value": {
                            "kind": "frontdoor_tool_approval_batch",
                            "batch_id": "batch:123",
                            "review_items": [
                                {
                                    "tool_call_id": "call-1",
                                    "name": "exec",
                                    "risk_level": "high",
                                    "arguments": {"command": "echo hi"},
                                }
                            ],
                        },
                    }
                ],
            )
            self.prompt_payloads: list[object] = []
            self.prompt_batch_payloads: list[list[object]] = []
            self.queued_follow_up_payloads: list[list[object]] = []
            self._listeners = set()

        def subscribe(self, listener):
            self._listeners.add(listener)
            return lambda: self._listeners.discard(listener)

        def state_dict(self) -> dict[str, object]:
            return {
                "status": self.state.status,
                "is_running": self.state.is_running,
                "pending_interrupts": list(self.state.pending_interrupts),
            }

        def inflight_turn_snapshot(self):
            return {
                "status": "paused",
                "source": "user",
                "turn_id": "turn-approval-1",
                "interrupts": list(self.state.pending_interrupts),
            }

        def paused_execution_context_snapshot(self):
            return {
                "status": "paused",
                "source": "user",
                "turn_id": "turn-approval-1",
                "interrupts": list(self.state.pending_interrupts),
            }

        async def prompt(self, user_message):
            self.prompt_payloads.append(user_message)
            raise RuntimeError("prompt should not run")

        async def prompt_batch(self, user_messages):
            self.prompt_batch_payloads.append(list(user_messages or []))
            raise RuntimeError("prompt batch should not run")

        async def queue_follow_up_batch(self, user_messages, persist_transcript: bool = True):
            _ = persist_transcript
            batch = list(user_messages or [])
            self.queued_follow_up_payloads.append(batch)
            return batch

    live_session = _ApprovalPendingSession()
    session_manager = SessionManager(tmp_path)
    session_manager.get_or_create("web:shared")
    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")

    monkeypatch.setattr(
        websocket_ceo,
        "get_agent",
        lambda: SimpleNamespace(
            sessions=session_manager,
            main_task_service=SimpleNamespace(
                startup=lambda: None,
                registry=SimpleNamespace(
                    subscribe_ceo=lambda _session_id: asyncio.Queue(),
                    subscribe_global_ceo=lambda: asyncio.Queue(),
                    unsubscribe_ceo=lambda _session_id, _queue: None,
                    unsubscribe_global_ceo=lambda _queue: None,
                    next_ceo_seq=lambda _session_id: 1,
                    publish_global_ceo=lambda _envelope: None,
                ),
            ),
        ),
    )
    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", lambda _agent: None)
    monkeypatch.setattr(
        websocket_ceo,
        "get_runtime_manager",
        lambda _agent: SimpleNamespace(get_or_create=lambda **kwargs: live_session, get=lambda _key: live_session),
    )
    monkeypatch.setattr(websocket_ceo, "workspace_path", lambda: tmp_path)

    client = TestClient(app)
    with client.websocket_connect("/api/ws/ceo?session_id=web:shared") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "client.user_message", "text": "blocked while approval pending"})
        error_payload, _seen = _recv_until(
            ws,
            lambda payload: payload.get("type") in {"error", "ceo.error"},
        )

    assert error_payload["data"]["code"] == "ceo_approval_pending"
    assert live_session.prompt_payloads == []
    assert live_session.prompt_batch_payloads == []
    assert live_session.queued_follow_up_payloads == []


def test_ceo_websocket_blocks_user_message_when_pending_batch_only_exists_in_paused_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    _mock_workspace(monkeypatch, tmp_path)
    session_id = "web:shared"
    web_ceo_sessions.write_paused_execution_context(
        session_id,
        {
            "status": "paused",
            "interrupts": [
                {
                    "id": "interrupt-disk-approval-1",
                    "value": {
                        "kind": "frontdoor_tool_approval_batch",
                        "batch_id": "batch:disk-1",
                        "review_items": [
                            {
                                "tool_call_id": "call-1",
                                "name": "exec",
                                "risk_level": "high",
                                "arguments": {"command": "echo hi"},
                            }
                        ],
                    },
                }
            ],
        },
    )

    class _DiskOnlyApprovalPendingSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(status="paused", is_running=False, pending_interrupts=[])
            self.prompt_payloads: list[object] = []
            self._listeners = set()

        def subscribe(self, listener):
            self._listeners.add(listener)
            return lambda: self._listeners.discard(listener)

        def state_dict(self) -> dict[str, object]:
            return {
                "status": self.state.status,
                "is_running": self.state.is_running,
                "pending_interrupts": list(self.state.pending_interrupts),
            }

        def inflight_turn_snapshot(self):
            return None

        def paused_execution_context_snapshot(self):
            return None

        async def prompt(self, user_message):
            self.prompt_payloads.append(user_message)
            raise RuntimeError("prompt should not run")

    live_session = _DiskOnlyApprovalPendingSession()
    session_manager = SessionManager(tmp_path)
    session_manager.save(session_manager.get_or_create(session_id))
    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")

    monkeypatch.setattr(
        websocket_ceo,
        "get_agent",
        lambda: SimpleNamespace(
            sessions=session_manager,
            main_task_service=SimpleNamespace(
                startup=lambda: None,
                registry=SimpleNamespace(
                    subscribe_ceo=lambda _session_id: asyncio.Queue(),
                    subscribe_global_ceo=lambda: asyncio.Queue(),
                    unsubscribe_ceo=lambda _session_id, _queue: None,
                    unsubscribe_global_ceo=lambda _queue: None,
                    next_ceo_seq=lambda _session_id: 1,
                    publish_global_ceo=lambda _envelope: None,
                ),
            ),
        ),
    )
    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", lambda _agent: None)
    monkeypatch.setattr(
        websocket_ceo,
        "get_runtime_manager",
        lambda _agent: SimpleNamespace(get_or_create=lambda **kwargs: live_session, get=lambda _key: live_session),
    )

    client = TestClient(app)
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "client.user_message", "text": "blocked from paused snapshot"})
        error_payload, _seen = _recv_until(
            ws,
            lambda payload: payload.get("type") in {"error", "ceo.error"},
        )

    assert error_payload["data"]["code"] == "ceo_approval_pending"
    assert live_session.prompt_payloads == []


def test_ceo_websocket_user_message_batch_payload_dispatches_single_fresh_batch_turn(tmp_path, monkeypatch) -> None:
    class _BatchSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(status="idle", is_running=False, pending_interrupts=[])
            self.prompt_payloads: list[object] = []
            self.prompt_batch_payloads: list[list[object]] = []
            self._listeners = set()

        def subscribe(self, listener):
            self._listeners.add(listener)
            return lambda: self._listeners.discard(listener)

        def state_dict(self) -> dict[str, object]:
            return {
                "status": self.state.status,
                "is_running": self.state.is_running,
                "pending_interrupts": list(self.state.pending_interrupts),
            }

        def inflight_turn_snapshot(self):
            return None

        async def prompt(self, user_message):
            self.prompt_payloads.append(user_message)
            return SimpleNamespace(output="single")

        async def prompt_batch(self, user_messages):
            self.prompt_batch_payloads.append(list(user_messages or []))
            return SimpleNamespace(output="batch")

    live_session = _BatchSession()
    session_manager = SessionManager(tmp_path)
    session_manager.get_or_create("web:shared")
    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")

    monkeypatch.setattr(
        websocket_ceo,
        "get_agent",
        lambda: SimpleNamespace(
            sessions=session_manager,
            main_task_service=SimpleNamespace(
                startup=lambda: None,
                registry=SimpleNamespace(
                    subscribe_ceo=lambda _session_id: asyncio.Queue(),
                    subscribe_global_ceo=lambda: asyncio.Queue(),
                    unsubscribe_ceo=lambda _session_id, _queue: None,
                    unsubscribe_global_ceo=lambda _queue: None,
                    next_ceo_seq=lambda _session_id: 1,
                    publish_global_ceo=lambda _envelope: None,
                ),
            ),
        ),
    )
    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", lambda _agent: None)
    monkeypatch.setattr(
        websocket_ceo,
        "get_runtime_manager",
        lambda _agent: SimpleNamespace(get_or_create=lambda **kwargs: live_session, get=lambda _key: live_session),
    )
    monkeypatch.setattr(websocket_ceo, "workspace_path", lambda: tmp_path)

    client = TestClient(app)
    with client.websocket_connect("/api/ws/ceo?session_id=web:shared") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()
        ws.send_json(
            {
                "type": "client.user_message",
                "messages": [
                    {"text": "先补齐 skill 元数据"},
                    {"text": "再检查 filesystem_write 是否可用"},
                ],
            }
        )

    assert live_session.prompt_payloads == []
    assert len(live_session.prompt_batch_payloads) == 1
    assert [
        websocket_ceo._history_text(item.content)
        for item in live_session.prompt_batch_payloads[0]
    ] == [
        "先补齐 skill 元数据",
        "再检查 filesystem_write 是否可用",
    ]


def test_ceo_websocket_turn_patch_carries_live_execution_trace_summary(tmp_path: Path, monkeypatch) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)

    class _ToolPatchSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(status="idle", is_running=False)
            self._listeners = set()
            self._snapshot = None

        def subscribe(self, listener):
            self._listeners.add(listener)

            def _unsubscribe() -> None:
                self._listeners.discard(listener)

            return _unsubscribe

        def state_dict(self) -> dict[str, object]:
            return {"status": self.state.status, "is_running": self.state.is_running}

        def inflight_turn_snapshot(self):
            return copy.deepcopy(self._snapshot)

        async def _emit(self, event_type: str, **payload) -> None:
            event = AgentEvent(type=event_type, timestamp="2026-03-18T12:00:00", payload=payload)
            for listener in list(self._listeners):
                result = listener(event)
                if hasattr(result, "__await__"):
                    await result

        async def prompt(self, user_message) -> SimpleNamespace:
            _ = user_message
            self.state.status = "running"
            self.state.is_running = True
            self._snapshot = {
                "turn_id": "turn-user-live",
                "status": "running",
                "source": "user",
                "user_message": {"content": "Install the skill"},
                "assistant_text": "Working on it...",
                "execution_trace_summary": {},
            }
            await self._emit("state_snapshot", state=self.state_dict())
            self._snapshot["execution_trace_summary"] = {
                "active_stage_id": "inflight-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "inflight-stage-1",
                        "stage_index": 1,
                        "stage_goal": "",
                        "tool_round_budget": 0,
                        "tool_rounds_used": 1,
                        "status": "active",
                        "mode": "自主执行",
                        "stage_kind": "normal",
                        "system_generated": True,
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "archive_ref": "",
                        "archive_stage_index_start": 0,
                        "archive_stage_index_end": 0,
                        "rounds": [
                            {
                                "round_index": 1,
                                "tools": [
                                    {
                                        "status": "running",
                                        "tool_name": "skill-installer",
                                        "text": "skill-installer started",
                                        "tool_call_id": "skill-installer:1",
                                        "source": "user",
                                    }
                                ],
                            }
                        ],
                        "created_at": "",
                        "finished_at": "",
                    }
                ],
            }
            await self._emit(
                "tool_execution_start",
                tool_name="skill-installer",
                tool_call_id="skill-installer:1",
                text="skill-installer started",
                source="user",
            )
            await self._emit("message_end", role="assistant", text="Still working.", source="user")
            self.state.status = "completed"
            self.state.is_running = False
            self._snapshot = None
            await self._emit("state_snapshot", state=self.state_dict())
            return SimpleNamespace(output="Still working.")

    session_id = "web:ceo-tool-patch"
    session_manager = SessionManager(tmp_path)
    live_session = _ToolPatchSession()
    agent = SimpleNamespace(
        sessions=session_manager,
        main_task_service=_TaskService(),
    )
    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: _RuntimeManager(live_session))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        _recv_until(ws, lambda payload: payload.get("type") == "ceo.sessions.snapshot")

        ws.send_json({"type": "client.user_message", "text": "Install the skill"})

        patch_payload, _seen = _recv_until(
            ws,
            lambda payload: payload.get("type") == "ceo.turn.patch"
            and isinstance(payload.get("data", {}).get("inflight_turn"), dict)
            and isinstance(payload.get("data", {}).get("inflight_turn", {}).get("execution_trace_summary"), dict)
            and list(
                (payload.get("data", {}).get("inflight_turn", {}).get("execution_trace_summary", {}) or {})
                .get("stages")
                or []
            ),
        )
        inflight_turn = patch_payload["data"]["inflight_turn"]
        assert inflight_turn["turn_id"] == "turn-user-live"
        assert inflight_turn["assistant_text"] == "Working on it..."
        tool = inflight_turn["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"][0]
        assert tool["tool_name"] == "skill-installer"
        assert tool["tool_call_id"] == "skill-installer:1"
        assert "interaction_trace" not in inflight_turn
        assert "stage" not in inflight_turn

        final_payload, _seen = _recv_until(ws, lambda payload: payload.get("type") == "ceo.reply.final")

    assert final_payload["data"]["text"] == "Still working."
    assert final_payload["data"]["turn_id"] == "turn-user-live"


def test_ceo_websocket_error_payload_omits_legacy_interaction_trace(tmp_path: Path, monkeypatch) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)
    session_id = "web:ceo-live-error-trace"
    session_manager = SessionManager(tmp_path)
    live_session = _FakeErrorSession()
    agent = SimpleNamespace(
        sessions=session_manager,
        main_task_service=_TaskService(),
    )
    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: _RuntimeManager(live_session))

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "snapshot.ceo"
        assert ws.receive_json()["type"] == "ceo.state"

        ws.send_json({"type": "client.user_message", "text": "Open bilibili"})

        messages = []
        for _ in range(6):
            payload = ws.receive_json()
            messages.append(payload)
            if payload.get("type") == "ceo.error":
                break

    error_events = [item for item in messages if item["type"] == "ceo.error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["message"] == "CEO frontdoor exceeded maximum iterations"
    assert error_events[0]["data"]["source"] == "user"
    assert "interaction_trace" not in error_events[0]["data"]


def test_ceo_websocket_manual_pause_restores_paused_inflight_turn_without_final_reply(tmp_path: Path, monkeypatch) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)
    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)

    class _CancelToken:
        def cancel(self, *, reason: str = "") -> None:
            _ = reason

    class _PauseableRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input
            await on_progress(
                "skill-installer started",
                event_kind="tool_start",
                event_data={"tool_name": "skill-installer"},
            )
            session.state.latest_message = "Working on it..."
            session._frontdoor_stage_state = {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Pause and restore me",
                        "status": "active",
                        "rounds": [
                            {
                                "round_index": 1,
                                "tool_names": ["skill-installer"],
                                "tool_call_ids": ["skill-installer:1"],
                                "tools": [
                                    {
                                        "tool_call_id": "skill-installer:1",
                                        "tool_name": "skill-installer",
                                        "status": "running",
                                        "source": "user",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            await session._emit_state_snapshot()
            await asyncio.Future()

    class _AgentLoopStub:
        def __init__(self, workspace: Path) -> None:
            self.model = "gpt-test"
            self.reasoning_effort = None
            self.sessions = SessionManager(workspace)
            self.main_task_service = _TaskService()
            self.multi_agent_runner = _PauseableRunner()
            self.memory_manager = None
            self.commit_service = None
            self.prompt_trace = False
            self._active_tasks: dict[str, set[asyncio.Task[object]]] = {}
            self.web_session_heartbeat = _HeartbeatController()

        def create_session_cancellation_token(self, _session_key: str):
            return _CancelToken()

        def release_session_cancellation_token(self, _session_key: str, _token) -> None:
            return None

        def _register_active_task(self, session_key: str, task: asyncio.Task[object]) -> None:
            bucket = self._active_tasks.setdefault(str(session_key or ""), set())
            bucket.add(task)

        async def cancel_session_tasks(self, session_key: str) -> int:
            key = str(session_key or "")
            tasks = list(self._active_tasks.pop(key, set()))
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            return len(tasks)

        def _use_rag_memory(self) -> bool:
            return False

    session_id = "web:ceo-pause-reconnect"
    agent = _AgentLoopStub(tmp_path)
    runtime_manager = SessionRuntimeManager(agent)
    holder = SimpleNamespace(manager=runtime_manager)

    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: holder.manager)

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        _recv_until(ws, lambda payload: payload.get("type") == "ceo.sessions.snapshot")

        ws.send_json({"type": "client.user_message", "text": "Pause and restore me"})

        patch_payload, _tool_seen = _recv_until(
            ws,
            lambda payload: payload.get("type") == "ceo.turn.patch"
            and isinstance(payload.get("data", {}).get("inflight_turn"), dict)
            and list(
                ((payload.get("data", {}).get("inflight_turn", {}) or {}).get("execution_trace_summary", {}) or {})
                .get("stages")
                or []
            ),
        )
        inflight_turn = patch_payload["data"]["inflight_turn"]
        tool = inflight_turn["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"][0]
        assert tool["tool_name"] == "skill-installer"
        assert tool["source"] == "user"
        assert "interaction_trace" not in inflight_turn
        assert "stage" not in inflight_turn

        ws.send_json({"type": "client.pause_turn"})

        paused_state, seen = _recv_until(
            ws,
            lambda payload: payload.get("type") == "ceo.state"
            and str(payload.get("data", {}).get("state", {}).get("status") or "") == "completed",
        )
        pause_ack = next(item for item in seen if item.get("type") == "ceo.control_ack")
        assert pause_ack["data"]["source"] == "user"
        assert all(item.get("type") != "ceo.reply.final" for item in seen)

    holder.manager = SessionRuntimeManager(agent)

    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        snapshot, _seen = _recv_until(ws, lambda payload: payload.get("type") == "snapshot.ceo")

    inflight_turn = snapshot["data"].get("inflight_turn")
    assert inflight_turn is None
    assert [message["role"] for message in snapshot["data"].get("messages", [])] == ["user", "assistant"]
    assert snapshot["data"]["messages"][0]["content"] == "Pause and restore me"
    assert snapshot["data"]["messages"][1]["status"] == "paused"
    persisted = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in persisted.messages] == ["user", "assistant"]
    assert persisted.messages[0]["content"] == "Pause and restore me"
    assert persisted.messages[1]["status"] == "paused"
    assert persisted.messages[1]["metadata"]["source"] == "manual_pause_archive"
    assert agent.web_session_heartbeat.clear_calls == []


def test_ceo_websocket_does_not_restore_terminal_error_inflight_snapshot_from_disk(tmp_path: Path, monkeypatch) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)

    session_id = "web:ceo-stale-error"
    agent = SimpleNamespace(
        sessions=SessionManager(tmp_path),
        main_task_service=_TaskService(),
    )
    live_session = _FakeLiveSession()
    runtime_manager = SimpleNamespace(
        get=lambda key: live_session if str(key or "") == session_id else None,
        get_or_create=lambda **kwargs: live_session,
    )

    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: runtime_manager)

    web_ceo_sessions.write_inflight_turn_snapshot(
        session_id,
        {
            "status": "error",
            "assistant_text": "运行出错：CEO frontdoor exceeded maximum iterations",
            "last_error": {"message": "CEO frontdoor exceeded maximum iterations"},
        },
    )

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        for _ in range(10):
            snapshot = ws.receive_json()
            if snapshot.get("type") == "snapshot.ceo":
                break
        else:
            raise AssertionError("Did not receive snapshot.ceo payload")

    assert snapshot["data"].get("inflight_turn") is None


def test_ceo_websocket_unknown_local_session_falls_back_to_existing_active_session(tmp_path: Path, monkeypatch) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)

    session_manager = SessionManager(tmp_path)
    active_session_id = "web:ceo-existing"
    existing_session = web_ceo_sessions.create_web_ceo_session(session_manager, session_id=active_session_id)
    existing_session.add_message("user", "Keep the original context")
    existing_session.add_message("assistant", "Still here")
    session_manager.save(existing_session)
    web_ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(active_session_id)

    live_session = _FakeLiveSession()
    agent = SimpleNamespace(
        sessions=session_manager,
        main_task_service=_TaskService(),
    )
    runtime_manager = SimpleNamespace(
        get=lambda key: live_session if str(key or "") == active_session_id else None,
        get_or_create=lambda **kwargs: live_session,
    )

    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: runtime_manager)

    missing_session_id = "web:ceo-missing"
    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={missing_session_id}") as ws:
        hello = ws.receive_json()
        snapshot = ws.receive_json()
        state = ws.receive_json()
        sessions_snapshot = ws.receive_json()

    assert hello["type"] == "hello"
    assert hello["session_id"] == active_session_id
    assert snapshot["type"] == "snapshot.ceo"
    assert snapshot["session_id"] == active_session_id
    assert state["type"] == "ceo.state"
    assert state["session_id"] == active_session_id
    assert sessions_snapshot["type"] == "ceo.sessions.snapshot"
    assert sessions_snapshot["data"]["active_session_id"] == active_session_id
    assert [message["role"] for message in snapshot["data"]["messages"]] == ["user", "assistant"]
    assert snapshot["data"]["messages"][0]["content"] == "Keep the original context"
    assert snapshot["data"]["messages"][1]["content"] == "Still here"
    assert not session_manager.get_path(missing_session_id).exists()


@pytest.mark.asyncio
async def test_ceo_websocket_queues_running_turn_follow_up_and_chains_next_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _mock_workspace(monkeypatch, tmp_path)

    async def _ensure_services(_agent) -> None:
        return None

    monkeypatch.setattr(websocket_ceo, "ensure_web_runtime_services", _ensure_services)

    class _FollowUpChainingSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(
                status="idle",
                is_running=False,
                paused=False,
                pending_interrupts=[],
            )
            self._listeners = set()
            self.prompt_payloads: list[UserInputMessage] = []
            self.prompt_batch_payloads: list[list[UserInputMessage]] = []
            self.queued_follow_up_payloads: list[list[UserInputMessage]] = []
            self._queued_follow_ups: list[UserInputMessage] = []
            self.allow_first_turn_finish = asyncio.Event()

        def subscribe(self, listener):
            self._listeners.add(listener)

            def _unsubscribe() -> None:
                self._listeners.discard(listener)

            return _unsubscribe

        def state_dict(self) -> dict[str, object]:
            return {"status": self.state.status, "is_running": self.state.is_running}

        def inflight_turn_snapshot(self):
            return None

        async def _emit(self, event_type: str, **payload) -> None:
            event = AgentEvent(type=event_type, timestamp="2026-04-19T12:00:00", payload=payload)
            for listener in list(self._listeners):
                result = listener(event)
                if hasattr(result, "__await__"):
                    await result

        async def prompt(self, user_message) -> SimpleNamespace:
            self.prompt_payloads.append(user_message)
            self.state.status = "running"
            self.state.is_running = True
            await self._emit("state_snapshot", state=self.state_dict())
            if len(self.prompt_payloads) == 1:
                await self.allow_first_turn_finish.wait()
            self.state.status = "completed"
            self.state.is_running = False
            await self._emit(
                "message_end",
                role="assistant",
                text="first reply" if len(self.prompt_payloads) == 1 else "second reply",
                source="user",
                turn_id="turn-1" if len(self.prompt_payloads) == 1 else "turn-2",
            )
            await self._emit("state_snapshot", state=self.state_dict())
            return SimpleNamespace(output="first reply" if len(self.prompt_payloads) == 1 else "second reply")

        async def prompt_batch(self, user_messages) -> SimpleNamespace:
            self.prompt_batch_payloads.append(list(user_messages))
            self.state.status = "running"
            self.state.is_running = True
            await self._emit("state_snapshot", state=self.state_dict())
            self.state.status = "completed"
            self.state.is_running = False
            await self._emit(
                "message_end",
                role="assistant",
                text="second reply",
                source="user",
                turn_id="turn-2",
            )
            await self._emit("state_snapshot", state=self.state_dict())
            return SimpleNamespace(output="second reply")

        async def queue_follow_up_batch(self, user_messages, persist_transcript: bool = True):
            _ = persist_transcript
            batch = list(user_messages or [])
            self.queued_follow_up_payloads.append(batch)
            self._queued_follow_ups.extend(batch)
            return batch

        def drain_queued_follow_up_messages(self) -> list[UserInputMessage]:
            batch = list(self._queued_follow_ups)
            self._queued_follow_ups = []
            return batch

    session_id = "web:ceo-running-follow-up"
    live_session = _FollowUpChainingSession()
    agent = SimpleNamespace(
        sessions=SessionManager(tmp_path),
        main_task_service=_TaskService(),
    )
    runtime_manager = SimpleNamespace(
        get=lambda key: live_session if str(key or "") == session_id else None,
        get_or_create=lambda **kwargs: live_session,
    )

    monkeypatch.setattr(websocket_ceo, "get_agent", lambda: agent)
    monkeypatch.setattr(websocket_ceo, "get_runtime_manager", lambda _agent=None: runtime_manager)

    client = TestClient(_build_app())
    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        _recv_until(ws, lambda payload: payload.get("type") == "ceo.sessions.snapshot")

        ws.send_json({"type": "client.user_message", "text": "Original request"})
        _recv_until(
            ws,
            lambda payload: payload.get("type") == "ceo.state"
            and str(payload.get("data", {}).get("state", {}).get("status") or "") == "running",
        )

        ws.send_json({"type": "client.user_message", "text": "Queued follow-up"})
        live_session.allow_first_turn_finish.set()

        second_reply, seen = _recv_until(
            ws,
            lambda payload: payload.get("type") == "ceo.reply.final"
            and str(payload.get("data", {}).get("text") or "") == "second reply",
            limit=40,
        )

    assert second_reply["data"]["text"] == "second reply"
    assert len(live_session.queued_follow_up_payloads) == 1
    assert websocket_ceo._history_text(live_session.queued_follow_up_payloads[0][0].content) == "Queued follow-up"
    assert [websocket_ceo._history_text(item.content) for item in live_session.prompt_payloads] == [
        "Original request",
        "Queued follow-up",
    ]
    assert live_session.prompt_batch_payloads == []
    assert all(
        not (
            payload.get("type") == "error"
            and str(payload.get("data", {}).get("code") or "") == "ceo_turn_in_progress"
        )
        for payload in seen
    )


def test_ceo_websocket_filters_only_silent_internal_ack_message_end() -> None:
    assert websocket_ceo._should_forward_message_end(
        {"role": "assistant", "text": "normal reply", "heartbeat_internal": False}
    ) is True
    assert websocket_ceo._should_forward_message_end(
        {"role": "assistant", "text": "visible heartbeat reply", "heartbeat_internal": True, "source": "heartbeat"}
    ) is True
    assert websocket_ceo._should_forward_message_end(
        {"role": "assistant", "text": HEARTBEAT_OK, "heartbeat_internal": True}
    ) is False
    assert websocket_ceo._should_forward_message_end(
        {"role": "assistant", "text": HEARTBEAT_OK, "source": "cron"}
    ) is True
    assert websocket_ceo._should_forward_message_end(
        {"role": "assistant", "text": HEARTBEAT_OK}
    ) is False
    assert websocket_ceo._is_internal_ack_message_end(
        {"role": "assistant", "text": HEARTBEAT_OK, "source": "heartbeat", "heartbeat_reason": "task_terminal"}
    ) is False
    assert websocket_ceo._is_internal_ack_message_end(
        {"role": "assistant", "text": HEARTBEAT_OK, "source": "heartbeat", "heartbeat_reason": "tool_background"}
    ) is True


def test_ceo_upload_endpoint_rejects_oversized_image(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(websocket_ceo, "workspace_path", lambda: tmp_path)

    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")
    client = TestClient(app)

    response = client.post(
        "/api/ceo/uploads?session_id=web:shared",
        files=[
            (
                "files",
                (
                    "huge.png",
                    b"0" * (web_ceo_sessions.WEB_CEO_IMAGE_UPLOAD_MAX_BYTES + 1),
                    "image/png",
                ),
            )
        ],
    )

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["code"] == "image_upload_too_large"
    assert detail["limit_bytes"] == web_ceo_sessions.WEB_CEO_IMAGE_UPLOAD_MAX_BYTES


def test_ceo_tool_event_serializers_preserve_source() -> None:
    serialized = websocket_ceo._serialize_tool_event(
        AgentEvent(
            type="tool_execution_start",
            timestamp="2026-03-28T01:00:00",
            payload={"tool_name": "skill-installer", "text": "started", "source": "heartbeat"},
        )
    )

    assert serialized is not None
    assert serialized["source"] == "heartbeat"


def test_ceo_tool_event_serializer_falls_back_to_output_text_when_text_is_blank() -> None:
    serialized = websocket_ceo._serialize_tool_event(
        AgentEvent(
            type="tool_execution_end",
            timestamp="2026-04-21T10:00:00",
            payload={
                "tool_name": "load_tool_context",
                "text": "",
                "source": "user",
                "data": {
                    "tool_name": "load_tool_context",
                    "output_text": '{"tool_id":"filesystem_write"}',
                    "tool_call_id": "call-load-tool-1",
                },
            },
        )
    )

    assert serialized is not None
    assert serialized["status"] == "success"
    assert serialized["text"] == '{"tool_id":"filesystem_write"}'
    assert serialized["tool_call_id"] == "call-load-tool-1"


def test_execution_trace_snapshot_helpers_extract_task_ids_preview_and_updated_at() -> None:
    snapshot = {
        "assistant_text": "",
        "canonical_context": {
            "stages": [
                {
                    "rounds": [
                        {
                            "tools": [
                                {
                                    "tool_name": "create_async_task",
                                    "status": "success",
                                    "output_text": "created background task task:demo-123",
                                    "timestamp": "2026-04-07T12:10:00",
                                }
                            ]
                        }
                    ]
                }
            ]
        },
        "persisted_at": "2026-04-07T12:00:00",
    }
    message = {
        "role": "assistant",
        "content": "",
        "canonical_context": snapshot["canonical_context"],
    }

    task_ids = web_ceo_sessions._extract_task_ids_from_message(message)
    preview = web_ceo_sessions._inflight_preview_text(snapshot)
    updated_at = web_ceo_sessions._inflight_updated_at(snapshot)
    has_history = web_ceo_sessions._snapshot_has_material_live_history(snapshot, require_active_stage=False)

    assert task_ids == ["task:demo-123"]
    assert "task:demo-123" in preview
    assert updated_at == "2026-04-07T12:10:00"
    assert has_history is True


def test_ceo_snapshot_filters_internal_cron_user_message() -> None:
    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {"role": "user", "content": "visible user"},
            {"role": "assistant", "content": "visible answer"},
            {
                "role": "user",
                "content": "internal cron prompt",
                "metadata": {"cron_internal": True, "cron_job_id": "job-77"},
            },
            {"role": "assistant", "content": "scheduled update", "metadata": {"source": "cron"}},
        ]
    )

    assert [item["content"] for item in snapshot] == [
        "visible user",
        "visible answer",
        "scheduled update",
    ]


def test_ceo_snapshot_uses_raw_upload_text_even_when_empty_for_attachment_only_message() -> None:
    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {
                "role": "user",
                "content": "\n".join(
                    [
                        "Uploaded attachments:",
                        "- file: report.pdf (local path: D:/NewProjects/G3KU/.g3ku/web-ceo-uploads/web_test/report.pdf)",
                        "You may inspect the local file paths above when helpful.",
                    ]
                ),
                "metadata": {
                    "web_ceo_raw_text": "",
                    "web_ceo_uploads": [
                        {
                            "path": "D:/NewProjects/G3KU/.g3ku/web-ceo-uploads/web_test/report.pdf",
                            "name": "report.pdf",
                            "mime_type": "application/pdf",
                            "kind": "file",
                            "size": 51200,
                        }
                    ],
                },
            }
        ]
    )

    assert len(snapshot) == 1
    assert snapshot[0]["role"] == "user"
    assert snapshot[0]["content"] == ""
    assert snapshot[0]["attachments"][0]["name"] == "report.pdf"


def test_ceo_uploaded_file_endpoint_serves_file_inline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    session_id = "web:test"
    upload_dir = web_ceo_sessions.upload_dir_for_session(session_id)
    file_path = upload_dir / "report.txt"
    file_path.write_text("hello attachment", encoding="utf-8")

    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")
    client = TestClient(app)

    response = client.get(
        "/api/ceo/uploads/file",
        params={
            "session_id": session_id,
            "path": str(file_path),
        },
    )

    assert response.status_code == 200
    assert response.text == "hello attachment"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"].startswith("inline;")


def test_ceo_uploaded_file_endpoint_serves_unicode_filename_inline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    session_id = "web:test-unicode"
    upload_dir = web_ceo_sessions.upload_dir_for_session(session_id)
    file_path = upload_dir / "QQ图片20210228225059.jpg"
    payload = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    file_path.write_bytes(payload)

    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")
    client = TestClient(app)

    response = client.get(
        "/api/ceo/uploads/file",
        params={
            "session_id": session_id,
            "path": str(file_path),
        },
    )

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["content-disposition"].startswith("inline;")
    assert "filename*=" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_web_session_heartbeat_delays_background_tool_prompt(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-tool"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession()
    manager = _FakeToolExecutionManager(
        [
            {
                "status": "background_running",
                "execution_id": "tool-exec:1",
                "tool_name": "skill-installer",
                "elapsed_seconds": 90.0,
                "poll_count": 2,
                "recommended_wait_seconds": 0.05,
                "runtime_snapshot": {"summary_text": "still fetching remote repository"},
            },
            {
                "status": "background_running",
                "execution_id": "tool-exec:1",
                "tool_name": "skill-installer",
                "elapsed_seconds": 150.0,
                "poll_count": 3,
                "recommended_wait_seconds": 0.05,
                "runtime_snapshot": {"summary_text": "still fetching remote repository"},
            },
        ]
    )
    task_service = _TaskService()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=manager),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    service.enqueue_tool_background(
        session_id=session_id,
        payload={
            "status": "background_running",
            "tool_name": "skill-installer",
            "execution_id": "tool-exec:1",
            "elapsed_seconds": 30.0,
            "recommended_wait_seconds": 0.2,
            "runtime_snapshot": {"summary_text": "still fetching remote repository"},
        },
    )
    service._started = True

    initial_delay = await service._run_session(session_id)
    assert initial_delay is not None
    assert initial_delay > 0
    assert live_session.prompts == []

    await asyncio.sleep(0.21)

    next_delay = await service._run_session(session_id)

    assert next_delay is not None
    assert next_delay > 0
    assert len(live_session.prompts) == 1
    prompt = live_session.prompts[0]
    assert isinstance(prompt, UserInputMessage)
    assert "tool-exec:1" in str(prompt.content)
    assert "already been refreshed" in str(prompt.content)
    assert "Do not start a new tool chain" in str(prompt.content)
    assert "你正在处理内部事件，不是在处理新的用户输入" in str(prompt.content)
    assert manager.calls == [("tool-exec:1", 0.1)]
    published_types = [envelope["type"] for _session_id, envelope in task_service.registry.published]
    assert "ceo.internal.ack" in published_types

    await asyncio.sleep(0.22)

    assert len(live_session.prompts) >= 2
    assert manager.calls[:2] == [("tool-exec:1", 0.1), ("tool-exec:1", 0.1)]


def test_web_session_heartbeat_accepts_enqueues_without_manual_pause_reason_gate(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-manual-pause"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    persisted.metadata = web_ceo_sessions.normalize_ceo_metadata(
        {},
        session_key=session_id,
    )
    session_manager.save(persisted)
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=SimpleNamespace(get=lambda _key: None),
        main_task_service=_TaskService(),
        session_manager=session_manager,
    )

    accepted_terminal = service.enqueue_task_terminal_payload(
        {
            "task_id": "task:manual-pause-terminal",
            "session_id": session_id,
            "status": "success",
            "finished_at": "2026-03-28T01:34:32+08:00",
            "dedupe_key": "task-terminal:task:manual-pause-terminal:success:2026-03-28T01:34:32+08:00",
        }
    )
    accepted_stall = service.enqueue_task_stall_payload(
        {
            "task_id": "task:manual-pause-stall",
            "session_id": session_id,
            "bucket_minutes": 15,
            "dedupe_key": "task-stall:task:manual-pause-stall:15",
        }
    )
    service.enqueue_tool_background(
        session_id=session_id,
        payload={
            "status": "background_running",
            "tool_name": "skill-installer",
            "execution_id": "tool-exec:manual-pause",
        },
    )
    service.enqueue_tool_terminal(
        session_id=session_id,
        payload={
            "status": "completed",
            "tool_name": "skill-installer",
            "execution_id": "tool-exec:manual-pause",
        },
    )

    assert accepted_terminal is True
    assert accepted_stall is False
    assert len(service._events.peek(session_id)) == 2


def test_web_session_heartbeat_replays_pending_terminal_outbox_and_records_enqueue_result(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-replay-outbox"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)

    payload = {
        "task_id": "task:replay-demo",
        "session_id": session_id,
        "status": "failed",
        "finished_at": "2026-03-28T01:34:32+08:00",
        "dedupe_key": "task-terminal:task:replay-demo:failed:2026-03-28T01:34:32+08:00",
    }
    enqueue_results: list[tuple[str, bool | None, str]] = []

    class _Store:
        @staticmethod
        def list_pending_task_terminal_outbox(*, limit: int = 500):
            _ = limit
            return [{"dedupe_key": payload["dedupe_key"], "session_id": session_id, "payload": dict(payload)}]

        @staticmethod
        def list_pending_task_stall_outbox(*, limit: int = 500):
            _ = limit
            return []

        @staticmethod
        def mark_task_terminal_outbox_enqueue_result(
            dedupe_key: str,
            *,
            accepted: bool | None,
            rejected_reason: str,
            updated_at: str,
        ) -> None:
            _ = updated_at
            enqueue_results.append((str(dedupe_key or ""), accepted, str(rejected_reason or "")))

    task_service = SimpleNamespace(
        store=_Store(),
        registry=_Registry(),
        get_task=lambda _task_id: None,
        get_node_detail_payload=lambda _task_id, _node_id: None,
    )
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=SimpleNamespace(get=lambda _key: None),
        main_task_service=task_service,
        session_manager=session_manager,
    )

    counts = service.replay_pending_outbox(session_id=session_id)

    assert counts == {"task_terminal": 1, "task_stall": 0}
    assert len(service._events.peek(session_id)) == 1
    assert enqueue_results == [(payload["dedupe_key"], True, "")]


@pytest.mark.asyncio
async def test_web_session_heartbeat_runs_immediately_when_background_tool_turns_terminal(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-terminal"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession()
    task_service = _TaskService()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    service.enqueue_tool_background(
        session_id=session_id,
        payload={
            "status": "background_running",
            "tool_name": "skill-installer",
            "execution_id": "tool-exec:1",
            "elapsed_seconds": 30.0,
            "recommended_wait_seconds": 600.0,
            "runtime_snapshot": {"summary_text": "still fetching remote repository"},
        },
    )
    service.enqueue_tool_terminal(
        session_id=session_id,
        payload={
            "status": "completed",
            "tool_name": "skill-installer",
            "execution_id": "tool-exec:1",
            "message": "skill installation finished",
            "final_result": "installed",
        },
    )
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert len(live_session.prompts) == 1
    prompt = live_session.prompts[0]
    assert "reached a terminal state" in str(prompt.content)
    assert "still running" not in str(prompt.content)
    assert service._events.peek(session_id) == []


@pytest.mark.asyncio
async def test_web_session_heartbeat_tool_only_terminal_uses_visible_reply_and_notifier_when_model_returns_text(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-tool-terminal-no-notify"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(output="tool terminal internal note")
    task_service = _TaskService()
    notified: list[tuple[str, str]] = []

    async def _notify(current_session_id: str, text: str) -> None:
        notified.append((current_session_id, text))

    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
        reply_notifier=_notify,
    )
    service.enqueue_tool_background(
        session_id=session_id,
        payload={
            "status": "background_running",
            "tool_name": "skill-installer",
            "execution_id": "tool-exec:no-notify",
            "elapsed_seconds": 30.0,
            "recommended_wait_seconds": 600.0,
            "runtime_snapshot": {"summary_text": "still fetching remote repository"},
        },
    )
    service.enqueue_tool_terminal(
        session_id=session_id,
        payload={
            "status": "completed",
            "tool_name": "skill-installer",
            "execution_id": "tool-exec:no-notify",
            "message": "skill installation finished",
            "final_result": "installed",
        },
    )
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert notified == [(session_id, "tool terminal internal note")]
    assert [envelope["type"] for _session, envelope in task_service.registry.published] == ["ceo.reply.final"]


@pytest.mark.asyncio
async def test_web_session_heartbeat_repairs_task_terminal_when_model_returns_heartbeat_ok_then_text(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-fallback"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(outputs=[HEARTBEAT_OK, "整理后的最终结论"])
    task_service = _TaskService()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    payload = {
        "task_id": "task:demo-terminal",
        "session_id": session_id,
        "title": "demo terminal task",
        "status": "success",
        "brief_text": "task finished successfully",
        "finished_at": "2026-03-23T01:34:32+08:00",
        "dedupe_key": "task-terminal:task:demo-terminal:success:2026-03-23T01:34:32+08:00",
    }
    accepted = service.enqueue_task_terminal_payload(payload)
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert len(live_session.prompts) == 2
    assert service._events.peek(session_id) == []
    assert len(task_service.registry.published) == 1
    published_session, envelope = task_service.registry.published[0]
    assert published_session == session_id
    assert envelope["type"] == "ceo.reply.final"
    assert envelope["data"]["source"] == "heartbeat"
    assert envelope["data"]["turn_id"] == "turn-heartbeat-default"
    assert len(task_service.delivered) == 1
    assert task_service.delivered[0][0] == "task-terminal:task:demo-terminal:success:2026-03-23T01:34:32+08:00"
    assert "must not reply with HEARTBEAT_OK" in str(live_session.prompts[1].content)

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert reloaded.messages[-1]["content"] == "整理后的最终结论"
    assert reloaded.metadata.get("last_task_memory", {}).get("task_ids") == ["task:demo-terminal"]
    assert reloaded.metadata.get("last_task_memory", {}).get("source") == "heartbeat"
    assert reloaded.metadata.get("last_task_memory", {}).get("reason") == "task_terminal"


@pytest.mark.asyncio
async def test_web_session_heartbeat_repairs_unpassed_task_terminal_when_model_returns_heartbeat_ok_then_text(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-unpassed-continuation"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(outputs=[HEARTBEAT_OK, "虽然未通过验收，但结果已基本可交付。"])
    task_service = _TaskService()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    payload = {
        "task_id": "task:demo-unpassed",
        "session_id": session_id,
        "title": "demo unpassed task",
        "status": "success",
        "failure_class": "business_unpassed",
        "final_acceptance_status": "failed",
        "brief_text": "acceptance failed",
        "failure_reason": "Acceptance failed after final review.",
        "finished_at": "2026-03-28T01:34:32+08:00",
        "dedupe_key": "task-terminal:task:demo-unpassed:success:2026-03-28T01:34:32+08:00",
    }
    accepted = service.enqueue_task_terminal_payload(payload)
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert len(task_service.registry.published) == 1
    published_session, envelope = task_service.registry.published[0]
    assert published_session == session_id
    assert envelope["type"] == "ceo.reply.final"
    assert envelope["data"]["source"] == "heartbeat"
    assert envelope["data"]["turn_id"] == "turn-heartbeat-default"
    assert "must not reply with HEARTBEAT_OK" in str(live_session.prompts[1].content)

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert reloaded.messages[-1]["content"] == "虽然未通过验收，但结果已基本可交付。"
    assert reloaded.metadata.get("last_task_memory", {}).get("task_ids") == ["task:demo-unpassed"]
    assert reloaded.metadata.get("last_task_memory", {}).get("reason") == "task_terminal"


@pytest.mark.asyncio
async def test_web_session_heartbeat_uses_fixed_error_after_task_terminal_repair_attempt_limit(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-engine-failure"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(outputs=[HEARTBEAT_OK, HEARTBEAT_OK, HEARTBEAT_OK, HEARTBEAT_OK, HEARTBEAT_OK, HEARTBEAT_OK])
    task_service = _TaskService()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    payload = {
        "task_id": "task:demo-engine-failed",
        "session_id": session_id,
        "title": "demo engine failed task",
        "status": "failed",
        "failure_class": "engine_failure",
        "brief_text": "model provider failed",
        "failure_reason": "Model provider call failed after exhausting the configured fallback chain.",
        "finished_at": "2026-03-28T02:34:32+08:00",
        "dedupe_key": "task-terminal:task:demo-engine-failed:failed:2026-03-28T02:34:32+08:00",
    }
    accepted = service.enqueue_task_terminal_payload(payload)
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert len(task_service.registry.published) == 1
    published_session, envelope = task_service.registry.published[0]
    assert published_session == session_id
    assert envelope["type"] == "ceo.reply.final"
    assert envelope["data"]["turn_id"] == "turn-heartbeat-default"
    assert "系统在整理该结果时连续失败" in str(envelope["data"]["text"] or "")
    assert "task:demo-engine-failed" in str(envelope["data"]["text"] or "")
    assert len(live_session.prompts) == 6
    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert reloaded.messages[-1]["content"]


@pytest.mark.asyncio
async def test_web_session_heartbeat_does_not_auto_retry_engine_failure_in_place(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-engine-retry"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(outputs=[HEARTBEAT_OK, "已读取 root 输出并整理回复。"])
    task_service = _TaskService()
    task_id = "task:demo-engine-retry"
    task_service.tasks[task_id] = SimpleNamespace(
        task_id=task_id,
        status="failed",
        metadata={"failure_class": "engine_failure"},
    )
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    payload = {
        "task_id": task_id,
        "session_id": session_id,
        "title": "demo engine retry task",
        "status": "failed",
        "failure_class": "engine_failure",
        "brief_text": "model provider failed",
        "failure_reason": "Model provider call failed after exhausting the configured fallback chain.",
        "finished_at": "2026-03-28T03:34:32+08:00",
        "dedupe_key": "task-terminal:task:demo-engine-retry:failed:2026-03-28T03:34:32+08:00",
    }
    accepted = service.enqueue_task_terminal_payload(payload)
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert task_service.retry_calls == []
    assert task_service.continue_calls == []
    assert len(task_service.registry.published) == 1
    published_session, envelope = task_service.registry.published[0]
    assert published_session == session_id
    assert envelope["type"] == "ceo.reply.final"
    assert envelope["data"]["turn_id"] == "turn-heartbeat-default"


@pytest.mark.asyncio
async def test_web_session_heartbeat_prompt_includes_terminal_root_output_and_metadata(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-root-output"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(outputs=[HEARTBEAT_OK, "已读取 root 输出并整理回复。"])
    task_service = _TaskService()
    task_id = "task:demo-root-output"
    task_service.tasks[task_id] = SimpleNamespace(
        task_id=task_id,
        root_node_id="node:root",
        metadata={},
        final_output="Top 3 recommendation list",
        final_output_ref="artifact:artifact:root-output",
        failure_reason="",
    )
    task_service.node_details[(task_id, "node:root")] = {
        "item": {
            "node_id": "node:root",
            "task_id": task_id,
            "node_kind": "execution",
            "final_output": "Top 3 recommendation list",
            "final_output_ref": "artifact:artifact:root-output",
            "check_result": "accepted",
            "failure_reason": "",
        }
    }
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    accepted = service.enqueue_task_terminal_payload(
        {
            "task_id": task_id,
            "session_id": session_id,
            "title": "demo root output task",
            "status": "success",
            "brief_text": "task finished successfully",
            "finished_at": "2026-03-27T01:34:32+08:00",
            "dedupe_key": "task-terminal:task:demo-root-output:success:2026-03-27T01:34:32+08:00",
        }
    )
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    prompt_text = str(live_session.prompts[0].content)
    assert "Result node: execution node:root" in prompt_text
    assert "Result output: Top 3 recommendation list" in prompt_text
    assert "Result output ref: artifact:artifact:root-output" in prompt_text

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert reloaded.messages[-1]["content"] == "已读取 root 输出并整理回复。"
    assert reloaded.metadata.get("last_task_memory", {}).get("task_ids") == [task_id]
    assert reloaded.metadata["last_task_memory"]["task_results"] == [
        {
            "task_id": task_id,
            "node_id": "node:root",
            "node_kind": "execution",
            "node_reason": "root_terminal",
            "output_excerpt": "Top 3 recommendation list",
            "output_ref": "artifact:artifact:root-output",
            "check_result": "accepted",
        }
    ]


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_continues_full_context_and_appends_hidden_heartbeat_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
    from g3ku.runtime.frontdoor.prompt_cache_contract import FrontdoorPromptContract

    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
    )
    runner = CeoFrontDoorRunner(loop=loop)
    captured: dict[str, object] = {}
    heartbeat_rules_text = "heartbeat stable system"
    heartbeat_event_bundle = "heartbeat request bundle"
    existing_baseline = [
        {"role": "system", "content": "frontdoor fallback"},
        {"role": "user", "content": "prior visible request"},
        {"role": "assistant", "content": "prior visible answer"},
    ]

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec"]}

    async def _build_for_ceo(**kwargs):
        captured["builder_kwargs"] = kwargs
        seed_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = kwargs.get("user_content")
        return SimpleNamespace(
            tool_names=["exec"],
            model_messages=[*seed_messages, {"role": "user", "content": user_content}],
            stable_messages=[*seed_messages, {"role": "user", "content": user_content}],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    def _fake_build_frontdoor_prompt_contract(**kwargs):
        captured["contract_kwargs"] = kwargs
        return FrontdoorPromptContract(
            request_messages=list(kwargs.get("stable_messages") or []),
            prompt_cache_key="frontdoor-cache-key",
            diagnostics={"stable_prompt_signature": "frontdoor-sig"},
            stable_prefix_hash="stable-hash",
            dynamic_appendix_hash="dynamic-hash",
            stable_messages=list(kwargs.get("stable_messages") or []),
            dynamic_appendix_messages=list(kwargs.get("dynamic_appendix_messages") or []),
            diagnostic_dynamic_messages=[],
            cache_family_revision="frontdoor:v1",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai:gpt-4.1"])
    monkeypatch.setattr(ceo_runtime_ops, "build_frontdoor_prompt_contract", _fake_build_frontdoor_prompt_contract)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=list(existing_baseline),
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={},
        _semantic_context_state={},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=ceo_runtime_ops.CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    state_update = await runner._graph_prepare_turn(
        {
            "user_input": {
                "content": heartbeat_event_bundle,
                "metadata": {
                    "heartbeat_internal": True,
                    "heartbeat_stable_rules_text": heartbeat_rules_text,
                    "heartbeat_event_bundle_text": heartbeat_event_bundle,
                    "heartbeat_retrieval_query": "tool_background skill-installer task:demo-1",
                },
            }
        },
        runtime=runtime,
    )

    builder_seed = list(captured["builder_kwargs"]["request_body_seed_messages"])
    assert [message["role"] for message in builder_seed] == ["system", "user", "assistant", "system"]
    assert builder_seed[:3] == existing_baseline
    assert builder_seed[3]["content"] == heartbeat_rules_text
    assert builder_seed[3]["metadata"] == {
        "source": "heartbeat",
        "prompt_visible": True,
        "ui_visible": False,
        "internal_prompt_kind": "heartbeat_rule",
    }
    assert captured["builder_kwargs"]["user_content"] == heartbeat_event_bundle
    assert captured["contract_kwargs"]["scope"] == "ceo_frontdoor"
    assert captured["contract_kwargs"]["stable_messages"] == [
        *builder_seed,
        {
            "role": "user",
            "content": heartbeat_event_bundle,
        },
    ]
    assert state_update["frontdoor_request_body_messages"] == [
        *existing_baseline,
        {
            "role": "system",
            "content": heartbeat_rules_text,
            "metadata": {
                "source": "heartbeat",
                "prompt_visible": True,
                "ui_visible": False,
                "internal_prompt_kind": "heartbeat_rule",
            },
        },
        {
            "role": "user",
            "content": heartbeat_event_bundle,
            "metadata": {
                "source": "heartbeat",
                "prompt_visible": True,
                "ui_visible": False,
                "internal_prompt_kind": "heartbeat_event_bundle",
            },
        },
    ]
    assert state_update["messages"] == state_update["frontdoor_request_body_messages"]
    assert state_update["dynamic_appendix_messages"] == []
    assert state_update["cache_family_revision"] == "frontdoor:v1"
    assert state_update["prompt_cache_key"] == "frontdoor-cache-key"
    assert state_update["prompt_cache_diagnostics"] == {"stable_prompt_signature": "frontdoor-sig"}


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_heartbeat_inherits_previous_tool_state_without_reselection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
    from g3ku.runtime.frontdoor.prompt_cache_contract import FrontdoorPromptContract
    from g3ku.runtime.frontdoor.tool_contract import is_frontdoor_tool_contract_message

    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
    )
    runner = CeoFrontDoorRunner(loop=loop)
    heartbeat_rules_text = "heartbeat stable system"
    heartbeat_event_bundle = "heartbeat request bundle"
    existing_baseline = [
        {"role": "system", "content": "frontdoor fallback"},
        {"role": "user", "content": "prior visible request"},
        {"role": "assistant", "content": "prior visible answer"},
    ]

    async def _resolver_should_not_run(**kwargs):
        raise AssertionError(f"resolve_for_actor should not run for inherited heartbeat state: {kwargs}")

    async def _builder_should_not_run(**kwargs):
        raise AssertionError(f"build_for_ceo should not run for inherited heartbeat state: {kwargs}")

    def _fake_build_frontdoor_prompt_contract(**kwargs):
        request_messages = [
            *list(kwargs.get("stable_messages") or []),
            *list(kwargs.get("dynamic_appendix_messages") or []),
        ]
        return FrontdoorPromptContract(
            request_messages=request_messages,
            prompt_cache_key="frontdoor-cache-key",
            diagnostics={"stable_prompt_signature": "frontdoor-sig"},
            stable_prefix_hash="stable-hash",
            dynamic_appendix_hash="dynamic-hash",
            stable_messages=list(kwargs.get("stable_messages") or []),
            dynamic_appendix_messages=list(kwargs.get("dynamic_appendix_messages") or []),
            diagnostic_dynamic_messages=[],
            cache_family_revision=str(kwargs.get("cache_family_revision") or "exp:prior"),
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolver_should_not_run)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _builder_should_not_run)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai:gpt-4.1"])
    monkeypatch.setattr(runner, "_selected_tool_schemas", lambda names: [{"name": name, "parameters": {"type": "object"}} for name in list(names or [])])
    monkeypatch.setattr(ceo_runtime_ops, "build_frontdoor_prompt_contract", _fake_build_frontdoor_prompt_contract)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=list(existing_baseline),
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={"active_stage_id": "", "transition_required": False, "stages": []},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={},
        _semantic_context_state={},
        _frontdoor_hydrated_tool_names=["filesystem_write"],
        _frontdoor_selection_debug={"query_text": "prior visible request", "tool_selection": {"source": "prior"}},
        _frontdoor_capability_snapshot_exposure_revision="exp:prior",
        _frontdoor_visible_tool_ids=["create_async_task", "task_list", "filesystem_write", "web_fetch"],
        _frontdoor_visible_skill_ids=["find-skills"],
        _frontdoor_provider_tool_schema_names=["create_async_task", "task_list", "filesystem_write"],
    )
    runtime = SimpleNamespace(
        context=ceo_runtime_ops.CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    state_update = await runner._graph_prepare_turn(
        {
            "tool_names": ["create_async_task", "task_list", "filesystem_write"],
            "provider_tool_names": ["create_async_task", "task_list", "filesystem_write"],
            "pending_provider_tool_names": [],
            "provider_tool_exposure_pending": False,
            "provider_tool_exposure_revision": "pte:prior",
            "provider_tool_exposure_commit_reason": "",
            "candidate_tool_names": ["web_fetch"],
            "candidate_tool_items": [{"tool_id": "web_fetch", "description": "fetch web pages"}],
            "hydrated_tool_names": ["filesystem_write"],
            "visible_skill_ids": ["find-skills"],
            "candidate_skill_ids": ["find-skills"],
            "rbac_visible_tool_names": ["create_async_task", "task_list", "filesystem_write", "web_fetch"],
            "rbac_visible_skill_ids": ["find-skills"],
            "cache_family_revision": "exp:prior",
            "frontdoor_selection_debug": {"query_text": "prior visible request", "tool_selection": {"source": "prior"}},
            "user_input": {
                "content": heartbeat_event_bundle,
                "metadata": {
                    "heartbeat_internal": True,
                    "heartbeat_stable_rules_text": heartbeat_rules_text,
                    "heartbeat_event_bundle_text": heartbeat_event_bundle,
                    "heartbeat_retrieval_query": "this should be ignored",
                },
            },
        },
        runtime=runtime,
    )

    assert state_update["tool_names"] == ["create_async_task", "task_list", "filesystem_write"]
    assert state_update["provider_tool_names"] == ["create_async_task", "task_list", "filesystem_write", "web_fetch"]
    assert state_update["candidate_tool_names"] == ["web_fetch"]
    assert state_update["candidate_tool_items"] == [{"tool_id": "web_fetch", "description": "fetch web pages"}]
    assert state_update["hydrated_tool_names"] == ["filesystem_write"]
    assert state_update["visible_skill_ids"] == ["find-skills"]
    assert state_update["candidate_skill_ids"] == ["find-skills"]
    assert state_update["rbac_visible_tool_names"] == ["create_async_task", "task_list", "filesystem_write", "web_fetch"]
    assert state_update["rbac_visible_skill_ids"] == ["find-skills"]
    assert state_update["cache_family_revision"] == "exp:prior"
    assert state_update["frontdoor_selection_debug"]["query_text"] == "prior visible request"
    assert state_update["frontdoor_selection_debug"]["tool_selection"] == {"source": "prior"}
    assert state_update["frontdoor_selection_debug"]["callable_tool_names"] == [
        "create_async_task",
        "task_list",
        "filesystem_write",
    ]
    assert state_update["frontdoor_selection_debug"]["candidate_tool_names"] == ["web_fetch"]
    assert state_update["frontdoor_selection_debug"]["hydrated_tool_names"] == ["filesystem_write"]
    assert state_update["frontdoor_request_body_messages"] == [
        *existing_baseline,
        {
            "role": "system",
            "content": heartbeat_rules_text,
            "metadata": {
                "source": "heartbeat",
                "prompt_visible": True,
                "ui_visible": False,
                "internal_prompt_kind": "heartbeat_rule",
            },
        },
        {
            "role": "user",
            "content": heartbeat_event_bundle,
            "metadata": {
                "source": "heartbeat",
                "prompt_visible": True,
                "ui_visible": False,
                "internal_prompt_kind": "heartbeat_event_bundle",
            },
        },
    ]
    contract_messages = [
        dict(item)
        for item in list(state_update["dynamic_appendix_messages"] or [])
        if is_frontdoor_tool_contract_message(dict(item))
    ]
    assert len(contract_messages) == 1
    contract_text = str(contract_messages[0]["content"] or "")
    assert "callable_tools: `create_async_task`, `task_list`, `filesystem_write`" in contract_text
    assert "hydrated_tools: `filesystem_write`" in contract_text
    assert "candidate_skills (loadable with `load_skill_context`): `find-skills`" in contract_text
    assert 'Call `load_skill_context(skill_id="<skill_id>")`' in contract_text
    assert "`web_fetch`: fetch web pages" in contract_text


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_cron_inherits_previous_tool_state_without_reselection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
    from g3ku.runtime.frontdoor.prompt_cache_contract import FrontdoorPromptContract
    from g3ku.runtime.frontdoor.tool_contract import is_frontdoor_tool_contract_message

    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
    )
    runner = CeoFrontDoorRunner(loop=loop)
    existing_baseline = [
        {"role": "system", "content": "frontdoor fallback"},
        {"role": "user", "content": "prior visible request"},
        {"role": "assistant", "content": "prior visible answer"},
    ]

    async def _resolver_should_not_run(**kwargs):
        raise AssertionError(f"resolve_for_actor should not run for inherited cron state: {kwargs}")

    async def _builder_should_not_run(**kwargs):
        raise AssertionError(f"build_for_ceo should not run for inherited cron state: {kwargs}")

    def _fake_build_frontdoor_prompt_contract(**kwargs):
        request_messages = [
            *list(kwargs.get("stable_messages") or []),
            *list(kwargs.get("dynamic_appendix_messages") or []),
        ]
        return FrontdoorPromptContract(
            request_messages=request_messages,
            prompt_cache_key="frontdoor-cache-key",
            diagnostics={"stable_prompt_signature": "frontdoor-sig"},
            stable_prefix_hash="stable-hash",
            dynamic_appendix_hash="dynamic-hash",
            stable_messages=list(kwargs.get("stable_messages") or []),
            dynamic_appendix_messages=list(kwargs.get("dynamic_appendix_messages") or []),
            diagnostic_dynamic_messages=[],
            cache_family_revision=str(kwargs.get("cache_family_revision") or "exp:prior"),
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolver_should_not_run)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _builder_should_not_run)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai:gpt-4.1"])
    monkeypatch.setattr(runner, "_selected_tool_schemas", lambda names: [{"name": name, "parameters": {"type": "object"}} for name in list(names or [])])
    monkeypatch.setattr(ceo_runtime_ops, "build_frontdoor_prompt_contract", _fake_build_frontdoor_prompt_contract)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=list(existing_baseline),
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={"active_stage_id": "", "transition_required": False, "stages": []},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={},
        _semantic_context_state={},
        _frontdoor_hydrated_tool_names=["filesystem_write"],
        _frontdoor_selection_debug={"query_text": "prior visible request", "tool_selection": {"source": "prior"}},
        _frontdoor_capability_snapshot_exposure_revision="exp:prior",
        _frontdoor_visible_tool_ids=["create_async_task", "task_list", "filesystem_write", "web_fetch"],
        _frontdoor_visible_skill_ids=["find-skills"],
        _frontdoor_provider_tool_schema_names=["create_async_task", "task_list", "filesystem_write"],
    )
    runtime = SimpleNamespace(
        context=ceo_runtime_ops.CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    state_update = await runner._graph_prepare_turn(
        {
            "tool_names": ["create_async_task", "task_list", "filesystem_write"],
            "provider_tool_names": ["create_async_task", "task_list", "filesystem_write"],
            "pending_provider_tool_names": [],
            "provider_tool_exposure_pending": False,
            "provider_tool_exposure_revision": "pte:prior",
            "provider_tool_exposure_commit_reason": "",
            "candidate_tool_names": ["web_fetch"],
            "candidate_tool_items": [{"tool_id": "web_fetch", "description": "fetch web pages"}],
            "hydrated_tool_names": ["filesystem_write"],
            "visible_skill_ids": ["find-skills"],
            "candidate_skill_ids": ["find-skills"],
            "rbac_visible_tool_names": ["create_async_task", "task_list", "filesystem_write", "web_fetch"],
            "rbac_visible_skill_ids": ["find-skills"],
            "cache_family_revision": "exp:prior",
            "frontdoor_selection_debug": {"query_text": "prior visible request", "tool_selection": {"source": "prior"}},
            "user_input": {
                "content": "Create a detached task for the scheduled work.",
                "metadata": {
                    "cron_internal": True,
                    "cron_job_id": "job-77",
                    "cron_max_runs": 2,
                    "cron_delivery_index": 1,
                    "cron_delivered_runs": 0,
                    "cron_reminder_text": "Create a detached task for the scheduled work.",
                },
            },
        },
        runtime=runtime,
    )

    assert state_update["tool_names"] == ["create_async_task", "task_list", "filesystem_write"]
    assert state_update["provider_tool_names"] == ["create_async_task", "task_list", "filesystem_write", "web_fetch"]
    assert state_update["candidate_tool_names"] == ["web_fetch"]
    assert state_update["candidate_tool_items"] == [{"tool_id": "web_fetch", "description": "fetch web pages"}]
    assert state_update["hydrated_tool_names"] == ["filesystem_write"]
    assert state_update["visible_skill_ids"] == ["find-skills"]
    assert state_update["candidate_skill_ids"] == ["find-skills"]
    assert state_update["rbac_visible_tool_names"] == ["create_async_task", "task_list", "filesystem_write", "web_fetch"]
    assert state_update["rbac_visible_skill_ids"] == ["find-skills"]
    assert state_update["cache_family_revision"] == "exp:prior"
    assert state_update["frontdoor_selection_debug"]["query_text"] == "prior visible request"
    assert state_update["frontdoor_selection_debug"]["tool_selection"] == {"source": "prior"}
    assert state_update["frontdoor_selection_debug"]["callable_tool_names"] == [
        "create_async_task",
        "task_list",
        "filesystem_write",
    ]
    assert state_update["frontdoor_selection_debug"]["candidate_tool_names"] == ["web_fetch"]
    assert state_update["frontdoor_selection_debug"]["hydrated_tool_names"] == ["filesystem_write"]
    assert state_update["frontdoor_request_body_messages"] == [
        *existing_baseline,
        {
            "role": "system",
            "content": "\n".join(
                [
                    "你接收到了之前你定时的任务，如下：",
                    "当前定时任务 ID：job-77",
                    "当前发送次数：1/2",
                    "注意：",
                    "- 此定时任务提醒为内部指令，而非新的用户消息。",
                    "要求：",
                    "- 请立即按任务要求执行。",
                ]
            ),
            "metadata": {
                "source": "cron",
                "prompt_visible": True,
                "ui_visible": False,
                "internal_prompt_kind": "cron_rule",
                "cron_job_id": "job-77",
            },
        },
        {
            "role": "system",
            "content": "[CRON INTERNAL EVENT]\n{\n  \"message_type\": \"cron_internal_event\",\n  \"cron_job_id\": \"job-77\",\n  \"delivery_index\": 1,\n  \"max_runs\": 2,\n  \"delivered_runs_before_this_turn\": 0,\n  \"scheduled_run_at_ms\": null,\n  \"last_delivered_at_ms\": null,\n  \"reminder_text\": \"Create a detached task for the scheduled work.\",\n  \"semantic_role\": \"internal_self_reminder\"\n}",
            "metadata": {
                "source": "cron",
                "prompt_visible": True,
                "ui_visible": False,
                "internal_prompt_kind": "cron_event_bundle",
                "cron_job_id": "job-77",
            },
        },
    ]
    contract_messages = [
        dict(item)
        for item in list(state_update["dynamic_appendix_messages"] or [])
        if is_frontdoor_tool_contract_message(dict(item))
    ]
    assert len(contract_messages) == 1
    contract_text = str(contract_messages[0]["content"] or "")
    assert "callable_tools: `create_async_task`, `task_list`, `filesystem_write`" in contract_text
    assert "hydrated_tools: `filesystem_write`" in contract_text
    assert "candidate_skills (loadable with `load_skill_context`): `find-skills`" in contract_text
    assert 'Call `load_skill_context(skill_id="<skill_id>")`' in contract_text
    assert "`web_fetch`: fetch web pages" in contract_text


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_internal_turn_without_prior_baseline_falls_back_to_normal_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
    from g3ku.runtime.frontdoor.prompt_cache_contract import FrontdoorPromptContract

    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
    )
    runner = CeoFrontDoorRunner(loop=loop)
    captured: dict[str, object] = {}
    heartbeat_rules_text = "heartbeat stable system"
    heartbeat_event_bundle = "heartbeat request bundle"

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        captured["resolver_called"] = {"actor_role": actor_role, "session_id": session_id}
        return {"skills": [], "tool_families": [], "tool_names": ["exec"]}

    async def _build_for_ceo(**kwargs):
        captured["builder_kwargs"] = kwargs
        seed_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = kwargs.get("user_content")
        return SimpleNamespace(
            tool_names=["exec"],
            model_messages=[*seed_messages, {"role": "user", "content": user_content}],
            stable_messages=[*seed_messages, {"role": "user", "content": user_content}],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    def _fake_build_frontdoor_prompt_contract(**kwargs):
        return FrontdoorPromptContract(
            request_messages=list(kwargs.get("stable_messages") or []),
            prompt_cache_key="frontdoor-cache-key",
            diagnostics={"stable_prompt_signature": "frontdoor-sig"},
            stable_prefix_hash="stable-hash",
            dynamic_appendix_hash="dynamic-hash",
            stable_messages=list(kwargs.get("stable_messages") or []),
            dynamic_appendix_messages=list(kwargs.get("dynamic_appendix_messages") or []),
            diagnostic_dynamic_messages=[],
            cache_family_revision="frontdoor:v1",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai:gpt-4.1"])
    monkeypatch.setattr(ceo_runtime_ops, "build_frontdoor_prompt_contract", _fake_build_frontdoor_prompt_contract)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={},
        _semantic_context_state={},
        _frontdoor_hydrated_tool_names=["filesystem_write"],
        _frontdoor_selection_debug={"query_text": "prior visible request"},
        _frontdoor_capability_snapshot_exposure_revision="exp:prior",
        _frontdoor_visible_tool_ids=["filesystem_write"],
        _frontdoor_visible_skill_ids=["find-skills"],
        _frontdoor_provider_tool_schema_names=["filesystem_write"],
    )
    runtime = SimpleNamespace(
        context=ceo_runtime_ops.CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    state_update = await runner._graph_prepare_turn(
        {
            "tool_names": ["filesystem_write"],
            "provider_tool_names": ["filesystem_write"],
            "candidate_tool_names": ["web_fetch"],
            "candidate_tool_items": [{"tool_id": "web_fetch", "description": "fetch web pages"}],
            "hydrated_tool_names": ["filesystem_write"],
            "visible_skill_ids": ["find-skills"],
            "candidate_skill_ids": ["find-skills"],
            "rbac_visible_tool_names": ["filesystem_write", "web_fetch"],
            "rbac_visible_skill_ids": ["find-skills"],
            "cache_family_revision": "exp:prior",
            "frontdoor_selection_debug": {"query_text": "prior visible request"},
            "user_input": {
                "content": heartbeat_event_bundle,
                "metadata": {
                    "heartbeat_internal": True,
                    "heartbeat_stable_rules_text": heartbeat_rules_text,
                    "heartbeat_event_bundle_text": heartbeat_event_bundle,
                },
            },
        },
        runtime=runtime,
    )

    assert captured["resolver_called"] == {"actor_role": "ceo", "session_id": "web:shared"}
    assert captured["builder_kwargs"]["request_body_seed_messages"] == [
        {
            "role": "system",
            "content": heartbeat_rules_text,
            "metadata": {
                "source": "heartbeat",
                "prompt_visible": True,
                "ui_visible": False,
                "internal_prompt_kind": "heartbeat_rule",
            },
        }
    ]
    assert captured["builder_kwargs"]["user_content"] == heartbeat_event_bundle
    assert state_update["tool_names"] == ["exec"]
    assert state_update["provider_tool_names"] == ["exec"]
    assert state_update["pending_provider_tool_names"] == []
    assert state_update["provider_tool_exposure_pending"] is False
    assert state_update["cache_family_revision"] == "frontdoor:v1"


@pytest.mark.asyncio
async def test_web_session_heartbeat_prefers_acceptance_output_when_final_acceptance_failed(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-acceptance-output"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(outputs=[HEARTBEAT_OK, "已读取 acceptance 输出并整理回复。"])
    task_service = _TaskService()
    task_id = "task:demo-acceptance-output"
    task_service.tasks[task_id] = SimpleNamespace(
        task_id=task_id,
        root_node_id="node:root",
        metadata={
            "final_acceptance": {
                "required": True,
                "prompt": "检查最终结果",
                "node_id": "node:acceptance",
                "status": "failed",
            }
        },
        final_output="Execution Deliverable: root answer",
        final_output_ref="artifact:artifact:root-output",
        failure_reason="Acceptance Failure: evidence mismatch",
    )
    task_service.node_details[(task_id, "node:acceptance")] = {
        "item": {
            "node_id": "node:acceptance",
            "task_id": task_id,
            "node_kind": "acceptance",
            "final_output": "Acceptance node full output",
            "final_output_ref": "artifact:artifact:accept-output",
            "check_result": "acceptance failed",
            "failure_reason": "Acceptance Failure: evidence mismatch",
        }
    }
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    accepted = service.enqueue_task_terminal_payload(
        {
            "task_id": task_id,
            "session_id": session_id,
            "title": "demo acceptance output task",
            "status": "failed",
            "brief_text": "acceptance failed",
            "failure_reason": "Acceptance Failure: evidence mismatch",
            "finished_at": "2026-03-27T01:35:32+08:00",
            "dedupe_key": "task-terminal:task:demo-acceptance-output:failed:2026-03-27T01:35:32+08:00",
        }
    )
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    prompt_text = str(live_session.prompts[0].content)
    assert "Result node: acceptance node:acceptance" in prompt_text
    assert "Result source: acceptance_failed" in prompt_text
    assert "Result output: Acceptance node full output" in prompt_text
    assert "Result output ref: artifact:artifact:accept-output" in prompt_text

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert reloaded.messages[-1]["content"] == "已读取 acceptance 输出并整理回复。"
    assert reloaded.metadata.get("last_task_memory", {}).get("task_ids") == [task_id]
    assert reloaded.metadata["last_task_memory"]["task_results"] == [
        {
            "task_id": task_id,
            "node_id": "node:acceptance",
            "node_kind": "acceptance",
            "node_reason": "acceptance_failed",
            "output_excerpt": "Acceptance node full output",
            "output_ref": "artifact:artifact:accept-output",
            "check_result": "acceptance failed",
            "failure_reason": "Acceptance Failure: evidence mismatch",
        }
    ]


@pytest.mark.asyncio
async def test_web_session_heartbeat_final_reply_discards_preserved_user_turn(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-final-discard"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatFinalSession(output="Background install finished successfully.")
    task_service = _TaskService()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    payload = {
        "task_id": "task:demo-terminal",
        "session_id": session_id,
        "title": "demo terminal task",
        "status": "success",
        "brief_text": "task finished successfully",
        "finished_at": "2026-03-23T01:34:32+08:00",
        "dedupe_key": "task-terminal:task:demo-terminal:success:2026-03-23T01:34:32+08:00",
    }
    accepted = service.enqueue_task_terminal_payload(payload)
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert live_session.clear_calls == 1
    assert len(task_service.registry.published) == 2
    assert [envelope["type"] for _session, envelope in task_service.registry.published] == [
        "ceo.turn.discard",
        "ceo.reply.final",
    ]
    discard_session, discard_envelope = task_service.registry.published[0]
    final_session, final_envelope = task_service.registry.published[1]
    assert discard_session == session_id
    assert discard_envelope["data"]["source"] == "user"
    assert discard_envelope["data"]["turn_id"] == "turn-user-preserved"
    assert final_session == session_id
    assert final_envelope["data"]["source"] == "heartbeat"
    assert final_envelope["data"]["turn_id"] == "turn-heartbeat-final"
    assert "Background install finished successfully." in str(final_envelope["data"]["text"])
    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert reloaded.messages[-1]["role"] == "assistant"
    assert "Background install finished successfully." in str(reloaded.messages[-1]["content"])
    assert reloaded.messages[-1]["turn_id"] == "turn-heartbeat-final"


@pytest.mark.asyncio
async def test_web_session_heartbeat_calls_reply_notifier_for_final_output(tmp_path: Path) -> None:
    session_id = "china:qqbot:acct:user:peer"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatFinalSession(output="Background install finished successfully.")
    task_service = _TaskService()
    notified: list[tuple[str, str]] = []

    async def _notify(current_session_id: str, text: str) -> None:
        notified.append((current_session_id, text))

    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
        reply_notifier=_notify,
    )
    payload = {
        "task_id": "task:demo-terminal",
        "session_id": session_id,
        "title": "demo terminal task",
        "status": "success",
        "brief_text": "task finished successfully",
        "finished_at": "2026-03-23T01:34:32+08:00",
        "dedupe_key": "task-terminal:task:demo-terminal:success:2026-03-23T01:34:32+08:00",
    }
    accepted = service.enqueue_task_terminal_payload(payload)
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert notified == [(session_id, "Background install finished successfully.")]


@pytest.mark.asyncio
async def test_web_session_heartbeat_uses_persisted_transcript_turn_without_duplicate_visible_reply(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-persisted-runtime-turn"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _PersistingHeartbeatSession(
        session_manager=session_manager,
        session_id=session_id,
        output="Background install finished successfully.",
    )
    task_service = _TaskService()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    accepted = service.enqueue_task_terminal_payload(
        {
            "task_id": "task:demo-persisted-heartbeat",
            "session_id": session_id,
            "title": "demo persisted heartbeat",
            "status": "success",
            "brief_text": "task finished successfully",
            "finished_at": "2026-03-27T09:34:32+08:00",
            "dedupe_key": "task-terminal:task:demo-persisted-heartbeat:success:2026-03-27T09:34:32+08:00",
        }
    )
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    assert live_session.persist_transcript_flags == [True]
    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded.messages] == ["system", "user", "assistant"]
    assert reloaded.messages[0]["metadata"]["internal_prompt_kind"] == "heartbeat_rule"
    assert reloaded.messages[0]["metadata"]["ui_visible"] is False
    assert reloaded.messages[1]["metadata"]["internal_prompt_kind"] == "heartbeat_event_bundle"
    assert reloaded.messages[1]["metadata"]["ui_visible"] is False
    assert reloaded.messages[2]["content"] == "Background install finished successfully."
    assert reloaded.messages[2]["turn_id"] == live_session.turn_id
    assert reloaded.messages[2]["metadata"] == {
        "source": "heartbeat",
        "prompt_visible": True,
        "ui_visible": True,
        "reason": "task_terminal",
        "task_ids": ["task:demo-persisted-heartbeat"],
        "task_results": [{"task_id": "task:demo-persisted-heartbeat"}],
    }
    assert task_service.registry.published == []


def test_web_session_heartbeat_user_input_carries_full_event_bundle_text_for_continuation(tmp_path: Path) -> None:
    session_manager = SessionManager(tmp_path)
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=SimpleNamespace(get_or_create=lambda **kwargs: None),
        main_task_service=_TaskService(),
        session_manager=session_manager,
    )
    events = [
        SimpleNamespace(
            reason="task_terminal",
            payload={
                "task_id": "task:demo-terminal",
                "session_id": "web:shared",
                "title": "demo terminal task",
                "status": "success",
                "brief_text": "task finished successfully",
            },
        )
    ]

    user_input = service._build_heartbeat_user_input(
        events,
        heartbeat_reason="task_terminal",
        normalized_metadata={},
    )

    assert "[SESSION EVENTS]" in str(user_input.content)
    assert user_input.metadata["heartbeat_event_bundle_text"] == str(user_input.content)
    assert str(user_input.metadata["heartbeat_stable_rules_text"]).strip()


@pytest.mark.asyncio
async def test_web_session_heartbeat_second_visible_reply_is_not_appended_after_normal_assistant_reply(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-no-second-visible-reply"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    persisted.add_message("user", "Install the weather skill")
    persisted.add_message("assistant", "I started the install.")
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(output="Background install finished successfully.")
    task_service = _TaskService()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    accepted = service.enqueue_task_terminal_payload(
        {
            "task_id": "task:demo-hidden-persist",
            "session_id": session_id,
            "title": "demo hidden persist task",
            "status": "success",
            "brief_text": "task finished successfully",
            "finished_at": "2026-03-27T09:34:32+08:00",
            "dedupe_key": "task-terminal:task:demo-hidden-persist:success:2026-03-27T09:34:32+08:00",
        }
    )
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert len(reloaded.messages) == 3
    assert [message["role"] for message in reloaded.messages] == ["user", "assistant", "assistant"]
    assert reloaded.messages[1]["content"] == "I started the install."
    assert "Background install finished successfully." in str(reloaded.messages[2]["content"])
    assert reloaded.messages[2]["turn_id"] == "turn-heartbeat-default"
    assert web_ceo_sessions.transcript_messages(reloaded) == [
        {"role": "user", "content": "Install the weather skill", "timestamp": reloaded.messages[0]["timestamp"]},
        {"role": "assistant", "content": "I started the install.", "timestamp": reloaded.messages[1]["timestamp"]},
        {
            "role": "assistant",
            "content": "Background install finished successfully.",
            "timestamp": reloaded.messages[2]["timestamp"],
                "metadata": {
                    "source": "heartbeat",
                    "prompt_visible": True,
                    "ui_visible": True,
                    "reason": "task_terminal",
                    "task_ids": ["task:demo-hidden-persist"],
                    "task_results": [{"task_id": "task:demo-hidden-persist"}],
                },
            "turn_id": "turn-heartbeat-default",
        },
    ]
    assert reloaded.metadata["last_task_memory"] == {
        "version": web_ceo_sessions.TASK_MEMORY_VERSION,
        "task_ids": ["task:demo-hidden-persist"],
        "source": "heartbeat",
        "reason": "task_terminal",
        "updated_at": reloaded.metadata["last_task_memory"]["updated_at"],
        "task_results": [{"task_id": "task:demo-hidden-persist"}],
    }


def test_web_ceo_session_summary_helpers_exclude_hidden_heartbeat_reply_surfaces() -> None:
    session = SimpleNamespace(
        key="web:summary-hidden-heartbeat",
        created_at="2026-03-27T08:00:00+08:00",
        updated_at="",
        metadata={"title": "Visible Session", "last_preview_text": ""},
        messages=[
            {
                "role": "user",
                "content": "Visible user request",
                "timestamp": "2026-03-27T08:00:00+08:00",
            },
            {
                "role": "assistant",
                "content": "Visible assistant reply",
                "timestamp": "2026-03-27T08:01:00+08:00",
            },
            {
                "role": "assistant",
                "content": "Hidden heartbeat reply",
                "timestamp": "2026-03-27T08:02:00+08:00",
                "metadata": {"source": "heartbeat", "history_visible": False},
            },
        ],
    )
    normalized_metadata = web_ceo_sessions.normalize_ceo_metadata(session.metadata, session_key=session.key)

    summary = web_ceo_sessions.build_session_summary(
        session,
        is_active=False,
        normalized_metadata=normalized_metadata,
    )

    assert summary["preview_text"] == "Visible assistant reply"
    assert summary["message_count"] == 2
    assert summary["updated_at"] == "2026-03-27T08:01:00+08:00"
    assert summary["last_llm_output_at"] == "2026-03-27T08:01:00+08:00"
    assert web_ceo_sessions._channel_preview_text(session) == "Visible assistant reply"
    assert web_ceo_sessions._session_updated_at(session) == "2026-03-27T08:01:00+08:00"
    assert web_ceo_sessions._session_last_assistant_at(session) == "2026-03-27T08:01:00+08:00"

    hidden_only_session = SimpleNamespace(
        key="web:hidden-only-heartbeat",
        created_at="2026-03-27T09:00:00+08:00",
        updated_at="2026-03-27T09:05:00+08:00",
        metadata={"title": "Hidden Only", "last_preview_text": "Hidden heartbeat reply"},
        messages=[
            {
                "role": "assistant",
                "content": "Hidden heartbeat reply",
                "timestamp": "2026-03-27T09:05:00+08:00",
                "metadata": {"source": "heartbeat", "history_visible": False},
            }
        ],
    )
    hidden_only_metadata = web_ceo_sessions.normalize_ceo_metadata(
        hidden_only_session.metadata,
        session_key=hidden_only_session.key,
    )

    hidden_only_summary = web_ceo_sessions.build_session_summary(
        hidden_only_session,
        is_active=False,
        normalized_metadata=hidden_only_metadata,
    )

    assert hidden_only_summary["preview_text"] == ""
    assert hidden_only_summary["message_count"] == 0
    assert hidden_only_summary["updated_at"] == ""
    assert hidden_only_summary["last_llm_output_at"] == ""
    assert web_ceo_sessions._channel_preview_text(hidden_only_session) == ""
    assert web_ceo_sessions._session_updated_at(hidden_only_session) == ""
    assert web_ceo_sessions._session_last_assistant_at(hidden_only_session) == ""


def test_websocket_build_ceo_snapshot_keeps_archived_paused_assistant_status_for_ui_restore() -> None:
    summary = {
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_goal": "inspect repository",
                "rounds": [],
            }
        ]
    }

    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {
                "role": "assistant",
                "content": "已暂停",
                "turn_id": "paused-turn-1",
                "status": "paused",
                "canonical_context": summary,
                "metadata": {
                    "history_visible": False,
                    "source": "manual_pause_archive",
                },
            }
        ]
    )

    assert snapshot == [
        {
            "role": "assistant",
            "content": "已暂停",
            "turn_id": "paused-turn-1",
            "status": "paused",
            "canonical_context": summary,
            "canonical_context_delta": summary,
        }
    ]


def test_websocket_build_ceo_snapshot_hides_internal_prompt_messages_but_keeps_visible_reply() -> None:
    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {
                "role": "system",
                "content": "Hidden heartbeat rules",
                "timestamp": "2026-03-27T09:00:00+08:00",
                "metadata": {
                    "source": "heartbeat",
                    "prompt_visible": True,
                    "ui_visible": False,
                    "internal_prompt_kind": "heartbeat_rule",
                },
            },
            {
                "role": "user",
                "content": "Hidden heartbeat event bundle",
                "timestamp": "2026-03-27T09:00:05+08:00",
                "metadata": {
                    "source": "heartbeat",
                    "heartbeat_internal": True,
                    "prompt_visible": True,
                    "ui_visible": False,
                    "internal_prompt_kind": "heartbeat_event_bundle",
                },
            },
            {
                "role": "assistant",
                "content": "Visible heartbeat summary",
                "timestamp": "2026-03-27T09:00:10+08:00",
                "turn_id": "turn-heartbeat-visible",
                "metadata": {
                    "source": "heartbeat",
                    "prompt_visible": True,
                    "ui_visible": True,
                },
            },
        ]
    )

    assert snapshot == [
        {
            "role": "assistant",
            "content": "Visible heartbeat summary",
            "turn_id": "turn-heartbeat-visible",
            "timestamp": "2026-03-27T09:00:10+08:00",
        }
    ]


def test_websocket_build_ceo_snapshot_hides_pending_user_messages_while_running_turn_exists() -> None:
    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {
                "role": "user",
                "content": "Current running request",
                "timestamp": "2026-04-21T10:00:00+08:00",
                "metadata": {
                    "_transcript_turn_id": "turn-live-1",
                    "_transcript_state": "pending",
                },
            },
            {
                "role": "user",
                "content": "Queued follow-up",
                "timestamp": "2026-04-21T10:00:05+08:00",
                "metadata": {
                    "_transcript_turn_id": "turn-follow-up-1",
                    "_transcript_state": "pending",
                },
            },
        ],
        inflight_turn={
            "turn_id": "turn-live-1",
            "source": "user",
            "status": "running",
            "user_messages": [
                {"role": "user", "content": "Current running request"},
            ],
        },
    )

    assert snapshot == []


def test_context_assembly_always_keeps_tool_execution_control_tools_visible() -> None:
    service = CeoMessageBuilder(
        loop=SimpleNamespace(),
        prompt_builder=SimpleNamespace(build=lambda **kwargs: ""),
    )

    selected, trace = service._select_tools(
        query_text="install a skill and maybe stop it if needed",
        visible_names=[
            "create_async_task",
            "skill-installer",
            "stop_tool_execution",
        ],
        visible_families=[],
        core_tools={"create_async_task"},
        extension_top_k=1,
    )

    assert "stop_tool_execution" in selected
    assert "wait_tool_execution" not in selected
    assert "create_async_task" in selected
    assert trace["reserved_internal_tool_names"] == ["stop_tool_execution"]
