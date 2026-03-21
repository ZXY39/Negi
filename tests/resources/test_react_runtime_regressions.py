from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.rag_memory import ContextRecordV2, MemoryManager
from g3ku.agent.tools.base import Tool
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.runtime.ceo_async_task_guard import GUARD_OVERLAY_MARKER, maybe_build_execution_overlay
from g3ku.runtime.tool_history import analyze_tool_call_history
from main.runtime.internal_tools import SpawnPrecheckTool
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
        self.node_outputs: list[dict[str, object]] = []

    def set_pause_state(self, task_id: str, pause_requested: bool, is_paused: bool) -> None:
        _ = task_id, pause_requested, is_paused

    def update_node_input(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def upsert_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def append_node_output(self, *args, **kwargs) -> None:
        self.node_outputs.append({'args': args, 'kwargs': kwargs})

    def update_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def remove_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs


class _RecordingTool(Tool):
    def __init__(self, *, name: str = 'filesystem', result: str = '{"status":"ok"}') -> None:
        self._name = name
        self._result = result
        self.calls: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f'{self._name} stub'

    @property
    def parameters(self) -> dict[str, object]:
        return {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'path'},
                'action': {'type': 'string', 'description': 'action'},
                'children': {'type': 'array', 'items': {'type': 'object'}},
            },
        }

    async def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self._result


def _tool_call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


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
    overlay = maybe_build_execution_overlay(iteration=21, can_spawn_children=True)

    request_messages = loop._apply_temporary_system_overlay(base_messages, overlay_text=overlay)

    assert base_messages[0]['content'] == 'base system'
    assert request_messages[0]['role'] == 'system'
    assert '当前你已调用20轮工具' in str(request_messages[0]['content'])
    assert f'{GUARD_OVERLAY_MARKER}\n' in str(request_messages[0]['content'])
    assert request_messages[1:] == base_messages


def test_validate_spawn_precheck_requires_first_precheck_for_real_tools() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)

    validation = loop._validate_spawn_precheck_turn(
        response_tool_calls=[
            _tool_call('call_1', 'filesystem', {'path': '/tmp/demo', 'action': 'read'}),
        ],
        tools={'spawn_precheck': SpawnPrecheckTool()},
        runtime_context={'node_kind': 'execution', 'can_spawn_children': True},
        round_index=1,
    )

    assert validation.valid is False
    assert 'spawn_precheck_missing' in validation.violation_codes
    assert validation.round_metadata['spawn_precheck']['valid'] is False


def test_validate_spawn_precheck_rejects_spawn_child_nodes_mixed_with_other_tools() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)

    validation = loop._validate_spawn_precheck_turn(
        response_tool_calls=[
            _tool_call('call_pre', 'spawn_precheck', {
                'decision': 'spawn_child_nodes',
                'reason': 'task can be split',
                'rule_ids': [1, 3],
                'rule_semantics': 'matched',
            }),
            _tool_call('call_spawn', 'spawn_child_nodes', {'children': [{'goal': 'a', 'prompt': 'b'}]}),
            _tool_call('call_fs', 'filesystem', {'path': '/tmp/demo', 'action': 'read'}),
        ],
        tools={'spawn_precheck': SpawnPrecheckTool()},
        runtime_context={'node_kind': 'execution', 'can_spawn_children': True},
        round_index=1,
    )

    assert validation.valid is False
    assert 'spawn_child_nodes_mixed_with_other_tools' in validation.violation_codes


def test_validate_spawn_precheck_allows_control_only_turn_without_precheck() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)

    validation = loop._validate_spawn_precheck_turn(
        response_tool_calls=[
            _tool_call('call_wait', 'wait_tool_execution', {'execution_id': 'exec-1'}),
        ],
        tools={'spawn_precheck': SpawnPrecheckTool()},
        runtime_context={'node_kind': 'execution', 'can_spawn_children': True},
        round_index=1,
    )

    assert validation.valid is True
    assert validation.executable_tool_calls
    assert validation.round_metadata['spawn_precheck']['present'] is False


