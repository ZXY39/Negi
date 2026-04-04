from __future__ import annotations

import json
from pathlib import Path

import pytest

from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.agent.tools.base import Tool
from main.protocol import now_iso
from main.runtime.chat_backend import build_stable_prompt_cache_key
from main.runtime.internal_tools import SpawnChildNodesTool, SubmitFinalResultTool, SubmitNextStageTool
from main.runtime.stage_budget import STAGE_TOOL_NAME, visible_tools_for_stage_iteration
from main.runtime.stage_messages import build_execution_stage_overlay
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


def _final_result_call(
    *,
    call_id: str = "call:final",
    status: str,
    delivery_status: str,
    summary: str,
    answer: str,
    evidence: list[dict[str, object]] | None = None,
    remaining_work: list[str] | None = None,
    blocking_reason: str = "",
) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="submit_final_result",
        arguments={
            "status": status,
            "delivery_status": delivery_status,
            "summary": summary,
            "answer": answer,
            "evidence": list(evidence or []),
            "remaining_work": list(remaining_work or []),
            "blocking_reason": blocking_reason,
        },
    )


def _submit_final_result_tool(*, node_kind: str = "execution") -> SubmitFinalResultTool:
    async def _submit(payload: dict[str, object]) -> dict[str, object]:
        return dict(payload)

    return SubmitFinalResultTool(_submit, node_kind=node_kind)


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


def _tool_result_payload(
    *,
    call_id: str,
    tool_name: str,
    content: str,
    status: str = 'success',
) -> dict[str, dict[str, object]]:
    started_at = now_iso()
    finished_at = now_iso()
    return {
        'live_state': {
            'tool_call_id': call_id,
            'tool_name': tool_name,
            'status': status,
            'started_at': started_at,
            'finished_at': finished_at,
            'elapsed_seconds': 0.1,
        },
        'tool_message': {
            'role': 'tool',
            'tool_call_id': call_id,
            'name': tool_name,
            'content': content,
            'started_at': started_at,
            'finished_at': finished_at,
            'elapsed_seconds': 0.1,
            'status': status,
        },
    }


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
        final_result = await service._react_loop._execute_tool(
            tools={'submit_final_result': _submit_final_result_tool()},
            tool_name='submit_final_result',
            arguments={
                'status': 'failed',
                'delivery_status': 'blocked',
                'summary': 'blocked after budget',
                'answer': '',
                'evidence': [],
                'remaining_work': [],
                'blocking_reason': 'budget exhausted',
            },
            runtime_context=runtime_context,
        )
        assert exhausted_ordinary.startswith('Error: current stage budget is exhausted')
        assert exhausted_spawn.startswith('Error: current stage budget is exhausted')
        assert not final_result.startswith('Error:')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_acceptance_stage_blocks_other_tools_before_stage_and_after_budget(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    ordinary = _StaticTool('ordinary_tool')
    try:
        record = await _create_web_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=root,
            goal='accept root output',
            acceptance_prompt='verify the root output',
            parent_node_id=root.node_id,
        )
        tools = service.node_runner._build_tools(task=task, node=acceptance)
        assert 'submit_next_stage' in tools
        assert 'submit_final_result' in tools
        assert 'spawn_child_nodes' not in tools

        runtime_context = {
            'task_id': record.task_id,
            'node_id': acceptance.node_id,
            'node_kind': 'acceptance',
            'actor_role': 'inspection',
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
            acceptance.node_id,
            stage_goal='inspect evidence lines and final verdict consistency',
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
            acceptance.node_id,
            tool_calls=[{'id': 'call:ordinary', 'name': 'ordinary_tool', 'arguments': {}}],
            created_at=now_iso(),
        )

        exhausted = await service._react_loop._execute_tool(
            tools={'ordinary_tool': ordinary},
            tool_name='ordinary_tool',
            arguments={},
            runtime_context=runtime_context,
        )
        assert exhausted.startswith('Error: current stage budget is exhausted')
    finally:
        await service.close()


