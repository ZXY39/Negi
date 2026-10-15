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
- 新一轮复用同一个执行子节点 ⇒ 沿用它原有的验收节点，不再另配一颗；
- 清扫子树 ⇒ 落定归取消方（派发器按调用方给的 reason 落行），NodeRunner 不得再判第二次；
  本进程没有派发 entry 的那些只上报、不强判。
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
    return _FakeStore(
        {ROOT_ID: parent, EXEC_ID: child},
        task=SimpleNamespace(
            task_id=TASK_ID, status='in_progress', root_node_id=ROOT_ID, session_id='web:fault-hold',
        ),
    )


class _FallbackLogService:
    """未显式打桩的 log_service 方法一律空操作。

    清扫这条道会碰很多帧/账本读口，而有些用例只关心「节点行有没有被落定」，
    不需要为每个旁路读口各写一条 lambda。已在内层桩上的方法照旧走内层。
    """

    def __init__(self, inner) -> None:
        object.__setattr__(self, '_inner', inner)

    def __getattr__(self, name):
        inner = object.__getattribute__(self, '_inner')
        if hasattr(inner, name):
            return getattr(inner, name)
        return lambda *args, **kwargs: None


def _runner(store: _FakeStore) -> NodeRunner:
    runner = object.__new__(NodeRunner)
    runner._store = store  # type: ignore[attr-defined]
    runner._spawn_operation_locks = {}  # type: ignore[attr-defined]
    # 活性探针默认不接线＝本进程说不出谁在驱动，节点行能读到就按行算。
    runner.node_liveness_probe = None  # type: ignore[attr-defined]
    runner._log_service = SimpleNamespace(  # type: ignore[attr-defined]
        update_node_metadata=lambda node_id, mutator: runner._store.update_node(
            node_id, lambda record: record.model_copy(update={'metadata': mutator(dict(record.metadata or {}))})
        ),
        publish_node_state=lambda task_id, node_id: None,
        refresh_task_view=lambda task_id, mark_unread=False: None,
        ensure_node_output_externalized=lambda task_id, node_id: None,
        ensure_node_result_payload_externalized=lambda task_id, node_id: None,
        update_node_check_result=lambda task_id, node_id, check_result: None,
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


def test_settle_spec_fault_settles_normally_once_the_bound_node_is_terminal() -> None:
    """孩子确实已终态时照常结算——拒绝的判据是"还活着"，不是"出过异常"。

    两个 handler 现在共用这一条结算道，所以"记得先问守卫"不再是调用方的责任。
    """
    store = _round_store('failed')
    runner = _runner(store)
    # 本用例只问"结算还是重抛"这条路由，不测帧读取；重建真实结论会碰若干与本用例无关的
    # 读口，在这里包一层空操作替身，而不是把 _runner 的共享桩改成万能桩（那会让本文件
    # 所有用例失去"生产代码调到新读口时立刻报错"的判别力）。
    runner._log_service = _FallbackLogService(runner._log_service)  # type: ignore[attr-defined]
    cached_payload = store.nodes[ROOT_ID].metadata['spawn_operations'][ROUND_ID]

    settled = runner._settle_spec_fault(
        task=SimpleNamespace(task_id=TASK_ID),
        parent=store.nodes[ROOT_ID],
        cache_key=ROUND_ID,
        cached_payload=cached_payload,
        index=0,
        spec=_specs()[0],
        exc=asyncio.CancelledError(),
    )
    assert settled is not None, '绑定节点已终态时应当结算，而不是把轮次吊住'


def test_settle_spec_fault_refuses_while_the_bound_node_is_live() -> None:
    """绑定节点还活着 ⇒ 返回 None 让调用方重抛，交给滞留道等真实结论。"""
    store = _round_store('in_progress')
    runner, diagnostics = _reporting_runner(store)

    settled = runner._settle_spec_fault(
        task=SimpleNamespace(task_id=TASK_ID),
        parent=store.nodes[ROOT_ID],
        cache_key=ROUND_ID,
        cached_payload=store.nodes[ROOT_ID].metadata['spawn_operations'][ROUND_ID],
        index=0,
        spec=_specs()[0],
        exc=asyncio.CancelledError(),
    )
    assert settled is None
    titles = [str(item.get('title') or '') for item in diagnostics]
    assert 'spawn_settlement_refused_live_node' in titles, titles


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


# --- supersede：落定归取消方，NodeRunner 不再判第二次 --------------------------


def _reporting_runner(store: _FakeStore) -> tuple[NodeRunner, list[dict]]:
    runner = _runner(store)
    diagnostics: list[dict] = []
    runner._emit_spawn_diagnostic = lambda **kwargs: diagnostics.append(dict(kwargs))  # type: ignore[assignment]
    return runner, diagnostics


def test_cancel_spawn_subtrees_hands_the_reason_to_the_owner_and_returns_ownership() -> None:
    """取消方（派发器）才是活性的权威；reason 必须传过去，节点行才读得出为什么被停。"""
    store = _round_store('in_progress')
    runner, diagnostics = _reporting_runner(store)
    seen: list[tuple] = []

    async def _executor(task_id, node_ids, reason=''):
        seen.append((task_id, tuple(node_ids), str(reason or '')))
        return {EXEC_ID}

    runner.cancel_node_subtree_executor = _executor

    owned = asyncio.run(
        runner._cancel_spawn_subtrees(
            task_id=TASK_ID,
            node_ids={EXEC_ID, ACC_ID},
            reason_text='superseded by newer spawn round: r2',
        )
    )

    assert owned == {EXEC_ID}
    assert set(seen[0][1]) == {EXEC_ID, ACC_ID}, '整批目标都要交给取消方，不能在这里丢节点'
    assert seen[0][2] == 'superseded by newer spawn round: r2'

    runner._report_nodes_without_owner(
        task_id=TASK_ID,
        parent_node_id=ROOT_ID,
        cache_key='',
        reason_text='superseded by newer spawn round: r2',
        node_ids={EXEC_ID, ACC_ID} - owned,
    )

    reported = [item for item in diagnostics if str(item.get('title') or '') == 'spawn_supersede_left_to_owner']
    assert len(reported) == 1, reported
    assert f'left_node_id={ACC_ID}' in str(reported[0].get('detail') or '')
    # 有主的那颗由取消方落定，NodeRunner 一个字都不写。
    assert store.get_node(EXEC_ID).status == 'in_progress'


def test_unwired_executor_reports_every_node_and_lands_none() -> None:
    """执行体没接线＝一个都算不上有主：全部上报，一个都不落终态。

    旧实现在这种形状下会用 50 毫秒轮询库行重推一遍、把 2 秒预算内没等到终态的节点强判
    failed——那是一条代码没有证据的判决（同文件当时的注释就写着 2 秒掐不住一次真实回合）。
    """
    store = _round_store('in_progress')
    runner, diagnostics = _reporting_runner(store)
    runner.cancel_node_subtree_executor = None

    owned = asyncio.run(runner._cancel_spawn_subtrees(task_id=TASK_ID, node_ids={EXEC_ID}, reason_text='x'))
    assert owned == set()

    runner._report_nodes_without_owner(
        task_id=TASK_ID,
        parent_node_id=ROOT_ID,
        cache_key='',
        reason_text='x',
        node_ids={EXEC_ID} - owned,
    )

    assert store.get_node(EXEC_ID).status == 'in_progress'
    reported = [item for item in diagnostics if str(item.get('title') or '') == 'spawn_supersede_left_to_owner']
    assert len(reported) == 1
    detail = str(reported[0].get('detail') or '')
    assert f'left_node_id={EXEC_ID}' in detail
    assert 'status=in_progress' in detail, detail


def test_supersede_sweep_does_not_land_nodes_the_owner_cannot_account_for() -> None:
    """走真实清扫入口：本进程没有派发 entry 的节点，判据必须是上报而不是强判。

    这条是行为判据，不是参数形状判据。旧实现在这里用 50 毫秒轮询库行重推一遍再强判
    `failed`（预算 2 秒，而同文件当时的注释写着一次模型回合 p50 15s / p90 60s 掐不住），
    于是在别的进程里真在跑的分支会被本进程按一句猜测判死。落定归取消方；无主的只上报。

    判别力落在诊断标题上：这里 `_log_service` 是空操作替身，节点行的写入观察不到，
    所以不假装断言了状态——HEAD 与本改动分别产出 `spawn_supersede_forced_live_node`
    与 `spawn_supersede_left_to_owner`，这条用例据此红/绿。
    """
    store = _round_store('in_progress')
    runner, diagnostics = _reporting_runner(store)
    runner.cancel_node_subtree_executor = None
    # 旧实现的强判会走 `_mark_finished`，它需要 `_react_loop`；给一个全空操作的替身，
    # 让判据落在诊断标题上而不是以 AttributeError 提前退出。
    runner._react_loop = _FallbackLogService(object())  # type: ignore[attr-defined]
    # 清扫这条道会碰很多帧与账本读口；本用例只关心清扫走了哪条判决道。
    runner._log_service = _FallbackLogService(runner._log_service)  # type: ignore[attr-defined]

    asyncio.run(
        runner._settle_superseded_spawn_operations(
            task=SimpleNamespace(task_id=TASK_ID, root_node_id=ROOT_ID, session_id='web:supersede'),
            parent=store.get_node(ROOT_ID),
            exclude_cache_key='call_01_new',
            replacement_round_id='call_01_new',
        )
    )

    titles = [str(item.get('title') or '') for item in diagnostics]
    assert 'spawn_supersede_left_to_owner' in titles, titles
    assert 'spawn_supersede_forced_live_node' not in titles, titles


# --- 滞留道：活性问持有者，停等要可见 ---------------------------------------


def _parking_runner(store: _FakeStore):
    runner, diagnostics = _reporting_runner(store)
    paused: list[dict] = []
    logged: list[dict] = []
    store.get_task_node_pause = lambda node_id: None  # type: ignore[attr-defined]
    runner._log_service.append_task_error_log = (  # type: ignore[attr-defined]
        lambda task_id, node_id, **kwargs: logged.append({'task_id': task_id, 'node_id': node_id, **kwargs})
    )
    runner._log_service.set_node_pause_state = (  # type: ignore[attr-defined]
        lambda task_id, node_id, **kwargs: paused.append({'task_id': task_id, 'node_id': node_id, **kwargs})
    )
    return runner, diagnostics, paused, logged


async def _boom(**_kwargs):
    raise sqlite3.OperationalError('disk I/O error')


def test_hold_parks_the_parent_when_nothing_is_progressing() -> None:
    """没人推进且说不清会不会再推进 ⇒ 父节点可见地停下，而不是静默挂着。"""
    from main.errors import NodePausedError

    store = _round_store('in_progress')
    runner, diagnostics, paused, logged = _parking_runner(store)
    runner.node_liveness_probe = lambda task_id, node_id: False
    runner._spawn_children_locked = _boom  # type: ignore[attr-defined]

    with pytest.raises(NodePausedError):
        asyncio.run(
            runner._spawn_children(task_id=TASK_ID, parent_node_id=ROOT_ID, specs=_specs(), call_id=ROUND_ID)
        )

    assert paused and paused[0]['node_id'] == ROOT_ID
    assert paused[0]['pause_requested'] is True and paused[0]['is_paused'] is True
    assert paused[0]['pause_reason'] == 'error'
    remark = str(paused[0]['remark'] or '')
    assert 'spawn_round_stalled' in remark and 'unknown=' in remark
    assert logged and 'spawn_round_stalled' in str(logged[0].get('error_text') or '')
    titles = [str(item.get('title') or '') for item in diagnostics]
    assert 'spawn_round_parked_for_operator' in titles, titles
    # 暂停的是父节点；孩子由自己的派发 entry 驱动，这里一个字都不写。
    assert store.get_node(EXEC_ID).status == 'in_progress'


def test_hold_never_parks_while_the_owner_is_making_progress() -> None:
    """持有者说在跑就不算停等——防的是"一次存储抖动把任务暂停"这种过度反应。"""
    store = _round_store('in_progress')
    runner, _diagnostics, paused, _logged = _parking_runner(store)
    runner.node_liveness_probe = lambda task_id, node_id: True
    runner._spawn_children_locked = _boom  # type: ignore[attr-defined]

    async def _converge_soon():
        await asyncio.sleep(0.03)
        store.nodes[EXEC_ID] = store.nodes[EXEC_ID].model_copy(
            update={'status': 'success', 'final_output': 'shard written', 'finished_at': now_iso()}
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

    assert paused == [], '有人驱动时不得落暂停'
    assert len(results) == 1


def test_hold_probe_turns_its_own_read_failures_into_unknown() -> None:
    """滞留道的读失败计入 unknown，绝不外抛、也绝不当成终态。

    这里抛出去会顶替原异常穿出 `_spawn_children` 的 except 体，工具照旧变成
    `Error executing spawn_child_nodes`——本道要防的事被本道自己做一遍。
    """
    store = _round_store('in_progress')
    runner, _diagnostics = _reporting_runner(store)
    original_get_node = store.get_node

    def _flaky_get_node(node_id):
        if str(node_id or '').strip() == EXEC_ID:
            raise sqlite3.OperationalError('disk I/O error')
        return original_get_node(node_id)

    store.get_node = _flaky_get_node  # type: ignore[assignment]

    def _dead_probe(_task_id, _node_id):
        raise sqlite3.OperationalError('probe down')

    runner.node_liveness_probe = _dead_probe  # type: ignore[attr-defined]

    state = runner._spawn_round_hold_probe(task_id=TASK_ID, parent_node_id=ROOT_ID, cache_key=ROUND_ID)

    assert state['driven'] == [] and state['converged'] == []
    assert state['unknown'] == [EXEC_ID], state


def test_cancel_gate_treats_an_unreadable_store_as_not_a_cancellation() -> None:
    """这道门在滞留道之前，它自己抛就等于绕过滞留道——读不到一律「不算真取消」。"""
    store = _round_store('in_progress')
    runner, _diagnostics = _reporting_runner(store)

    def _throw(_task_id):
        raise sqlite3.OperationalError('disk I/O error')

    store.get_task = _throw  # type: ignore[assignment]

    assert runner._should_propagate_child_pipeline_cancellation(
        task_id=TASK_ID, parent_node_id=ROOT_ID,
    ) is False


def test_orphan_candidate_is_parked_and_never_signed_failed() -> None:
    """收尸器不再签发终态：无人驱动又没有重放路径 ＝ 可见地停等，不是"节点失败了"。

    三载体合取里任何一环都可能只是读不到——父节点正常 success 之后它的帧就是被
    `_mark_finished` 删掉的（设计态）。用这样一条合取去 `fail_paused_node` 不可逆，
    2026-10-15 就是这样杀掉了 6 个仍在跑的节点。
    """
    from main.runtime.task_actor_service import TaskActorService

    store = _round_store('in_progress')
    runner = object.__new__(NodeRunner)
    runner._store = store  # type: ignore[attr-defined]
    paused: list[tuple] = []
    logged: list[dict] = []
    runner._mark_node_paused = (  # type: ignore[assignment]
        lambda task_id, node_id, **kwargs: paused.append((task_id, node_id, kwargs))
    )
    actor = object.__new__(TaskActorService)
    actor._log_service = SimpleNamespace(  # type: ignore[attr-defined]
        append_task_error_log=lambda **kwargs: logged.append(dict(kwargs)),
    )
    actor._node_runner = runner  # type: ignore[attr-defined]

    actor._report_orphan_candidate(  # type: ignore[attr-defined]
        task_id=TASK_ID, node=store.get_node(EXEC_ID), node_id=EXEC_ID,
    )

    assert logged, '错误行不落就没有人会被叫醒'
    assert 'spawn_orphan_candidate' in str(logged[0].get('error_text') or '')
    assert paused and paused[0][1] == EXEC_ID
    assert paused[0][2].get('reason') == 'error'
    assert store.get_node(EXEC_ID).status == 'in_progress', '候选节点被签成了终态'

