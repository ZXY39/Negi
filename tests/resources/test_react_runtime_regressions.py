from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.rag_memory import ContextRecordV2, MemoryManager
from g3ku.agent.tools.base import Tool
from g3ku.providers.base import LLMResponse
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService


class _FakeTaskStore:
    def __init__(self) -> None:
        self._task = SimpleNamespace(cancel_requested=False, pause_requested=False)

    def get_task(self, task_id: str):
        _ = task_id
        return self._task


class _FakeLogService:
    def __init__(self) -> None:
        self._store = _FakeTaskStore()
        self._content_store = None
        self._snapshot_payload_builder = None

    def set_pause_state(self, task_id: str, pause_requested: bool, is_paused: bool) -> None:
        _ = task_id, pause_requested, is_paused

    def update_node_input(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def upsert_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def append_node_output(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def update_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def remove_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs


def test_prepare_messages_passthrough_keeps_original_messages_even_with_content_store() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    original = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        {"role": "assistant", "content": "plain assistant summary"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-a", "content": "tool output"},
    ]

    prepared = loop._prepare_messages(original, runtime_context={"task_id": "task-1", "node_id": "node-1"})

    assert prepared == original
    assert all("[[G3KU_COMPACT_HISTORY_V1]]" not in str(item.get("content") or "") for item in prepared)


@pytest.mark.asyncio
async def test_react_loop_run_keeps_long_history_uncompacted() -> None:
    calls: list[list[dict[str, object]]] = []

    class _Backend:
        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get("messages") or [])])
            return LLMResponse(
                content='{"status":"failed","delivery_status":"blocked","summary":"done","answer":"","evidence":[],"remaining_work":[],"blocking_reason":"done"}',
                tool_calls=[],
                finish_reason="stop",
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
    ]
    for index in range(40):
        messages.append({"role": "assistant", "content": f"assistant-{index}"})
        messages.append({"role": "tool", "name": "filesystem", "tool_call_id": f"call-{index}", "content": f"tool-{index}"})

    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=messages,
        tools={},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=2,
    )

    assert result.status == "failed"
    assert len(calls) == 1
    sent_messages = calls[0]
    assert len(sent_messages) == len(messages)
    assert all("[[G3KU_COMPACT_HISTORY_V1]]" not in str(item.get("content") or "") for item in sent_messages)


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
async def test_enrich_node_messages_passes_visibility_filtered_allowlists() -> None:
    captured: dict[str, object] = {}

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            return key == "unified_context"

        async def retrieve_block(self, **kwargs):
            captured.update(kwargs)
            return "memory block"

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='filesystem'),
        SimpleNamespace(tool_id='content'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator'),
    ]

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="where is the plan", goal="where is the plan", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[{"role": "system", "content": "base prompt"}],
    )

    assert captured["session_key"] == "web:ceo-origin"
    assert captured["channel"] == "web"
    assert captured["chat_id"] == "shared"
    assert captured["allowed_context_types"] == ["memory", "resource", "skill"]
    assert captured["allowed_resource_record_ids"] == ["tool:filesystem", "tool:content"]
    assert captured["allowed_skill_record_ids"] == ["skill:skill-creator"]
    assert "memory block" in enriched[0]["content"]


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


def test_apply_temporary_system_overlay_keeps_base_messages_untouched() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    base_messages = [
        {'role': 'system', 'content': 'base system'},
        {'role': 'user', 'content': 'base user'},
    ]
    overlay = 'temporary system overlay'

    request_messages = loop._apply_temporary_system_overlay(base_messages, overlay_text=overlay)

    assert base_messages[0]['content'] == 'base system'
    assert request_messages[0]['role'] == 'system'
    assert request_messages[0]['content'] == overlay
    assert request_messages[1:] == base_messages


def test_execution_result_protocol_message_avoids_partial_guidance() -> None:
    message = ReActToolLoop._result_protocol_message(node_kind='execution')

    assert 'failed+partial' not in message
    assert 'delivery_status="partial"' in message
    assert 'failed+blocked' in message
    assert 'If you are ending the node now' in message
    assert 'If the task is not complete yet' in message


def test_execution_result_contract_violation_message_keeps_workflow_open() -> None:
    message = ReActToolLoop._result_contract_violation_message(
        ['summary must not be empty'],
        node_kind='execution',
    )

    assert 'Fix every violation and reply with only one JSON object.' not in message
    assert 'If you are ending the node now' in message
    assert 'do not force another premature result JSON' in message


def test_acceptance_result_contract_violation_message_uses_final_or_blocked_only() -> None:
    message = ReActToolLoop._result_contract_violation_message(
        ['summary must not be empty'],
        node_kind='acceptance',
    )

    assert 'failed+partial' not in message
    assert 'delivery_status="partial"' in message
    assert 'failed+final' in message
    assert 'failed+blocked' in message


@pytest.mark.asyncio
async def test_react_loop_uses_system_overlay_for_execution_result_repair() -> None:
    calls: list[list[dict[str, object]]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='not json yet',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='{"status":"failed","delivery_status":"blocked","summary":"done","answer":"","evidence":[],"remaining_work":[],"blocking_reason":"done"}',
                    tool_calls=[],
                    finish_reason='stop',
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
        tools={},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=3,
    )

    assert result.status == 'failed'
    assert len(calls) == 2
    second_request = calls[1]
    assert second_request[0]['role'] == 'system'
    assert 'If you are ending the node now' in str(second_request[0]['content'])
    assert not any(
        message.get('role') == 'user' and 'If you are ending the node now' in str(message.get('content') or '')
        for message in second_request
    )
