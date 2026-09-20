"""节点账本承载收口标记（Phase C）：`ExecutionStageRecord.context_visible` 的读写与导出形态。

口径与前门一致：缺失即可见，只有收口侧写键；账本条数、`stage_index` 序列与 Web 时间线都不变。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from g3ku.runtime.stage_prompt_compaction import (
    completed_stage_blocks,
    retained_completed_stage_ids,
)
from main.models import (
    ExecutionStageState,
    NodeRecord,
    normalize_execution_stage_metadata,
)
from main.monitoring.execution_trace import build_execution_trace
from main.monitoring.log_service import TaskLogService


def _stage(index: int, *, archived: bool = False, status: str = '完成') -> dict:
    stage = {
        'stage_id': f'node-stage-{index}',
        'stage_index': index,
        'stage_kind': 'normal',
        'status': status,
        'stage_goal': f'目标 {index}',
        'completed_stage_summary': f'结论 {index}',
        'tool_round_budget': 3,
        'tool_rounds_used': 1,
        'key_refs': [{'ref': f'artifact:out-{index}', 'note': f'产物 {index}'}],
        'created_at': f'2026-09-20T0{index}:00:00+08:00',
        'finished_at': f'2026-09-20T0{index}:00:30+08:00',
        'rounds': [{'round_id': f'r{index}', 'round_index': 1, 'tool_call_ids': [f'call:{index}']}],
    }
    if archived:
        stage['context_visible'] = False
    return stage


def _state(*, active_stage_id: str = '', stages: list[dict]) -> dict:
    return {'active_stage_id': active_stage_id, 'transition_required': False, 'stages': stages}


def _node(stages: list[dict]) -> NodeRecord:
    return NodeRecord(
        node_id='node-1',
        task_id='task-1',
        root_node_id='node-1',
        goal='完成任务',
        prompt='提示词',
        created_at='2026-09-20T01:00:00+08:00',
        updated_at='2026-09-20T01:00:00+08:00',
        metadata={'execution_stages': _state(stages=stages)},
    )


def test_legacy_node_stage_stays_visible_and_writes_no_flag() -> None:
    """存量账本没有这个键：读出来是可见，再落盘的字节形态与改动前一致。"""
    state = normalize_execution_stage_metadata(_state(stages=[_stage(1), _stage(2)]))
    assert [stage.context_visible for stage in state.stages] == [True, True]
    payload = state.model_dump(mode='json')
    assert all('context_visible' not in stage for stage in payload['stages'])
    assert 'context_visible' not in state.model_dump_json()


def test_archived_flag_round_trips_through_normalize_and_persist() -> None:
    state = normalize_execution_stage_metadata(_state(stages=[_stage(1), _stage(2, archived=True)]))
    assert [stage.context_visible for stage in state.stages] == [True, False]
    payload = state.model_dump(mode='json')
    assert 'context_visible' not in payload['stages'][0]
    assert payload['stages'][1]['context_visible'] is False
    # 收口不删记录、不改序号：条目数与 stage_index 序列必须原样。
    again = normalize_execution_stage_metadata(payload)
    assert [stage.stage_index for stage in again.stages] == [1, 2]
    assert [stage.context_visible for stage in again.stages] == [True, False]


def test_active_stage_dump_keeps_the_same_shape() -> None:
    """活动阶段永不收口，落盘形态也不带键（收口只发生在完成的阶段上）。"""
    state = ExecutionStageState.model_validate(_state(active_stage_id='node-stage-1', stages=[_stage(1, status='进行中')]))
    assert 'context_visible' not in state.model_dump(mode='json')['stages'][0]


def test_archived_node_stage_stops_rendering_and_frees_raw_window_slot() -> None:
    """渲染端已共用前门那套：标记一落到节点账本，块就不再逐轮重渲染。"""
    state = normalize_execution_stage_metadata(
        _state(stages=[_stage(1), _stage(2), _stage(3), _stage(4, archived=True)])
    )
    retained = retained_completed_stage_ids(state, keep_latest=2)
    # 收口阶段不占近场 raw 窗口名额，否则已进过摘要的阶段会把还在跑的细节挤出去。
    assert retained == {'node-stage-2', 'node-stage-3'}
    blocks = completed_stage_blocks(state, skip_stage_ids=retained)
    indexes = [json.loads(block['content'].split('\n', 1)[1])['stage_index'] for block in blocks]
    assert indexes == [1]


def test_execution_trace_export_marks_only_archived_stages() -> None:
    trace = build_execution_trace(_node([_stage(1), _stage(2, archived=True)]))
    stages = trace['stages'] if isinstance(trace['stages'], list) else []
    assert [stage['stage_index'] for stage in stages] == [1, 2]
    assert 'context_visible' not in stages[0]
    assert stages[1]['context_visible'] is False


def test_gate_snapshot_marks_only_archived_stages() -> None:
    node = _node([_stage(1), _stage(2, archived=True)])
    fake_self = SimpleNamespace(
        _task_lock=lambda task_id: threading.RLock(),
        _store=SimpleNamespace(get_node=lambda node_id: node),
        _execution_stage_state=lambda target: normalize_execution_stage_metadata(
            (target.metadata or {}).get('execution_stages') or {}
        ),
        _active_execution_stage=lambda state: None,
    )
    snapshot = TaskLogService.execution_stage_gate_snapshot(fake_self, 'task-1', 'node-1')
    completed = snapshot['completed_stages']
    assert [stage['stage_index'] for stage in completed] == [1, 2]
    assert 'context_visible' not in completed[0]
    assert completed[1]['context_visible'] is False
