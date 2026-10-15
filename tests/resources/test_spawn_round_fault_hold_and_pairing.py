"""派生轮故障不得提前返回 + 执行节点与验收节点 1:1 配对（task:869c064afdb2，2026-10-15）。

实盘两条账：

1. 根节点 node:9abf8e3455c2 的第 2 轮派生在 05:54:30 被记成
   `Error executing spawn_child_nodes: OperationalError: disk I/O error`，而它绑定的 6 个
   子节点与 64 个后代当时全部非终态，之后 37 分钟仍跑了 262 次模型调用。父节点读到"这批
   失败了"就用新 call id 重发，重发开局把整棵在飞子树结清成 superseded：59 个节点被强判
   failed，70 个节点里 63 个作废，整批从头重做。
2. 同一刀把 node:5c5d792b2bd7 的验收节点打成 failed，而该执行节点当时已是 success（停在
   waiting_acceptance，清扫跳过 success），于是下一轮收养了被验品、却给同一个执行节点
   另配了一颗验收节点——界面上出现两个检验节点。

本文件锁定修复契约：
- 派生轮抛异常且本轮仍绑着非终态节点 ⇒ 不向模型交付"派生失败"，等节点收敛后交付真实结论；
- 派生轮抛异常且本轮没有活节点 ⇒ 原异常照旧上抛（不改写既有故障车道）；
- 冻结/取消类控制信号 ⇒ 绕过滞留，绝不被改写成一次交付；
- 新一轮复用同一个执行子节点 ⇒ 沿用它原有的验收节点，不再另配一颗。
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

import main.runtime.node_runner as node_runner_module
from main.models import NodeRecord, SpawnChildSpec
from main.protocol import now_iso
from main.runtime.node_runner import NodeRunner

TASK_ID = 'task:fault-hold'
ROOT_ID = 'node:root'
EXEC_ID = 'node:exec'
ACC_ID = 'node:acc'
ROUND_ID = 'call_00_round'


class _FakeStore:
    def __init__(self, nodes: dict[str, NodeRecord], *, task=None) -> None:
        self.nodes = dict(nodes)
        self.task = task

    def get_node(self, node_id: str):
        return self.nodes.get(str(node_id or '').strip())

    def get_task(self, task_id: str):
        return self.task

    def list_children(self, parent_node_id: str):
        parent = str(parent_node_id or '').strip()
        return [node for node in self.nodes.values() if str(node.parent_node_id or '').strip() == parent]

    def update_node(self, node_id: str, mutator):
        current = self.nodes.get(str(node_id or '').strip())
        if current is None:
            return None
        updated = mutator(current)
        self.nodes[str(node_id or '').strip()] = updated
        return updated


def _node(node_id: str, *, kind: str, status: str, parent: str, **overrides) -> NodeRecord:
    payload = dict(
        node_id=node_id,
        task_id=TASK_ID,
        root_node_id=ROOT_ID,
        parent_node_id=parent,
        depth=1 if parent == ROOT_ID else 2,
        node_kind=kind,
        status=status,
        goal='do the work',
        prompt='p',
        created_at=now_iso(),
        updated_at=now_iso(),
        metadata={},
    )
    payload.update(overrides)
    return NodeRecord(**payload)


def _round_store(child_status: str) -> _FakeStore:
    """记账里本轮仍绑着一个非终态执行子节点——正是故障发生时磁盘上留着的形状。"""
    parent = _node(ROOT_ID, kind='execution', status='in_progress', parent='', depth=0)
    parent.metadata = {
        'spawn_operations': {
            ROUND_ID: {
                'completed': False,
                'entries': [{
                    'index': 0,
                    'goal': 'do the work',
                    'status': 'running',
                    'child_node_id': EXEC_ID,
                    'acceptance_node_id': '',
                    'requires_acceptance': False,
                }],
            }
        }
    }
    child = _node(EXEC_ID, kind='execution', status=child_status, parent=ROOT_ID)
    return _FakeStore({ROOT_ID: parent, EXEC_ID: child}, task=SimpleNamespace(task_id=TASK_ID, status='in_progress'))


def _runner(store: _FakeStore) -> NodeRunner:
    runner = object.__new__(NodeRunner)
    runner._store = store  # type: ignore[attr-defined]
    runner._spawn_operation_locks = {}  # type: ignore[attr-defined]
    runner._log_service = SimpleNamespace(  # type: ignore[attr-defined]
        update_node_metadata=lambda node_id, mutator: runner._store.update_node(
            node_id, lambda record: record.model_copy(update={'metadata': mutator(dict(record.metadata or {}))})
        ),
        publish_node_state=lambda task_id, node_id: None,
        refresh_task_view=lambda task_id, mark_unread=False: None,
        ensure_node_output_externalized=lambda task_id, node_id: None,
        ensure_node_result_payload_externalized=lambda task_id, node_id: None,
    )
    return runner


def _specs() -> list[SpawnChildSpec]:
    return [SpawnChildSpec(goal='do the work', prompt='p', execution_policy={'mode': 'focus'})]


@pytest.fixture(autouse=True)
def _fast_hold_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_runner_module, '_SPAWN_FAULT_HOLD_SLICE_SECONDS', 0.01, raising=False)
    monkeypatch.setattr(node_runner_module, '_SPAWN_FAULT_HOLD_REPORT_SECONDS', 0.05, raising=False)


def test_spawn_round_fault_holds_until_bound_children_converge(monkeypatch: pytest.MonkeyPatch) -> None:
    """本轮仍绑着 in_progress 子节点时，派生故障不得转成一次"派生失败"交付。"""
    store = _round_store('in_progress')
    runner = _runner(store)

    async def _boom(**_kwargs):
        raise sqlite3.OperationalError('disk I/O error')

    async def _converge_soon():
        await asyncio.sleep(0.03)
        store.nodes[EXEC_ID] = store.nodes[EXEC_ID].model_copy(
            update={'status': 'success', 'final_output': 'shard written', 'finished_at': now_iso()}
        )

    rebuilt: list[list[dict]] = []

    def _results(*, task_id: str, entries, specs):
        rebuilt.append([dict(item) for item in entries])
        return [SimpleNamespace(goal=str(item.get('goal') or ''), check_result='验收通过') for item in entries]

    runner._spawn_children_locked = _boom  # type: ignore[attr-defined]
    runner._spawn_round_results_from_entries = _results  # type: ignore[attr-defined]

    async def _run():
        watcher = asyncio.create_task(_converge_soon())
        try:
            return await runner._spawn_children(
                task_id=TASK_ID, parent_node_id=ROOT_ID, specs=_specs(), call_id=ROUND_ID
            )
        finally:
            await watcher

    results = asyncio.run(_run())

    assert rebuilt, 'converged entries must be delivered as the round result'
    assert rebuilt[0][0]['child_node_id'] == EXEC_ID
    assert len(results) == 1
    assert results[0].check_result == '验收通过'


def test_spawn_round_fault_without_live_nodes_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """本轮什么都没绑定（评审/物化前就坏了）时保持原车道：异常照旧上抛。"""
    store = _round_store('in_progress')
    store.nodes[ROOT_ID].metadata = {'spawn_operations': {ROUND_ID: {'completed': False, 'entries': [{}]}}}
    runner = _runner(store)

    async def _boom(**_kwargs):
        raise sqlite3.OperationalError('disk I/O error')

    runner._spawn_children_locked = _boom  # type: ignore[attr-defined]

    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(
            runner._spawn_children(task_id=TASK_ID, parent_node_id=ROOT_ID, specs=_specs(), call_id=ROUND_ID)
        )


def test_spawn_round_control_signals_bypass_the_hold() -> None:
    """冻结信号绝不被滞留改写成一次交付（孤儿子节点事故的同一条合同）。"""
    from main.errors import NodePausedError

    store = _round_store('in_progress')
    runner = _runner(store)

    async def _freeze(**_kwargs):
        raise NodePausedError(TASK_ID, ROOT_ID)

    runner._spawn_children_locked = _freeze  # type: ignore[attr-defined]

    with pytest.raises(NodePausedError):
        asyncio.run(
            runner._spawn_children(task_id=TASK_ID, parent_node_id=ROOT_ID, specs=_specs(), call_id=ROUND_ID)
        )


def test_reused_execution_child_keeps_its_original_acceptance_node() -> None:
    """清扫把验收节点打成 failed 后，复用同一执行子节点的那一轮必须沿用它，不再另配一颗。"""
    parent = _node(ROOT_ID, kind='execution', status='in_progress', parent='', depth=0)
    child = _node(EXEC_ID, kind='execution', status='success', parent=ROOT_ID)
    swept = _node(
        ACC_ID,
        kind='acceptance',
        status='failed',
        parent=EXEC_ID,
        failure_reason='superseded by newer spawn round: call_00_next',
        final_output='superseded by newer spawn round: call_00_next',
        metadata={'accepted_node_id': EXEC_ID, 'spawn_owner_kind': 'acceptance'},
    )
    store = _FakeStore({ROOT_ID: parent, EXEC_ID: child, ACC_ID: swept})
    runner = _runner(store)
    bound: list[dict] = []
    runner._update_spawn_entry = lambda **kwargs: bound.append(dict(kwargs))  # type: ignore[attr-defined]
    runner._stamp_spawn_owner_metadata = lambda **kwargs: None  # type: ignore[attr-defined]

    def _never_create(**_kwargs):
        raise AssertionError('a second acceptance node must not be minted for the same execution child')

    runner.create_acceptance_node = _never_create  # type: ignore[attr-defined]

    cached_payload = {'entries': [{'index': 0, 'child_node_id': EXEC_ID, 'acceptance_node_id': ''}]}
    acceptance = runner._ensure_spawn_acceptance_node(
        task=SimpleNamespace(task_id=TASK_ID),
        parent=parent,
        child=child,
        spec=SimpleNamespace(goal='do the work', acceptance_prompt='按验收标准核验'),
        cache_key=ROUND_ID,
        cached_payload=cached_payload,
        index=0,
    )

    assert acceptance.node_id == ACC_ID
    assert acceptance.status == 'in_progress'
    assert acceptance.failure_reason == ''
    assert bound and bound[0]['acceptance_node_id'] == ACC_ID


def test_spawn_round_cancelled_with_live_children_holds_instead_of_faking_failure() -> None:
    """来源不明的取消不得留下"子节点失败"：整轮交给滞留道等真实终态。

    实盘 16:47:58：看门狗轮询读库抛 OperationalError → request_tool_cancellation →
    两个父节点的 12 条 entry 全被结算成 `Error: CancelledError`，其中 3 条的孩子
    早已 success。5 分钟后收尸器照着这本假账杀了 6 个节点。
    """
    store = _round_store('in_progress')
    runner = _runner(store)
    runner._should_propagate_child_pipeline_cancellation = lambda **_kwargs: False  # type: ignore[attr-defined]

    async def _cancel(**_kwargs):
        raise asyncio.CancelledError()

    async def _converge_soon():
        await asyncio.sleep(0.03)
        store.nodes[EXEC_ID] = store.nodes[EXEC_ID].model_copy(
            update={'status': 'success', 'final_output': 'done', 'finished_at': now_iso()}
        )

    settled: list[dict] = []
    runner._spawn_children_locked = _cancel  # type: ignore[attr-defined]
    runner._update_spawn_entry = lambda **kwargs: settled.append(dict(kwargs))  # type: ignore[attr-defined]
    runner._spawn_round_results_from_entries = (  # type: ignore[attr-defined]
        lambda **_kwargs: [SimpleNamespace(goal='do the work', check_result='真实结论')]
    )

    async def _run():
        watcher = asyncio.create_task(_converge_soon())
        try:
            return await runner._spawn_children(
                task_id=TASK_ID, parent_node_id=ROOT_ID, specs=_specs(), call_id=ROUND_ID
            )
        finally:
            await watcher

    results = asyncio.run(_run())

    assert [s.get('status') for s in settled] != ['error'], 'a live child must never be settled as error'
    assert results[0].check_result == '真实结论'


def test_refuse_settlement_allows_it_once_the_bound_node_is_terminal() -> None:
    """孩子确实已终态时照常结算——拒绝的判据是"还活着"，不是"出过异常"。"""
    store = _round_store('failed')
    runner = _runner(store)
    cached_payload = store.nodes[ROOT_ID].metadata['spawn_operations'][ROUND_ID]

    refused = runner._refuse_spawn_settlement_over_live_node(
        task=SimpleNamespace(task_id=TASK_ID),
        parent=store.nodes[ROOT_ID],
        cache_key=ROUND_ID,
        cached_payload=cached_payload,
        index=0,
        exc=asyncio.CancelledError(),
    )
    assert refused is False


def test_watchdog_poll_read_failure_does_not_become_a_cancellation() -> None:
    """看门狗每次轮询的暂停检查读不到库时，只报告这一拍没查成，不外抛。"""
    import main.runtime.react_loop as react_loop_module

    loop = object.__new__(react_loop_module.ReActToolLoop)
    store = SimpleNamespace(
        get_task=lambda _task_id: (_ for _ in ()).throw(sqlite3.OperationalError('disk I/O error')),
    )
    loop._log_service = SimpleNamespace(_store=store)  # type: ignore[attr-defined]

    with pytest.raises(sqlite3.OperationalError):
        loop._check_pause_or_cancel(TASK_ID, ROOT_ID)

    loop._check_pause_or_cancel(TASK_ID, ROOT_ID, observation_only=True)


def test_watchdog_poll_still_raises_a_real_pause_in_observation_mode() -> None:
    """降级只针对"读不到"；真有人请求暂停时控制信号必须照旧冒泡。"""
    import main.runtime.react_loop as react_loop_module
    from main.errors import TaskPausedError

    loop = object.__new__(react_loop_module.ReActToolLoop)
    store = SimpleNamespace(
        get_task=lambda _task_id: SimpleNamespace(
            status='in_progress', pause_requested=True, cancel_requested=False, failure_reason=''
        ),
    )
    loop._log_service = SimpleNamespace(_store=store, set_pause_state=lambda *a, **k: None)  # type: ignore[attr-defined]
    loop._abort_queued_tool_waits = lambda *a, **k: None  # type: ignore[attr-defined]

    with pytest.raises(TaskPausedError):
        loop._check_pause_or_cancel(TASK_ID, ROOT_ID, observation_only=True)


def test_ad_hoc_blocked_verifier_is_not_adopted_as_the_spawn_acceptance() -> None:
    """阻塞核验车道给同一个执行节点配的临时验收人（blocked_verification_only）不是配对对象。"""
    parent = _node(ROOT_ID, kind='execution', status='in_progress', parent='', depth=0)
    child = _node(EXEC_ID, kind='execution', status='success', parent=ROOT_ID)
    ad_hoc = _node(
        ACC_ID,
        kind='acceptance',
        status='in_progress',
        parent=EXEC_ID,
        goal='blocked-check:do the work',
        metadata={'accepted_node_id': EXEC_ID, 'blocked_verification_only': True},
    )
    store = _FakeStore({ROOT_ID: parent, EXEC_ID: child, ACC_ID: ad_hoc})
    runner = _runner(store)
    runner._update_spawn_entry = lambda **kwargs: None  # type: ignore[attr-defined]
    runner._stamp_spawn_owner_metadata = lambda **kwargs: None  # type: ignore[attr-defined]
    created: list[str] = []

    def _create(**_kwargs):
        created.append('created')
        return _node('node:acc-new', kind='acceptance', status='in_progress', parent=EXEC_ID)

    runner.create_acceptance_node = _create  # type: ignore[attr-defined]

    acceptance = runner._ensure_spawn_acceptance_node(
        task=SimpleNamespace(task_id=TASK_ID),
        parent=parent,
        child=child,
        spec=SimpleNamespace(goal='do the work', acceptance_prompt='按验收标准核验'),
        cache_key=ROUND_ID,
        cached_payload={'entries': [{'index': 0, 'child_node_id': EXEC_ID, 'acceptance_node_id': ''}]},
        index=0,
    )

    assert created == ['created']
    assert acceptance.node_id == 'node:acc-new'
