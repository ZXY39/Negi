from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.rag_memory import ContextRecordV2, MemoryManager
from g3ku.providers.base import LLMResponse
from g3ku.runtime.tool_history import analyze_tool_call_history
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


def test_compact_history_preserves_complete_tool_turns() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        {
            "role": "assistant",
            "content": "turn-1",
            "tool_calls": [
                {"id": "call-a|fc_1", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
                {"id": "call-b|fc_2", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
                {"id": "call-c|fc_3", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
                {"id": "call-d|fc_4", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-a|fc_1", "content": "a"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-b|fc_2", "content": "b"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-c|fc_3", "content": "c"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-d|fc_4", "content": "d"},
        {
            "role": "assistant",
            "content": "turn-2",
            "tool_calls": [
                {"id": "call-e|fc_5", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
                {"id": "call-f|fc_6", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
                {"id": "call-g|fc_7", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-e|fc_5", "content": "e"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-f|fc_6", "content": "f"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-g|fc_7", "content": "g"},
        {
            "role": "assistant",
            "content": "turn-3",
            "tool_calls": [
                {"id": "call-h|fc_8", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
                {"id": "call-i|fc_9", "type": "function", "function": {"name": "filesystem", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-h|fc_8", "content": "h"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-i|fc_9", "content": "i"},
    ]

    compacted = loop._compact_history(messages, preserve_non_system=6)
    compacted_analysis = analyze_tool_call_history(compacted)

    assert compacted_analysis.orphan_tool_result_ids == []
    assert compacted_analysis.dangling_assistant_call_ids == []
    assert any(item.get("role") == "assistant" and str(item.get("content") or "") == "turn-2" for item in compacted)
    assert any(item.get("role") == "assistant" and str(item.get("content") or "") == "turn-3" for item in compacted)
    assert compacted[-7:] == messages[-7:]


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
