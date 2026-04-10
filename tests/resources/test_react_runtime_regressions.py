from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.rag_memory import ContextRecordV2, MemoryManager
from g3ku.agent.tools.base import Tool
from g3ku.content import ContentNavigationService, parse_content_envelope
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.runtime.context.node_context_selection import NodeContextSelectionResult
import main.service.runtime_service as runtime_service_module
from main.runtime.internal_tools import SubmitFinalResultTool
from main.runtime.react_loop import ReActToolLoop
from main.runtime.tool_call_repair import extract_tool_calls_from_xml_pseudo_content
from main.service.runtime_service import MainRuntimeService
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore


class _FakeTaskStore:
    def __init__(self) -> None:
        self._task = SimpleNamespace(cancel_requested=False, pause_requested=False)
        self._node = None

    def get_task(self, task_id: str):
        _ = task_id
        return self._task

    def get_node(self, node_id: str):
        _ = node_id
        return self._node


class _FakeLogService:
    def __init__(self) -> None:
        self._store = _FakeTaskStore()
        self._content_store = None
        self._frames: dict[tuple[str, str], dict[str, object]] = {}

    def set_pause_state(self, task_id: str, pause_requested: bool, is_paused: bool) -> None:
        _ = task_id, pause_requested, is_paused

    def update_node_input(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def upsert_frame(self, task_id: str, payload: dict[str, object], publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        node_id = str((payload or {}).get("node_id") or "").strip()
        self._frames[(str(task_id), node_id)] = dict(payload or {})

    def append_node_output(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def update_frame(self, task_id: str, node_id: str, mutate, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        key = (str(task_id), str(node_id))
        current = dict(self._frames.get(key) or {})
        self._frames[key] = dict(mutate(current) or {})

    def remove_frame(self, task_id: str, node_id: str, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        self._frames.pop((str(task_id), str(node_id)), None)

    def read_runtime_frame(self, task_id: str, node_id: str):
        return dict(self._frames.get((str(task_id), str(node_id))) or {})


def _submit_final_result_tool(*, node_kind: str = "execution") -> SubmitFinalResultTool:
    async def _submit(payload: dict[str, object]) -> dict[str, object]:
        return dict(payload)

    return SubmitFinalResultTool(_submit, node_kind=node_kind)


class _DirectLoadTool(Tool):
    @property
    def name(self) -> str:
        return "direct_load_tool"

    @property
    def description(self) -> str:
        return "Return a large direct-load payload."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        _ = kwargs
        payload = {
            "ok": True,
            "level": "l2",
            "content": "\n".join(f"tool line {index:03d}" for index in range(1, 321)),
            "l0": "tool short summary",
            "l1": "tool structured overview",
            "path": "/virtual/content-tool.md",
            "uri": "g3ku://resource/tool/content",
        }
        return json.dumps(payload, ensure_ascii=False)


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
                "value": {"type": "string", "description": "value"},
            },
            "required": ["value"],
        }

    async def execute(self, value: str, **kwargs):
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
                "count": {"type": "integer", "description": "count"},
            },
            "required": ["count"],
        }

    async def execute(self, count: int, **kwargs):
        _ = kwargs
        self._sink.append(int(count))
        return json.dumps({"ok": True, "count": int(count)}, ensure_ascii=False)


class _TypedSchemaTool(Tool):
    @property
    def name(self) -> str:
        return "typed_schema_tool"

    @property
    def description(self) -> str:
        return "exercise XML schema coercion"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "enabled": {"type": "boolean"},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                    },
                    "required": ["source"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "note": {"type": "string"},
            },
            "required": ["count", "enabled", "metadata", "tags", "note"],
        }

    async def execute(self, **kwargs):
        return json.dumps(kwargs, ensure_ascii=False)


def test_tool_model_visible_schema_falls_back_to_authoritative_schema() -> None:
    class _FakeTool(Tool):
        @property
        def name(self) -> str:
            return "fake_tool"

        @property
        def description(self) -> str:
            return "Full schema description."

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Full contract description.",
                    }
                },
                "required": ["value"],
            }

        async def execute(self, **kwargs: Any) -> Any:
            return kwargs

    tool = _FakeTool()

    assert (
        tool.to_schema()["function"]["parameters"]["properties"]["value"]["description"]
        == "Full contract description."
    )
    assert tool.to_model_schema() == tool.to_schema()


def test_tool_model_visible_schema_uses_override_without_changing_validation_contract() -> None:
    class _OverrideTool(Tool):
        @property
        def name(self) -> str:
            return "override_tool"

        @property
        def description(self) -> str:
            return "Authoritative execution contract."

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "full_value": {
                        "type": "string",
                        "description": "Authoritative parameter used for validation.",
                    }
                },
                "required": ["full_value"],
            }

        @property
        def model_description(self) -> str:
            return "Compact model-facing contract."

        @property
        def model_parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Compact model-visible field.",
                    }
                },
                "required": ["summary"],
            }

        async def execute(self, **kwargs: Any) -> Any:
            return kwargs

    tool = _OverrideTool()

    assert tool.to_schema()["function"]["description"] == "Authoritative execution contract."
    assert tool.to_schema()["function"]["parameters"]["required"] == ["full_value"]
    assert tool.to_model_schema()["function"]["description"] == "Compact model-facing contract."
    assert tool.to_model_schema()["function"]["parameters"]["required"] == ["summary"]
    assert tool.validate_params({"summary": "preview"}) == ["missing required full_value"]


