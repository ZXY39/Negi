from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

if "litellm" not in sys.modules:
    litellm_stub = types.ModuleType("litellm")

    async def _unreachable_acompletion(*args, **kwargs):
        raise AssertionError("litellm acompletion should not be used in CEO regression tests")

    litellm_stub.acompletion = _unreachable_acompletion
    litellm_stub.api_base = None
    litellm_stub.suppress_debug_info = True
    litellm_stub.drop_params = True
    sys.modules["litellm"] = litellm_stub

import g3ku.shells.web as web_shell
from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.context.types import ContextAssemblyResult
from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor._ceo_runtime_ops import _build_args_schema
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


class _TaskControlTool(Tool):
    @property
    def name(self) -> str:
        return "task_control"

    @property
    def description(self) -> str:
        return "continue an existing task"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["pause", "resume"]},
                "target_task_id": {"type": "string"},
                "reason": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": ["action", "target_task_id"],
        }

    async def execute(
        self,
        action: str,
        target_task_id: str,
        reason: str = "",
        **kwargs,
    ) -> str:
        _ = action, target_task_id, reason, kwargs
        return '{"status":"completed"}'


class _NestedContractTool(Tool):
    @property
    def name(self) -> str:
        return "nested_contract_tool"

    @property
    def description(self) -> str:
        return "preserve nested schema contract"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Nested items to preserve.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["profile", "preference"],
                                "description": "Nested enum field.",
                            },
                            "value": {
                                "type": "string",
                                "description": "Nested required value field.",
                            },
                            "meta": {
                                "type": "object",
                                "properties": {
                                    "source_excerpt": {
                                        "type": "string",
                                        "description": "Nested object leaf.",
                                    }
                                },
                                "required": ["source_excerpt"],
                            },
                        },
                        "required": ["kind", "value", "meta"],
                    },
                }
            },
            "required": ["items"],
        }

    async def execute(self, items: list[dict[str, object]], **kwargs) -> str:
        _ = kwargs
        return json.dumps({"ok": True, "items": items}, ensure_ascii=False)


def _assembly_result(*, tool_names: list[str], recent_history: list[dict[str, object]] | None = None, trace: dict[str, object] | None = None) -> ContextAssemblyResult:
    return ContextAssemblyResult(
        system_prompt="SYSTEM PROMPT",
        recent_history=list(recent_history or []),
        tool_names=tool_names,
        trace=dict(trace or {}),
    )


def _nested_items_item_schema(schema: dict[str, object]) -> dict[str, object]:
    properties = dict(schema.get("properties") or {})
    items_schema = dict(properties.get("items") or {})
    nested = items_schema.get("items")
    if isinstance(nested, dict) and "$ref" in nested:
        ref_name = str(nested.get("$ref") or "").split("/")[-1]
        return dict((schema.get("$defs") or {}).get(ref_name) or {})
    return dict(nested or {})


def test_build_args_schema_preserves_declared_json_types() -> None:
    schema_model = _build_args_schema(_TaskControlTool())
    schema = schema_model.model_json_schema()
    properties = dict(schema.get("properties") or {})

    target_task_schema = dict(properties.get("target_task_id") or {})
    force_schema = dict(properties.get("force") or {})
    action_schema = dict(properties.get("action") or {})

    target_task_types = {
        str(item.get("type") or "").strip()
        for item in list(target_task_schema.get("anyOf") or [])
        if isinstance(item, dict)
    }
    force_types = {
        str(item.get("type") or "").strip()
        for item in list(force_schema.get("anyOf") or [])
        if isinstance(item, dict)
    }

    assert set(action_schema.get("enum") or []) == {"pause", "resume"}
    assert "string" in target_task_types or target_task_schema.get("type") == "string"
    assert "boolean" in force_types or force_schema.get("type") == "boolean"


def test_build_args_schema_preserves_nested_array_object_contracts() -> None:
    schema_model = _build_args_schema(_NestedContractTool())
    schema = schema_model.model_json_schema()
    nested_item = _nested_items_item_schema(schema)
    nested_properties = dict(nested_item.get("properties") or {})
    meta_schema = dict(nested_properties.get("meta") or {})
    if "$ref" in meta_schema:
        ref_name = str(meta_schema.get("$ref") or "").split("/")[-1]
        meta_schema = dict((schema.get("$defs") or {}).get(ref_name) or {})

    assert schema.get("required") == ["items"]
    assert nested_item.get("required") == ["kind", "value", "meta"]
    assert dict(nested_properties.get("kind") or {}).get("enum") == ["profile", "preference"]
    assert "value" in nested_properties
    assert dict(meta_schema.get("properties") or {}).get("source_excerpt") is not None


