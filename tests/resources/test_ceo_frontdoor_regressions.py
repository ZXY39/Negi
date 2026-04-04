from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import g3ku.shells.web as web_shell
from g3ku.agent.tools.base import Tool
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.context.types import ContextAssemblyResult
from g3ku.runtime.frontdoor._ceo_langgraph_impl import _build_args_schema
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import SessionManager


class _IngestRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ingest_turn(self, **kwargs) -> None:
        self.calls.append(dict(kwargs))


class _CommitRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def maybe_commit(self, **kwargs):
        self.calls.append(dict(kwargs))
        return None


class _MultiAgentRunner:
    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        _ = user_input, on_progress
        setattr(session, "_last_route_kind", "direct_reply")
        return "assistant reply"


class _FakeToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in list(tools)}
        self.tool_names = sorted(self._tools)
        self.runtime_contexts: list[dict[str, object]] = []

    def get(self, name: str):
        return self._tools.get(str(name or "").strip())

    def push_runtime_context(self, context: dict[str, object]):
        self.runtime_contexts.append(dict(context))
        return object()

    def pop_runtime_context(self, token) -> None:
        _ = token


class _BackendRecorder:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)


class _RecordingTool(Tool):
    def __init__(self, name: str, sink: list[tuple[str, str]]) -> None:
        self._name = name
        self._sink = sink

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"record {self._name}"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, value: str, **kwargs) -> str:
        _ = kwargs
        self._sink.append((self._name, value))
        return json.dumps({"ok": True, "tool": self._name, "value": value}, ensure_ascii=False)


class _CountTool(Tool):
    def __init__(self, sink: list[int]) -> None:
        self._sink = sink

    @property
    def name(self) -> str:
        return "count_tool"

    @property
    def description(self) -> str:
        return "record integer counts"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
            },
            "required": ["count"],
        }

    async def execute(self, count: int, **kwargs) -> str:
        _ = kwargs
        self._sink.append(int(count))
        return json.dumps({"ok": True, "count": int(count)}, ensure_ascii=False)


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
                "continuation_of_task_id": {"type": "string"},
                "reuse_existing": {"type": "boolean"},
            },
            "required": ["task", "core_requirement", "execution_policy"],
        }

    async def execute(self, task: str, core_requirement: str, execution_policy: dict[str, object], **kwargs) -> str:
        _ = task, core_requirement, execution_policy, kwargs
        return "创建任务成功task:demo-123"


def _assembly_result(*, tool_names: list[str], recent_history: list[dict[str, object]] | None = None, trace: dict[str, object] | None = None) -> ContextAssemblyResult:
    return ContextAssemblyResult(
        system_prompt="SYSTEM PROMPT",
        recent_history=list(recent_history or []),
        tool_names=tool_names,
        trace=dict(trace or {}),
    )


def test_build_args_schema_preserves_declared_json_types() -> None:
    schema_model = _build_args_schema(_ContinuationTaskTool())
    schema = schema_model.model_json_schema()
    properties = dict(schema.get("properties") or {})

    continuation_schema = dict(properties.get("continuation_of_task_id") or {})
    reuse_schema = dict(properties.get("reuse_existing") or {})
    task_schema = dict(properties.get("task") or {})

    continuation_types = {
        str(item.get("type") or "").strip()
        for item in list(continuation_schema.get("anyOf") or [])
        if isinstance(item, dict)
    }
    reuse_types = {
        str(item.get("type") or "").strip()
        for item in list(reuse_schema.get("anyOf") or [])
        if isinstance(item, dict)
    }

    assert task_schema.get("type") == "string"
    assert "string" in continuation_types or continuation_schema.get("type") == "string"
    assert "boolean" in reuse_types or reuse_schema.get("type") == "boolean"


