"""验收抢跑闸门：对方没有一次已闭合的 final 提交时，验收节点不许开回合。

实盘 `task:1d9cddf9858e`（2026-10-03）：一次 `resume_node`（force=True）放了 73 个暂停
节点，其中 37 个是验收节点。16 个当场开跑并烧掉 150 次模型调用，结清的 7 份判词全都
对着空交接下「交付物不存在」的结论，其中 2 份是 `blocked` 终局失败。这些判词留在
spawn entry 绑定的 `acceptance_node_id` 上，被检验节点真正提交时会被当成本轮裁定复用。

判据落在事件本身而不是登记位：对方闭合 = 已终态，或 握手登记过一次指针可解析的
final 提交 **且** 它的帧里没有未闭合的非提交工具轮。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from main.models import NodeFinalResult, NodeRecord
from main.protocol import now_iso
from main.runtime.acceptance_handshake import ACCEPTANCE_HANDSHAKE_KEY
from main.runtime.node_runner import NodeRunner

TASK_ID = 'task:gate'
ROOT_ID = 'node:root'
EXEC_ID = 'node:exec'
ACC_ID = 'node:acc'


class _FakeStore:
    def __init__(self, nodes: dict[str, NodeRecord]) -> None:
        self.nodes = dict(nodes)

    def get_node(self, node_id: str):
        return self.nodes.get(str(node_id or '').strip())

    def get_task(self, task_id: str):
        return None

    def update_node(self, node_id: str, mutator):
        current = self.nodes.get(str(node_id or '').strip())
        if current is None:
            return None
        updated = mutator(current)
        self.nodes[str(node_id or '').strip()] = updated
        return updated


def _execution(status: str = 'in_progress', metadata: dict | None = None) -> NodeRecord:
    return NodeRecord(
        node_id=EXEC_ID,
        task_id=TASK_ID,
        root_node_id=ROOT_ID,
        parent_node_id=ROOT_ID,
        depth=1,
        node_kind='execution',
        status=status,
        goal='do the work',
        prompt='p',
        created_at=now_iso(),
        updated_at=now_iso(),
        metadata=dict(metadata or {}),
    )


def _acceptance(accepted_node_id: str = EXEC_ID, **overrides) -> NodeRecord:
    payload = dict(
        node_id=ACC_ID,
        task_id=TASK_ID,
        root_node_id=ROOT_ID,
        parent_node_id=accepted_node_id,
        depth=2,
        node_kind='acceptance',
        status='in_progress',
        goal='verify the work',
        prompt='子节点输出摘要：(empty)',
        created_at=now_iso(),
        updated_at=now_iso(),
        metadata={'accepted_node_id': accepted_node_id, 'spawn_owner_kind': 'acceptance'},
    )
    payload.update(overrides)
    return NodeRecord(**payload)


def _runner(
    execution: NodeRecord,
    *,
    pending_notice_ids: tuple[str, ...] = (),
    pending_tool_calls: tuple[dict, ...] = (),
) -> NodeRunner:
    runner = object.__new__(NodeRunner)
    runner._store = _FakeStore({execution.node_id: execution, ACC_ID: _acceptance(execution.node_id)})  # type: ignore[attr-defined]
    runner._log_service = SimpleNamespace(  # type: ignore[attr-defined]
        read_runtime_frame_payload=lambda task_id, node_id: {'pending_tool_calls': list(pending_tool_calls)}
    )
    runner.nodes_with_pending_distribution_notices = lambda task_id: list(pending_notice_ids)  # type: ignore[attr-defined]
    return runner


def _task(root_node_id: str = ROOT_ID) -> SimpleNamespace:
    return SimpleNamespace(task_id=TASK_ID, root_node_id=root_node_id, metadata={})


def _handshake(**overrides) -> dict:
    payload = {
        'state': 'waiting_acceptance',
        'acceptance_node_id': ACC_ID,
        'latest_execution_result_ref': 'artifact:result-1',
        'latest_execution_result_summary': '交付了 3 份文件',
    }
    payload.update(overrides)
    return {ACCEPTANCE_HANDSHAKE_KEY: payload}


def _freeze(runner: NodeRunner, *, root: bool = False) -> str:
    return runner._final_acceptance_freeze_reason(
        task=_task(root_node_id=ROOT_ID if root else ROOT_ID),
        node=_acceptance(ROOT_ID if root else EXEC_ID),
    )


def test_freezes_while_accepted_node_has_no_submission() -> None:
    """实盘那 7 份判词的形状：验收节点已存在、对方一次都没提交过。"""
    assert '尚未提交' in _freeze(_runner(_execution()))


def test_registration_of_submission_releases_the_gate() -> None:
    """三条派验车道都在派发前登记握手（带可解析指针），合法核验不受闸门影响。"""
    assert _freeze(_runner(_execution(metadata=_handshake()))) == ''


def test_registered_state_without_a_resolvable_pointer_still_freezes() -> None:
    """登记过 state 但指针为空 = 尾块无内容可给，放行只会产出一轮看不见交付的裁定。"""
    reason = _freeze(_runner(_execution(metadata=_handshake(latest_execution_result_ref=''))))
    assert '尚未提交' in reason


def test_open_non_submit_tool_round_freezes_even_with_a_registered_submission() -> None:
    """打回续跑时握手仍留着上一轮 ref：对方又开工了，就不许拿旧指针再裁一次。

    实盘样本：验收 node:9b3f22b1c0be 的对方 in_progress、帧里未闭合调用是
    `submit_next_stage`（收阶段不是收交付），握手 `waiting_execution_retry` + 旧 ref。
    """
    runner = _runner(
        _execution(metadata=_handshake(state='waiting_execution_retry')),
        pending_tool_calls=({'id': 'call_1', 'name': 'submit_next_stage', 'arguments': {}},),
    )
    assert '未闭合的工具轮' in _freeze(runner)


def test_final_submission_in_flight_is_not_an_open_round() -> None:
    """阻塞核验与根验收都在 `submit_final_result` 仍挂着时派验，那条就是闭合动作。"""
    runner = _runner(
        _execution(metadata=_handshake(state='waiting_block_verification')),
        pending_tool_calls=({'id': 'call_2', 'name': 'submit_final_result', 'arguments': {}},),
    )
    assert _freeze(runner) == ''


def test_terminal_accepted_node_is_never_frozen() -> None:
    assert _freeze(_runner(_execution(status='success'))) == ''


def test_root_lane_still_freezes_on_unconsumed_notices() -> None:
    """根最终验收的旧判据保留：已提交但仍压着通知时不能抢验。"""
    root = _execution(metadata=_handshake()).model_copy(update={'node_id': ROOT_ID, 'parent_node_id': ''})
    runner = _runner(root, pending_notice_ids=(ROOT_ID,))
    reason = runner._final_acceptance_freeze_reason(task=_task(), node=_acceptance(ROOT_ID))
    assert '未消费通知' in reason


def test_notice_rule_stays_scoped_to_the_root_final_acceptance() -> None:
    """未消费通知那条判据不外扩到 spawn 验收：打回反馈本身就是发给执行节点的通知。"""
    runner = _runner(_execution(metadata=_handshake()), pending_notice_ids=(EXEC_ID,))
    assert _freeze(runner) == ''


def test_reused_terminal_acceptance_is_voided_before_reverify() -> None:
    """spawn entry 绑定的验收节点已终态时，旧判词必须先作废再重验。"""
    stale = _acceptance(status='failed', final_output='目标文件不存在', failure_reason='目标文件不存在', finished_at=now_iso())
    runner = object.__new__(NodeRunner)
    runner._store = _FakeStore({EXEC_ID: _execution(status='success'), ACC_ID: stale})  # type: ignore[attr-defined]
    synced: list[str] = []
    runner._log_service = SimpleNamespace(  # type: ignore[attr-defined]
        update_node_metadata=lambda node_id, mutator: runner._store.update_node(
            node_id, lambda record: record.model_copy(update={'metadata': mutator(dict(record.metadata or {}))})
        ),
        sync_node_read_model=lambda task_id, node_id: synced.append(node_id),
        refresh_task_view=lambda task_id, mark_unread=False: None,
    )
    runner._update_spawn_entry = lambda **kwargs: None  # type: ignore[attr-defined]
    runner._stamp_spawn_owner_metadata = lambda **kwargs: None  # type: ignore[attr-defined]

    cached_payload = {'entries': [{'acceptance_node_id': ACC_ID, 'child_node_id': EXEC_ID}]}
    acceptance = runner._ensure_spawn_acceptance_node(
        task=_task(),
        parent=_execution().model_copy(update={'node_id': ROOT_ID}),
        child=stale.model_copy(update={'node_kind': 'execution', 'status': 'success'}),
        spec=SimpleNamespace(goal='do the work', acceptance_prompt='按验收标准核验'),
        cache_key='round-1',
        cached_payload=cached_payload,
        index=0,
    )

    assert acceptance.status == 'in_progress'
    assert acceptance.failure_reason == ''
    assert [entry['failure_reason'] for entry in acceptance.metadata['rejection_history']] == ['目标文件不存在']
    assert synced == [ACC_ID]


def test_child_pipeline_waits_instead_of_consuming_a_frozen_acceptance() -> None:
    """冻结的 partial 绝不能进裁定消费点：它会被读成一次通过。

    构造：第一次判定冻结（对方还在重跑上一轮打回），等待后对方闭合，第二次判定放行。
    断言验收节点只被派发一次、续接通知只在真打回后发、握手从未被写成 accepted。
    """
    runner = object.__new__(NodeRunner)
    child = _execution(status='in_progress')
    acceptance = _acceptance()
    runner._store = _FakeStore({
        EXEC_ID: child,
        ACC_ID: acceptance,
        ROOT_ID: child.model_copy(update={'node_id': ROOT_ID, 'parent_node_id': '', 'depth': 0}),
    })  # type: ignore[attr-defined]
    runner.reconcile_spawn_entry_child_bindings = lambda **kwargs: None  # type: ignore[attr-defined]
    runner._stamp_spawn_owner_metadata = lambda **kwargs: None  # type: ignore[attr-defined]
    runner._task_terminal_reason = lambda task_id, task=None: ''  # type: ignore[attr-defined]
    runner._node_terminal_reason = lambda node, default_failed='', default_success='': ''  # type: ignore[attr-defined]
    runner._update_spawn_entry = lambda **kwargs: None  # type: ignore[attr-defined]
    runner._ensure_spawn_acceptance_node = lambda **kwargs: acceptance  # type: ignore[attr-defined]
    runner._refresh_acceptance_node_metadata = lambda **kwargs: kwargs['node']  # type: ignore[attr-defined]
    runner._set_execution_waiting_acceptance_state = lambda **kwargs: None  # type: ignore[attr-defined]
    runner._child_handoff_payload = lambda **kwargs: {  # type: ignore[attr-defined]
        'summary': '交付正文', 'output_ref': 'artifact:out-1', 'result_payload_ref': 'artifact:result-1', 'evidence_summary': '',
    }
    runner._log_service = SimpleNamespace(  # type: ignore[attr-defined]
        update_node_check_result=lambda task_id, node_id, value: None
    )
    runner._supersede_consumed_notices_from_source = MagicMock()  # type: ignore[attr-defined]
    runner._persist_node_notification_direct = MagicMock()  # type: ignore[attr-defined]
    runner._spawn_failure_info_from_node = lambda **kwargs: None  # type: ignore[attr-defined]

    dispatched: list[str] = []
    reasons = iter(['验收冻结：被检验执行节点仍有未闭合的工具轮，需待其提交 final 结果后再继续核验。', ''])
    runner._final_acceptance_freeze_reason = lambda **kwargs: next(reasons)  # type: ignore[attr-defined]

    async def _run_nested(task_id: str, node_id: str) -> NodeFinalResult:
        dispatched.append(node_id)
        if node_id == EXEC_ID:
            return NodeFinalResult(status='success', delivery_status='final', summary='done', answer='正文', evidence=[], remaining_work=[], blocking_reason='')
        return NodeFinalResult(status='success', delivery_status='final', summary='验收通过', answer='', evidence=[], remaining_work=[], blocking_reason='')

    runner._run_nested_node = _run_nested  # type: ignore[attr-defined]

    def _handle(*, task, acceptance, result):  # noqa: ANN001 - 只在真派发后被调用
        assert result.delivery_status != 'partial', '冻结结果不得进入裁定消费点'
        return result

    runner._handle_acceptance_node_result = _handle  # type: ignore[attr-defined]

    cached_payload = {
        'entries': [{
            'child_node_id': EXEC_ID,
            'acceptance_node_id': ACC_ID,
            'requires_acceptance': True,
            'status': 'running',
        }]
    }
    spec = SimpleNamespace(goal='do the work', acceptance_prompt='按验收标准核验')
    parent = _execution().model_copy(update={'node_id': ROOT_ID, 'status': 'in_progress'})

    result = asyncio.run(runner._run_child_pipeline(
        task=_task(),
        parent=parent,
        spec=spec,
        cache_key='round-1',
        cached_payload=cached_payload,
        index=0,
    ))

    assert result.check_result == '验收通过'
    # 对方：进管线一次 + 冻结等待一次；验收：只在闭合后派发一次
    assert dispatched.count(ACC_ID) == 1
    assert dispatched.count(EXEC_ID) == 2
    assert dispatched.index(EXEC_ID) < dispatched.index(ACC_ID)
    # 等待轮没有拒收，所以不该发续接通知
    runner._persist_node_notification_direct.assert_not_called()
    runner._supersede_consumed_notices_from_source.assert_not_called()