def test_tool_registry_langchain_tool_preserves_nested_array_object_contracts() -> None:
    registry = ToolRegistry()
    registry.register(_NestedContractTool())

    langchain_tool = registry.to_langchain_tools_filtered(["nested_contract_tool"])[0]
    schema = langchain_tool.args_schema.model_json_schema()
    nested_item = _nested_items_item_schema(schema)
    nested_properties = dict(nested_item.get("properties") or {})

    assert schema.get("required") == ["items"]
    assert nested_item.get("required") == ["kind", "value", "meta"]
    assert dict(nested_properties.get("kind") or {}).get("enum") == ["profile", "preference"]



def test_frontdoor_runtime_no_longer_exposes_legacy_history_summarizer_entrypoint() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    assert not hasattr(runner, "_summarize_messages")

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
                        id="call-stage-1",
                        name="submit_next_stage",
                        arguments={
                            "stage_goal": "Create the CEO stage before using record_tool",
                            "tool_round_budget": 1,
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
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
            LLMResponse(content="done", finish_reason="stop"),
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
    assert len(backend.calls) == 3


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
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-stage-1",
                        name="submit_next_stage",
                        arguments={
                            "stage_goal": "Open a stage before issuing XML tool syntax",
                            "tool_round_budget": 1,
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
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
    assert len(backend.calls) == 3
    assert not any(
        "XML-style pseudo tool calling" in str(item.get("content") or "")
        for item in list(backend.calls[2].get("messages") or [])
    )


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_repairs_xml_tool_call_via_json_payload_after_local_extraction_fails(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    executed: list[int] = []
    backend = _BackendRecorder(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-stage-1",
                        name="submit_next_stage",
                        arguments={
                            "stage_goal": "Open a stage before repairing the XML payload",
                            "tool_round_budget": 1,
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
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
    assert len(backend.calls) == 4
    repair_messages = list(backend.calls[2].get("messages") or [])
    assert any("XML-style pseudo tool calling" in str(item.get("content") or "") for item in repair_messages)


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_retries_empty_turn_until_valid_result(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))

    monkeypatch.setattr("g3ku.runtime.frontdoor._ceo_runtime_ops.asyncio.sleep", _fake_sleep)

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
    class _DispatchAsyncTaskTool(Tool):
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

        async def execute(self, task: str, core_requirement: str, execution_policy: dict[str, object], **kwargs) -> str:
            _ = task, core_requirement, execution_policy, kwargs
            return "创建任务成功task:demo-123"

    async def _noop_ready() -> None:
        return None

    async def _noop_startup() -> None:
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
                            "stage_goal": "Create a stage before dispatching the async task",
                            "tool_round_budget": 1,
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
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
            LLMResponse(
                content="后台修复任务已经建立，任务号 `task:demo-123`。我先继续排查，完成后直接把结果同步给你。",
                finish_reason="stop",
            ),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=SimpleNamespace(
            startup=_noop_startup,
            get_task=lambda task_id: SimpleNamespace(task_id=task_id)
        ),
        tools=_FakeToolRegistry([_DispatchAsyncTaskTool()]),
        max_iterations=8,
        resource_manager=None,
        tool_execution_manager=None,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(frontdoor_interrupt_tool_names=["message"])
        ),
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

    assert output == "后台修复任务已经建立，任务号 `task:demo-123`。我先继续排查，完成后直接把结果同步给你。"
    assert len(backend.calls) == 3



def test_build_prompt_context_no_longer_uses_summary_text_overlay() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = runner.build_prompt_context(
        state={
            "summary_text": "## CEO Durable Summary\n- durable memory",
            "frontdoor_stage_state": {
                "active_stage_id": "stage-1",
                "transition_required": True,
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_goal": "Inspect the current request",
                        "tool_round_budget": 1,
                        "tool_rounds_used": 1,
                        "status": "active",
                        "mode": "自主执行",
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "rounds": [],
                    }
                ],
            },
        },
        runtime=SimpleNamespace(),
        tools=[],
    )

    overlay = str(result["system_overlay"])
    assert "当前 CEO 阶段工具轮次预算已耗尽：1/1。" in overlay
    assert "先不要直接结束。请先总结本阶段已完成的进展" in overlay
    assert "Use the existing CEO layered context rules." not in overlay
    assert "## CEO Durable Summary" not in overlay


def test_build_prompt_context_keeps_dispatch_overlay_and_exhausted_stage_instruction() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = runner.build_prompt_context(
        state={
            "repair_overlay_text": "Dispatch result is already available. Reply naturally based on the verified task id task:demo-123.",
            "frontdoor_stage_state": {
                "active_stage_id": "stage-1",
                "transition_required": True,
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_goal": "Dispatch the follow-up investigation",
                        "tool_round_budget": 1,
                        "tool_rounds_used": 1,
                        "status": "active",
                        "mode": "自主执行",
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "rounds": [
                            {
                                "round_id": "stage-1:round-1",
                                "round_index": 1,
                                "tool_names": ["create_async_task"],
                                "tool_call_ids": ["call-1"],
                                "budget_counted": True,
                            }
                        ],
                    }
                ],
            },
        },
        runtime=SimpleNamespace(),
        tools=[],
    )

    overlay = str(result["system_overlay"])
    assert "verified task id task:demo-123" in overlay
    assert "当前 CEO 阶段工具轮次预算已耗尽：1/1。" in overlay
    assert "必须先总结本阶段进展，并调用 `submit_next_stage` 创建下一阶段。" in overlay