def test_stage_visibility_keeps_all_tools_visible_before_stage_and_after_budget() -> None:
    tools = {
        STAGE_TOOL_NAME: _StaticTool(STAGE_TOOL_NAME),
        'ordinary_tool': _StaticTool('ordinary_tool'),
    }

    before_stage = visible_tools_for_stage_iteration(
        tools,
        has_active_stage=False,
        transition_required=False,
    )
    exhausted_stage = visible_tools_for_stage_iteration(
        tools,
        has_active_stage=True,
        transition_required=True,
    )

    assert set(before_stage) == {STAGE_TOOL_NAME, 'ordinary_tool'}
    assert set(exhausted_stage) == {STAGE_TOOL_NAME, 'ordinary_tool'}


def test_spawn_child_nodes_tool_requires_execution_policy_for_each_child() -> None:
    async def _spawn_callback(specs, call_id):
        _ = specs, call_id
        return []

    tool = SpawnChildNodesTool(_spawn_callback)

    errors = tool.validate_params(
        {
            'children': [
                {
                    'goal': 'child goal',
                    'prompt': 'child prompt',
                }
            ]
        }
    )

    assert 'missing required children[0].execution_policy' in errors


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

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
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
async def test_execution_trace_uses_tool_result_records_for_completed_stage_steps(tmp_path: Path):
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
            stage_goal='load skills first',
            tool_round_budget=3,
        )
        tool_calls = [
            ToolCallRequest(
                id='call:skill:batch',
                name='load_skill_context',
                arguments={'skill_id': 'batch-web-planning'},
            ),
            ToolCallRequest(
                id='call:skill:memory',
                name='load_skill_context',
                arguments={'skill_id': 'memory'},
            ),
        ]
        service.log_service.append_node_output(
            record.task_id,
            record.root_node_id,
            content='',
            tool_calls=[
                {'id': call.id, 'name': call.name, 'arguments': dict(call.arguments or {})}
                for call in tool_calls
            ],
        )
        service.log_service.record_tool_result_batch(
            task_id=record.task_id,
            node_id=record.root_node_id,
            response_tool_calls=tool_calls,
            results=[
                _tool_result_payload(
                    call_id='call:skill:batch',
                    tool_name='load_skill_context',
                    content='loaded batch-web-planning skill body',
                ),
                _tool_result_payload(
                    call_id='call:skill:memory',
                    tool_name='load_skill_context',
                    content='loaded memory skill body',
                ),
            ],
        )
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=[
                {'id': call.id, 'name': call.name, 'arguments': dict(call.arguments or {})}
                for call in tool_calls
            ],
            created_at=now_iso(),
        )
        service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='plan child collection',
            tool_round_budget=2,
            completed_stage_summary='skills loaded',
        )

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')

        assert detail is not None
        stages = detail['item']['execution_trace']['stages']
        assert len(stages) == 2
        first_round_tools = stages[0]['rounds'][0]['tools']
        assert [item['tool_name'] for item in first_round_tools] == ['load_skill_context', 'load_skill_context']
        assert [item['status'] for item in first_round_tools] == ['success', 'success']
        assert [item['output_text'] for item in first_round_tools] == [
            'loaded batch-web-planning skill body',
            'loaded memory skill body',
        ]
        assert all(str(item['finished_at'] or '').strip() for item in first_round_tools)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_persisted_tool_result_output_ref_stays_canonical_while_execution_trace_preserves_wrapper_ref(tmp_path: Path):
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
        navigator = service.content_store
        inner = navigator.maybe_externalize_text(
            'canonical body',
            runtime={'task_id': record.task_id, 'node_id': record.root_node_id},
            display_name='inner',
            source_kind='node_output',
            force=True,
        )
        assert inner is not None
        wrapped = navigator.maybe_externalize_text(
            json.dumps(inner.to_dict(), ensure_ascii=False),
            runtime={'task_id': record.task_id, 'node_id': record.root_node_id},
            display_name='wrapped',
            source_kind='tool_result:content',
            force=True,
        )
        assert wrapped is not None

        service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='inspect wrapped tool result output refs',
            tool_round_budget=1,
        )
        tool_calls = [
            ToolCallRequest(
                id='call:content',
                name='content',
                arguments={'action': 'open', 'ref': wrapped.ref},
            )
        ]
        service.log_service.append_node_output(
            record.task_id,
            record.root_node_id,
            content='',
            tool_calls=[{'id': 'call:content', 'name': 'content', 'arguments': {'action': 'open', 'ref': wrapped.ref}}],
        )
        service.log_service.record_tool_result_batch(
            task_id=record.task_id,
            node_id=record.root_node_id,
            response_tool_calls=tool_calls,
            results=[
                _tool_result_payload(
                    call_id='call:content',
                    tool_name='content',
                    content=json.dumps(wrapped.to_dict(), ensure_ascii=False),
                )
            ],
        )
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=[{'id': 'call:content', 'name': 'content', 'arguments': {'action': 'open', 'ref': wrapped.ref}}],
            created_at=now_iso(),
        )

        tool_results = service.store.list_task_node_tool_results(record.task_id, record.root_node_id)
        assert len(tool_results) == 1
        assert tool_results[0].output_ref == inner.ref

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        tool_step = detail['item']['execution_trace']['stages'][0]['rounds'][0]['tools'][0]
        assert tool_step['output_ref'] == wrapped.ref
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_inline_content_json_tool_results_preserve_structured_refs(tmp_path: Path):
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
        navigator = service.content_store
        inner = navigator.maybe_externalize_text(
            'alpha\nneedle\nomega\n',
            runtime={'task_id': record.task_id, 'node_id': record.root_node_id},
            display_name='inner',
            source_kind='node_output',
            force=True,
        )
        assert inner is not None
        wrapped = navigator.maybe_externalize_text(
            json.dumps(inner.to_dict(), ensure_ascii=False),
            runtime={'task_id': record.task_id, 'node_id': record.root_node_id},
            display_name='wrapped',
            source_kind='tool_result:content',
            force=True,
        )
        assert wrapped is not None
        inline_open_payload = service.open_content(ref=wrapped.ref)

        service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='inspect inline content.open payload output refs',
            tool_round_budget=1,
        )
        tool_calls = [
            ToolCallRequest(
                id='call:content-open',
                name='content',
                arguments={'action': 'open', 'ref': wrapped.ref},
            )
        ]
        service.log_service.append_node_output(
            record.task_id,
            record.root_node_id,
            content='',
            tool_calls=[{'id': 'call:content-open', 'name': 'content', 'arguments': {'action': 'open', 'ref': wrapped.ref}}],
        )
        service.log_service.record_tool_result_batch(
            task_id=record.task_id,
            node_id=record.root_node_id,
            response_tool_calls=tool_calls,
            results=[
                _tool_result_payload(
                    call_id='call:content-open',
                    tool_name='content',
                    content=json.dumps(inline_open_payload, ensure_ascii=False),
                )
            ],
        )
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=[{'id': 'call:content-open', 'name': 'content', 'arguments': {'action': 'open', 'ref': wrapped.ref}}],
            created_at=now_iso(),
        )

        tool_results = service.store.list_task_node_tool_results(record.task_id, record.root_node_id)
        assert len(tool_results) == 1
        assert tool_results[0].output_ref == inner.ref

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        tool_step = detail['item']['execution_trace']['stages'][0]['rounds'][0]['tools'][0]
        assert tool_step['output_ref'] == wrapped.ref
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

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
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

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
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
                content='',
                tool_calls=[
                    _final_result_call(
                        status='failed',
                        delivery_status='blocked',
                        summary='stop',
                        answer='',
                        evidence=[],
                        remaining_work=[],
                        blocking_reason='stop',
                    )
                ],
                finish_reason='tool_calls',
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
        first_prefix = str((calls[0]['messages'][0] or {}).get('content') or '')
        second_prefix = str((calls[1]['messages'][0] or {}).get('content') or '')
        third_prefix = str((calls[2]['messages'][0] or {}).get('content') or '')
        assert first_prefix == second_prefix == third_prefix
        overlay_1 = str((calls[0]['messages'][-1] or {}).get('content') or '')
        overlay_2 = str((calls[1]['messages'][-1] or {}).get('content') or '')
        overlay_3 = str((calls[2]['messages'][-1] or {}).get('content') or '')
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
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=[{'id': 'call:stage-one', 'name': 'filesystem', 'arguments': {'path': 'stage-one'}}],
            created_at=now_iso(),
        )
        second = service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='第二阶段；优先派生：复杂验证；自行完成：整合结果',
            tool_round_budget=4,
            completed_stage_summary='finished stage one summary',
            key_refs=[{'ref': 'artifact:artifact:stage-one', 'note': 'stage one note'}],
        )

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        stages = detail['item']['execution_trace']['stages']
        assert [stage['status'] for stage in stages] == ['完成', '进行中']
        assert stages[0]['stage_id'] == first['stage_id']
        assert stages[1]['stage_id'] == second['stage_id']
        assert stages[0]['completed_stage_summary'] == 'finished stage one summary'
        assert stages[0]['key_refs'] == [{'ref': 'artifact:artifact:stage-one', 'note': 'stage one note'}]
        assert stages[1]['completed_stage_summary'] == ''
        assert stages[1]['key_refs'] == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_react_loop_counts_meaningful_tool_results_from_tool_result_store(tmp_path: Path):
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
        service.log_service.record_tool_result_batch(
            task_id=record.task_id,
            node_id=record.root_node_id,
            response_tool_calls=[
                ToolCallRequest(id='call:filesystem', name='filesystem', arguments={'path': str(tmp_path)}),
                ToolCallRequest(id='call:stage', name='submit_next_stage', arguments={'stage_goal': 'ignored'}),
            ],
            results=[
                _tool_result_payload(
                    call_id='call:filesystem',
                    tool_name='filesystem',
                    content='filesystem listed files',
                ),
                _tool_result_payload(
                    call_id='call:stage',
                    tool_name='submit_next_stage',
                    content='{"status":"ok"}',
                ),
            ],
        )

        assert service._react_loop._node_has_meaningful_tool_results(
            task_id=record.task_id,
            node_id=record.root_node_id,
        ) is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_next_stage_rejects_zero_progress_stage_switch(tmp_path: Path):
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

        with pytest.raises(ValueError, match='current active stage has no substantive progress yet'):
            service.log_service.submit_next_stage(
                record.task_id,
                record.root_node_id,
                stage_goal='第二阶段；优先派生：复杂验证；自行完成：整合结果',
                tool_round_budget=4,
            )

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        stages = detail['item']['execution_trace']['stages']
        assert len(stages) == 1
        assert stages[0]['stage_id'] == first['stage_id']
        assert stages[0]['status'] == '进行中'
        assert stages[0]['tool_rounds_used'] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_next_stage_allows_switch_after_spawn_only_progress(tmp_path: Path):
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
            stage_goal='第一阶段；优先派生：并行采集；自行完成：整合上下文',
            tool_round_budget=3,
        )
        service.log_service.record_execution_stage_round(
            record.task_id,
            record.root_node_id,
            tool_calls=[{'id': 'call:spawn-only', 'name': 'spawn_child_nodes', 'arguments': {'children': []}}],
            created_at=now_iso(),
        )
        second = service.log_service.submit_next_stage(
            record.task_id,
            record.root_node_id,
            stage_goal='第二阶段；优先派生：复杂验证；自行完成：整合结果',
            tool_round_budget=4,
            completed_stage_summary='spawn-only progress is still substantive',
        )

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        stages = detail['item']['execution_trace']['stages']
        assert [stage['stage_id'] for stage in stages] == [first['stage_id'], second['stage_id']]
        assert [stage['status'] for stage in stages] == ['完成', '进行中']
        assert stages[0]['tool_rounds_used'] == 0
        assert len(stages[0]['rounds']) == 1
        assert stages[0]['rounds'][0]['budget_counted'] is False
        assert [item['tool_name'] for item in stages[0]['rounds'][0]['tools']] == ['spawn_child_nodes']
    finally:
        await service.close()


