from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.session_service import HEARTBEAT_OK, WebSessionHeartbeatService
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.api import ceo_sessions, websocket_ceo
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted, CeoPendingInterrupt
from g3ku.runtime.manager import SessionRuntimeManager
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import SessionManager


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
        self.continuation_tasks: dict[tuple[str, str], object] = {}
        self.retry_calls: list[str] = []

    async def startup(self) -> None:
        return None

    def _mark_task_terminal_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        self.delivered.append((str(dedupe_key or ""), str(delivered_at or "")))

    def get_task(self, task_id: str):
        return self.tasks.get(str(task_id or "").strip())

    def get_node_detail_payload(self, task_id: str, node_id: str):
        key = (str(task_id or "").strip(), str(node_id or "").strip())
        return self.node_details.get(key)

    def find_reusable_continuation_task(self, *, session_id: str, continuation_of_task_id: str):
        key = (str(session_id or '').strip(), str(continuation_of_task_id or '').strip())
        return self.continuation_tasks.get(key)

    async def retry_task(self, task_id: str):
        normalized = str(task_id or "").strip()
        self.retry_calls.append(normalized)
        current = self.tasks.get(normalized)
        if current is None:
            return None
        current.status = "in_progress"
        return current


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
    def __init__(self, *, output: str = HEARTBEAT_OK) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompts: list[UserInputMessage] = []
        self._listeners = set()
        self._output = output

    def subscribe(self, listener):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    async def prompt(self, user_message, persist_transcript: bool = False) -> SimpleNamespace:
        _ = persist_transcript
        self.prompts.append(user_message)
        return SimpleNamespace(output=self._output)


