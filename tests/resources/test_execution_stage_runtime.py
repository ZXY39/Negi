from __future__ import annotations

from pathlib import Path

import pytest

from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.agent.tools.base import Tool
from main.protocol import now_iso
from main.runtime.internal_tools import SubmitNextStageTool
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


class _StaticTool(Tool):
    def __init__(self, name: str, result: str = 'ok') -> None:
        self._name = name
        self._result = result

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f'{self._name} tool'

    @property
    def parameters(self) -> dict:
        return {'type': 'object', 'properties': {}}

    async def execute(self, **kwargs):
        _ = kwargs
        return self._result


def _mark_worker_online(service: MainRuntimeService) -> None:
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )


async def _create_web_task(service: MainRuntimeService):
    _mark_worker_online(service)
    return await service.create_task('stage test task', session_id='web:shared')


@pytest.mark.asyncio
async def test_execution_stage_blocks_other_tools_before_stage_and_after_budget(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    ordinary = _StaticTool('ordinary_tool')
    spawn = _StaticTool('spawn_child_nodes')
    try:
        record = await _create_web_task(service)
        runtime_context = {
            'task_id': record.task_id,
            'node_id': record.root_node_id,
            'node_kind': 'execution',
            'actor_role': 'execution',
        }

        blocked = await service._react_loop._execute_tool(
            tools={'ordinary_tool': ordinary},
            tool_name='ordinary_tool',
            arguments={},
            runtime_context=runtime_context,
        )
        assert blocked.startswith('Error: no active stage')

        service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='完成当前阶段；优先派生：复杂子任务；自行完成：直接读取与整理',
            tool_round_budget=1,
        )

        allowed = await service._react_loop._execute_tool(
            tools={'ordinary_tool': ordinary},
            tool_name='ordinary_tool',
            arguments={},
            runtime_context=runtime_context,
        )
        assert allowed == 'ok'

        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=[{'id': 'call:ordinary', 'name': 'ordinary_tool', 'arguments': {}}],
            created_at=now_iso(),
        )

        exhausted_ordinary = await service._react_loop._execute_tool(
            tools={'ordinary_tool': ordinary},
            tool_name='ordinary_tool',
            arguments={},
            runtime_context=runtime_context,
        )
        exhausted_spawn = await service._react_loop._execute_tool(
            tools={'spawn_child_nodes': spawn},
            tool_name='spawn_child_nodes',
            arguments={},
            runtime_context=runtime_context,
        )
        assert exhausted_ordinary.startswith('Error: current stage budget is exhausted')
        assert exhausted_spawn.startswith('Error: current stage budget is exhausted')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stage_round_counts_once_and_spawn_promotes_stage_mode_in_trace(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        stage = service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='完成当前阶段；优先派生：模块A；自行完成：模块B',
            tool_round_budget=2,
        )
        tool_calls = [
            {'id': 'call:one', 'name': 'filesystem', 'arguments': {'path': 'a'}},
            {'id': 'call:two', 'name': 'spawn_child_nodes', 'arguments': {'children': []}},
        ]
        updated = service.log_service.append_node_output(
            record.task_id,
            record.root_node_id,
            content='',
            tool_calls=tool_calls,
        )
        assert updated is not None
        created_at = str(updated.output[-1].created_at or '')
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=tool_calls,
            created_at=created_at,
        )
        service.log_service.mark_execution_stage_contains_spawn(record.task_id, record.root_node_id)

        snapshot = service.log_service.execution_stage_gate_snapshot(record.task_id, record.root_node_id)
        active = dict(snapshot.get('active_stage') or {})
        assert active['stage_id'] == stage['stage_id']
        assert active['mode'] == '包含派生'
        assert active['tool_rounds_used'] == 1

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id)
        assert detail is not None
        stages = detail['item']['execution_trace']['stages']
        assert len(stages) == 1
        assert stages[0]['mode'] == '包含派生'
        assert stages[0]['tool_rounds_used'] == 1
        assert len(stages[0]['rounds']) == 1
        assert [item['tool_name'] for item in stages[0]['rounds'][0]['tools']] == ['filesystem', 'spawn_child_nodes']
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_ref_based_content_reads_now_consume_stage_budget(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        stage = service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='回读已有 ref；优先派生：无；自行完成：读取已外置内容',
            tool_round_budget=2,
        )
        tool_calls = [
            {
                'id': 'call:content-open',
                'name': 'content',
                'arguments': {'action': 'open', 'ref': 'path:tools/agent_browser/resource.yaml', 'path': ''},
            },
            {
                'id': 'call:content-search',
                'name': 'content',
                'arguments': {'action': 'search', 'ref': 'artifact:artifact:test', 'path': '', 'query': 'browser'},
            },
        ]
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=tool_calls,
            created_at=now_iso(),
        )

        snapshot = service.log_service.execution_stage_gate_snapshot(record.task_id, record.root_node_id)
        active = dict(snapshot.get('active_stage') or {})
        assert active['stage_id'] == stage['stage_id']
        assert active['tool_rounds_used'] == 1

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id)
        assert detail is not None
        rounds = detail['item']['execution_trace']['stages'][0]['rounds']
        assert len(rounds) == 1
        assert rounds[0]['budget_counted'] is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_mixed_ref_reads_and_regular_tools_still_consume_stage_budget(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        stage = service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='混合读取；优先派生：无；自行完成：ref 回读 + 仓库探索',
            tool_round_budget=2,
        )
        tool_calls = [
            {
                'id': 'call:content-open',
                'name': 'content',
                'arguments': {'action': 'open', 'ref': 'path:tools/agent_browser/resource.yaml', 'path': ''},
            },
            {
                'id': 'call:filesystem',
                'name': 'filesystem',
                'arguments': {'action': 'list', 'path': 'E:\\Program\\G3KU\\tools'},
            },
        ]
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=tool_calls,
            created_at=now_iso(),
        )

        snapshot = service.log_service.execution_stage_gate_snapshot(record.task_id, record.root_node_id)
        active = dict(snapshot.get('active_stage') or {})
        assert active['stage_id'] == stage['stage_id']
        assert active['tool_rounds_used'] == 1

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id)
        assert detail is not None
        rounds = detail['item']['execution_trace']['stages'][0]['rounds']
        assert len(rounds) == 1
        assert rounds[0]['budget_counted'] is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_final_budgeted_round_is_allowed_and_next_turn_is_blocked(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    ordinary = _StaticTool('filesystem', result='round-ok')
    try:
        record = await _create_web_task(service)
        service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='最后一轮应允许执行；优先派生：无；自行完成：读取文件',
            tool_round_budget=1,
        )
        runtime_context = {
            'task_id': record.task_id,
            'node_id': record.root_node_id,
            'node_kind': 'execution',
            'actor_role': 'execution',
            'stage_turn_granted': True,
        }
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=[{'id': 'call:last', 'name': 'filesystem', 'arguments': {}}],
            created_at=now_iso(),
        )
        allowed = await service._react_loop._execute_tool(
            tools={'filesystem': ordinary},
            tool_name='filesystem',
            arguments={},
            runtime_context=runtime_context,
        )
        assert allowed == 'round-ok'

        blocked = await service._react_loop._execute_tool(
            tools={'filesystem': ordinary},
            tool_name='filesystem',
            arguments={},
            runtime_context={k: v for k, v in runtime_context.items() if k != 'stage_turn_granted'},
        )
        assert blocked.startswith('Error: current stage budget is exhausted')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_react_loop_uses_stable_prompt_cache_key_despite_dynamic_stage_overlay(tmp_path: Path):
    calls: list[dict[str, object]] = []

    class _Backend:
        def __init__(self) -> None:
            self._turn = 0

        async def chat(self, **kwargs):
            self._turn += 1
            calls.append(
                {
                    'prompt_cache_key': kwargs.get('prompt_cache_key'),
                    'messages': list(kwargs.get('messages') or []),
                }
                )
            if self._turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:stage',
                            name='submit_next_stage',
                            arguments={
                                'stage_goal': '第一阶段；优先派生：无；自行完成：读取文件',
                                'tool_round_budget': 2,
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                        usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 90},
                    )
            if self._turn == 2:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:file',
                            name='filesystem',
                            arguments={'action': 'list', 'path': 'E:\\Program\\G3KU'},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 12, 'output_tokens': 5, 'cache_hit_tokens': 92},
                )
            return LLMResponse(
                content='{"status":"failed","delivery_status":"blocked","summary":"stop","answer":"","evidence":[],"remaining_work":[],"blocking_reason":"stop"}',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 11, 'output_tokens': 4, 'cache_hit_tokens': 95},
            )

    service = MainRuntimeService(
        chat_backend=_Backend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('react loop cache key task', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        assert len(calls) == 3
        assert str(calls[0]['prompt_cache_key'] or '').strip()
        assert calls[1]['prompt_cache_key'] == calls[2]['prompt_cache_key']
        overlay_1 = str((calls[0]['messages'][0] or {}).get('content') or '')
        overlay_2 = str((calls[1]['messages'][0] or {}).get('content') or '')
        overlay_3 = str((calls[2]['messages'][0] or {}).get('content') or '')
        assert overlay_1 != overlay_2
        assert overlay_2 != overlay_3
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_next_stage_closes_previous_stage_and_starts_new_stage(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        first = service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='第一阶段；优先派生：无；自行完成：整理上下文',
            tool_round_budget=3,
        )
        second = service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='第二阶段；优先派生：复杂验证；自行完成：整合结果',
            tool_round_budget=4,
        )

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id)
        assert detail is not None
        stages = detail['item']['execution_trace']['stages']
        assert [stage['status'] for stage in stages] == ['完成', '进行中']
        assert stages[0]['stage_id'] == first['stage_id']
        assert stages[1]['stage_id'] == second['stage_id']
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stage_summary_is_exposed_in_live_runtime_frame(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='阶段摘要；优先派生：搜索外部依赖；自行完成：本地整理',
            tool_round_budget=5,
        )

        snapshot = service.get_task_detail_payload(record.task_id, mark_read=False)
        assert snapshot is not None
        frames = list((snapshot['progress']['live_state'] or {}).get('frames') or [])
        assert frames
        root_frame = next(item for item in frames if item['node_id'] == record.root_node_id)
        assert root_frame['stage_status'] == '进行中'
        assert root_frame['stage_goal'] == '阶段摘要；优先派生：搜索外部依赖；自行完成：本地整理'
        assert root_frame['stage_total_steps'] == 5
    finally:
        await service.close()