def test_execution_stage_overlay_warns_before_zero_progress_stage_switch() -> None:
    overlay = build_execution_stage_overlay(
        node_kind='execution',
        stage_gate={
            'enabled': True,
            'has_active_stage': True,
            'transition_required': False,
            'active_stage': {
                'mode': '自主执行',
                'status': '进行中',
                'stage_goal': '整理上下文后继续推进',
                'tool_round_budget': 3,
                'tool_rounds_used': 0,
                'rounds': [],
            },
        },
    )

    assert overlay is not None
    assert '不要再次调用 `submit_next_stage`' in overlay


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
        frames = list(snapshot['frontier'] or [])
        assert frames
        root_frame = next(item for item in frames if item['node_id'] == record.root_node_id)
        assert root_frame['stage_status'] == '进行中'
        assert root_frame['stage_goal'] == '阶段摘要；优先派生：搜索外部依赖；自行完成：本地整理'
        assert root_frame['stage_total_steps'] == 5
    finally:
        await service.close()


def test_submit_next_stage_tool_schema_budget_max_is_ten() -> None:
    async def _submit(
        stage_goal: str,
        tool_round_budget: int,
        completed_stage_summary: str,
        key_refs: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            'stage_goal': stage_goal,
            'tool_round_budget': tool_round_budget,
            'completed_stage_summary': completed_stage_summary,
            'key_refs': key_refs,
        }

    tool = SubmitNextStageTool(_submit)

    assert tool.parameters['properties']['tool_round_budget']['minimum'] == 1
    assert tool.parameters['properties']['tool_round_budget']['maximum'] == 10
    assert 'completed_stage_summary' in tool.parameters['properties']
    assert 'key_refs' in tool.parameters['properties']
    assert tool.parameters['properties']['key_refs']['items']['required'] == ['ref', 'note']