def test_tool_model_visible_schema_api_docs_call_out_non_authoritative_contract() -> None:
    assert Tool.parameters.__doc__ is not None
    assert Tool.model_parameters.__doc__ is not None
    assert "authoritative" in Tool.parameters.__doc__.lower()
    assert "not used for validation" in Tool.model_parameters.__doc__.lower()


def test_prepare_messages_rebuilds_prompt_from_completed_stages_and_active_window() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "active_stage_id": "stage-2",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": "自主执行",
                        "status": "完成",
                        "stage_goal": "inspect the first stage",
                        "completed_stage_summary": "finished stage one",
                        "key_refs": [
                            {
                                "ref": "artifact:artifact:stage-one",
                                "note": "stage one evidence summary",
                            }
                        ],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-2",
                        "stage_index": 2,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": "自主执行",
                        "status": "进行中",
                        "stage_goal": "inspect the second stage",
                        "tool_round_budget": 3,
                        "tool_rounds_used": 0,
                    },
                ],
            }
        }
    )
    original = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-1",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage one raw detail"},
        {
            "role": "tool",
            "name": "content",
            "tool_call_id": "call-stage-archive",
            "content": "archived stage history excerpt",
            "ephemeral": True,
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-2",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "current stage assistant detail"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-a", "content": "current stage tool output"},
    ]

    prepared = loop._prepare_messages(original, runtime_context={"task_id": "task-1", "node_id": "node-1"})

    assert prepared == original
    rendered_contents = [str(item.get("content") or "") for item in prepared]
    assert "stage one raw detail" in rendered_contents
    assert "archived stage history excerpt" in rendered_contents
    assert "current stage assistant detail" in rendered_contents
    assert "current stage tool output" in rendered_contents


def test_prepare_messages_is_idempotent_for_compacted_stage_prompt() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "active_stage_id": "stage-2",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": "自主执行",
                        "status": "完成",
                        "stage_goal": "inspect the first stage",
                        "completed_stage_summary": "finished stage one",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-2",
                        "stage_index": 2,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": "自主执行",
                        "status": "进行中",
                        "stage_goal": "inspect the second stage",
                        "tool_round_budget": 3,
                        "tool_rounds_used": 0,
                    },
                ],
            }
        }
    )
    original = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-1",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage one raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-2",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "current stage assistant detail"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-a", "content": "current stage tool output"},
    ]

    prepared = loop._prepare_messages(original, runtime_context={"task_id": "task-1", "node_id": "node-1"})
    prepared_again = loop._prepare_messages(prepared, runtime_context={"task_id": "task-1", "node_id": "node-1"})

    assert prepared_again == prepared


@pytest.mark.asyncio
async def test_execute_tool_calls_marks_stage_history_archive_reads_ephemeral() -> None:
    class _ArchiveTool(Tool):
        @property
        def name(self) -> str:
            return "content"

        @property
        def description(self) -> str:
            return "open archived stage history"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs):
            _ = kwargs
            return {
                "ok": True,
                "ref": "artifact:artifact:archive",
                "handle": {
                    "ref": "artifact:artifact:archive",
                    "source_kind": "stage_history_archive",
                },
                "excerpt": "archived stage body",
            }

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    results = await loop._execute_tool_calls(
        task=SimpleNamespace(task_id="task-1"),
        node=SimpleNamespace(node_id="node-1", depth=0, node_kind="execution"),
        response_tool_calls=[ToolCallRequest(id="call-archive", name="content", arguments={})],
        tools={"content": _ArchiveTool()},
        allowed_content_refs=[],
        runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
    )

    assert len(results) == 1
    assert results[0]["tool_message"]["ephemeral"] is True
    assert results[0]["live_state"]["ephemeral"] is True