def test_validate_spawn_precheck_rejects_precheck_when_node_cannot_spawn() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)

    validation = loop._validate_spawn_precheck_turn(
        response_tool_calls=[
            _tool_call('call_pre', 'spawn_precheck', {
                'decision': 'continue_self_execute',
                'reason': 'keep going',
                'rule_ids': [1, 2, 3, 4],
                'rule_semantics': 'unmatched',
            }),
            _tool_call('call_fs', 'filesystem', {'path': '/tmp/demo', 'action': 'read'}),
        ],
        tools={},
        runtime_context={'node_kind': 'execution', 'can_spawn_children': False},
        round_index=1,
    )

    assert validation.valid is False
    assert 'spawn_precheck_not_allowed' in validation.violation_codes


@pytest.mark.asyncio
async def test_react_loop_soft_blocks_invalid_spawn_precheck_turn_and_requests_retry() -> None:
    log_service = _FakeLogService()
    filesystem_tool = _RecordingTool(name='filesystem')
    requests: list[list[dict[str, object]]] = []

    class _Backend:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            requests.append([dict(item) for item in list(kwargs.get('messages') or [])])
            if self.calls == 1:
                return LLMResponse(
                    content='searching without precheck',
                    tool_calls=[_tool_call('call_fs', 'filesystem', {'path': '/tmp/demo', 'action': 'read'})],
                    finish_reason='stop',
                )
            return LLMResponse(
                content=json.dumps(
                    {
                        'status': 'success',
                        'delivery_status': 'final',
                        'summary': 'done',
                        'answer': 'done',
                        'evidence': [],
                        'remaining_work': [],
                        'blocking_reason': '',
                    },
                    ensure_ascii=False,
                ),
                tool_calls=[],
                finish_reason='stop',
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=4)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-1","goal":"demo"}'},
        ],
        tools={'spawn_precheck': SpawnPrecheckTool(), 'filesystem': filesystem_tool},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1', 'node_kind': 'execution', 'can_spawn_children': True},
        max_iterations=4,
    )

    assert result.status == 'success'
    assert filesystem_tool.calls == []
    assert log_service.node_outputs[0]['kwargs']['round_metadata']['spawn_precheck']['valid'] is False
    assert 'spawn_precheck_missing' in log_service.node_outputs[0]['kwargs']['round_metadata']['spawn_precheck']['violation_codes']
    assert any(
        str(item.get('role') or '') == 'user' and '[SPAWN_PRECHECK_PROTOCOL_ERROR]' in str(item.get('content') or '')
        for item in requests[1]
    )


@pytest.mark.asyncio
async def test_react_loop_executes_valid_continue_self_execute_turn_without_dangling_precheck() -> None:
    log_service = _FakeLogService()
    filesystem_tool = _RecordingTool(name='filesystem', result='{"status":"ok","summary":"read ok"}')
    requests: list[list[dict[str, object]]] = []

    class _Backend:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            requests.append([dict(item) for item in list(kwargs.get('messages') or [])])
            if self.calls == 1:
                return LLMResponse(
                    content='first turn',
                    tool_calls=[
                        _tool_call('call_pre', 'spawn_precheck', {
                            'decision': 'continue_self_execute',
                            'reason': 'single file read is cheaper',
                            'rule_ids': [1, 2, 3, 4],
                            'rule_semantics': 'unmatched',
                        }),
                        _tool_call('call_fs', 'filesystem', {'path': '/tmp/demo', 'action': 'read'}),
                    ],
                    finish_reason='stop',
                )
            return LLMResponse(
                content=json.dumps(
                    {
                        'status': 'success',
                        'delivery_status': 'final',
                        'summary': 'done',
                        'answer': 'done',
                        'evidence': [{'kind': 'file', 'path': '/tmp/demo', 'ref': '', 'start_line': 1, 'end_line': 1, 'note': 'checked'}],
                        'remaining_work': [],
                        'blocking_reason': '',
                    },
                    ensure_ascii=False,
                ),
                tool_calls=[],
                finish_reason='stop',
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=4)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-1","goal":"demo"}'},
        ],
        tools={'spawn_precheck': SpawnPrecheckTool(), 'filesystem': filesystem_tool},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1', 'node_kind': 'execution', 'can_spawn_children': True},
        max_iterations=4,
    )

    assert result.status == 'success'
    assert len(filesystem_tool.calls) == 1
    assert log_service.node_outputs[0]['kwargs']['round_metadata']['spawn_precheck']['valid'] is True
    assert log_service.node_outputs[0]['kwargs']['round_metadata']['spawn_precheck']['decision'] == 'continue_self_execute'
    assert analyze_tool_call_history(requests[1]).dangling_assistant_call_ids == []