@pytest.mark.asyncio
async def test_graph_normalize_model_output_rejects_plain_text_when_transition_required() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "我在。请直接告诉我你现在要我做什么。",
                "tool_calls": [],
                "finish_reason": "stop",
            },
            "route_kind": "direct_reply",
            "used_tools": ["task_progress", "task_failed_nodes"],
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": True,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Inspect task failure history",
                        "tool_round_budget": 4,
                        "tool_rounds_used": 4,
                        "status": "active",
                        "mode": "自主执行",
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "rounds": [],
                    }
                ],
            },
        },
        runtime=SimpleNamespace(),
    )

    assert result["next_step"] == "call_model"
    assert "submit_next_stage" in str(result["repair_overlay_text"])
    assert "先不要直接结束。" in str(result["repair_overlay_text"])
    assert str(result.get("final_output") or "") == ""


@pytest.mark.asyncio
async def test_graph_prepare_turn_keeps_messages_raw_and_drops_summary_state() -> None:
    loop = SimpleNamespace()
    runner = CeoFrontDoorRunner(loop=loop)

    result = await runner._graph_prepare_turn(
        {
            "messages": [{"role": "user", "content": f"message {idx}"} for idx in range(6)],
            "user_input": {"content": "follow up", "metadata": {}},
        },
        runtime=SimpleNamespace(context=SimpleNamespace(session=None)),
    )

    assert result["messages"] == [{"role": "user", "content": f"message {idx}"} for idx in range(6)]
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


@pytest.mark.asyncio
async def test_graph_execute_tools_drops_frontdoor_summary_fields(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

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

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    result = await runner._graph_execute_tools(
        {
            "messages": [{"role": "user", "content": f"message {idx}"} for idx in range(6)],
            "tool_call_payloads": [{"id": "call-1", "name": "missing_tool", "arguments": {"value": "alpha"}}],
            "response_payload": {"content": "tool preface"},
            "synthetic_tool_calls_used": False,
            "parallel_enabled": False,
            "max_parallel_tool_calls": None,
            "used_tools": [],
            "route_kind": "direct_reply",
            "summary_payload": {"stable_facts": ["old fact"]},
            "summary_model_key": "summary-model",
            "tool_names": [],
            "user_input": {"content": "follow up", "metadata": {}},
        },
        runtime=runtime,
    )

    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


@pytest.mark.asyncio
async def test_graph_execute_tools_ignores_stale_frontdoor_summary_inputs(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

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

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    result = await runner._graph_execute_tools(
        {
            "messages": [{"role": "user", "content": f"message {idx}"} for idx in range(6)],
            "tool_call_payloads": [{"id": "call-1", "name": "missing_tool", "arguments": {"value": "alpha"}}],
            "response_payload": {"content": "tool preface"},
            "synthetic_tool_calls_used": False,
            "parallel_enabled": False,
            "max_parallel_tool_calls": None,
            "used_tools": [],
            "route_kind": "direct_reply",
            "summary_payload": {
                "stable_preferences": ["reply in Chinese"],
                "stable_facts": ["old fact"],
                "open_loops": ["stale loop"],
                "recent_actions": ["stale action"],
                "narrative": "Old model summary.",
            },
            "summary_model_key": "summary-model",
            "tool_names": [],
            "user_input": {"content": "follow up", "metadata": {}},
        },
        runtime=runtime,
    )

    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result