@pytest.mark.asyncio
async def test_react_loop_orphan_tool_result_circuit_breaker_fails_current_node() -> None:
    calls: list[list[dict[str, object]]] = []

    class _Backend:
        async def chat(self, **kwargs):
            message_batch = [dict(item) for item in list(kwargs.get("messages") or [])]
            calls.append(message_batch)
            return LLMResponse(
                content="not json",
                tool_calls=[],
                finish_reason="stop",
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=5)
    initial_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-orphan|fc_orphan", "content": '{"ok": true}'},
    ]

    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=initial_messages,
        tools={},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=5,
    )

    assert result.status == "failed"
    assert result.delivery_status == "blocked"
    assert "orphan tool result" in result.summary
    assert "call-orphan" in result.blocking_reason
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_react_loop_execute_tool_keeps_direct_load_payload_inline(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    log_service = _FakeLogService()
    log_service._content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    try:
        rendered = await loop._execute_tool(
            tools={"direct_load_tool": _DirectLoadTool()},
            tool_name="direct_load_tool",
            arguments={},
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
        )

        assert parse_content_envelope(rendered) is None
        payload = json.loads(rendered)
        assert payload["uri"] == "g3ku://resource/tool/content"
        assert payload["content"].startswith("tool line 001")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_react_loop_externalizes_tool_messages_with_canonical_ref(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    log_service = _FakeLogService()
    log_service._content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    try:
        inner = log_service._content_store.maybe_externalize_text(
            "alpha\nneedle\nomega\n",
            runtime={"task_id": "task-1", "node_id": "node-1"},
            display_name="inner",
            source_kind="node_output",
            force=True,
        )
        wrapped = log_service._content_store.maybe_externalize_text(
            json.dumps(inner.to_dict(), ensure_ascii=False),
            runtime={"task_id": "task-1", "node_id": "node-1"},
            display_name="wrapped",
            source_kind="tool_result:content",
            force=True,
        )

        assert inner is not None
        assert wrapped is not None

        tool_payload = {
            "ok": True,
            "ref": wrapped.ref,
            "requested_ref": wrapped.ref,
            "resolved_ref": inner.ref,
            "wrapper_ref": wrapped.ref,
            "wrapper_depth": 1,
            "start_line": 1,
            "end_line": 280,
            "excerpt": "\n".join(f"line {index:03d} needle context" for index in range(280)),
        }

        rendered = loop._render_tool_message_content(
            json.dumps(tool_payload, ensure_ascii=False),
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
            tool_name="content",
        )

        payload = parse_content_envelope(rendered)

        assert payload is not None
        assert payload.ref.startswith("artifact:")
        assert payload.ref != inner.ref
        assert payload.resolved_ref == inner.ref
    finally:
        store.close()


@pytest.mark.asyncio
async def test_react_loop_uses_latest_model_refs_from_supplier_between_turns() -> None:
    model_ref_calls: list[list[str]] = []
    current_refs = ['old-model']

    class _Backend:
        def __init__(self) -> None:
            self._calls = 0

        async def chat(self, **kwargs):
            self._calls += 1
            model_ref_calls.append(list(kwargs.get("model_refs") or []))
            if self._calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="call-1", name="flip_refs", arguments={})],
                    finish_reason="tool_calls",
                    usage={"input_tokens": 8, "output_tokens": 3},
                )
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:final",
                        name="submit_final_result",
                        arguments={
                            "status": "failed",
                            "delivery_status": "blocked",
                            "summary": "done",
                            "answer": "",
                            "evidence": [],
                            "remaining_work": [],
                            "blocking_reason": "done",
                        },
                    )
                ],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    class _FlipRefsTool(Tool):
        @property
        def name(self) -> str:
            return "flip_refs"

        @property
        def description(self) -> str:
            return "flip refs"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            _ = kwargs
            current_refs[:] = ["new-model"]
            return "refs flipped"

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)

    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        ],
        tools={
            "flip_refs": _FlipRefsTool(),
            "submit_final_result": _submit_final_result_tool(),
        },
        model_refs=['old-model'],
        model_refs_supplier=lambda: list(current_refs),
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=3,
    )

    assert result.summary == "done"
    assert model_ref_calls == [["old-model"], ["new-model"]]


@pytest.mark.asyncio
async def test_enrich_node_messages_visible_only_fallback_injects_all_visible_skills_and_retrieves_memory_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector_calls: list[dict[str, object]] = []
    retrieve_block_calls: list[dict[str, object]] = []

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            return key == "unified_context"

        async def retrieve_block(self, **kwargs):
            retrieve_block_calls.append(dict(kwargs))
            return "memory block"

    async def _fake_build_node_context_selection(**kwargs):
        selector_calls.append(dict(kwargs))
        return NodeContextSelectionResult(
            mode="visible_only",
            memory_search_visible=True,
            selected_skill_ids=["skill-creator", "tmux"],
            selected_tool_names=["filesystem", "memory_search"],
            memory_query="Prompt: where is the plan\nGoal: where is the plan\nCore requirement: where is the plan",
            retrieval_scope={
                "search_context_types": ["memory"],
                "allowed_context_types": ["memory"],
                "allowed_resource_record_ids": [],
                "allowed_skill_record_ids": [],
            },
            trace={"mode": "visible_only"},
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='filesystem'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill creator'),
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['filesystem', 'memory_search']

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="where is the plan", goal="where is the plan", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"where is the plan"}'},
        ],
    )

    assert selector_calls == [
        {
            "loop": None,
            "memory_manager": service.memory_manager,
            "prompt": "where is the plan",
            "goal": "where is the plan",
            "core_requirement": "where is the plan",
            "visible_skills": service.list_visible_skill_resources(actor_role="execution", session_id="web:ceo-origin"),
            "visible_tool_families": service.list_visible_tool_families(actor_role="execution", session_id="web:ceo-origin"),
            "visible_tool_names": ["filesystem", "memory_search"],
        }
    ]
    assert retrieve_block_calls == [
        {
            "query": "Prompt: where is the plan\nGoal: where is the plan\nCore requirement: where is the plan",
            "session_key": "web:ceo-origin",
            "channel": "web",
            "chat_id": "shared",
            "search_context_types": ["memory"],
            "allowed_context_types": ["memory"],
            "allowed_resource_record_ids": [],
            "allowed_skill_record_ids": [],
        }
    ]
    user_payload = json.loads(str(enriched[1]["content"] or ""))
    assert user_payload["visible_skills"] == [
        {
            "skill_id": "skill-creator",
            "display_name": "skill-creator",
            "description": "skill creator",
        },
        {
            "skill_id": "tmux",
            "display_name": "tmux",
            "description": "terminal workflow",
        },
    ]
    assert "memory block" in enriched[0]["content"]


