from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.session_service import HEARTBEAT_OK, WebSessionHeartbeatService
from g3ku.runtime.context.assembly import ContextAssemblyService
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.api import websocket_ceo
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import SessionManager


class _Registry:
    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self.published: list[tuple[str, dict[str, object]]] = []

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


class _TaskService:
    def __init__(self) -> None:
        self.registry = _Registry()

    async def startup(self) -> None:
        return None


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


class _HeartbeatRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def enqueue_tool_background(self, *, session_id: str, payload: dict[str, object]) -> None:
        self.calls.append((session_id, dict(payload)))


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


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")
    return app


def _mock_workspace(monkeypatch, workspace: Path) -> None:
    monkeypatch.setattr(websocket_ceo, "workspace_path", lambda: workspace)
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: workspace)


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
    assert [item["kind"] for item in snapshot["tool_events"]] == ["", "tool_background"]
    assert all(item["text"] != "watchdog synthetic update" for item in snapshot["tool_events"])


@pytest.mark.asyncio
async def test_runtime_agent_session_persists_tool_events_into_session_transcript(
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
    assert [item["status"] for item in reloaded_session.messages[1]["tool_events"]] == ["running", "success"]
    assert reloaded_session.messages[1]["tool_events"][0]["tool_name"] == "skill-installer"


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

        messages = [ws.receive_json(), ws.receive_json(), ws.receive_json()]

    final_events = [item for item in messages if item["type"] == "ceo.reply.final"]
    assert len(final_events) == 1
    assert final_events[0]["data"]["text"] == "I will keep waiting for the install."


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
            "recommended_wait_seconds": 0.05,
            "runtime_snapshot": {"summary_text": "still fetching remote repository"},
        },
    )
    service._started = True

    initial_delay = await service._run_session(session_id)
    assert initial_delay is not None
    assert initial_delay > 0
    assert live_session.prompts == []

    await asyncio.sleep(0.06)

    next_delay = await service._run_session(session_id)

    assert next_delay is not None
    assert next_delay > 0
    assert len(live_session.prompts) == 1
    prompt = live_session.prompts[0]
    assert isinstance(prompt, UserInputMessage)
    assert "tool-exec:1" in str(prompt.content)
    assert "already been refreshed" in str(prompt.content)
    assert "Do not call wait_tool_execution" in str(prompt.content)
    assert manager.calls == [("tool-exec:1", 0.1)]
    published_types = [envelope["type"] for _session_id, envelope in task_service.registry.published]
    assert "ceo.turn.discard" in published_types

    await asyncio.sleep(0.08)

    assert len(live_session.prompts) >= 2
    assert manager.calls[:2] == [("tool-exec:1", 0.1), ("tool-exec:1", 0.1)]


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


def test_context_assembly_always_keeps_tool_execution_control_tools_visible() -> None:
    service = ContextAssemblyService(
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