class _FakeHeartbeatFinalSession(_FakeHeartbeatSession):
    def __init__(self, *, output: str = "Background task finished.") -> None:
        super().__init__(output=output)
        self._preserved_snapshot: dict[str, object] | None = {
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
                values={"tool_call_payloads": [{"name": "create_async_task"}]},
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
    assert snapshot["user_message"]["content"] == "Install the weather skill"
    assert snapshot["assistant_text"] == "Still installing dependencies..."
    tools = snapshot["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]
    assert [item["tool_name"] for item in tools] == ["skill-installer"]


@pytest.mark.asyncio
async def test_runtime_agent_session_hides_cron_internal_prompt_but_persists_reply(
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
            metadata={"cron_internal": True, "cron_job_id": "job-77"},
        )
    )

    inflight = captured["snapshot"]
    assert isinstance(inflight, dict)
    assert inflight["source"] == "cron"
    assert "user_message" not in inflight
    tools = inflight["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]
    assert [item["tool_name"] for item in tools] == ["cron"]

    persisted = loop.sessions.get_or_create("web:shared")
    assert [message["role"] for message in persisted.messages] == ["assistant"]
    assert persisted.messages[0]["content"] == "Scheduled progress update."
    assert persisted.messages[0]["metadata"] == {"source": "cron", "cron_job_id": "job-77"}

    message_end = next(event for event in events if event.type == "message_end")
    assert message_end.payload["source"] == "cron"
    assert message_end.payload["heartbeat_internal"] is False


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
    paused_state = next(
        event
        for event in events
        if event.type == "state_snapshot"
        and str((event.payload.get("state") or {}).get("status") or "") == "paused"
    )
    assert pause_ack.payload["manual_pause_waiting_reason"] is True
    assert pause_ack.payload["source"] == "user"
    assert paused_state.payload["state"]["manual_pause_waiting_reason"] is True
    assert all(event.type != "message_end" for event in events)
    assert heartbeat.clear_calls == ["web:pause-manual"]
    assert session.manual_pause_waiting_reason() is True
    assert session.inflight_turn_snapshot() is None
    assert web_ceo_sessions.read_inflight_turn_snapshot("web:pause-manual") is None
    paused_snapshot = session.paused_execution_context_snapshot()
    assert paused_snapshot is not None
    assert paused_snapshot["status"] == "paused"
    assert paused_snapshot["user_message"]["content"] == "Pause and wait"
    assert web_ceo_sessions.read_paused_execution_context("web:pause-manual") is not None

    reloaded = SessionManager(tmp_path).get_or_create("web:pause-manual")
    assert [message["role"] for message in reloaded.messages] == ["user"]
    assert reloaded.messages[0]["content"] == "Pause and wait"
    normalized_metadata = web_ceo_sessions.normalize_ceo_metadata(reloaded.metadata, session_key="web:pause-manual")
    assert normalized_metadata["manual_pause_waiting_reason"] is True


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
    assert [message["role"] for message in reloaded.messages] == ["user"]
    assert reloaded.messages[0]["content"] == "Pause without duplicate transcript"
    assert reloaded.messages[0]["metadata"]["_transcript_state"] == "pending"


@pytest.mark.asyncio
async def test_runtime_agent_session_follow_up_after_manual_pause_uses_new_transcript_turn(
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

    class _AnswerRunner:
        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, session, on_progress
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
    result = await session.prompt("Why no async task?")

    assert result.output == "Because this follow-up only needed a direct explanation."

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in reloaded.messages] == ["user", "user", "assistant"]
    assert reloaded.messages[0]["content"] == "Original paused request"
    assert reloaded.messages[0]["metadata"]["_transcript_state"] == "pending"
    assert reloaded.messages[1]["content"] == "Why no async task?"
    assert reloaded.messages[1]["metadata"]["_transcript_state"] == "completed"
    assert reloaded.messages[2]["content"] == "Because this follow-up only needed a direct explanation."
    first_turn_id = str(reloaded.messages[0]["metadata"]["_transcript_turn_id"]).strip()
    second_turn_id = str(reloaded.messages[1]["metadata"]["_transcript_turn_id"]).strip()
    assert first_turn_id
    assert second_turn_id
    assert first_turn_id != second_turn_id


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
        {"manual_pause_waiting_reason": True},
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
    assert heartbeat.replay_calls == [session_id]
    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    normalized_metadata = web_ceo_sessions.normalize_ceo_metadata(reloaded.metadata, session_key=session_id)
    assert normalized_metadata["manual_pause_waiting_reason"] is False


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
    assert recreated.resume_payloads == [{"approved": True}]
    assert response.json()["output"] == "approved reply"


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

    snapshot = session.inflight_turn_snapshot()

    assert snapshot is not None
    tools = snapshot["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]
    assert [item["kind"] for item in tools] == ["tool_background"]
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
    assert tool["status"] == "success"
    assert tool["text"] == "completed"


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
    tools = reloaded_session.messages[1]["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]
    assert [item["status"] for item in tools] == ["success"]
    assert tools[0]["tool_name"] == "skill-installer"


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
    tools = reloaded_session.messages[1]["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"]
    assert [item["status"] for item in tools] == ["running"]

    recent_history = web_ceo_sessions.extract_live_raw_tail(reloaded_session, turn_limit=4)
    assert recent_history[-2] == {"role": "user", "content": "Open bilibili"}
    assert "运行出错：CEO frontdoor exceeded maximum iterations" in recent_history[-1]["content"]


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
        assert inflight_turn["assistant_text"] == "Working on it..."
        tool = inflight_turn["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"][0]
        assert tool["tool_name"] == "skill-installer"
        assert tool["tool_call_id"] == "skill-installer:1"
        assert "interaction_trace" not in inflight_turn
        assert "stage" not in inflight_turn

        final_payload, _seen = _recv_until(ws, lambda payload: payload.get("type") == "ceo.reply.final")

    assert final_payload["data"]["text"] == "Still working."


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
            and isinstance(payload.get("data", {}).get("inflight_turn", {}).get("execution_trace_summary"), dict)
            and list(
                (payload.get("data", {}).get("inflight_turn", {}).get("execution_trace_summary", {}) or {})
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
            and str(payload.get("data", {}).get("state", {}).get("status") or "") == "paused",
        )
        pause_ack = next(item for item in seen if item.get("type") == "ceo.control_ack")
        assert pause_ack["data"]["manual_pause_waiting_reason"] is True
        assert pause_ack["data"]["source"] == "user"
        assert paused_state["data"]["state"]["manual_pause_waiting_reason"] is True
        assert all(item.get("type") != "ceo.reply.final" for item in seen)

    holder.manager = SessionRuntimeManager(agent)

    with client.websocket_connect(f"/api/ws/ceo?session_id={session_id}") as ws:
        snapshot, _seen = _recv_until(ws, lambda payload: payload.get("type") == "snapshot.ceo")

    inflight_turn = snapshot["data"].get("inflight_turn")
    assert isinstance(inflight_turn, dict)
    assert inflight_turn["status"] == "paused"
    assert inflight_turn["user_message"]["content"] == "Pause and restore me"
    tool = inflight_turn["execution_trace_summary"]["stages"][0]["rounds"][0]["tools"][0]
    assert tool["tool_name"] == "skill-installer"
    assert [message["role"] for message in snapshot["data"].get("messages", [])] == ["user"]
    assert snapshot["data"]["messages"][0]["content"] == "Pause and restore me"
    persisted = SessionManager(tmp_path).get_or_create(session_id)
    assert [message["role"] for message in persisted.messages] == ["user"]
    assert persisted.messages[0]["content"] == "Pause and restore me"
    assert agent.web_session_heartbeat.clear_calls == [session_id]


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


def test_ceo_websocket_filters_heartbeat_internal_message_end() -> None:
    assert websocket_ceo._should_forward_message_end(
        {"role": "assistant", "text": "normal reply", "heartbeat_internal": False}
    ) is True
    assert websocket_ceo._should_forward_message_end(
        {"role": "assistant", "text": HEARTBEAT_OK, "heartbeat_internal": True}
    ) is False
    assert websocket_ceo._should_forward_message_end(
        {"role": "assistant", "text": HEARTBEAT_OK}
    ) is False


def test_ceo_tool_event_serializers_preserve_source() -> None:
    serialized = websocket_ceo._serialize_tool_event(
        AgentEvent(
            type="tool_execution_start",
            timestamp="2026-03-28T01:00:00",
            payload={"tool_name": "skill-installer", "text": "started", "source": "heartbeat"},
        )
    )
    normalized = websocket_ceo._normalize_snapshot_tool_events(
        [{"tool_name": "skill-installer", "text": "started", "source": "heartbeat"}]
    )

    assert serialized is not None
    assert serialized["source"] == "heartbeat"
    assert normalized[0]["source"] == "heartbeat"


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
    assert "Do not call wait_tool_execution" in str(prompt.content)
    assert "任务终结结果意味着任务已达到最终状态" in str(prompt.content)
    assert manager.calls == [("tool-exec:1", 0.1)]
    published_types = [envelope["type"] for _session_id, envelope in task_service.registry.published]
    assert "ceo.turn.discard" in published_types

    await asyncio.sleep(0.22)

    assert len(live_session.prompts) >= 2
    assert manager.calls[:2] == [("tool-exec:1", 0.1), ("tool-exec:1", 0.1)]


def test_web_session_heartbeat_skips_enqueues_while_waiting_for_manual_pause_reason(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-manual-pause"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    persisted.metadata = web_ceo_sessions.normalize_ceo_metadata(
        {"manual_pause_waiting_reason": True},
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

    assert accepted_terminal is False
    assert accepted_stall is False
    assert service._events.peek(session_id) == []


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
async def test_web_session_heartbeat_forces_task_terminal_reply_when_model_returns_heartbeat_ok(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-fallback"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(output=HEARTBEAT_OK)
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
    assert len(live_session.prompts) == 1
    assert service._events.peek(session_id) == []
    assert len(task_service.registry.published) == 1
    published_session, envelope = task_service.registry.published[0]
    assert published_session == session_id
    assert envelope["type"] == "ceo.reply.final"
    assert "demo-terminal" in str(envelope["data"]["text"])
    assert "已完成" in str(envelope["data"]["text"])
    assert len(task_service.delivered) == 1
    assert task_service.delivered[0][0] == "task-terminal:task:demo-terminal:success:2026-03-23T01:34:32+08:00"

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert reloaded.messages[-1]["role"] == "assistant"
    assert reloaded.messages[-1]["metadata"]["source"] == "heartbeat"
    assert "demo-terminal" in str(reloaded.messages[-1]["content"])
    assert "已完成" in str(reloaded.messages[-1]["content"])


@pytest.mark.asyncio
async def test_web_session_heartbeat_reports_unpassed_continuation_task_in_fallback_reply(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-unpassed-continuation"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(output=HEARTBEAT_OK)
    task_service = _TaskService()
    task_service.continuation_tasks[(session_id, "task:demo-unpassed")] = SimpleNamespace(task_id="task:cont-2")
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
    assert envelope["data"]["text"] == "任务 `demo-unpassed` 已完成但未通过验收，已经续跑为 `cont-2`，我会继续推进。"

    reloaded = SessionManager(tmp_path).get_or_create(session_id)
    assert reloaded.messages[-1]["metadata"]["source"] == "heartbeat"
    assert reloaded.messages[-1]["metadata"]["task_ids"] == ["task:demo-unpassed", "task:cont-2"]
    assert reloaded.messages[-1]["content"] == "任务 `demo-unpassed` 已完成但未通过验收，已经续跑为 `cont-2`，我会继续推进。"


@pytest.mark.asyncio
async def test_web_session_heartbeat_does_not_report_continuation_for_engine_failure(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-engine-failure"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(output=HEARTBEAT_OK)
    task_service = _TaskService()
    task_service.continuation_tasks[(session_id, "task:demo-engine-failed")] = SimpleNamespace(task_id="task:cont-3")
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
    assert envelope["data"]["text"] == "任务 `demo-engine-failed` 已失败：model provider failed"


@pytest.mark.asyncio
async def test_web_session_heartbeat_auto_retries_engine_failure_in_place(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-engine-retry"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(output=HEARTBEAT_OK)
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
    assert task_service.retry_calls == [task_id]
    assert len(task_service.registry.published) == 1
    published_session, envelope = task_service.registry.published[0]
    assert published_session == session_id
    assert envelope["type"] == "ceo.reply.final"
    assert envelope["data"]["text"] == "任务 `demo-engine-retry` 遇到工程故障，已在原任务内继续重试。"


@pytest.mark.asyncio
async def test_web_session_heartbeat_prompt_includes_terminal_root_output_and_metadata(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-root-output"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(output=HEARTBEAT_OK)
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
    assert reloaded.messages[-1]["metadata"]["task_results"] == [
        {
            "task_id": task_id,
            "node_id": "node:root",
            "node_kind": "execution",
            "node_reason": "root_terminal",
            "output": "Top 3 recommendation list",
            "output_ref": "artifact:artifact:root-output",
            "check_result": "accepted",
        }
    ]


@pytest.mark.asyncio
async def test_web_session_heartbeat_prefers_acceptance_output_when_final_acceptance_failed(tmp_path: Path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-acceptance-output"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(output=HEARTBEAT_OK)
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
    assert reloaded.messages[-1]["metadata"]["task_results"] == [
        {
            "task_id": task_id,
            "node_id": "node:acceptance",
            "node_kind": "acceptance",
            "node_reason": "acceptance_failed",
            "output": "Acceptance node full output",
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
    assert final_session == session_id
    assert final_envelope["data"]["source"] == "heartbeat"
    assert "Background install finished successfully." in str(final_envelope["data"]["text"])


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
            "wait_tool_execution",
        ],
        visible_families=[],
        core_tools={"create_async_task"},
        extension_top_k=1,
    )

    assert "stop_tool_execution" in selected
    assert "wait_tool_execution" in selected
    assert "create_async_task" in selected
    assert trace["reserved"] == ["stop_tool_execution", "wait_tool_execution"]