@pytest.mark.asyncio
async def test_enrich_node_messages_uses_selector_narrowed_skills_and_memory_only_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve_block_calls: list[dict[str, object]] = []

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            return key == "unified_context"

        async def retrieve_block(self, **kwargs):
            retrieve_block_calls.append(dict(kwargs))
            return "semantic block"

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            mode="dense_rerank",
            memory_search_visible=True,
            selected_skill_ids=["tmux", "skill-creator"],
            selected_tool_names=["content"],
            memory_query="Prompt: terminal workflow\nGoal: terminal workflow\nCore requirement: terminal workflow",
            retrieval_scope={
                "search_context_types": ["memory"],
                "allowed_context_types": ["memory"],
                "allowed_resource_record_ids": [],
                "allowed_skill_record_ids": [],
            },
            trace={"mode": "dense_rerank"},
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='content'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill creator'),
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content', 'memory_search']

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="terminal workflow", goal="terminal workflow", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"terminal workflow"}'},
        ],
    )

    assert retrieve_block_calls == [
        {
            "query": "Prompt: terminal workflow\nGoal: terminal workflow\nCore requirement: terminal workflow",
            "session_key": "web:ceo-origin",
            "channel": "web",
            "chat_id": "shared",
            "search_context_types": ["memory"],
            "allowed_context_types": ["memory"],
            "allowed_resource_record_ids": [],
            "allowed_skill_record_ids": [],
        }
    ]
    user_payload = json.loads(str(enriched[1]["content"] or ""))
    assert user_payload["visible_skills"] == [
        {
            "skill_id": "tmux",
            "display_name": "tmux",
            "description": "terminal workflow",
        },
        {
            "skill_id": "skill-creator",
            "display_name": "skill-creator",
            "description": "skill creator",
        }
    ]
    assert "semantic block" in enriched[0]["content"]


@pytest.mark.asyncio
async def test_enrich_node_messages_skips_memory_retrieval_when_memory_search_not_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve_block_calls: list[dict[str, object]] = []

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            return key == "unified_context"

        async def retrieve_block(self, **kwargs):
            retrieve_block_calls.append(dict(kwargs))
            return "unexpected memory block"

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            mode="dense_rerank",
            memory_search_visible=False,
            selected_skill_ids=["tmux"],
            selected_tool_names=["filesystem"],
            memory_query="",
            retrieval_scope={
                "search_context_types": [],
                "allowed_context_types": [],
                "allowed_resource_record_ids": [],
                "allowed_skill_record_ids": [],
            },
            trace={"mode": "dense_rerank"},
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='filesystem'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill'),
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['filesystem']

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="where is the plan", goal="where is the plan", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"where is the plan"}'},
        ],
    )

    user_messages = [message for message in enriched if message.get("role") == "user"]
    assert len(user_messages) == 1
    payload = json.loads(str(user_messages[0].get("content") or ""))
    assert payload["visible_skills"] == [
        {
            "skill_id": "tmux",
            "display_name": "tmux",
            "description": "terminal workflow",
        }
    ]
    assert retrieve_block_calls == []


@pytest.mark.asyncio
async def test_enrich_node_messages_still_applies_selector_when_unified_context_feature_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve_block_calls: list[dict[str, object]] = []

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            _ = key
            return False

        async def retrieve_block(self, **kwargs):
            retrieve_block_calls.append(dict(kwargs))
            return "unexpected memory block"

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            mode="dense_rerank",
            memory_search_visible=True,
            selected_skill_ids=["tmux"],
            selected_tool_names=["content"],
            memory_query="Prompt: terminal workflow\nGoal: terminal workflow\nCore requirement: terminal workflow",
            retrieval_scope={
                "search_context_types": ["memory"],
                "allowed_context_types": ["memory"],
                "allowed_resource_record_ids": [],
                "allowed_skill_record_ids": [],
            },
            trace={"mode": "dense_rerank"},
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='content'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill creator'),
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content', 'memory_search']

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="terminal workflow", goal="terminal workflow", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"terminal workflow"}'},
        ],
    )

    user_payload = json.loads(str(enriched[1]["content"] or ""))
    assert user_payload["visible_skills"] == [
        {
            "skill_id": "tmux",
            "display_name": "tmux",
            "description": "terminal workflow",
        }
    ]
    assert retrieve_block_calls == []


