"""共享池回合闸、闸外排队可见性，以及停滞事件的"只报事实"合同。

来源：2026-10-12 `task:edf015cc47dc` 的取证。三条实盘病灶各自有归属：
- 04:05:56 读数 `run 5/12`、`queued execution=0 / inspection=11` ⇒ 容量按角色拆开算，
  7 个格子空着而验收节点在闸外等（保留窗内 10% 的拍都是这个形态）。
- 根的 entry 在闸外排了 40 分钟，`task_runtime_meta` 却报 `dispatch_running 0 /
  dispatch_queued 0` ⇒ `resume_node` 重挂协程时不记账也不 publish。
- 03:08:33 那一秒同发两档 `suspected_stall`，正文里「最后可见输出＝此刻」与「已静默
  30 分钟」并存 ⇒ 被挡住的 pass 不停表，而事件的时长来自上一拍的锚点、字段现读新值。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.runtime.adaptive_tool_budget import AdaptiveToolBudgetController
from main.runtime.task_actor_service import TaskNodeDispatcher
from main.service.task_stall_callback import normalize_task_stall_payload
from main.service.task_stall_notifier import TaskStallNotifier


def _controller(execution: int = 8, inspection: int = 4) -> AdaptiveToolBudgetController:
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': execution, 'inspection': inspection})
    return controller


@pytest.mark.asyncio
async def test_shared_pool_admits_a_waiting_role_into_another_roles_idle_slots() -> None:
    """验收节点能进执行角色的空格子：容量不再按角色各自封顶。

    拆开的旧形态里 inspection 到 4 就封顶，哪怕整池 12 格里有 7 格空着。
    """
    controller = _controller()
    for _ in range(5):
        await asyncio.wait_for(controller.acquire_entry_slot(role='execution'), timeout=1.0)

    admitted = []
    for _ in range(3):
        admitted.append(await asyncio.wait_for(controller.acquire_entry_slot(role='inspection'), timeout=1.0))
    assert len(admitted) == 3
    snapshot = controller.entry_snapshot()
    assert snapshot['pool']['running'] == 8
    assert snapshot['inspection']['running'] == 3
    # 角色不再各自封顶：4 格的旧上限如果被保留，第三个 inspection 会永远进不来
    assert snapshot['inspection']['limit'] == snapshot['pool']['limit'] == 12


@pytest.mark.asyncio
async def test_shared_pool_keeps_the_reserved_slots_of_a_waiting_role() -> None:
    """执行侧不能把**正在排队**的验收角色的保留格借空。

    保留格的语义要说准：没人等的角色的那几格是可以借走的（否则就退回"拆开算"的空转），
    一旦那个角色有等待者，借走它的保留格就要被拒绝——这正是 04:05:56 那个现场缺的半边：
    池位 12 只用了 5，而 11 个验收节点在闸外。
    """
    controller = _controller()
    # 池 12；execution 保留 3、inspection 保留 3
    # 每次 acquire 都带超时：角色封顶那版的实现会让"借到第 9 格"永久挂住，
    # 判据回归时要看到失败而不是把测试跑挂。
    for _ in range(12):
        await asyncio.wait_for(controller.acquire_entry_slot(role='execution'), timeout=1.0)
    assert controller.entry_snapshot()['pool']['queued'] == 0, "没有等待者时执行侧可以占满整池"

    inspection_waiter = asyncio.create_task(controller.acquire_entry_slot(role='inspection'))
    execution_greedy = asyncio.create_task(controller.acquire_entry_slot(role='execution'))
    await asyncio.sleep(0.05)
    assert not inspection_waiter.done()
    assert not execution_greedy.done(), "执行侧借走了正在排队的验收角色的保留格"
    snapshot = controller.entry_snapshot()
    assert snapshot['inspection']['queued'] == 1
    assert snapshot['execution']['queued'] == 1

    # 腾出一格 ⇒ FIFO 队首的验收先进（它用的是自家保留格）
    controller.release_entry_slot(role='execution')
    await asyncio.wait_for(inspection_waiter, timeout=1.0)
    snapshot = controller.entry_snapshot()
    assert snapshot['inspection']['running'] == 1
    assert snapshot['pool']['running'] == 12
    # 那个执行侧的贪心请求仍被留在闸外：验收还有 2 格保留额没拿满
    assert not execution_greedy.done(), "验收的保留份额被侵占"

    execution_greedy.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution_greedy
    snapshot = controller.entry_snapshot()
    # 被取消的等待者不能把排队计数留成残值（保留格判据全靠它）
    assert snapshot['execution']['queued'] == 0
    assert snapshot['pool']['queued'] == 0


def _dispatcher_stub(budget: AdaptiveToolBudgetController) -> tuple[TaskNodeDispatcher, dict]:
    published: dict = {}
    store = SimpleNamespace(get_node=lambda node_id: SimpleNamespace(node_kind='execution', task_id='task:stub'))
    log_service = SimpleNamespace(update_task_runtime_meta=lambda task_id, **kwargs: published.update(kwargs))
    node_runner = SimpleNamespace()
    dispatcher = TaskNodeDispatcher(
        task_id='task:stub',
        store=store,
        log_service=log_service,
        node_runner=node_runner,
        execution_limit=8,
        inspection_limit=4,
        entry_budget=budget,
    )
    return dispatcher, published


@pytest.mark.asyncio
async def test_resume_node_publishes_the_gate_queue_it_rejoined() -> None:
    """分发释放后重挂的 entry 必须记进 `dispatch_queued`：界面与判据都读这一份。

    实盘：根节点在闸外排了 40 分钟，meta 仍是 `dispatch_running 0 / dispatch_queued 0`，
    于是"这个任务在不在排队"从任何一侧都读不出来。
    """
    budget = _controller()
    dispatcher, published = _dispatcher_stub(budget)

    entry = dispatcher._get_or_create_entry('node:root')
    dispatcher._mark_entry_running(entry)
    assert published['dispatch_queued']['execution'] == 0
    assert published['dispatch_running']['execution'] == 1

    # 模拟一次 hold 冻结后的收尾：条目结清，等待重新挂上（分发释放路径干的事）
    dispatcher._finish_entry(entry)
    entry.task = None
    published.clear()

    await dispatcher.resume_node('node:root')

    assert published['dispatch_queued']['execution'] == 1, "闸外排队没有被记账"
    # 活闸位是池位数，不再是角色地板
    assert published['dispatch_limits']['pool'] == 12
    assert published['dispatch_limits']['execution'] == 12

    await dispatcher.close()


class _BlockedService:
    """分类判据说"这一拍不可操作"的服务桩（分发屏障就是这个形态）。"""

    def __init__(self) -> None:
        self.task = SimpleNamespace(
            task_id='task:blocked',
            status='in_progress',
            is_paused=False,
            pause_requested=False,
            cancel_requested=False,
            created_at='2026-10-12T02:00:00+08:00',
        )
        self.emitted: list[dict] = []
        self.meta: dict = {
            'last_visible_output_at': '2026-10-12T02:00:00+08:00',
            'last_stall_notice_bucket_minutes': 0,
        }
        self.log_service = SimpleNamespace(
            read_runtime_state=lambda *_a, **_k: dict(self.meta),
            read_task_runtime_meta=lambda *_a, **_k: dict(self.meta),
            update_task_runtime_meta=lambda *_a, **_k: self.meta.update(_k),
            list_node_pauses=lambda *_a, **_k: [],
        )

    def get_task(self, task_id: str):
        return self.task

    def _task_origin_session_id(self, task):
        return 'web:ceo-stub'

    def is_task_stall_actionable(self, task_id: str, *, runtime_state=None) -> bool:
        return False

    def build_task_stall_payload(self, task_id: str, **_kwargs) -> dict:
        return {}

    def emit_task_stall(self, payload) -> bool:
        self.emitted.append(payload)
        return True

    @staticmethod
    def _stall_now_iso() -> str:
        return '2026-10-12T03:08:33+08:00'


@pytest.mark.asyncio
async def test_blocked_stall_pass_keeps_the_timer_alive(tmp_path: Path) -> None:
    """被不可操作原因挡住的一拍不许把定时器弄死。

    旧写法直接 return：屏障期间没人再判，只有可见输出或屏障解除那次复位能救活，
    而救活的当场就是"同秒追赶齐发"的那一拍。
    """
    service = _BlockedService()
    notifier = TaskStallNotifier(service=service)
    notifier._schedule(service.task.task_id)
    assert service.task.task_id in notifier._tasks
    await asyncio.sleep(0.05)
    # 立即到期（静默早已超过一档）⇒ pass 跑完，被挡住，但必须重排而不是消失
    await asyncio.sleep(0.05)
    assert service.task.task_id in notifier._tasks, "被挡住的一拍把定时器摘掉了"
    assert service.emitted == []
    notifier.cancel_task(service.task.task_id)


def test_stall_payload_normalizer_keeps_the_facts_and_drops_the_gaps() -> None:
    """`stall_detail` 要能活着穿过归一化；空块不写成 0（0 会被读成"闸口空闲"）。"""
    payload = normalize_task_stall_payload(
        {
            'task_id': 'task:edf015cc47dc',
            'session_id': 'ext:qq-official-1:f8a8',
            'bucket_minutes': 30,
            'last_visible_output_at': '2026-10-11T19:08:33+00:00',
            'reason': 'suspected_stall',
            'stall_detail': {
                'entry_gate': {
                    'pool_running': 12,
                    'pool_limit': 12,
                    'pool_queued': 30,
                    'binding_seconds': 251.4,
                    'reserved': {'execution': 3, 'inspection': 3},
                    'task_running': {'execution': 0, 'inspection': 0},
                    'task_queued': {'execution': 1},
                    'empty_block': {},
                },
                'turn_queue': {
                    'running': 0,
                    'waiting': 13,
                    'oldest_wait_seconds': 2405,
                    'head_pinned_seconds': 2400,
                    'head_busy_reason': 'no_candidate',
                    'head_node_id': 'node:03ef30fcff41',
                    'head_required_window': 392742,
                },
                'upstream_rate': {
                    'requests_per_minute': 111.8,
                    'completions_per_minute': 27.0,
                    'penalty_429_max': 17.2,
                    'local_pressure_state': 'normal',
                },
                'occupiers': [
                    {'task_id': 'task:e25f6cb420c8', 'running': 11, 'queued': 22},
                    {'task_id': '', 'running': 3, 'queued': 0},
                ],
                'lane': {'distribution_state': '', 'frames': []},
            },
        }
    )
    detail = payload['stall_detail']
    assert detail['entry_gate']['pool_limit'] == 12
    assert detail['entry_gate']['task_queued'] == {'execution': 1}
    assert 'empty_block' not in detail['entry_gate']
    assert detail['upstream_rate']['completions_per_minute'] == 27.0
    assert [item['task_id'] for item in detail['occupiers']] == ['task:e25f6cb420c8']
    assert 'lane' not in detail
    # 闸内那层的排队要能活着到会话，且 0 是真 0（granted=0 是这条车道的核心信号）
    assert detail['turn_queue']['running'] == 0
    assert detail['turn_queue']['waiting'] == 13
    assert detail['turn_queue']['head_busy_reason'] == 'no_candidate'
    assert 'head_window_zero' not in detail['turn_queue']


def test_stall_detail_lines_state_facts_and_offer_no_actions() -> None:
    """事实行只描述状态：不出现动作句（暂停/恢复/等没人跑）。"""
    from g3ku.heartbeat.prompt_lane import _task_stall_detail_lines

    lines = _task_stall_detail_lines(
        {
            'entry_gate': {
                'pool_running': 12,
                'pool_limit': 12,
                'pool_queued': 30,
                'binding_seconds': 251.4,
                'reserved': {'execution': 3, 'inspection': 3},
                'task_running': {'execution': 0},
                'task_queued': {'execution': 1},
            },
            'turn_queue': {
                'running': 0,
                'waiting': 13,
                'oldest_wait_seconds': 2405,
                'head_pinned_seconds': 2400,
                'head_busy_reason': 'no_candidate',
                'head_node_id': 'node:03ef30fcff41',
                'head_required_window': 392742,
            },
            'upstream_rate': {
                'requests_per_minute': 111.8,
                'completions_per_minute': 27.0,
                'penalty_429_max': 17.2,
                'local_pressure_state': 'normal',
                'machine_pressure_state': 'normal',
            },
            'occupiers': [{'task_id': 'task:e25f6cb420c8', 'running': 11, 'queued': 22}],
            'lane': {'distribution_state': 'distributing', 'distribution_epoch_id': 'epoch:2221c1667c3e'},
        }
    )
    text = '\n'.join(lines)
    assert 'Node-turn gate (shared pool): running 12/12' in text
    assert 'waiting 30' in text
    assert 'completions/min 27.0' in text
    assert 'task:e25f6cb420c8 (running 11, waiting 22)' in text
    assert 'Distribution barrier active: state=distributing' in text
    # 闸位显示"10 个在运行"而这一层 granted=0：会话要靠这两行才不会再误判成"在推进"
    assert 'Node-turn queue (already past the gate' in text
    assert 'granted 0, waiting 13, oldest wait 2405s' in text
    assert 'one request holding the grant window for 2400s' in text
    assert 'head reason=no_candidate' in text
    assert 'needs a 392742-token window' in text
    for banned in ('Suggested', 'pause', 'Pause', 'resume', 'Consider', 'should'):
        assert banned not in text, f"事实行里混进了动作句：{banned}"


@pytest.mark.asyncio
async def test_model_route_lease_success_counts_once_and_failure_not_at_all() -> None:
    """完成量轴：只有"真发过请求且成功释放"才计一次，失败释放与没发出去的租约都不计。"""
    from main.runtime.model_load_balancer import LEASE_OUTCOME_SUCCESS
    from main.runtime.model_route import ModelRouteLease
    from tests.resources.test_model_load_balancer import _balancer, _group

    def _lease(*, node_id: str, request_started: bool, lease_id: int) -> ModelRouteLease:
        return ModelRouteLease(
            lease_id=lease_id,
            node_id=node_id,
            task_id='task:edf015cc47dc',
            route_index=0,
            group_key='group_1',
            model_key='m_a',
            key_index=0,
            quota_bucket_key='key:m_a',
            selection_reason='least_load',
            config_revision=1,
            request_started=request_started,
        )

    balancer = _balancer(_group('group_1', 'm_a'))
    lease = _lease(node_id='node:b8772bb1145e', request_started=True, lease_id=1)
    assert balancer.rate_pressure()['rolling_completions_60s_sum'] == 0

    balancer.release(lease, outcome=LEASE_OUTCOME_SUCCESS)
    assert balancer.rate_pressure()['rolling_completions_60s_sum'] == 1
    # 幂等：同一个租约不会被释放两次
    balancer.release(lease, outcome=LEASE_OUTCOME_SUCCESS)
    assert balancer.rate_pressure()['rolling_completions_60s_sum'] == 1

    failed = _lease(node_id='node:other', request_started=True, lease_id=2)
    balancer.release(failed, outcome='error', error='HTTP 429: rpm exhausted')
    assert balancer.rate_pressure()['rolling_completions_60s_sum'] == 1

    never_sent = _lease(node_id='node:third', request_started=False, lease_id=3)
    balancer.release(never_sent, outcome=LEASE_OUTCOME_SUCCESS)
    assert balancer.rate_pressure()['rolling_completions_60s_sum'] == 1