def test_submit_next_stage_tool_schema_budget_max_is_ten() -> None:
    async def _submit(stage_goal: str, tool_round_budget: int) -> dict[str, object]:
        return {'stage_goal': stage_goal, 'tool_round_budget': tool_round_budget}

    tool = SubmitNextStageTool(_submit)

    assert tool.parameters['properties']['tool_round_budget']['minimum'] == 1
    assert tool.parameters['properties']['tool_round_budget']['maximum'] == 10


@pytest.mark.asyncio
async def test_submit_next_stage_rejects_budget_above_ten(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        with pytest.raises(ValueError, match='tool_round_budget must be between 1 and 10'):
            service.log_service.submit_next_stage(
                record.task_id,
                record.root_node_id,
                stage_goal='预算校验；优先派生：无；自行完成：拒绝超出上限的阶段预算',
                tool_round_budget=11,
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_next_stage_does_not_trip_repeated_action_breaker(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self._turn = 0

        async def chat(self, **kwargs):
            self._turn += 1
            if self._turn <= 3:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id=f'call:stage:{self._turn}',
                            name='submit_next_stage',
                            arguments={
                                'stage_goal': '重复阶段切换；优先派生：无；自行完成：验证重复阶段提交不会触发 repeated-action breaker',
                                'tool_round_budget': 1,
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            return LLMResponse(
                content='{"status":"failed","delivery_status":"blocked","summary":"intentional stop","answer":"","evidence":[],"remaining_work":["stop after breaker check"],"blocking_reason":"intentional stop"}',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    service = MainRuntimeService(
        chat_backend=_Backend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('submit-next-stage breaker regression', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert task.status == 'failed'
        assert 'repeated tool call detected: submit_next_stage' not in str(task.failure_reason or '')
        assert 'intentional stop' in str(task.failure_reason or '')
    finally:
        await service.close()