def test_filter_retrieved_records_preserves_memory_and_filters_catalog_context() -> None:
    records = [
        ContextRecordV2(record_id="memory-1", context_type="memory", uri="g3ku://memory/web/shared/memory-1"),
        ContextRecordV2(record_id="tool:filesystem", context_type="resource", uri="g3ku://resource/tool/filesystem"),
        ContextRecordV2(record_id="tool:exec", context_type="resource", uri="g3ku://resource/tool/exec"),
        ContextRecordV2(record_id="skill:skill-creator", context_type="skill", uri="g3ku://skill/skill-creator"),
        ContextRecordV2(record_id="skill:tmux", context_type="skill", uri="g3ku://skill/tmux"),
    ]

    filtered = MemoryManager._filter_retrieved_records(
        records,
        allowed_context_types=["memory", "resource", "skill"],
        allowed_resource_record_ids=["tool:filesystem"],
        allowed_skill_record_ids=["skill:skill-creator"],
    )

    assert [record.record_id for record in filtered] == [
        "memory-1",
        "tool:filesystem",
        "skill:skill-creator",
    ]


@pytest.mark.asyncio
async def test_execute_tool_blocks_repeated_overflowed_search() -> None:
    class _FilesystemTool(Tool):
        @property
        def name(self) -> str:
            return 'filesystem'

        @property
        def description(self) -> str:
            return 'Filesystem stub'

        @property
        def parameters(self) -> dict[str, object]:
            return {
                'type': 'object',
                'properties': {
                    'action': {'type': 'string', 'description': 'action'},
                    'path': {'type': 'string', 'description': 'path'},
                    'query': {'type': 'string', 'description': 'query'},
                },
                'required': ['action', 'path', 'query'],
            }

        async def execute(self, **kwargs):
            raise AssertionError(f'overflowed search should not execute again: {kwargs!r}')

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop._execute_tool(
        tools={'filesystem': _FilesystemTool()},
        tool_name='filesystem',
        arguments={'action': 'search', 'path': '/tmp/demo.py', 'query': 'needle'},
        runtime_context={'prior_overflow_signatures': ['filesystem|/tmp/demo.py|needle']},
    )

    assert result == 'Error: previous search overflowed; refine query before retrying'


@pytest.mark.asyncio
async def test_execute_tool_passes_runtime_context_to_name_mangled_class_tool() -> None:
    class _RuntimeCaptureTool(Tool):
        @property
        def name(self) -> str:
            return 'capture_runtime'

        @property
        def description(self) -> str:
            return 'Capture runtime context'

        @property
        def parameters(self) -> dict[str, object]:
            return {
                'type': 'object',
                'properties': {
                    'value': {'type': 'string', 'description': 'value'},
                },
                'required': ['value'],
            }

        async def execute(self, value: str, __g3ku_runtime: dict[str, object] | None = None, **kwargs):
            runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
            return json.dumps(
                {
                    'value': value,
                    'current_tool_call_id': runtime.get('current_tool_call_id'),
                    'kwargs_runtime': kwargs.get('__g3ku_runtime'),
                },
                ensure_ascii=False,
            )

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop._execute_tool(
        tools={'capture_runtime': _RuntimeCaptureTool()},
        tool_name='capture_runtime',
        arguments={'value': 'demo'},
        runtime_context={'current_tool_call_id': 'call:test-runtime'},
    )

    payload = json.loads(result)
    assert payload['value'] == 'demo'
    assert payload['current_tool_call_id'] == 'call:test-runtime'
    assert payload['kwargs_runtime'] is None


def test_apply_temporary_system_overlay_keeps_base_messages_untouched() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    base_messages = [
        {'role': 'system', 'content': 'base system'},
        {'role': 'user', 'content': 'base user'},
    ]
    overlay = 'temporary system overlay'

    request_messages = loop._apply_temporary_system_overlay(base_messages, overlay_text=overlay)

    assert base_messages[0]['content'] == 'base system'
    assert request_messages[0] == base_messages[0]
    assert request_messages[1]['role'] == 'user'
    assert 'System note for this turn only:' in str(request_messages[1]['content'])
    assert overlay in str(request_messages[1]['content'])
    assert 'base user' in str(request_messages[1]['content'])


def test_detect_xml_pseudo_tool_call_matches_supported_shape_only() -> None:
    matched = ReActToolLoop._detect_xml_pseudo_tool_call(
        '<minimax:tool_call><invoke name="filesystem"><parameter name="path">docs/a.md</parameter></invoke></minimax:tool_call>',
        allowed_tool_names={'filesystem', 'submit_final_result'},
    )
    rejected_unknown = ReActToolLoop._detect_xml_pseudo_tool_call(
        '<minimax:tool_call><invoke name="unknown_tool"><parameter name="path">docs/a.md</parameter></invoke></minimax:tool_call>',
        allowed_tool_names={'filesystem', 'submit_final_result'},
    )
    rejected_plain_xml = ReActToolLoop._detect_xml_pseudo_tool_call(
        '<note><path>docs/a.md</path></note>',
        allowed_tool_names={'filesystem', 'submit_final_result'},
    )

    assert matched is not None
    assert matched['tool_names'] == ['filesystem']
    assert rejected_unknown is None
    assert rejected_plain_xml is None


