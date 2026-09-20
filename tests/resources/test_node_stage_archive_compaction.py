"""节点侧阶段收口（Phase C 账本标记 + Phase D 压缩信封）。

Phase C：`ExecutionStageRecord.context_visible` 的读写与导出形态——口径与前门一致，
缺失即可见、只有收口侧写键，账本条数、`stage_index` 序列与 Web 时间线都不变。
Phase D：节点压缩算出被吞集合、让模型挑证据引用编号并由运行时逐字回填、把完整账本
导到 `task_temp_dir`，并在压缩块里留下收口水位线给落盘点（Phase E）应用。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from g3ku.runtime.stage_prompt_compaction import (
    STAGE_ARCHIVE_HEADING,
    STAGE_REF_CANDIDATE_HEADING,
    STAGE_REF_INDEX_HEADING,
    completed_stage_blocks,
    render_stage_ref_candidate_block,
    retained_completed_stage_ids,
    stage_archive_selector_from_request_messages,
)
from main.models import (
    ExecutionStageState,
    NodeRecord,
    normalize_execution_stage_metadata,
)
from main.monitoring.execution_trace import build_execution_trace
from main.monitoring.log_service import TaskLogService
from main.runtime.react_loop import ReActToolLoop


def _stage(index: int, *, archived: bool = False, status: str = '完成', key_refs=None, tool_call_ids=None) -> dict:
    stage = {
        'stage_id': f'node-stage-{index}',
        'stage_index': index,
        'stage_kind': 'normal',
        'status': status,
        'stage_goal': f'目标 {index}',
        'completed_stage_summary': f'结论 {index}',
        'tool_round_budget': 3,
        'tool_rounds_used': 1,
        'key_refs': list(key_refs if key_refs is not None else [{'ref': f'artifact:out-{index}', 'note': f'产物 {index}'}]),
        'created_at': f'2026-09-20T0{index}:00:00+08:00',
        'finished_at': f'2026-09-20T0{index}:00:30+08:00',
        'rounds': ([{'round_id': f'r{index}', 'round_index': 1, 'tool_call_ids': list(tool_call_ids)}] if tool_call_ids else []),
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


# ---- Phase D：节点压缩的被吞集合、证据引用回填与归档 -------------------------


def _loop(*, stages: list[dict], temp_dir: str = '') -> ReActToolLoop:
    node = _node(stages)
    log_service = SimpleNamespace(
        _store=SimpleNamespace(get_node=lambda node_id: node if str(node_id) == 'node-1' else None),
        read_task_runtime_meta=lambda task_id: {'task_temp_dir': temp_dir} if temp_dir else {},
    )
    return ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)


def _plan(loop: ReActToolLoop, *, recent_tail: list[dict] | None = None) -> dict:
    state = loop._node_durable_stage_state('node-1')
    return loop._node_stage_archive_plan(stage_state=state, recent_tail=list(recent_tail or []))


def _swallowable_stages(*, key_refs=None) -> list[dict]:
    """6 条完成阶段，只有第 1 条带证据引用：raw 窗口保住 4~6，被吞的是 1~3。"""
    return [_stage(index, key_refs=key_refs if index == 1 else []) for index in range(1, 7)]


def test_unregistered_in_flight_stage_is_never_swallowed() -> None:
    """节点状态写中文 `进行中`，渲染层历史上只排除英文 `active`（前门才是英文词表）。

    没登记成 `active_stage_id` 的在跑阶段一旦被算进被吞集合，就是把它还在写的轮次连同
    key_refs 一起收进归档——摘要里没有它，正文里也没它。判定改成"终态白名单"。"""
    stages = [
        {**_stage(1, status='进行中'), 'key_refs': [{'ref': 'artifact:还在产出的中间物', 'note': '在跑'}]},
        _stage(2),
        _stage(3),
        _stage(4),
        _stage(5),
        _stage(6),
        _stage(7),
    ]
    loop = _loop(stages=stages)
    plan = _plan(loop)
    # 7 条里保留窗口吃掉 5/6/7，被吞的只能是已终态的 2/3/4；在跑的 1 号不进去。
    assert plan['stage_ids'] == ['node-stage-2', 'node-stage-3', 'node-stage-4']
    assert [item['ref'] for item in plan['candidates']] == ['artifact:out-2', 'artifact:out-3', 'artifact:out-4']
    assert 'artifact:还在产出的中间物' not in json.dumps(plan['candidates'], ensure_ascii=False)
    assert [item['stage_index'] for item in plan['records']] == [2, 3, 4]


def test_node_plan_spares_raw_window_and_tail_survivors() -> None:
    loop = _loop(stages=[
        _stage(1, key_refs=[{'ref': 'artifact:a1', 'note': '结论产物'}]),
        _stage(2, tool_call_ids=['call:two']),
        _stage(3),
        _stage(4),
        _stage(5, status='进行中'),
    ])
    plan = _plan(loop, recent_tail=[{'role': 'tool', 'tool_call_id': 'call:two', 'content': '还在体内的工具结果'}])
    # 1 = 被吞；2 = 肉身还在保留尾部；3/4 = 近场 raw 窗口；5 = 活动阶段。
    assert plan['stage_ids'] == ['node-stage-1']
    assert [item['ref'] for item in plan['candidates']] == ['artifact:a1']
    assert [item['stage_index'] for item in plan['records']] == [1]
    # 交给模型的只有编号清单本身，引用正文由运行时回填。
    instruction_tail = render_stage_ref_candidate_block(plan['candidates'])
    assert instruction_tail.splitlines() == [STAGE_REF_CANDIDATE_HEADING, '[#1] artifact:a1 — 结论产物']


def test_node_envelope_backfills_selected_refs_verbatim_and_archives(tmp_path: Path) -> None:
    live = tmp_path / 'keep.txt'
    live.write_text('x', encoding='utf-8')
    dead = tmp_path / 'gone.txt'
    loop = _loop(
        stages=_swallowable_stages(
            key_refs=[
                {'ref': str(live), 'note': '有效产物'},
                {'ref': str(dead), 'note': '已失效'},
                {'ref': 'task:7e2a270eec34', 'note': '在跑的任务'},
            ]
        ),
        temp_dir=str(tmp_path),
    )
    plan = _plan(loop)
    assert [item['candidate_id'] for item in plan['candidates']] == [1, 2, 3]
    text, payload, diagnostics = loop._node_stage_archive_envelope(
        task_id='task-1',
        node_id='node-1',
        compressed_text='## 一、结论\n任务结论。\n\n## 证据索引\n- [#1]\n- [#2]\n- [#3]\n- [#9]\n',
        plan=plan,
    )
    assert '## 一、结论' in text
    # 模型只回编号，正文由运行时逐字回填；死链 (#2) 与越界编号 (#9) 都不出现，句柄照常保留。
    assert f'- stage 1 | {live} — 有效产物' in text
    assert 'gone.txt' not in text
    assert '- stage 1 | task:7e2a270eec34 — 在跑的任务' in text
    assert STAGE_ARCHIVE_HEADING in text
    # 指针只声明"本次压缩不再逐条展开"：节点还没有把标记写回账本的落点，
    # 声称"已收口、不再逐轮进入上下文"会让模型误判这些细节已经不在体内。
    assert '已收口' not in text and '不再逐轮进入上下文' not in text
    assert str(tmp_path) in text
    assert diagnostics['stage_ref_candidate_count'] == 3
    assert diagnostics['stage_ref_selected_count'] == 2
    assert diagnostics['stage_ref_dropped_dead'] == 1
    assert diagnostics['stage_archive_pending_count'] == 3

    assert 'stage_ids' not in payload
    assert payload['archived_through_created_at'] == '2026-09-20T03:00:00+08:00'
    archived = json.loads(Path(payload['ref']).read_text(encoding='utf-8'))
    assert archived['kind'] == 'node_stage_archive'
    assert archived['owner'] == 'task:task-1/node:node-1'
    assert archived['stage_count'] == 3
    assert [stage['stage_index'] for stage in archived['stages']] == [1, 2, 3]
    # 归档留全量逐字 key_refs（含死链）：收口不等于丢数据。
    assert [item['ref'] for item in archived['stages'][0]['key_refs']] == [str(live), str(dead), 'task:7e2a270eec34']


def test_node_envelope_without_selection_still_archives(tmp_path: Path) -> None:
    loop = _loop(stages=_swallowable_stages(), temp_dir=str(tmp_path))
    text, payload, diagnostics = loop._node_stage_archive_envelope(
        task_id='task-1',
        node_id='node-1',
        compressed_text='## 一、结论\n只有正文。',
        plan=_plan(loop),
    )
    assert STAGE_REF_INDEX_HEADING not in text
    assert STAGE_ARCHIVE_HEADING in text
    assert diagnostics['stage_ref_selected_count'] == 0
    assert payload['archived_through_created_at'] == '2026-09-20T03:00:00+08:00'


def test_node_envelope_writes_no_archive_pointer_when_temp_dir_unresolved(tmp_path: Path) -> None:
    """落不了盘就整轮不收口：压缩块不带 `stage_archive`，落盘点因此一枚标记都不会打。"""
    loop = _loop(
        stages=_swallowable_stages(key_refs=[{'ref': 'artifact:a1', 'note': '结论产物'}]),
        temp_dir='',
    )
    text, payload, diagnostics = loop._node_stage_archive_envelope(
        task_id='task-1',
        node_id='node-1',
        compressed_text='## 一、结论\n正文。\n\n## 证据索引\n- [#1]\n',
        plan=_plan(loop),
    )
    assert payload == {}
    assert diagnostics['stage_archive_ref'] == ''
    assert STAGE_ARCHIVE_HEADING not in text
    # 证据引用不依赖归档能落盘，照常回填进摘要正文。
    assert '- stage 1 | artifact:a1 — 结论产物' in text


def test_node_chunked_lane_is_pointer_only(tmp_path: Path) -> None:
    loop = _loop(
        stages=_swallowable_stages(key_refs=[{'ref': 'artifact:a1', 'note': '结论产物'}]),
        temp_dir=str(tmp_path),
    )
    text, payload, diagnostics = loop._node_stage_archive_envelope(
        task_id='task-1',
        node_id='node-1',
        compressed_text='[块 1]\n## 证据索引\n- [#1]\n',
        plan=_plan(loop),
        allow_ref_selection=False,
    )
    # 分块车道不解析也不重写选择小节：块摘要里的编号原样留着，运行时不回填。
    assert '- [#1]' in text
    assert 'artifact:a1' not in text
    assert diagnostics['stage_ref_selected_count'] == 0
    assert payload['stage_count'] == 3


def test_node_rewrite_carries_archive_into_compact_block(tmp_path: Path) -> None:
    loop = _loop(stages=_swallowable_stages(), temp_dir=str(tmp_path))
    _text, payload, _diagnostics = loop._node_stage_archive_envelope(
        task_id='task-1',
        node_id='node-1',
        compressed_text='## 一、结论\n正文。',
        plan=_plan(loop),
    )
    rewritten, compact_payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
        node_id='node-1',
        request_messages=[
            {'role': 'system', 'content': '节点契约'},
            {'role': 'user', 'content': '任务目标'},
            {'role': 'assistant', 'content': '执行流水'},
        ],
        compressed_text=_text,
        stage_archive=payload,
    )
    assert compact_payload['kind'] == 'node_token_compaction_llm'
    assert compact_payload['stage_archive']['archived_through_created_at'] == '2026-09-20T03:00:00+08:00'
    assert 'stage_ids' not in compact_payload['stage_archive']
    block = next(
        str(item.get('content') or '')
        for item in rewritten
        if str(item.get('content') or '').startswith('[G3KU_TOKEN_COMPACT_V2]')
    )
    assert STAGE_ARCHIVE_HEADING in block
    assert json.loads(block.split('\n', 2)[1])['stage_archive']['stage_count'] == 3
    # 落盘点（Phase E）用共享读取器把水位线读回来：元数据行必须能单独解析，
    # 前缀与 JSON 之间、JSON 与正文之间都得是真换行。
    assert stage_archive_selector_from_request_messages(rewritten) == {
        'stage_ids': [],
        'archived_through_created_at': '2026-09-20T03:00:00+08:00',
    }