def test_prompt_cache_key_changes_when_stage_context_blocks_change() -> None:
    base_messages = [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': '{"task_id":"task-1","goal":"demo"}'},
    ]
    compact_a = {
        'role': 'assistant',
        'content': '[G3KU_STAGE_COMPACT_V1]\n{"stage_index":1,"completed_stage_summary":"alpha"}',
    }
    compact_b = {
        'role': 'assistant',
        'content': '[G3KU_STAGE_COMPACT_V1]\n{"stage_index":1,"completed_stage_summary":"beta"}',
    }

    first = build_stable_prompt_cache_key([*base_messages, compact_a], None, 'model-a')
    second = build_stable_prompt_cache_key([*base_messages, compact_b], None, 'model-a')

    assert first != second


def test_submit_final_result_tool_schema_is_hard_switched_to_final_or_blocked() -> None:
    tool = _submit_final_result_tool()

    assert tool.parameters['properties']['delivery_status']['enum'] == ['final', 'blocked']
    assert tool.parameters['required'] == [
        'status',
        'delivery_status',
        'summary',
        'answer',
        'evidence',
        'remaining_work',
        'blocking_reason',
    ]


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
async def test_submit_next_stage_ignores_completed_recap_without_active_stage(tmp_path: Path):
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
            stage_goal='first stage only',
            tool_round_budget=2,
            completed_stage_summary='ignored summary',
            key_refs=[{'ref': 'artifact:artifact:ignored', 'note': 'ignored note'}],
        )

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        stages = detail['item']['execution_trace']['stages']
        assert len(stages) == 1
        assert stages[0]['stage_id'] == first['stage_id']
        assert stages[0]['completed_stage_summary'] == ''
        assert stages[0]['key_refs'] == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_completed_stage_archives_oldest_ten_and_inserts_compression_stage(tmp_path: Path):
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
            stage_goal='stage 1',
            tool_round_budget=1,
        )
        for index in range(2, 23):
            previous = index - 1
            service.log_service.record_execution_stage_round(
                record.task_id,
                record.root_node_id,
                tool_calls=[{'id': f'call:stage:{previous}', 'name': 'filesystem', 'arguments': {'path': f'stage-{previous}'}}],
                created_at=now_iso(),
            )
            service.log_service.submit_next_stage(
                record.task_id,
                record.root_node_id,
                stage_goal=f'stage {index}',
                tool_round_budget=1,
                completed_stage_summary=f'finished stage {previous}',
                key_refs=[{'ref': f'artifact:artifact:stage-{previous}', 'note': f'note {previous}'}],
            )

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        stages = detail['item']['execution_trace']['stages']
        compression_stages = [stage for stage in stages if stage['stage_kind'] == 'compression']
        assert len(compression_stages) == 1
        compression = compression_stages[0]
        assert compression['archive_stage_index_start'] == 1
        assert compression['archive_stage_index_end'] == 10
        assert str(compression['archive_ref']).startswith('artifact:')

        completed_normal = [
            stage['stage_index']
            for stage in stages
            if stage['stage_kind'] == 'normal' and stage['status'] != '进行中'
        ]
        assert completed_normal == list(range(11, 22))
        active_stage = next(stage for stage in stages if stage['status'] == '进行中')
        assert active_stage['stage_index'] == 22

        archive_artifact = service.get_artifact(str(compression['archive_ref']).split(':', 1)[1])
        assert archive_artifact is not None
        archive_payload = json.loads(Path(archive_artifact.path).read_text(encoding='utf-8'))
        assert archive_payload['stage_index_start'] == 1
        assert archive_payload['stage_index_end'] == 10
        assert len(archive_payload['stages']) == 10
        assert archive_payload['stages'][0]['key_refs'] == [{'ref': 'artifact:artifact:stage-1', 'note': 'note 1'}]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_execution_node_can_finish_via_submit_final_result_tool(tmp_path: Path):
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status='success',
                        delivery_status='final',
                        summary='done',
                        answer='done',
                        evidence=[{'kind': 'artifact', 'note': 'final result tool path'}],
                        remaining_work=[],
                        blocking_reason='',
                    )
                ],
                finish_reason='tool_calls',
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
        record = await service.create_task('submit-final-result success', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        assert task.status == 'success'
        assert root.status == 'success'
        assert root.final_output == 'done'
        assert str((root.metadata or {}).get('result_payload_ref') or '').strip()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_execution_node_can_finish_via_xml_direct_final_result(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            return LLMResponse(
                content='<minimax:tool_call><invoke name="submit_final_result"><parameter name="status">success</parameter><parameter name="delivery_status">final</parameter><parameter name="summary">done</parameter><parameter name="answer">done</parameter><parameter name="evidence">[]</parameter><parameter name="remaining_work">[]</parameter><parameter name="blocking_reason"></parameter></invoke></minimax:tool_call>',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('submit-final-result xml direct success', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        assert backend.turn == 1
        assert task.status == 'success'
        assert root.final_output == 'done'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_execution_node_rejects_failed_final_then_accepts_blocked(tmp_path: Path):
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
                        _final_result_call(
                            status='failed',
                            delivery_status='final',
                            summary='invalid execution failure',
                            answer='',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status='failed',
                        delivery_status='blocked',
                        summary='blocked',
                        answer='',
                        evidence=[],
                        remaining_work=[],
                        blocking_reason='blocked correctly',
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('invalid failed final', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert backend.turn == 2
        assert task.status == 'failed'
        assert 'blocked correctly' in str(task.failure_reason or '')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_old_text_json_no_longer_finishes_node(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='{"status":"success","delivery_status":"final","summary":"old path","answer":"old path","evidence":[{"kind":"artifact","note":"legacy"}],"remaining_work":[],"blocking_reason":""}',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status='success',
                        delivery_status='final',
                        summary='new path',
                        answer='new path',
                        evidence=[{'kind': 'artifact', 'note': 'new tool path'}],
                        remaining_work=[],
                        blocking_reason='',
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('legacy text result path', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        assert backend.turn == 2
        assert task.status == 'success'
        assert root.final_output == 'new path'
        assert len(list(root.output or [])) >= 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_missing_initial_stage_can_auto_recover_after_protocol_repair(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn < 3:
                return LLMResponse(
                    content='still prose, no tool call yet',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status='success',
                        delivery_status='final',
                        summary='done',
                        answer='done',
                        evidence=[],
                        remaining_work=[],
                        blocking_reason='',
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('auto stage recover', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        assert backend.turn == 3
        assert task.status == 'success'
        assert root.status == 'success'
        assert root.final_output == 'done'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_invalid_submit_final_result_fails_after_five_attempts(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        call_id=f'call:final:{self.turn}',
                        status='failed',
                        delivery_status='final',
                        summary='still invalid',
                        answer='',
                        evidence=[],
                        remaining_work=[],
                        blocking_reason='',
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('invalid final result limit', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert backend.turn == 5
        assert task.status == 'failed'
        assert 'Invalid final result submission detected 5 consecutive times' in str(task.failure_reason or '')
        assert 'execution failed result requires delivery_status=blocked' in str(task.failure_reason or '')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_next_stage_only_loop_fails_after_five_turns(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id=f'call:stage:{self.turn}',
                        name='submit_next_stage',
                        arguments={
                            'stage_goal': f'stage only loop {self.turn}',
                            'tool_round_budget': 1,
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('stage only loop', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert backend.turn == 6
        assert task.status == 'failed'
        assert 'Invalid stage progression detected 5 consecutive times' in str(task.failure_reason or '')
        assert 'no substantive progress yet' in str(task.failure_reason or '')
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'delivery_status', 'answer', 'blocking_reason'),
    [
        ('success', 'final', 'accepted', ''),
        ('failed', 'final', 'rejected', ''),
        ('failed', 'blocked', '', 'missing evidence'),
    ],
)
async def test_acceptance_node_supports_allowed_final_result_combinations(
    tmp_path: Path,
    status: str,
    delivery_status: str,
    answer: str,
    blocking_reason: str,
) -> None:
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status=status,
                        delivery_status=delivery_status,
                        summary='acceptance result',
                        answer=answer,
                        evidence=[{'kind': 'artifact', 'note': 'acceptance evidence'}] if status == 'success' else [],
                        remaining_work=[],
                        blocking_reason=blocking_reason,
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    service = MainRuntimeService(
        chat_backend=_Backend(),
        store_path=tmp_path / f'{status}-{delivery_status}.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / f'{status}-{delivery_status}-governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=root,
            goal='accept root output',
            acceptance_prompt='verify the root output',
            parent_node_id=root.node_id,
        )
        result = await service.node_runner.run_node(record.task_id, acceptance.node_id)
        latest = service.store.get_node(acceptance.node_id)
        assert latest is not None
        assert result.status == status
        assert result.delivery_status == delivery_status
        assert latest.status == status
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
                content='',
                tool_calls=[
                    _final_result_call(
                        status='failed',
                        delivery_status='blocked',
                        summary='intentional stop',
                        answer='',
                        evidence=[],
                        remaining_work=['stop after breaker check'],
                        blocking_reason='intentional stop',
                    )
                ],
                finish_reason='tool_calls',
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