def test_extract_tool_calls_from_xml_pseudo_content_coerces_schema_typed_parameters() -> None:
    extracted = extract_tool_calls_from_xml_pseudo_content(
        (
            '<minimax:tool_call>'
            '<invoke name="typed_schema_tool">'
            '<parameter name="count">5</parameter>'
            '<parameter name="enabled">true</parameter>'
            '<parameter name="metadata">{"source":"xml"}</parameter>'
            '<parameter name="tags">["alpha","beta"]</parameter>'
            '<parameter name="note">hello &amp; goodbye</parameter>'
            '</invoke>'
            '</minimax:tool_call>'
        ),
        visible_tools={"typed_schema_tool": _TypedSchemaTool()},
    )

    assert extracted.matched is True
    assert extracted.issue == ""
    assert len(extracted.tool_calls) == 1
    arguments = extracted.tool_calls[0].arguments
    assert arguments["count"] == 5
    assert arguments["enabled"] is True
    assert arguments["metadata"] == {"source": "xml"}
    assert arguments["tags"] == ["alpha", "beta"]
    assert arguments["note"] == "hello & goodbye"


def test_extract_tool_calls_from_xml_pseudo_content_returns_issue_for_invalid_parameter_type() -> None:
    extracted = extract_tool_calls_from_xml_pseudo_content(
        '<minimax:tool_call><invoke name="count_tool"><parameter name="count">oops</parameter></invoke></minimax:tool_call>',
        visible_tools={"count_tool": _CountTool([])},
    )

    assert extracted.matched is True
    assert extracted.tool_calls == []
    assert 'must be an integer' in extracted.issue


def test_extract_tool_calls_from_xml_pseudo_content_is_all_or_nothing_for_multi_invoke() -> None:
    extracted = extract_tool_calls_from_xml_pseudo_content(
        (
            '<minimax:tool_call>'
            '<invoke name="count_tool"><parameter name="count">1</parameter></invoke>'
            '<invoke name="count_tool"><parameter name="count">oops</parameter></invoke>'
            '</minimax:tool_call>'
        ),
        visible_tools={"count_tool": _CountTool([])},
    )

    assert extracted.matched is True
    assert extracted.tool_calls == []
    assert 'must be an integer' in extracted.issue


