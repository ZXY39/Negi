"""验收抢跑闸门：被检验节点还没有可核验的提交时，验收节点不许开回合。

实盘 `task:1d9cddf9858e`（2026-10-03）：一次 `resume_node`（force=True）放了 73 个暂停
节点，其中 37 个是验收节点。16 个当场开跑并烧掉 150 次模型调用，结清的 7 份判词全都
对着空交接下「交付物不存在」的结论，其中 2 份是 `blocked` 终局失败。这些判词留在
spawn entry 绑定的 `acceptance_node_id` 上，被检验节点真正提交时会被当成本轮裁定复用。

验收节点的 bootstrap 在派发方 spawn 子节点那一刻就被创建（正文必然为空），所以"谁
把它派出去了"不可靠；可靠的是握手有没有登记过一次提交。
"""

from __future__ import annotations

from types import SimpleNamespace

from main.models import NodeRecord
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


def _runner(execution: NodeRecord, *, pending_notice_ids: tuple[str, ...] = ()) -> NodeRunner:
    runner = object.__new__(NodeRunner)
    runner._store = _FakeStore({execution.node_id: execution, ACC_ID: _acceptance(execution.node_id)})  # type: ignore[attr-defined]
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


def test_freezes_while_accepted_node_has_no_submission() -> None:
    """实盘那 7 份判词的形状：验收节点已存在、对方一次都没提交过。"""
    runner = _runner(_execution())
    reason = runner._final_acceptance_freeze_reason(task=_task(root_node_id=ROOT_ID), node=_acceptance())
    assert '尚未提交' in reason


def test_registration_of_a_submission_releases_the_gate() -> None:
    """三条派验车道都在派发前登记握手，合法核验不受闸门影响。"""
    runner = _runner(_execution(metadata=_handshake()))
    assert runner._final_acceptance_freeze_reason(task=_task(), node=_acceptance()) == ''


def test_terminal_accepted_node_is_never_frozen() -> None:
    runner = _runner(_execution(status='success', metadata={}))
    assert runner._final_acceptance_freeze_reason(task=_task(), node=_acceptance()) == ''


def test_root_lane_still_freezes_on_unconsumed_notices() -> None:
    """根最终验收的旧判据保留：已提交但仍压着通知时不能抢验。"""
    root = _execution(metadata=_handshake())
    root = root.model_copy(update={'node_id': ROOT_ID, 'parent_node_id': ''})
    runner = _runner(root, pending_notice_ids=(ROOT_ID,))
    reason = runner._final_acceptance_freeze_reason(
        task=_task(),
        node=_acceptance(ROOT_ID),
    )
    assert '未消费通知' in reason


def test_notice_rule_stays_scoped_to_the_root_final_acceptance() -> None:
    """未消费通知那条判据不外扩到 spawn 验收：打回反馈本身就是发给执行节点的通知。"""
    runner = _runner(_execution(metadata=_handshake()), pending_notice_ids=(EXEC_ID,))
    assert runner._final_acceptance_freeze_reason(task=_task(), node=_acceptance()) == ''


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