@pytest.mark.asyncio
async def test_runtime_agent_session_prompt_keeps_rag_ingest_payload_raw_and_skips_commit(tmp_path, monkeypatch) -> None:
    async def _noop_refresh(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    async def _noop_cancel(session_key: str) -> None:
        _ = session_key
        return None

    monkeypatch.setattr(web_shell, "refresh_web_agent_runtime", _noop_refresh)
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    memory_manager = _IngestRecorder()
    commit_service = _CommitRecorder()
    session_manager = SessionManager(tmp_path)
    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        multi_agent_runner=_MultiAgentRunner(),
        sessions=session_manager,
        memory_manager=memory_manager,
        prompt_trace=False,
        commit_service=commit_service,
        create_session_cancellation_token=lambda session_key: SimpleNamespace(cancel=lambda reason=None: None),
        release_session_cancellation_token=lambda session_key, token: None,
        cancel_session_tasks=_noop_cancel,
    )
    runtime_session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")

    result = await runtime_session.prompt("what changed?")

    assert result.output == "assistant reply"
    assert memory_manager.calls == [
        {
            "session_key": "web:shared",
            "channel": "web",
            "chat_id": "shared",
            "messages": [
                {"role": "user", "content": "what changed?"},
                {"role": "assistant", "content": "assistant reply"},
            ],
        }
    ]
    assert commit_service.calls == []


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_directly_executes_visible_tool_without_stage(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    executed: list[tuple[str, str]] = []
    backend = _BackendRecorder(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="record_tool",
                        arguments={"value": "alpha"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="deliver-1",
                        name="deliver_final_answer",
                        arguments={"answer": "done", "disposition": "completed"},
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_RecordingTool("record_tool", executed)]),
        max_iterations=8,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["record_tool"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return _assembly_result(tool_names=["record_tool"])

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: backend)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content="use the tool"), session=session)

    assert output == "done"
    assert executed == [("record_tool", "alpha")]
    assert len(backend.calls) == 2


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_does_not_duplicate_current_user_when_builder_already_includes_it(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder([LLMResponse(content="done", finish_reason="stop")])
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([]),
        max_iterations=8,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": []}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return _assembly_result(
            tool_names=[],
            recent_history=[
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "follow up"},
            ],
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: backend)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content="follow up"), session=session)

    assert output == "done"
    messages = list(backend.calls[0]["messages"] or [])
    assert [str(item.get("content") or "") for item in messages].count("follow up") == 1


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_executes_xml_tool_call_directly_without_repair(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    executed: list[tuple[str, str]] = []
    backend = _BackendRecorder(
        [
            LLMResponse(
                content='<minimax:tool_call><invoke name="record_tool"><parameter name="value">alpha</parameter></invoke></minimax:tool_call>',
                tool_calls=[],
                finish_reason="stop",
            ),
            LLMResponse(content="repair succeeded", finish_reason="stop"),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_RecordingTool("record_tool", executed)]),
        max_iterations=8,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["record_tool"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return _assembly_result(tool_names=["record_tool"])

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: backend)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content="repair xml please"), session=session)

    assert output == "repair succeeded"
    assert executed == [("record_tool", "alpha")]
    assert len(backend.calls) == 2
    assert not any(
        "XML-style pseudo tool calling" in str(item.get("content") or "")
        for item in list(backend.calls[1].get("messages") or [])
    )


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_repairs_xml_tool_call_via_json_payload_after_local_extraction_fails(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    executed: list[int] = []
    backend = _BackendRecorder(
        [
            LLMResponse(
                content='<minimax:tool_call><invoke name="count_tool"><parameter name="count">oops</parameter></invoke></minimax:tool_call>',
                tool_calls=[],
                finish_reason="stop",
            ),
            LLMResponse(
                content='{"name":"count_tool","arguments":{"count":2}}',
                tool_calls=[],
                finish_reason="stop",
            ),
            LLMResponse(content="repair succeeded", finish_reason="stop"),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_CountTool(executed)]),
        max_iterations=8,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["count_tool"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return _assembly_result(tool_names=["count_tool"])

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: backend)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content="repair xml please"), session=session)

    assert output == "repair succeeded"
    assert executed == [2]
    assert len(backend.calls) == 3
    repair_messages = list(backend.calls[1].get("messages") or [])
    assert any("XML-style pseudo tool calling" in str(item.get("content") or "") for item in repair_messages)


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_retries_empty_turn_until_valid_result(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))

    monkeypatch.setattr('g3ku.runtime.frontdoor.ceo_runner.asyncio.sleep', _fake_sleep)

    backend = _BackendRecorder(
        [
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_RecordingTool("record_tool", [])]),
        max_iterations=8,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["record_tool"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return _assembly_result(tool_names=["record_tool"])

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: backend)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content="try again"), session=session)

    assert output == "done"
    assert len(backend.calls) == 3
    assert sleep_calls == [1.0, 2.0]


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_finishes_turn_after_successful_async_task_dispatch(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="create_async_task",
                        arguments={
                            "task": "搜索上下文管理 skill",
                            "core_requirement": "确认是否存在可用的上下文管理类 skill 并给出建议",
                            "execution_policy": {"mode": "focus"},
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_ContinuationTaskTool()]),
        max_iterations=8,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["create_async_task"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return _assembly_result(tool_names=["create_async_task"])

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: backend)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content="帮我查有没有上下文管理 skill"), session=session)

    assert "task:demo-123" in output
    assert "异步任务" in output
    assert len(backend.calls) == 1