@pytest.mark.asyncio
async def test_react_loop_directly_executes_xml_reply_without_repair() -> None:
    calls: list[list[dict[str, object]]] = []
    executed: list[tuple[str, str]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='<minimax:tool_call><invoke name="record_one"><parameter name="value">hello</parameter></invoke></minimax:tool_call>',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:final',
                            name='submit_final_result',
                            arguments={
                                'status': 'success',
                                'delivery_status': 'final',
                                'summary': 'done',
                                'answer': 'done',
                                'evidence': [{'kind': 'artifact', 'note': 'json repair path'}],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get('messages') or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-json'),
        node=SimpleNamespace(node_id='node-xml-json', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-json","goal":"demo"}'},
        ],
        tools={
            'record_one': _RecordingTool('record_one', executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-json', 'node_id': 'node-xml-json'},
        max_iterations=3,
    )

    assert result.status == 'success'
    assert executed == [('record_one', 'hello')]
    assert len(calls) == 2
    second_request = calls[1]
    assert not any('XML-style pseudo tool calling' in str(item.get('content') or '') for item in second_request)
    assistant_turns = [item for item in second_request if item.get('role') == 'assistant']
    assert any(
        str((((tool_call or {}).get('function') or {}).get('name') or '')).strip() == 'record_one'
        for message in assistant_turns
        for tool_call in list(message.get('tool_calls') or [])
    )
    assert all(str(item.get('content') or '') != '<minimax:tool_call><invoke name="record_one"><parameter name="value">hello</parameter></invoke></minimax:tool_call>' for item in assistant_turns)


@pytest.mark.asyncio
async def test_react_loop_repairs_xml_reply_with_json_array_payload() -> None:
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content=(
                        '<minimax:tool_call>'
                        '<invoke name="count_tool"><parameter name="count">1</parameter></invoke>'
                        '<invoke name="count_tool"><parameter name="count">oops</parameter></invoke>'
                        '</minimax:tool_call>'
                    ),
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='[{"name":"count_tool","arguments":{"count":1}},{"name":"count_tool","arguments":{"count":2}}]',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:final',
                            name='submit_final_result',
                            arguments={
                                'status': 'success',
                                'delivery_status': 'final',
                                'summary': 'done',
                                'answer': 'done',
                                'evidence': [{'kind': 'artifact', 'note': 'json array repair path'}],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            _ = kwargs
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=4)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-array'),
        node=SimpleNamespace(node_id='node-xml-array', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-array","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-array', 'node_id': 'node-xml-array'},
        max_iterations=4,
    )

    assert result.status == 'success'
    assert executed == [1, 2]


@pytest.mark.asyncio
async def test_react_loop_directly_executes_xml_submit_final_result() -> None:
    calls: list[list[dict[str, object]]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='<minimax:tool_call><invoke name="submit_final_result"><parameter name="status">success</parameter><parameter name="delivery_status">final</parameter><parameter name="summary">done</parameter><parameter name="answer">done</parameter><parameter name="evidence">[]</parameter><parameter name="remaining_work">[]</parameter><parameter name="blocking_reason"></parameter></invoke></minimax:tool_call>',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get('messages') or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-final'),
        node=SimpleNamespace(node_id='node-xml-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-final","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-final', 'node_id': 'node-xml-final'},
        max_iterations=2,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_react_loop_repairs_xml_reply_with_json_object_tool_payload_after_local_extraction_fails() -> None:
    calls: list[list[dict[str, object]]] = []
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='<minimax:tool_call><invoke name="count_tool"><parameter name="count">oops</parameter></invoke></minimax:tool_call>',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='{"name":"count_tool","arguments":{"count":2}}',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:final',
                            name='submit_final_result',
                            arguments={
                                'status': 'success',
                                'delivery_status': 'final',
                                'summary': 'done',
                                'answer': 'done',
                                'evidence': [{'kind': 'artifact', 'note': 'json repair path'}],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get('messages') or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=4)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-json'),
        node=SimpleNamespace(node_id='node-xml-json', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-json","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-json', 'node_id': 'node-xml-json'},
        max_iterations=4,
    )

    assert result.status == 'success'
    assert executed == [2]
    assert len(calls) == 3
    second_request = calls[1]
    assert any('XML-style pseudo tool calling' in str(item.get('content') or '') for item in second_request)
    assert any('must be an integer' in str(item.get('content') or '') for item in second_request)


@pytest.mark.asyncio
async def test_react_loop_fails_after_three_xml_repair_attempts() -> None:
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                content = '<minimax:tool_call><invoke name="submit_final_result"><parameter name="status">success</parameter></invoke></minimax:tool_call>'
            elif self.turn == 2:
                content = 'still not valid json or structured tool call'
            else:
                content = '<minimax:tool_call><invoke name="submit_final_result"><parameter name="status">success</parameter></invoke></minimax:tool_call>'
            return LLMResponse(
                content=content,
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    backend = _Backend()
    loop = ReActToolLoop(chat_backend=backend, log_service=_FakeLogService(), max_iterations=5)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-fail'),
        node=SimpleNamespace(node_id='node-xml-fail', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-fail","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-fail', 'node_id': 'node-xml-fail'},
        max_iterations=5,
    )

    assert backend.turn == 3
    assert result.status == 'failed'
    assert result.delivery_status == 'blocked'
    assert 'XML pseudo tool-call repair failed 3 consecutive times' in result.blocking_reason


def test_execution_result_protocol_message_avoids_partial_guidance() -> None:
    message = ReActToolLoop._result_protocol_message(node_kind='execution')

    assert 'delivery_status="partial"' not in message
    assert 'submit_final_result' in message
    assert 'final|blocked' in message
    assert 'If you are ending the node now' in message
    assert 'If the task is not complete yet' in message


def test_execution_result_contract_violation_message_keeps_workflow_open() -> None:
    message = ReActToolLoop._result_contract_violation_message(
        ['summary must not be empty'],
        node_kind='execution',
    )

    assert 'submit_final_result' in message
    assert 'If you are ending the node now' in message
    assert 'do not force another premature final submission' in message


def test_acceptance_result_contract_violation_message_uses_final_or_blocked_only() -> None:
    message = ReActToolLoop._result_contract_violation_message(
        ['summary must not be empty'],
        node_kind='acceptance',
    )

    assert 'delivery_status="partial"' not in message
    assert 'submit_final_result' in message
    assert 'failed+final' in message
    assert 'failed+blocked' in message


@pytest.mark.asyncio
async def test_react_loop_uses_system_overlay_for_execution_result_repair() -> None:
    calls: list[list[dict[str, object]]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:invalid-final',
                            name='submit_final_result',
                            arguments={
                                'status': 'failed',
                                'delivery_status': 'final',
                                'summary': 'done',
                                'answer': '',
                                'evidence': [],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:final',
                            name='submit_final_result',
                            arguments={
                                'status': 'failed',
                                'delivery_status': 'blocked',
                                'summary': 'done',
                                'answer': '',
                                'evidence': [],
                                'remaining_work': [],
                                'blocking_reason': 'done',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get('messages') or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-1","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=3,
    )

    assert result.status == 'failed'
    assert len(calls) == 2
    second_request = calls[1]
    assert second_request[0]['role'] == 'system'
    assert any(item.get('role') == 'user' for item in second_request)
    merged_user_content = '\n'.join(str(item.get('content') or '') for item in second_request if item.get('role') == 'user')
    assert 'If you are ending the node now' in merged_user_content or 'Your last `submit_final_result` payload violated result contract' in merged_user_content
    assert 'submit_final_result' in merged_user_content


@pytest.mark.asyncio
async def test_react_loop_retries_empty_response_until_valid_result(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))

    monkeypatch.setattr('main.runtime.react_loop.asyncio.sleep', _fake_sleep)

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            self.turn += 1
            if self.turn < 3:
                return LLMResponse(
                    content='',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-empty-retry'),
        node=SimpleNamespace(node_id='node-empty-retry', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-empty-retry","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-empty-retry', 'node_id': 'node-empty-retry'},
        max_iterations=2,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert len(requests) == 3
    assert sleep_calls == [1.0, 2.0]


@pytest.mark.asyncio
async def test_react_loop_auto_wraps_plain_text_final_result() -> None:
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content='Final structured summary line 1\n- detail a\n- detail b',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-plain-final'),
        node=SimpleNamespace(node_id='node-plain-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-plain-final","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-plain-final', 'node_id': 'node-plain-final'},
        max_iterations=2,
    )

    assert result.status == 'success'
    assert result.delivery_status == 'final'
    assert result.answer == 'Final structured summary line 1\n- detail a\n- detail b'
    assert result.summary == 'auto-wrapped plain-text final result'
    assert result.evidence == []


@pytest.mark.asyncio
async def test_react_loop_auto_wraps_plain_text_final_result_with_tool_evidence() -> None:
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:count',
                            name='count_tool',
                            arguments={'count': 2},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='Final answer after tool usage',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-plain-final-with-tools'),
        node=SimpleNamespace(node_id='node-plain-final-with-tools', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-plain-final-with-tools","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-plain-final-with-tools', 'node_id': 'node-plain-final-with-tools'},
        max_iterations=3,
    )

    assert executed == [2]
    assert result.status == 'success'
    assert result.answer == 'Final answer after tool usage'
    assert result.evidence
    assert any('count_tool' in str(item.note or '') for item in result.evidence)


@pytest.mark.asyncio
async def test_react_loop_normalizes_sparse_success_final_result_payload_after_tool_usage() -> None:
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:count',
                            name='count_tool',
                            arguments={'count': 1},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:sparse-final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-sparse-final'),
        node=SimpleNamespace(node_id='node-sparse-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-sparse-final","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-sparse-final', 'node_id': 'node-sparse-final'},
        max_iterations=3,
    )

    assert executed == [1]
    assert result.status == 'success'
    assert result.answer == 'done'
    assert result.remaining_work == []
    assert result.blocking_reason == ''
    assert result.evidence


@pytest.mark.asyncio
async def test_react_loop_infers_evidence_kind_for_submit_final_result_payload() -> None:
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:evidence-kind-final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [
                                {
                                    'ref': 'artifact:artifact:demo-ref',
                                    'note': 'artifact evidence without explicit kind',
                                }
                            ],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-evidence-kind-final'),
        node=SimpleNamespace(node_id='node-evidence-kind-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-evidence-kind-final","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-evidence-kind-final', 'node_id': 'node-evidence-kind-final'},
        max_iterations=2,
    )

    assert result.status == 'success'
    assert len(result.evidence) == 1
    assert result.evidence[0].kind == 'artifact'
    assert result.evidence[0].ref == 'artifact:artifact:demo-ref'


@pytest.mark.asyncio
async def test_react_loop_recovers_raw_final_result_json_after_protocol_repair() -> None:
    requests: list[dict[str, object]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='{"status":"success","delivery_status":"final","summary":"done","answer":"done","evidence":[],"remaining_work":[],"blocking_reason":""}',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='{"status":"success","delivery_status":"final","summary":"done","answer":"done","evidence":[],"remaining_work":[],"blocking_reason":""}',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-raw-final-json'),
        node=SimpleNamespace(node_id='node-raw-final-json', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-raw-final-json","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-raw-final-json', 'node_id': 'node-raw-final-json'},
        max_iterations=3,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert len(requests) == 2
    assert requests[0].get('tool_choice') is None
    assert requests[1].get('tool_choice') == {
        'type': 'function',
        'function': {'name': 'submit_final_result'},
    }


@pytest.mark.asyncio
async def test_react_loop_restores_invalid_final_submission_count_from_runtime_frame() -> None:
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:invalid-final',
                            name='submit_final_result',
                            arguments={
                                'status': 'failed',
                                'delivery_status': 'final',
                                'summary': 'still invalid',
                                'answer': '',
                                'evidence': [],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:valid-final',
                        name='submit_final_result',
                        arguments={
                            'status': 'failed',
                            'delivery_status': 'blocked',
                            'summary': 'done',
                            'answer': '',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': 'done',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    backend = _Backend()
    log_service = _FakeLogService()
    log_service.upsert_frame(
        'task-persisted-invalid-final',
        {
            'node_id': 'node-persisted-invalid-final',
            'phase': 'before_model',
            'invalid_final_submission_count': 4,
            'last_invalid_final_submission_reason': 'persisted invalid final result',
            'last_contract_violations': ['execution failed result requires delivery_status=blocked'],
        },
    )
    loop = ReActToolLoop(chat_backend=backend, log_service=log_service, max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-persisted-invalid-final'),
        node=SimpleNamespace(node_id='node-persisted-invalid-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-persisted-invalid-final","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-persisted-invalid-final', 'node_id': 'node-persisted-invalid-final'},
        max_iterations=3,
    )

    assert result.status == 'failed'
    assert 'Invalid final result submission detected 5 consecutive times' in result.blocking_reason
    assert backend.turn == 1


@pytest.mark.asyncio
async def test_react_loop_retries_provider_chain_exhaustion_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))

    monkeypatch.setattr('main.runtime.react_loop.asyncio.sleep', _fake_sleep)

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            self.turn += 1
            if self.turn < 3:
                raise RuntimeError('Model provider call failed after exhausting the configured fallback chain.')
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-provider-retry'),
        node=SimpleNamespace(node_id='node-provider-retry', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-provider-retry","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-provider-retry', 'node_id': 'node-provider-retry'},
        max_iterations=3,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert len(requests) == 3
    assert sleep_calls == [1.0, 2.0]
