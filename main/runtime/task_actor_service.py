from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from loguru import logger

from main.errors import NodePausedError, TaskPausedError, describe_exception
from main.models import (
    NodeFinalResult,
    normalize_final_acceptance_metadata,
    normalize_result_payload,
)
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
    ACCEPTANCE_STATE_REJECTED_TERMINAL,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    normalize_acceptance_handshake,
)
from main.runtime.node_mailbox import notification_is_held
from main.runtime.node_runner import SKIPPED_CHECK_RESULT
from main.runtime.node_wake import PARKED_PHASES
from main.runtime.wait_graph import build_wait_nodes, find_wait_cycles
from main.types import KIND_ACCEPTANCE

_DEFAULT_NODE_DISPATCH_LIMITS = {
    'execution': 8,
    'inspection': 4,
}
# A3：释放后校验清扫的两段延迟（秒）。第一段后仍卡死则再 resume 一次，
# 第二段后仍卡死则落 ERROR（冻结→释放的终点必须可见）。
_RELEASE_VERIFICATION_DELAY_SECONDS = 5.0
_CURRENT_DISPATCH_LEASE: ContextVar['_DispatchLease | None'] = ContextVar(
    'task_node_dispatch_lease',
    default=None,
)


def _normalize_dispatch_limit(value: int | None, *, default: int) -> int | None:
    if value is None:
        return None
    return max(1, int(value or default or 1))


class _AdaptiveRoleGate:
    """角色闸：与 `asyncio.Semaphore` 同形（await acquire() / 同步 release()），
    但容量由压力状态机实时决定（critical→1、throttled→冻结、磁盘紧急→0、恢复按步 +1）。

    换成它是因为 worker 模式下角色信号量被置 None：一次 resume 放出的节点数远大于槽位数时，
    每个执行器在拿到任何槽位之前就已经把自己那份上下文物化出来，工具/模型槽位的闸门拦不住
    这份"存在成本"。放在这里而不是新造控制器：`_DispatchLease` 的"嵌套等待时释放、回来重取"
    语义直接复用，等子节点的节点不会占着闸位。
    """

    def __init__(self, budget, role: str) -> None:
        self._budget = budget
        self._role = str(role or '').strip().lower()

    async def acquire(self) -> None:
        await self._budget.acquire_entry_slot(role=self._role)

    def release(self) -> None:
        self._budget.release_entry_slot(role=self._role)


@dataclass(slots=True)
class _DispatchEntry:
    node_id: str
    role: str
    future: asyncio.Future[NodeFinalResult]
    task: asyncio.Task[None] | None = None
    queued_counted: bool = False
    running_counted: bool = False
    # interrupt_node 预置的合成结果：cancel 落地后 future 用它解析，
    # 而不是 run_node 取消路径产出的 canceled/failed 结果。
    interrupt_result: NodeFinalResult | None = None


class _DispatchLease:
    def __init__(self, *, dispatcher: 'TaskNodeDispatcher', entry: _DispatchEntry, semaphore: asyncio.Semaphore | None) -> None:
        self.dispatcher = dispatcher
        self.entry = entry
        self._semaphore = semaphore
        self._lock = asyncio.Lock()
        self._nested_wait_count = 0
        self._holding_slot = True
        self._closed = False

    async def wait_for(self, future: asyncio.Future[NodeFinalResult]) -> NodeFinalResult:
        await self._enter_nested_wait()
        try:
            # B5：shield 保护子节点派发 future——等待方（父管线/看门狗取消链）被
            # cancel 时，asyncio 会顺着 _fut_waiter 把被等的 future 一并取消，
            # 子节点 entry 因此"活着但 future 已死"，释放复活永久跳过
            # （2026-09-15 孤儿子节点事故的致命一环）。取消只应打断等待，
            # 不得穿透销毁子节点的派发凭据。
            return await asyncio.shield(future)
        finally:
            await self._exit_nested_wait()

    async def close(self) -> None:
        should_release = False
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._holding_slot:
                self._holding_slot = False
                should_release = True
        self.dispatcher._finish_entry(self.entry)
        if should_release:
            if self._semaphore is not None:
                self._semaphore.release()

    async def _enter_nested_wait(self) -> None:
        should_release = False
        async with self._lock:
            if self._closed:
                return
            self._nested_wait_count += 1
            if self._nested_wait_count == 1 and self._holding_slot:
                self._holding_slot = False
                should_release = True
        if should_release:
            self.dispatcher._suspend_entry(self.entry)
            if self._semaphore is not None:
                self._semaphore.release()

    async def _exit_nested_wait(self) -> None:
        need_acquire = False
        async with self._lock:
            if self._nested_wait_count > 0:
                self._nested_wait_count -= 1
            if self._closed:
                return
            if self._nested_wait_count == 0 and not self._holding_slot:
                need_acquire = True
        if not need_acquire:
            return
        if self._semaphore is not None:
            await self._semaphore.acquire()
        should_resume = False
        async with self._lock:
            if self._closed:
                if self._semaphore is not None:
                    self._semaphore.release()
                return
            if self._nested_wait_count == 0 and not self._holding_slot:
                self._holding_slot = True
                should_resume = True
            else:
                if self._semaphore is not None:
                    self._semaphore.release()
                return
        if should_resume:
            self.dispatcher._resume_entry(self.entry)


class TaskNodeDispatcher:
    def __init__(
        self,
        *,
        task_id: str,
        store,
        log_service,
        node_runner,
        execution_limit: int | None = _DEFAULT_NODE_DISPATCH_LIMITS['execution'],
        inspection_limit: int | None = _DEFAULT_NODE_DISPATCH_LIMITS['inspection'],
        entry_budget=None,
    ) -> None:
        self._task_id = str(task_id or '').strip()
        self._store = store
        self._log_service = log_service
        self._node_runner = node_runner
        self._limits = {
            'execution': _normalize_dispatch_limit(execution_limit, default=_DEFAULT_NODE_DISPATCH_LIMITS['execution']),
            'inspection': _normalize_dispatch_limit(inspection_limit, default=_DEFAULT_NODE_DISPATCH_LIMITS['inspection']),
        }
        if entry_budget is not None:
            entry_budget.configure_entry_ceilings(
                {
                    role: int(limit)
                    for role, limit in self._limits.items()
                    if limit is not None
                }
            )
        self._entry_budget = entry_budget
        if entry_budget is not None:
            # 每角色一个 gate 句柄（release 要按角色归还保留格账本），但格子本身是共享池：
            # `acquire_entry_slot(role=...)` 在池上判容量与保留格，不再各自封顶。
            self._semaphores = {
                role: (_AdaptiveRoleGate(entry_budget, role) if limit is not None else None)
                for role, limit in self._limits.items()
            }
        else:
            # 没接回合闸的部署（测试/嵌入式）也要共享，否则两个 Semaphore 各自封顶，
            # 一侧空转另一侧排队——正是拆开算的实盘病灶（10% 的采样拍）。任一角色不限
            # 时整个池视为不限，保持"这里没有闸门"的原语义。
            shared_total = sum(int(limit) for limit in self._limits.values() if limit is not None)
            shared = asyncio.Semaphore(shared_total) if (shared_total > 0 and all(
                limit is not None for limit in self._limits.values()
            )) else None
            self._semaphores = {role: shared for role in self._limits}
        self._entries: dict[str, _DispatchEntry] = {}
        self._closed = False
        self._last_snapshot_fingerprint: tuple[tuple[str, int], ...] | None = None
        # 任务演员的等待/唤醒信号：entry 创建/结束、节点复活都算一次"现场变化"。
        # `run_task` 的空转循环等它而不是忙轮询；超时只是兜底，不是判据。
        self._activity_event = asyncio.Event()
        self._publish_dispatch_state(force=True)

    def _notify_activity(self) -> None:
        event = getattr(self, '_activity_event', None)
        if event is not None:
            event.set()

    def has_live_entries(self) -> bool:
        return any(
            entry.task is not None and not entry.task.done()
            for entry in self._entries.values()
        )

    async def wait_activity(self, timeout: float = 0.5) -> None:
        event = getattr(self, '_activity_event', None)
        if event is None:
            self._activity_event = asyncio.Event()
            event = self._activity_event
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.05, float(timeout)))
        except asyncio.TimeoutError:
            return

    def schedule_node(self, node_id: str) -> None:
        """非阻塞派发：保证该节点有一条在跑的 entry，不等待它结束。

        与 `resume_node` 的区别只在返回形态——两者最终都落到 `_get_or_create_entry`。
        子节点创建后由这里立即起跑，父节点不再等整批子管线。
        """
        normalized_node_id = str(node_id or '').strip()
        if not normalized_node_id:
            return
        self._get_or_create_entry(normalized_node_id)
        self._notify_activity()

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {
            'dispatch_limits': self._effective_dispatch_limits(),
            'dispatch_running': {
                role: sum(1 for entry in self._entries.values() if entry.role == role and entry.running_counted)
                for role in self._limits
            },
            'dispatch_queued': {
                role: sum(1 for entry in self._entries.values() if entry.role == role and entry.queued_counted)
                for role in self._limits
            },
        }

    def _effective_dispatch_limits(self) -> dict[str, int]:
        """活闸位是**共享池**；角色键回显同一个池位数（该角色此刻真的最多能开到这么多）。

        接了回合闸就读存活池位（余量目标每拍推它），没接就回落到配置地板。只报地板会让
        "闸在动"这件事在 UI 上读不出来；而继续报"execution 8 / inspection 4"这种拆开的上限
        更错——它让人以为验收最多只能同时跑 4 个，实际它能借到整池。磁盘紧急时池位是 0，
        角色回显也如实是 0。每角色的保留格在 worker 心跳的 `entry_gate_reserved` 里，
        不在这里重复一份（两处真值迟早分叉）。
        """
        pool_floor = sum(int(limit) for limit in self._limits.values() if limit is not None)
        fallback = {**{role: pool_floor for role in self._limits}, 'pool': pool_floor}
        getter = getattr(self._entry_budget, 'entry_snapshot', None)
        if not callable(getter):
            return fallback
        try:
            live = dict(getter() or {})
        except Exception:
            return fallback
        pool = dict(live.get('pool') or {})
        if not pool:
            return fallback
        pool_limit = max(0, int(pool.get('limit') or 0))
        return {**{role: pool_limit for role in self._limits}, 'pool': pool_limit}

    async def execute_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        normalized_task_id = str(task_id or '').strip()
        if normalized_task_id != self._task_id:
            raise ValueError(f'mismatched dispatcher task id: {normalized_task_id} != {self._task_id}')
        entry = self._get_or_create_entry(node_id)
        current_lease = _CURRENT_DISPATCH_LEASE.get()
        if current_lease is not None and current_lease.dispatcher is self:
            if current_lease.entry.node_id == entry.node_id:
                raise RuntimeError(f'node dispatch cannot wait on itself: {entry.node_id}')
            return await current_lease.wait_for(entry.future)
        # B5：同 wait_for——外层（run_task/控制回合驱动）被取消时不得顺带
        # 取消 entry future；生命周期由 dispatcher.close()/cancel_nodes 权威管理。
        return await asyncio.shield(entry.future)

    def node_is_being_driven(self, node_id: str) -> bool | None:
        """这个节点此刻是不是本进程在驱动。三态，且每一态都可据此做不同的事。

        `True`  ＝ 有派发 entry 且协程未结束：进度在发生，等下去是有依据的。
        `False` ＝ 有 entry 但协程已结束：本进程的驱动已经停了，节点行现在是稳定的。
        `None`  ＝ 本进程没有这个节点的 entry。同进程内这是权威答案（entry 只在
        `future.done()` 且状态非终态时才会被摘掉，见 `_get_or_create_entry`）；但重启之后
        整张表是空的，这时它退化成「我说不出谁在驱动它」——所以调用方必须把 `None` 当
        未知处理，既不能当「还活着」也不能当「死了」。
        """
        entry = self._entries.get(str(node_id or '').strip())
        if entry is None:
            return None
        task = entry.task
        if task is None:
            return False
        return not task.done()

    async def cancel_nodes(self, node_ids: list[str], reason: str = '') -> set[str]:
        """取消这批节点的在飞执行体、把它们落定，并返回**本进程确实持有其 entry 的那些**节点 id。

        返回值是调用方唯一需要的活性结论：出现在返回值里的节点在这里已经等到协程真停并被
        落定，调用方不需要、也没有资格再判一次；没出现的（跨进程、重启后、非常规形态）是本
        进程「不知道谁在驱动」，只许上报，不许就地落终态。

        `reason` 由发起清扫的一方给出（例如 `superseded by newer spawn round: X`）：是这里
        停掉了它的执行体，就该由这里把「为什么停」写进行，而不是留一个 `canceled` 让操作员
        回头去猜这是人工取消还是被取代。
        """
        landing_reason = str(reason or '').strip() or 'canceled'
        entries: list[_DispatchEntry] = []
        owned: set[str] = set()
        seen: set[str] = set()
        for raw_node_id in list(node_ids or []):
            node_id = str(raw_node_id or '').strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            entry = self._entries.get(node_id)
            if entry is None:
                continue
            owned.add(node_id)
            entries.append(entry)
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
            elif not entry.future.done():
                try:
                    entry.future.set_result(
                        self._node_runner.fail_paused_node(
                            self._task_id, node_id, landing_reason, notify_parent=False
                        )
                    )
                except Exception:
                    entry.future.set_exception(asyncio.CancelledError())
        # 先等被取消的协程真停：直接 shield-await future 会永远等不到解析——
        # supersede-kill 会死锁（B3 配套修复）。
        running_tasks = [entry.task for entry in entries if entry.task is not None and not entry.task.done()]
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)
        # 落定这批节点。判据必须是**节点行**而不是 future 状态：普通取消路径上
        # `_run_entry` 把 future 置成 cancelled（已经 done），只补「未解析的 future」就会
        # 漏掉真正在飞的那批，把它们留在 in_progress——那正是过去调用方要自己轮询库行再强判
        # 的原因。`fail_paused_node` 对已终态的节点原样返回既有结果，所以这里不会把
        # success 改写掉。
        for entry in entries:
            try:
                # supersede/cancel 是清理车道，不是子节点的"结果"：给父节点发 child_final
                # 会让父节点把一次内部清扫读成子节点失败，也会误满足 all_children。
                result = self._node_runner.fail_paused_node(
                    self._task_id, entry.node_id, landing_reason, notify_parent=False
                )
            except Exception:
                if not entry.future.done():
                    entry.future.set_exception(asyncio.CancelledError())
                continue
            if not entry.future.done():
                entry.future.set_result(result)
        return owned

    async def resume_node(self, node_id: str) -> None:
        normalized_node_id = str(node_id or '').strip()
        entry = self._entries.get(normalized_node_id)
        if entry is None:
            entry = self._get_or_create_entry(normalized_node_id)
        if entry.future.done():
            return
        if entry.task is None or entry.task.done():
            # 重新挂上协程的同时要把"在闸外排队"这件事记账并 publish。旧写法只建任务，
            # 于是分发释放后根节点在回合闸外排了 40 分钟，而 runtime_meta 仍报
            # dispatch_running 0 / dispatch_queued 0（实盘 task:edf015cc47dc，10-12），
            # 界面「调度」与停滞判据都读这份假 0。
            entry.queued_counted = True
            entry.task = asyncio.create_task(
                self._run_entry(entry),
                name=f'task-node-resume:{self._task_id}:{normalized_node_id}',
            )
            self._publish_dispatch_state()
            self._notify_activity()

    async def interrupt_node(self, node_id: str, result: NodeFinalResult, *, on_stopped: Any = None) -> None:
        """定向中断一个运行中的节点，用合成结果解析其 future。

        用于验收打断（决策回合 resume_execution）：cancel 运行中的协程，
        等它真正停下后先执行 on_stopped（作废/清 frame 等权威重置），再
        以预置结果解析 future——父管线因此走正常结果分支，不会把
        CancelledError 转成 spawn 运行时错误杀死整条分支。
        """
        normalized_node_id = str(node_id or '').strip()
        entry = self._entries.get(normalized_node_id)
        if entry is None:
            if callable(on_stopped):
                outcome = on_stopped()
                if asyncio.iscoroutine(outcome):
                    await outcome
            return
        entry.interrupt_result = result
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
            await asyncio.gather(entry.task, return_exceptions=True)
        if callable(on_stopped):
            outcome = on_stopped()
            if asyncio.iscoroutine(outcome):
                await outcome
        if not entry.future.done():
            entry.future.set_result(result)

    async def fail_node(
        self,
        node_id: str,
        reason: str = '',
        *,
        notify_parent: bool = True,
    ) -> NodeFinalResult:
        normalized_node_id = str(node_id or '').strip()
        entry = self._entries.get(normalized_node_id)
        result = self._node_runner.fail_paused_node(
            self._task_id, normalized_node_id, reason, notify_parent=notify_parent
        )
        if entry is not None and not entry.future.done():
            entry.future.set_result(result)
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [entry.task for entry in self._entries.values() if entry.task is not None and not entry.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for entry in self._entries.values():
            if not entry.future.done():
                entry.future.cancel()
            self._finish_entry(entry)
        self._publish_dispatch_state(force=True)

    def _get_or_create_entry(self, node_id: str) -> _DispatchEntry:
        normalized_node_id = str(node_id or '').strip()
        node = self._store.get_node(normalized_node_id)
        if node is None:
            raise ValueError(f'node not found: {normalized_node_id}')
        entry = self._entries.get(normalized_node_id)
        if entry is not None:
            status = str(getattr(node, 'status', '') or '').strip().lower()
            if entry.future.done() and status not in {'success', 'failed'}:
                self._finish_entry(entry)
                self._entries.pop(normalized_node_id, None)
            else:
                return entry
        role = 'inspection' if str(node.node_kind or '').strip().lower() == 'acceptance' else 'execution'
        loop = asyncio.get_running_loop()
        future: asyncio.Future[NodeFinalResult] = loop.create_future()
        entry = _DispatchEntry(
            node_id=normalized_node_id,
            role=role,
            future=future,
            queued_counted=True,
        )
        entry.task = loop.create_task(
            self._run_entry(entry),
            name=f'task-node-dispatch:{self._task_id}:{normalized_node_id}',
        )
        self._entries[normalized_node_id] = entry
        self._publish_dispatch_state()
        self._notify_activity()
        return entry

    async def _run_entry(self, entry: _DispatchEntry) -> None:
        semaphore = self._semaphores[entry.role]
        lease: _DispatchLease | None = None
        context_token = None
        try:
            if semaphore is not None:
                await semaphore.acquire()
                if self._closed:
                    semaphore.release()
                    raise asyncio.CancelledError()
            elif self._closed:
                raise asyncio.CancelledError()
            self._mark_entry_running(entry)
            lease = _DispatchLease(dispatcher=self, entry=entry, semaphore=semaphore)
            context_token = _CURRENT_DISPATCH_LEASE.set(lease)
            result = await self._node_runner.run_node(self._task_id, entry.node_id)
        except NodePausedError as exc:
            # Child entries keep their waiter future pending so the parent
            # pipeline pauses naturally. The root has no parent waiter, so it
            # must surface the pause to the task actor instead.
            root = self._store.get_task(self._task_id)
            if root is not None and str(root.root_node_id or '').strip() == entry.node_id and not entry.future.done():
                entry.future.set_exception(exc)
            return
        except asyncio.CancelledError:
            interrupt_result = entry.interrupt_result
            if interrupt_result is not None:
                # 定向中断（验收打断）：吞掉取消，用合成结果解析 future，
                # 让父管线走正常结果分支而不是 CancelledError→spawn 运行时错误。
                if not entry.future.done():
                    entry.future.set_result(interrupt_result)
                return
            if not entry.future.done():
                entry.future.cancel()
            raise
        except Exception as exc:
            # A2：搁浅异常必须留痕——future 可能无人消费（父链路已死时连
            # asyncio 的 never-retrieved 告警都不会出现），日志是唯一线索。
            try:
                logger.error(
                    'node entry crashed; exception parked on dispatch future '
                    '(consumers may be gone): task={} node={} error={!r}',
                    self._task_id,
                    entry.node_id,
                    exc,
                )
            except Exception:
                pass
            if not entry.future.done():
                entry.future.set_exception(exc)
        else:
            if not entry.future.done():
                entry.future.set_result(
                    entry.interrupt_result if entry.interrupt_result is not None else result
                )
        finally:
            if context_token is not None:
                _CURRENT_DISPATCH_LEASE.reset(context_token)
            if lease is not None:
                await lease.close()
            else:
                self._finish_entry(entry)

    def _mark_entry_running(self, entry: _DispatchEntry) -> None:
        changed = False
        if entry.queued_counted:
            entry.queued_counted = False
            changed = True
        if not entry.running_counted:
            entry.running_counted = True
            changed = True
        if changed:
            self._publish_dispatch_state()

    def _suspend_entry(self, entry: _DispatchEntry) -> None:
        if not entry.running_counted:
            return
        entry.running_counted = False
        self._publish_dispatch_state()

    def _resume_entry(self, entry: _DispatchEntry) -> None:
        if entry.running_counted:
            return
        entry.running_counted = True
        self._publish_dispatch_state()

    def _finish_entry(self, entry: _DispatchEntry) -> None:
        changed = False
        if entry.queued_counted:
            entry.queued_counted = False
            changed = True
        if entry.running_counted:
            entry.running_counted = False
            changed = True
        if changed:
            self._publish_dispatch_state()
        self._notify_activity()

    def _publish_dispatch_state(self, *, force: bool = False) -> None:
        snapshot = self.snapshot()
        fingerprint = (
            ('limits_execution', int(snapshot['dispatch_limits'].get('execution') or 0)),
            ('limits_inspection', int(snapshot['dispatch_limits'].get('inspection') or 0)),
            ('limits_pool', int(snapshot['dispatch_limits'].get('pool') or 0)),
            ('running_execution', int(snapshot['dispatch_running']['execution'])),
            ('running_inspection', int(snapshot['dispatch_running']['inspection'])),
            ('queued_execution', int(snapshot['dispatch_queued']['execution'])),
            ('queued_inspection', int(snapshot['dispatch_queued']['inspection'])),
        )
        if not force and fingerprint == self._last_snapshot_fingerprint:
            return
        self._last_snapshot_fingerprint = fingerprint
        self._log_service.update_task_runtime_meta(self._task_id, **snapshot)


class TaskActorService:
    def __init__(
        self,
        *,
        store,
        log_service,
        node_runner,
        stall_notifier=None,
        node_dispatch_execution_limit: int | None = _DEFAULT_NODE_DISPATCH_LIMITS['execution'],
        node_dispatch_inspection_limit: int | None = _DEFAULT_NODE_DISPATCH_LIMITS['inspection'],
        node_dispatch_entry_budget=None,
    ) -> None:
        self._store = store
        self._log_service = log_service
        self._node_runner = node_runner
        self._stall_notifier = stall_notifier
        # P6 节点看门狗：结构判停（三条判据）。由 MainRuntimeService 注入。
        self.node_watchdog = None
        # 等待图环检测命中时的系统通知口（由 MainRuntimeService 注入）。
        self.wait_cycle_notifier = None
        self._dispatchers: dict[str, TaskNodeDispatcher] = {}
        self._node_dispatch_entry_budget = node_dispatch_entry_budget
        self._node_dispatch_limits = {
            'execution': _normalize_dispatch_limit(
                node_dispatch_execution_limit,
                default=_DEFAULT_NODE_DISPATCH_LIMITS['execution'],
            ),
            'inspection': _normalize_dispatch_limit(
                node_dispatch_inspection_limit,
                default=_DEFAULT_NODE_DISPATCH_LIMITS['inspection'],
            ),
        }
        # 引擎级中断（无取消/暂停标志的 CancelledError）后的尽力即时重排钩子：
        # 由 MainRuntimeService 接到 global_scheduler.enqueue_task。进程退出中
        # 调度器关闭时自然无效，任务由下一个 worker 启动恢复兜底。
        self.interrupted_task_requeue_callback = None
        # A3：释放后校验清扫任务（每任务替换式单飞，run_task finally 取消）。
        self._release_sweeps: dict[str, asyncio.Task[None]] = {}
        self._node_runner.nested_node_executor = self._execute_nested_node
        self._node_runner.cancel_node_subtree_executor = self._cancel_node_subtree
        self._node_runner.node_liveness_probe = self._node_is_being_driven
        # 子节点创建后的立即派发：拆掉阻塞承诺后，父节点不再等子管线，
        # 创建完成就必须交给调度器起跑。
        self._node_runner.dispatch_node_callback = self.schedule_node_nowait

    async def _resume_pending_notice_nodes(self, task_id: str) -> bool:
        # 通知账本以节点级状态为准（epoch 下线后任务级 meta 不再维护该索引）：
        # “还欠一次上下文注入”的节点就是需要被重新派发的节点。
        pending_node_ids = [
            str(item or '').strip()
            for item in list(self._node_runner.nodes_with_pending_distribution_notices(task_id=task_id) or [])
            if str(item or '').strip()
        ]
        if not pending_node_ids:
            return False
        resumed_any = False
        skipped_premature_acceptance = False
        for node_id in pending_node_ids:
            if self._final_acceptance_awaiting_execution_submission(task_id, node_id):
                # 门控：被检验执行节点尚未提交结果（握手未进入等待验收）时，
                # 不得由 notice-resume 抢跑最终验收——否则会对仍在执行的节点
                # 判出「交付物缺失」，绕过打回循环把任务提前终态
                # （事故复盘：task:eb6dda95055b 重启后验收抢跑，零打回终结）。
                skipped_premature_acceptance = True
                continue
            result = await self._execute_node(task_id, node_id)
            resumed_any = True
            await self._settle_final_acceptance_notice_result(task_id, node_id, result)
        if not resumed_any:
            # 只剩过早的最终验收节点：返回 False，让 run_task 落回根节点正常
            # 执行路径（执行节点先干活，提交后握手自然放行验收）。
            return False
        if skipped_premature_acceptance:
            # 其他节点消费了通知但最终验收仍过早：run_task 的派发循环会在回到
            # 静止后重新扫描，无需再显式补入队。
            pass
        return True

    def _is_root_final_acceptance_node(self, *, task, node) -> bool:
        if task is None or node is None:
            return False
        if str(getattr(node, 'node_kind', '') or '').strip().lower() != KIND_ACCEPTANCE:
            return False
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        if not bool(final_acceptance.required):
            return False
        node_id = str(getattr(node, 'node_id', '') or '').strip()
        if str(final_acceptance.node_id or '').strip() == node_id:
            return True
        metadata = getattr(node, 'metadata', None)
        return bool(dict(metadata or {}).get('final_acceptance')) if isinstance(metadata, dict) else False

    def _final_acceptance_awaiting_execution_submission(self, task_id: str, node_id: str) -> bool:
        """最终验收抢跑门控：是根最终验收节点且执行节点尚未提交待验时返回 True。

        仅当握手处于 waiting_acceptance（执行节点已提交结果、正等待验收）时
        放行；idle（从未提交）、waiting_execution_retry（应先跑执行节点）及
        各类终态一律拦截。
        """
        task = self._store.get_task(task_id)
        node = self._store.get_node(node_id)
        if not self._is_root_final_acceptance_node(task=task, node=node):
            return False
        execution = self._node_runner._accepted_execution_node(task_id=task_id, acceptance=node)
        if execution is None:
            return True
        if str(getattr(execution, 'node_id', '') or '').strip() != str(getattr(task, 'root_node_id', '') or '').strip():
            return False
        handshake = normalize_acceptance_handshake(
            (getattr(execution, 'metadata', None) or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
        )
        return str(handshake.get('state') or '').strip() not in {
            ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
        }

    async def _settle_final_acceptance_notice_result(
        self,
        task_id: str,
        node_id: str,
        result: NodeFinalResult | None,
    ) -> None:
        """把 notice-resume 跑完的最终验收结果接入打回循环。

        历史上该结果被直接丢弃，任务生死由 _terminal_result_after_notice_resume
        读取投影状态决定，打回从未被消费。现在统一经
        _handle_acceptance_node_result 路由（对齐子节点管线的打回语义）：
        - 验收通过 → 正常进入终态；
        - 任何一次拒绝 → 打回：执行节点带反馈复活，补入队下轮重跑（无次数上限）；
        """
        if result is None:
            return
        task = self._store.get_task(task_id)
        node = self._store.get_node(node_id)
        if not self._is_root_final_acceptance_node(task=task, node=node):
            return
        acceptance = node
        handled = await self._node_runner._handle_acceptance_node_result(
            task=task, acceptance=acceptance, result=result
        )
        if str(handled.delivery_status or '').strip() != 'partial':
            return

    async def _resume_inflight_final_acceptance(self, task_id: str) -> bool:
        """重派发「握手在等、但回合半路断掉」的最终验收节点——通知账本之外的兜底路径。

        验收节点进入回合的第一步就消费并合并自己的交接通知，所以
        `_resume_pending_notice_nodes` 在重启后永远看不到它。这里以握手状态为凭据
        把它重新派发，并复用同一套结算路径（`_settle_final_acceptance_notice_result`）
        与终态判定（`_terminal_result_after_notice_resume`）：命中即返回 True，
        一次 run_task 不会同时驱动根节点与验收节点。
        """
        node_id = str(self._node_runner.resumable_final_acceptance_node_id(task_id) or '').strip()
        if not node_id:
            return False
        dispatcher = self._dispatchers.get(str(task_id or '').strip())
        entry = None if dispatcher is None else dispatcher._entries.get(node_id)
        if entry is not None and entry.task is not None and not entry.task.done():
            return False
        task = self._store.get_task(task_id)
        root = None if task is None else self._store.get_node(str(task.root_node_id or '').strip())
        handshake = normalize_acceptance_handshake((getattr(root, 'metadata', None) or {}).get(ACCEPTANCE_HANDSHAKE_KEY))
        logger.warning(
            'final acceptance round re-dispatched after interruption: '
            'task={} acceptance_node={} inspected_execution_node={} state={}',
            task_id,
            node_id,
            str(getattr(root, 'node_id', '') or '').strip(),
            str(handshake.get('state') or '').strip(),
        )
        result = await self._execute_node(task_id, node_id)
        await self._settle_final_acceptance_notice_result(task_id, node_id, result)
        return True

    def configure_node_dispatch_limits(self, *, execution: int | None, inspection: int | None) -> None:
        self._node_dispatch_limits = {
            'execution': _normalize_dispatch_limit(execution, default=_DEFAULT_NODE_DISPATCH_LIMITS['execution']),
            'inspection': _normalize_dispatch_limit(inspection, default=_DEFAULT_NODE_DISPATCH_LIMITS['inspection']),
        }
        # 已经活着的闸位对象要跟着改：配置刷新只重建"以后"的 dispatcher，不重建闸。
        budget = self._node_dispatch_entry_budget
        if budget is not None:
            budget.configure_entry_ceilings(
                {
                    role: int(limit)
                    for role, limit in self._node_dispatch_limits.items()
                    if limit is not None
                }
            )

    async def run_task(self, task_id: str) -> None:
        task_record = self._store.get_task(task_id)
        if task_record is None:
            return
        blocker = getattr(self, 'web_role_blocked', None)
        if callable(blocker) and blocker():
            # web 进程在 worker 在线时没有任务执行体。这里若放行，web 与 worker 会
            # 同时驱动同一棵树：web 侧新建的 dispatcher 看不到 worker 在跑的子节点，
            # 孤儿收尸器会把健康子节点暂停（2026-10-25 前门唤醒实盘）。
            logger.warning('task actor refused in web runtime role: task={}', task_id)
            return
        if self._stall_notifier is not None and hasattr(self._stall_notifier, 'start_task'):
            self._stall_notifier.start_task(task_id)
        if self.node_watchdog is not None and hasattr(self.node_watchdog, 'start_task'):
            self.node_watchdog.start_task(task_id)
        control_only_return = False
        result = NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary='task failed',
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason='task failed',
        )
        root_node = self._store.get_node(task_record.root_node_id)
        dispatcher = self._create_dispatcher(task_id)
        self._dispatchers[task_id] = dispatcher
        try:
            if root_node is None:
                result = NodeFinalResult(
                    status='failed',
                    delivery_status='blocked',
                    summary='missing root node',
                    answer='',
                    evidence=[],
                    remaining_work=[],
                    blocking_reason='missing root node',
                )
            else:
                # C：孤儿收尸/重派发——每次 worker 拾取都决断"DB 非终态但无执行器
                # 也无重放路径"的节点，杜绝幽灵 in_progress（2026-09-15 事故 L3）。
                await self._reconcile_orphan_in_progress_nodes(task_id, dispatcher)
                resumed_offroot_nodes = await self._resume_pending_notice_nodes(task_id)
                if not resumed_offroot_nodes:
                    # 通知账本里没人可派发时，再看握手：半路断掉的验收回合已经从
                    # 账本里消失，落回根节点会让被检验节点白跑一轮新回合。
                    resumed_offroot_nodes = await self._resume_inflight_final_acceptance(task_id)
                if resumed_offroot_nodes:
                    await self._drain_acceptance_verdicts(task_id)
                    await self._node_runner.wait_pending_terminal_messages()
                    resumed_result = self._terminal_result_after_notice_resume(task_id)
                    if resumed_result is not None:
                        result = resumed_result
                    result, control_only_return = await self._drive_scheduler_loop(
                        task_id=task_id,
                        dispatcher=dispatcher,
                        root_node_id=root_node.node_id,
                        current_result=result,
                    )
                    if control_only_return:
                        return
                else:
                    root_phase = self._frame_phase(task_id, root_node.node_id)
                    if root_phase in PARKED_PHASES:
                        # 根节点也是停靠节点：没有信箱唤醒源时，恢复拾取不得点火。
                        # 第一回合没有帧，仍由这里执行；停靠后的复活交给
                        # `_resume_pending_notice_nodes` / `_dispatch_runnable_nodes`
                        # 的根节点唤醒分支（它们会按 wake_on 判据决定）。
                        logger.info(
                            'root turn deferred: parked phase={} task={}',
                            root_phase,
                            task_id,
                        )
                        result = NodeFinalResult(
                            status='success',
                            delivery_status='partial',
                            summary='root is parked; waiting for a wake source',
                            answer='',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                        )
                    else:
                        result = await dispatcher.execute_node(task_id, root_node.node_id)
                        if self._is_final_submission_result(result):
                            result = await self._run_final_acceptance_if_needed(task_id)
                    # 根回合（或最终验收）结束后，子节点与验收节点由派发循环独立推进；
                    # 根停靠时循环返回 control_only，等下一次投递把它叫醒。
                    result, control_only_return = await self._drive_scheduler_loop(
                        task_id=task_id,
                        dispatcher=dispatcher,
                        root_node_id=root_node.node_id,
                        current_result=result,
                    )
                    if control_only_return:
                        return
        except NodePausedError as exc:
            control_only_return = True
            # 磁盘治理（P0）：pause 状态落库是表现层写入，磁盘满时失败只丢展示，
            # 不得让二次写盘异常穿透 run_task（2026-09-09 连锁暂停事故路径）。
            try:
                self._log_service.set_node_pause_state(
                    task_id,
                    exc.node_id or str(getattr(root_node, 'node_id', '') or ''),
                    pause_requested=True,
                    is_paused=True,
                )
            except Exception:
                pass
            return
        except TaskPausedError:
            try:
                self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            except Exception:
                pass
            return
        except asyncio.CancelledError:
            latest = self._store.get_task(task_id)
            if latest is not None and bool(latest.pause_requested) and not bool(latest.cancel_requested):
                try:
                    self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
                except Exception:
                    pass
                return
            if latest is None or not bool(latest.cancel_requested):
                # 引擎级中断（worker 进程退出收尾、进程内杂散取消），不是用户
                # 取消：绝不落 failed/canceled 终态——任务保持 in_progress，交给
                # 下一个 worker 启动的 _recover_interrupted_task 恢复重排；进程
                # 仍活着时尽力即时重排（2026-09-16 task:eacd0f0467b7 事故：
                # 有序退出把任务终态化成 canceled，抢先于启动恢复，任务永久丢失）。
                # 用户取消的终态由 cancel_task 服务路径与 cancel_requested 分支保证。
                self._requeue_interrupted_task_best_effort(task_id)
                control_only_return = True
                return
            result = NodeFinalResult(
                status='failed',
                delivery_status='blocked',
                summary='canceled',
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason='canceled',
            )
        except Exception as exc:
            text = describe_exception(exc)
            result = NodeFinalResult(
                status='failed',
                delivery_status='blocked',
                summary=text,
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason=text,
            )
        finally:
            # getattr 防御：轻量 stub（object.__new__）不经过 __init__。
            sweeps = getattr(self, '_release_sweeps', None)
            if sweeps is not None:
                sweep = sweeps.pop(task_id, None)
                if sweep is not None and not sweep.done():
                    sweep.cancel()
            await dispatcher.close()
            self._dispatchers.pop(task_id, None)
            if self.node_watchdog is not None and hasattr(self.node_watchdog, 'cancel_task'):
                self.node_watchdog.cancel_task(task_id)
            latest = self._store.get_task(task_id)
            root = self._store.get_node(latest.root_node_id) if latest is not None else None
            if (
                latest is not None
                and not latest.is_paused
                and not control_only_return
                and result.status in {'success', 'failed'}
            ):
                if root is not None and root.status == 'in_progress':
                    self._log_service.update_node_status(
                        task_id,
                        latest.root_node_id,
                        status=result.status,
                        final_output=result.output,
                        failure_reason='' if result.status == 'success' else result.failure_text,
                    )
                else:
                    self._log_service.refresh_task_view(task_id, mark_unread=True)
            latest = self._store.get_task(task_id)
            if self._stall_notifier is not None and latest is not None:
                if bool(getattr(latest, 'is_paused', False)) or bool(getattr(latest, 'pause_requested', False)):
                    self._stall_notifier.pause_task(task_id)
                elif str(getattr(latest, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                    self._stall_notifier.terminal_task(latest)

    async def _drain_acceptance_verdicts(self, task_id: str) -> bool:
        """验收节点终局后，把裁决落到被验执行节点上（调度闸门模型的收口）。

        旧模型里 `_run_child_pipeline` 在同一个协程里依次驱动执行与验收，裁决的落账
        是它的私有步骤；拆掉那条循环后，验收节点的回合由调度器独立跑完，必须由这里
        把"验收节点已终局 + 握手仍在等裁决"这个持久组合翻译成下一步动作：

        * 通过 → 给被验执行节点签发成功终态（会向父节点发 `child_final`）；
        * 普通驳回 → 已有逻辑把双方复活、写回反馈；下一轮由可派发扫描重新拾起；
        * 不可修复驳回 → 给被验执行节点签发失败终态。

        只按持久状态判定，因此**幂等**：裁定落账后握手不再是"待裁决"，重复扫描是空转。
        这是 R13 的收口点——漏掉它，子节点提交后会永久停在 `waiting_acceptance`。
        """
        task = self._store.get_task(task_id)
        if task is None:
            return False
        progressed = False
        pending_states = {
            ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
        }
        for node in list(self._store.list_nodes(task_id) or []):
            if str(getattr(node, 'node_kind', '') or '').strip().lower() != KIND_ACCEPTANCE:
                continue
            node_terminal = str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}
            persisted_verdict = normalize_result_payload((node.metadata or {}).get('result_payload'))
            if not node_terminal and persisted_verdict is None:
                continue
            execution = self._node_runner._accepted_execution_node(task_id=task_id, acceptance=node)
            if execution is None:
                continue
            handshake = normalize_acceptance_handshake(
                (execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
            )
            handshake_state = str(handshake.get('state') or '').strip()
            named_node_id = str(handshake.get('acceptance_node_id') or '').strip()
            if named_node_id and named_node_id != str(node.node_id or '').strip():
                continue
            if not node_terminal:
                # 裁决已落盘、节点行还没收口（pass 后被重放回路重置，或进程在
                # “写结果→落终态”之间被杀）：只认绑定当前提交的裁决，陈旧载荷不得
                # 被当成本次裁定。
                persisted_submission_id = str(getattr(persisted_verdict, 'submission_id', '') or '').strip()
                handshake_submission_id = str(handshake.get('latest_execution_submission_id') or '').strip()
                if (
                    persisted_submission_id
                    and handshake_submission_id
                    and persisted_submission_id != handshake_submission_id
                ):
                    continue
                if handshake_state not in pending_states:
                    continue
                result = persisted_verdict
                handled = await self._node_runner._handle_acceptance_node_result(
                    task=task,
                    acceptance=node,
                    result=result,
                )
                progressed = True
                if str(getattr(handled, 'verdict', '') or '').strip() == 'reject_no_progress':
                    continue
                handled_status = str(getattr(handled, 'status', '') or '').strip().lower()
                handled_delivery = str(getattr(handled, 'delivery_status', '') or '').strip().lower()
                if handled_status == 'success' and handled_delivery == 'final':
                    self._node_runner.finalize_inspected_execution_node(
                        task_id=task_id, node_id=execution.node_id
                    )
                continue
            execution_terminal = str(getattr(execution, 'status', '') or '').strip().lower() in {
                'success',
                'failed',
            }
            if handshake_state not in pending_states:
                # 旧管线（或本函数的上一次运行）已经落过账，只差把执行节点行收尾：
                # 握手 accepted / rejected_terminal 且执行节点仍非终态，是旧模型把
                # "结果"记在 spawn entry 上、节点行没动留下的形态。补上终态才会
                # 触发 child_final，也才会让父节点的批次完成判据满足。
                if execution_terminal:
                    continue
                if bool(getattr(execution, 'is_paused', False)) or bool(
                    getattr(execution, 'pause_requested', False)
                ):
                    # 异常暂停（reject_no_progress 或看门狗）是人工裁决面：不许在这里
                    # 被 "补终态" 静默改写成 failed。
                    continue
                if handshake_state == ACCEPTANCE_STATE_ACCEPTED and str(node.status or '').strip().lower() == 'success':
                    self._node_runner.finalize_inspected_execution_node(
                        task_id=task_id, node_id=execution.node_id
                    )
                    progressed = True
                elif handshake_state == ACCEPTANCE_STATE_REJECTED_TERMINAL and str(node.status or '').strip().lower() == 'failed':
                    self._node_runner.finalize_inspected_execution_node(
                        task_id=task_id,
                        node_id=execution.node_id,
                        failure_reason=str(node.failure_reason or 'acceptance rejected without retry').strip()
                        or 'acceptance rejected without retry',
                    )
                    progressed = True
                continue
            result = self._node_runner._result_from_record(node)
            handled = await self._node_runner._handle_acceptance_node_result(
                task=task,
                acceptance=node,
                result=result,
            )
            progressed = True
            if str(getattr(handled, 'verdict', '') or '').strip() == 'reject_no_progress':
                # 被验节点已在裁决处理里转暂停·abnormal；这里不能再补终态。
                continue
            handled_status = str(getattr(handled, 'status', '') or '').strip().lower()
            handled_delivery = str(getattr(handled, 'delivery_status', '') or '').strip().lower()
            if handled_status == 'success' and handled_delivery == 'final':
                self._node_runner.finalize_inspected_execution_node(
                    task_id=task_id, node_id=execution.node_id
                )
            elif handled_status == 'failed' and handled_delivery == 'blocked':
                self._node_runner.finalize_inspected_execution_node(
                    task_id=task_id,
                    node_id=execution.node_id,
                    failure_reason=str(
                        getattr(handled, 'blocking_reason', '') or getattr(handled, 'summary', '') or ''
                    ).strip()
                    or 'acceptance rejected without retry',
                )
        return progressed

    def schedule_node_nowait(self, task_id: str, node_id: str) -> bool:
        """NodeRunner 的子节点立即派发口。没有活跃 dispatcher 时返回 False，
        由下一次 `run_task` 的可派发扫描兜底。"""
        dispatcher = self._dispatchers.get(str(task_id or '').strip())
        if dispatcher is None:
            return False
        dispatcher.schedule_node(str(node_id or '').strip())
        return True

    def _frame_phase(self, task_id: str, node_id: str) -> str:
        try:
            frame = self._log_service.read_runtime_frame(task_id, node_id)
        except Exception:
            return ''
        return str((frame or {}).get('phase') or '') if isinstance(frame, dict) else ''

    @staticmethod
    def _is_final_submission_result(result: NodeFinalResult) -> bool:
        """只有真实提交（success + final）才允许进入最终验收。

        `enter_wait` 等控制回合也返回 `success`，但 `delivery_status='partial'`；
        把它们当成提交会凭空创建一颗最终验收节点。
        """
        return (
            str(getattr(result, 'status', '') or '').strip().lower() == 'success'
            and str(getattr(result, 'delivery_status', '') or '').strip().lower() == 'final'
        )

    def _unread_rows(self, task_id: str, node_id: str) -> list[Any]:
        try:
            return [
                item
                for item in list(self._store.list_task_node_notifications(task_id, node_id) or [])
                if str(item.status or '').strip() == 'delivered'
                and not notification_is_held(item)
            ]
        except Exception:
            return []

    def _node_has_unread(self, task_id: str, node_id: str) -> bool:
        return bool(self._unread_rows(task_id, node_id))

    def _wake_policy_allows_dispatch(
        self,
        *,
        task_id: str,
        node: Any,
        unread_rows: list[Any],
        records: list[Any],
    ) -> bool:
        """停靠中的节点只要有可注入未读就复派；批次等待不再参与停靠判据。"""
        _ = task_id, node, records
        return bool(unread_rows)

    async def _dispatch_runnable_nodes(self, task_id: str, dispatcher: 'TaskNodeDispatcher') -> bool:
        """把"现在就该跑、却没人跑"的节点交给调度器。

        判据只看持久状态，不看"上一次是不是刚跑过"：

        * 执行节点：信箱有未读，或**还没有任何运行帧**（刚创建、第一次起跑）。
          有帧且无未读 = 停靠中（等待消息/等验收），再派发只会让它空转一回合。
          停靠中的节点只要有可注入未读就复派；批次 held 行不计入未读。
        * 验收节点：握手处于待裁决且登记了可解析的提交引用；或有未读消息。
          被验节点已终态时不派发（没有可检验的对象；T15 的联动由 P4 收口）。

        根节点不在本道：它的第一回合由 `run_task` 显式驱动，之后的复活走唤醒路径。
        """
        task = self._store.get_task(task_id)
        if task is None:
            return False
        root_node_id = str(getattr(task, 'root_node_id', '') or '').strip()
        dispatched = False
        records = list(self._store.list_nodes(task_id) or [])
        for node in records:
            node_id = str(getattr(node, 'node_id', '') or '').strip()
            if not node_id:
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            if bool(getattr(node, 'is_paused', False)) or bool(getattr(node, 'pause_requested', False)):
                continue
            if dispatcher.node_is_being_driven(node_id) is True:
                continue
            unread_rows = self._unread_rows(task_id, node_id)
            unread = bool(unread_rows)
            if node_id == root_node_id:
                # 根的第一回合由 run_task 显式驱动；这里只处理"根停靠着、信箱里有未读"
                # 的复活（投递时唤醒可能已失败，或消息是在进程重启前落箱的）。没有
                # 未读就不能重派：那会让一个等待中的根空转一回合。
                if not unread:
                    continue
                if not self._wake_policy_allows_dispatch(
                    task_id=task_id, node=node, unread_rows=unread_rows, records=records
                ):
                    continue
                dispatcher.schedule_node(node_id)
                dispatched = True
                continue
            if str(getattr(node, 'node_kind', '') or '').strip().lower() == KIND_ACCEPTANCE:
                execution = self._node_runner._accepted_execution_node(task_id=task_id, acceptance=node)
                if execution is None:
                    continue
                if str(getattr(execution, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                    continue
                if bool(getattr(execution, 'is_paused', False)) or bool(
                    getattr(execution, 'pause_requested', False)
                ):
                    # 被验节点处于暂停（看门狗/人工）：它这一轮没有结束，验收不得先跑。
                    # 实盘 task:a0d1bd9bc9fa：执行节点 14:42:52 被判停后，验收仍在跑。
                    continue
                if dispatcher.node_is_being_driven(execution.node_id) is True:
                    # 被验节点此刻在跑（可能被别的消息重新激活）：派发验收只会去审一份
                    # 它正在改写中的旧提交。等它停靠（waiting_acceptance）或再次提交
                    # （握手 ref 变化）后再派。
                    continue
                handshake = normalize_acceptance_handshake(
                    (execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
                )
                pending = (
                    str(handshake.get('state') or '').strip() == ACCEPTANCE_STATE_WAITING_ACCEPTANCE
                    and bool(str(handshake.get('latest_execution_result_ref') or '').strip())
                )
                if not pending and not unread:
                    continue
                dispatcher.schedule_node(node_id)
                dispatched = True
                continue
            if not unread:
                # 已提交、正等验收裁决的节点不重派：收尾回合会删掉运行帧，
                # “无帧”不等于“没起跑过”；再派一次只会在等裁决期间重复提交
                # （实盘：同一子节点 11 秒内三次交付，把刚通过的验收反复重开）。
                handshake = normalize_acceptance_handshake(
                    (node.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
                )
                if str(handshake.get('state') or '').strip() == ACCEPTANCE_STATE_WAITING_ACCEPTANCE:
                    continue
                try:
                    frame = self._log_service.read_runtime_frame(task_id, node_id)
                except Exception:
                    frame = None
                if frame is not None:
                    continue
            if unread and not self._wake_policy_allows_dispatch(
                task_id=task_id, node=node, unread_rows=unread_rows, records=records
            ):
                continue
            dispatcher.schedule_node(node_id)
            dispatched = True
        return dispatched

    def _consume_root_entry_pause(self, dispatcher: 'TaskNodeDispatcher', root_node_id: str) -> bool:
        """后起根回合被暂停时，异常留在没人 await 的 future 上——这里取走并报告。"""
        entry = dispatcher._entries.get(str(root_node_id or '').strip())
        if entry is None or not entry.future.done() or entry.future.cancelled():
            return False
        exc = entry.future.exception()
        return isinstance(exc, (NodePausedError, TaskPausedError))

    def _fail_remaining_nodes(self, task_id: str, dispatcher: 'TaskNodeDispatcher', reason: str) -> None:
        """任务已终局/取消：非根非终态节点不再有消费者，落成可解释的终态。

        T15：验收节点不按“残留节点”处理，而是随被验收节点终局、状态一致。
        """
        task = self._store.get_task(task_id)
        root_node_id = str(getattr(task, 'root_node_id', '') or '').strip() if task is not None else ''
        for node in list(self._store.list_nodes(task_id) or []):
            node_id = str(getattr(node, 'node_id', '') or '').strip()
            if not node_id or node_id == root_node_id:
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            if str(getattr(node, 'node_kind', '') or '').strip().lower() == KIND_ACCEPTANCE:
                execution = self._node_runner._accepted_execution_node(task_id=task_id, acceptance=node)
                execution_status = (
                    str(getattr(execution, 'status', '') or '').strip().lower() if execution is not None else ''
                )
                if execution_status in {'success', 'failed'}:
                    try:
                        self._log_service.update_node_status(
                            task_id,
                            node_id,
                            status=execution_status,
                            final_output=(
                                '随被验收节点终局（T15）：被验收节点已完成。'
                                if execution_status == 'success'
                                else '随被验收节点终局（T15）：被验收节点已失败。'
                            ),
                            failure_reason='' if execution_status == 'success' else '被验收节点已失败',
                        )
                        current = normalize_acceptance_handshake(
                            (execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
                        )
                        self._node_runner._update_execution_acceptance_handshake(
                            node_id=execution.node_id,
                            state=(
                                ACCEPTANCE_STATE_ACCEPTED
                                if execution_status == 'success'
                                else ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE
                            ),
                            acceptance_node_id=node_id,
                            rejection_count=int(current.get('rejection_count') or 0),
                            latest_execution_result_ref=str(current.get('latest_execution_result_ref') or ''),
                            latest_execution_result_summary=str(current.get('latest_execution_result_summary') or ''),
                            latest_rejection_feedback_ref=str(current.get('latest_rejection_feedback_ref') or ''),
                            latest_rejection_feedback_summary=str(current.get('latest_rejection_feedback_summary') or ''),
                        )
                    except Exception:
                        logger.exception('failed to settle acceptance node: task={} node={}', task_id, node_id)
                    continue
            try:
                self._node_runner.fail_paused_node(
                    task_id, node_id, reason, notify_parent=False
                )
            except Exception:
                logger.exception('failed to settle leftover node: task={} node={}', task_id, node_id)

    async def _break_wait_cycles(self, task_id: str) -> bool:
        """等待图环检测（P6 兜底）：命中则强制唤醒环里最靠近根的节点。

        判据只读持久事实：节点行 + 帧相位 + 握手状态。停靠节点可被任意消息唤醒，
        不参与成环；终态节点会发出终局消息，也不参与。
        """
        task = self._store.get_task(task_id)
        if task is None:
            return False
        records = list(self._store.list_nodes(task_id) or [])
        frames: dict[str, dict[str, Any]] = {}
        for record in records:
            node_id = str(getattr(record, 'node_id', '') or '').strip()
            if not node_id:
                continue
            try:
                payload_reader = getattr(self._log_service, 'read_runtime_frame_payload', None)
                frame = payload_reader(task_id, node_id) if callable(payload_reader) else None
                if frame is None:
                    frame = self._log_service.read_runtime_frame(task_id, node_id)
            except Exception:
                frame = None
            if isinstance(frame, dict):
                frames[node_id] = dict(frame)
        nodes = build_wait_nodes(
            records=records,
            frames=frames,
            handshake_lookup=lambda record: normalize_acceptance_handshake(
                (getattr(record, 'metadata', None) or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
                if isinstance(getattr(record, 'metadata', None), dict)
                else {}
            ),
        )
        cycles = find_wait_cycles(nodes)
        if not cycles:
            return False
        broke = False
        for cycle in cycles:
            node_id = str(cycle[0] or '').strip()
            if not node_id:
                continue
            reason = (
                '【运行时等待图检测】检测到等待成环：'
                + ' → '.join(cycle)
                + '。已强制唤醒本节点以打破死锁；请根据当前事实决定继续推进、调整要求还是终止这条分支。'
            )
            try:
                await self.resume_node_entry(task_id, node_id)
            except Exception:
                logger.exception('wait-cycle resume failed: task={} node={}', task_id, node_id)
            notifier = self.wait_cycle_notifier
            if callable(notifier):
                try:
                    notifier(task_id=task_id, node_id=node_id, reason=reason)
                except Exception:
                    logger.exception('wait-cycle notice failed: task={} node={}', task_id, node_id)
            try:
                logger.warning('wait-for cycle broken: task={} cycle={}', task_id, cycle)
            except Exception:
                pass
            broke = True
        return broke

    async def _drive_scheduler_loop(
        self,
        *,
        task_id: str,
        dispatcher: 'TaskNodeDispatcher',
        root_node_id: str,
        current_result: NodeFinalResult,
    ) -> tuple[NodeFinalResult, bool]:
        """任务演员的派发循环：跑到静止，再决定"继续等消息"还是"任务终局"。

        返回 `(result, control_only)`。`control_only=True` 表示根节点仍非终态且已无可
        派发的工作——它停靠在信箱上，下一次投递会重新入队/直接复活它的执行器。

        循环顺序是固定的：
        1. 等终局消息投递完（父节点的唤醒由投递完成触发）；
        2. 收验收裁决（可能复活执行节点或给执行节点签发终态）；
        3. 把"该跑却没人在跑"的节点交给调度器（含刚创建的子节点）；
        4. 有活执行器就等一次现场变化；全都静止再看根节点与任务状态。
        """
        task_snapshot = None
        while True:
            await self._node_runner.wait_pending_terminal_messages()
            # 非阻塞派生的轮次完成度从节点行派生：没有父协程的 gather 来宣布它。
            self._node_runner.reconcile_spawn_round_completion(task_id)
            if await self._drain_acceptance_verdicts(task_id):
                continue
            if await self._dispatch_runnable_nodes(task_id, dispatcher):
                continue
            # 只剩停靠节点时哨一次等待图：成环就强制唤醒最靠近根的节点。
            if await self._break_wait_cycles(task_id):
                continue
            if dispatcher.has_live_entries():
                await dispatcher.wait_activity()
                continue
            # 静止一拍：让最后排定的唤醒/投递任务先跑，避免把"消息还在路上"当成无事可做。
            await asyncio.sleep(0)
            if dispatcher.has_live_entries() or self._node_runner.has_pending_terminal_messages():
                continue
            task_snapshot = self._store.get_task(task_id)
            root = self._store.get_node(root_node_id)
            if task_snapshot is None or root is None:
                return current_result, False
            if bool(getattr(task_snapshot, 'cancel_requested', False)):
                self._fail_remaining_nodes(task_id, dispatcher, 'canceled')
                return (
                    NodeFinalResult(
                        status='failed',
                        delivery_status='blocked',
                        summary='canceled',
                        answer='',
                        evidence=[],
                        remaining_work=[],
                        blocking_reason='canceled',
                    ),
                    False,
                )
            if bool(getattr(task_snapshot, 'is_paused', False)) or bool(
                getattr(task_snapshot, 'pause_requested', False)
            ):
                return current_result, True
            if str(getattr(root, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                self._fail_remaining_nodes(task_id, dispatcher, 'parent task terminal')
                # 根在同一轮里刚被最终验收收口时，`current_result` 带着验收摘要与
                # check_result；只有它和节点行状态一致才沿用它，否则以库行为准。
                root_status = str(getattr(root, 'status', '') or '').strip().lower()
                if (
                    str(getattr(current_result, 'status', '') or '').strip().lower() == root_status
                    and str(getattr(current_result, 'delivery_status', '') or '').strip() == 'final'
                ):
                    return current_result, False
                return self._result_from_node(root), False
            if self._consume_root_entry_pause(dispatcher, root_node_id):
                return current_result, True
            return current_result, True

    async def _reconcile_orphan_in_progress_nodes(self, task_id: str, dispatcher: 'TaskNodeDispatcher') -> None:
        """C：孤儿收尸/重派发（每次 run_task 拾取时执行，幂等）。

        不变式：DB 里的 in_progress 执行节点必须对应"在跑/即将被派发"、"可由根重放
        恢复"，或"已可见地停等、等人裁定"，否则就是幽灵态——本道把它停成人能看见的
        形状，绝不把一次读不出来宣成节点失败（2026-09-15 孤儿子节点事故 L3 的原始
        诉求就是消灭幽灵态，消灭它靠让它可见，不靠替它签终态）。

        范围：只决断派生树内的 execution 节点（metadata 带 spawn_owner_kind='child'
        且 spawn_owner_round_id 非空）；acceptance 节点的生命周期由验收握手与
        `_resume_pending_notice_nodes`/最终验收机制管理，不在此收尸。
        """
        task = self._store.get_task(task_id)
        if task is None:
            return
        root_node_id = str(getattr(task, 'root_node_id', '') or '').strip()
        try:
            records = list(self._store.list_nodes(task_id) or [])
        except Exception:
            return
        for record in records:
            node_id = str(getattr(record, 'node_id', '') or '').strip()
            if not node_id or node_id == root_node_id:
                continue
            if str(getattr(record, 'node_kind', '') or '').strip().lower() != 'execution':
                continue
            if str(getattr(record, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            node = self._store.get_node(node_id)
            if node is None:
                continue
            metadata = dict(getattr(node, 'metadata', None) or {}) if isinstance(getattr(node, 'metadata', None), dict) else {}
            if str(metadata.get('spawn_owner_kind') or '').strip().lower() != 'child':
                continue
            if not str(metadata.get('spawn_owner_round_id') or '').strip():
                # 未挂派生轮的节点（非常规形态）不武断收尸，交由父管线/失速监控。
                continue
            if self._node_operator_paused(node):
                continue
            # 非阻塞派生下，"in_progress 且没有执行器"有四种正常形态，收尸器不能
            # 把其中任何一种读成幽灵：
            #   * 停靠中（waiting_explicit / waiting_acceptance）——等消息，唤醒源到达即继续；
            #   * 信箱有未读——复活/派发由可派发扫描负责；
            #   * 还没有运行帧——刚创建、尚未起跑；
            #   * 正在被本进程驱动。
            # 只有"有帧、不在停靠态、没有未读、也没有执行器"才是真正说不清去向的形态。
            phase = self._frame_phase(task_id, node_id)
            if phase in {'waiting_explicit', 'waiting_acceptance'}:
                continue
            if self._node_has_unread(task_id, node_id):
                continue
            try:
                frame_exists = self._log_service.read_runtime_frame(task_id, node_id) is not None
            except Exception:
                frame_exists = True  # 读不到就按"有帧"处理，宁可上报也不忽略
            if not frame_exists:
                continue
            if dispatcher.node_is_being_driven(node_id) is True:
                continue  # 已有活执行器（问持有者，不问投影）
            if self._orphan_recovery_chain_reachable(
                task_id=task_id,
                node=node,
                root_node_id=root_node_id,
                dispatcher=dispatcher,
            ):
                try:
                    logger.warning(
                        'orphan node re-dispatched at task pickup (recoverable via parent replay): '
                        'task={} node={}',
                        task_id,
                        node_id,
                    )
                except Exception:
                    pass
                await self.resume_node_entry(task_id, node_id)
                continue
            self._report_orphan_candidate(
                task_id=task_id,
                node=node,
                node_id=node_id,
            )

    def _report_orphan_candidate(self, *, task_id: str, node, node_id: str) -> None:
        """没人驱动、又没有重放路径 ⇒ 可见地暂停并请人裁定，**不**由收尸器签发终态。

        三载体合取里任何一环都可能只是"读不到"：父节点正常 success 之后它的帧本就是被
        `_mark_finished` 删掉的（那是设计态），而父行读到 None 也不等于父已经结束。用一条
        读不出来的合取去 `fail_paused_node` 是不可逆的——2026-10-15 就是这条把 6 个仍在跑的
        节点判死了，其中四个属于唯一还没交付分片的那一支。

        暂停是可逆的：`resume_node` 能救回来，看完现场的会话 agent／操作者也能用它本来就
        有的出口（`manage_task_nodes` 的 fail 要求节点先处于暂停态）把这一批落成 `canceled`。
        错误行带 `pause_reason='error'`，正好是 `task_node_error` 心跳车道的选择条件，
        所以这里不需要新通报机制。
        """
        reason = (
            'spawn_orphan_candidate: in_progress without executor or replay path — '
            '本进程没有它的执行体，父侧也找不到重放路径；这不是"节点失败了"，'
            '需要核对它到底在别处跑着还是已经停了，再决定 resume 还是落 canceled。'
        )
        try:
            logger.warning('orphan node parked as candidate: task={} node={}', task_id, node_id)
        except Exception:
            pass
        try:
            self._log_service.append_task_error_log(
                task_id=task_id,
                node_id=node_id,
                node_title=str(getattr(node, 'title', '') or getattr(node, 'goal', '') or ''),
                error_text=reason,
            )
        except Exception:
            logger.exception('orphan candidate error log failed: task={} node={}', task_id, node_id)
            return
        try:
            self._node_runner._mark_node_paused(task_id, node_id, reason='error', remark=reason)
        except Exception:
            logger.exception('orphan candidate park failed: task={} node={}', task_id, node_id)

    def _orphan_recovery_chain_reachable(
        self,
        *,
        task_id: str,
        node,
        root_node_id: str,
        dispatcher: 'TaskNodeDispatcher',
    ) -> bool:
        """孤儿可恢复 ⟺ 向上逐跳：父非终态、父的最新未完成 spawn 轮仍绑定该节点、
        且父帧保留该轮重放意图（waiting_children / pending_tool_calls 含轮 id），
        一路抵达根（run_task 即将派发）或某个活 entry。"""
        current = node
        seen: set[str] = set()
        while True:
            node_id = str(getattr(current, 'node_id', '') or '').strip()
            if not node_id or node_id in seen:
                return False
            seen.add(node_id)
            parent_id = str(getattr(current, 'parent_node_id', '') or '').strip()
            if not parent_id:
                return False
            parent = self._store.get_node(parent_id)
            if parent is None:
                return False
            if str(getattr(parent, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                return False  # 父已终态，不会再重放
            round_id = self._parent_round_binding_node(parent=parent, node_id=node_id)
            if not round_id:
                return False  # 父的最新未完成轮已不绑定该节点
            parent_entry = dispatcher._entries.get(parent_id)
            if parent_entry is not None and parent_entry.task is not None and not parent_entry.task.done():
                return True  # 父有活执行器，重放进行中
            if not self._parent_frame_intends_round_replay(task_id=task_id, parent=parent, round_id=round_id):
                return False
            if parent_id == root_node_id:
                return True  # 根即将被 run_task 派发且保留重放意图
            current = parent

    def _parent_round_binding_node(self, *, parent, node_id: str) -> str:
        """父节点最新未完成 spawn 轮若经 child/acceptance 绑定该节点，返回轮 id。"""
        try:
            latest = self._node_runner._latest_incomplete_spawn_round(parent=parent)
        except Exception:
            return ''
        if not latest:
            return ''
        round_id, payload = latest
        for item in list((payload or {}).get('entries') or []):
            if not isinstance(item, dict):
                continue
            for field in ('child_node_id', 'acceptance_node_id'):
                if str(item.get(field) or '').strip() == str(node_id or '').strip():
                    return str(round_id or '').strip()
        return ''

    def _parent_frame_intends_round_replay(self, *, task_id: str, parent, round_id: str) -> bool:
        """父帧是否保留对指定 spawn 轮的重放意图（释放/恢复后会同 id 重放）。"""
        normalized_round_id = str(round_id or '').strip()
        if not normalized_round_id:
            return False
        frame: dict[str, Any] = {}
        getter = getattr(self._log_service, 'read_runtime_frame', None)
        if callable(getter):
            try:
                frame = dict(getter(task_id, str(getattr(parent, 'node_id', '') or '').strip()) or {})
            except Exception:
                frame = {}
        if str(frame.get('phase') or '').strip() == 'waiting_children':
            return True
        for item in list(frame.get('pending_tool_calls') or []):
            if not isinstance(item, dict):
                continue
            candidate = str(item.get('id') or item.get('tool_call_id') or '').strip()
            if candidate and candidate == normalized_round_id:
                return True
        for item in list(frame.get('active_round_tool_call_ids') or []):
            if str(item or '').strip() == normalized_round_id:
                return True
        return False

    def request_cancel(self, task_id: str) -> None:
        self._log_service.request_cancel(task_id)
        if self._stall_notifier is not None and hasattr(self._stall_notifier, 'cancel_requested'):
            self._stall_notifier.cancel_requested(task_id)

    def request_pause(self, task_id: str) -> None:
        self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
        if self._stall_notifier is not None and hasattr(self._stall_notifier, 'pause_task'):
            self._stall_notifier.pause_task(task_id)

    def clear_pause(self, task_id: str) -> None:
        self._log_service.set_pause_state(task_id, pause_requested=False, is_paused=False)
        if self._stall_notifier is not None and hasattr(self._stall_notifier, 'reset_visible_output'):
            self._stall_notifier.reset_visible_output(task_id)

    def _requeue_interrupted_task_best_effort(self, task_id: str) -> None:
        """引擎级中断后的尽力即时重排（进程仍活着时避免任务悬空）。

        钩子由 MainRuntimeService 注入；进程退出收尾中调度器即将关闭，
        重排自然无效并由下一个 worker 的启动恢复兜底，因此这里绝不抛错。
        """
        callback = self.interrupted_task_requeue_callback
        if not callable(callback):
            return
        try:
            callback(task_id)
        except Exception:
            try:
                logger.debug('interrupted task requeue skipped: task={}', task_id)
            except Exception:
                pass

    async def _execute_nested_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        dispatcher = self._dispatchers.get(str(task_id or '').strip())
        if dispatcher is not None:
            return await dispatcher.execute_node(task_id, node_id)
        return await self._node_runner.run_node(task_id, node_id)

    def _node_is_being_driven(self, task_id: str, node_id: str) -> bool | None:
        """注入给 NodeRunner 的活性探针；没有 dispatcher 就说不出结论（返回 None）。"""
        dispatcher = self._dispatchers.get(str(task_id or '').strip())
        if dispatcher is None:
            return None
        return dispatcher.node_is_being_driven(node_id)

    async def _cancel_node_subtree(self, task_id: str, node_ids: list[str], reason: str = '') -> set[str]:
        """注入给 NodeRunner 的取消执行体；返回本进程实际持有的节点 id。

        没有 dispatcher（任务不在本进程跑）时返回空集——那是「不知道有没有人在驱动」，
        调用方必须按未持有处理：标记上报，绝不就地落终态。
        """
        dispatcher = self._dispatchers.get(str(task_id or '').strip())
        if dispatcher is None:
            return set()
        return await dispatcher.cancel_nodes(node_ids, reason)

    async def _execute_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        return await self._execute_nested_node(task_id, node_id)

    def _create_dispatcher(self, task_id: str) -> TaskNodeDispatcher:
        return TaskNodeDispatcher(
            task_id=task_id,
            store=self._store,
            log_service=self._log_service,
            node_runner=self._node_runner,
            execution_limit=self._node_dispatch_limits['execution'],
            inspection_limit=self._node_dispatch_limits['inspection'],
            entry_budget=self._node_dispatch_entry_budget,
        )

    @staticmethod
    def _node_operator_paused(node) -> bool:
        return bool(getattr(node, 'pause_requested', False)) or bool(getattr(node, 'is_paused', False))

    def _reset_stall_clock(self, task_id: str) -> None:
        notifier = self._stall_notifier
        if notifier is not None and hasattr(notifier, 'reset_visible_output'):
            try:
                notifier.reset_visible_output(task_id)
            except Exception:
                pass

    async def _revive_dispatch_entry_for_resume(
        self,
        task_id: str,
        dispatcher: 'TaskNodeDispatcher',
        node_id: str,
    ) -> str:
        """把单个已登记 entry 恢复到"有活执行器"，并回报现场判读。

        返回 `resumed` / `deferred` / `missing` / `skipped:<reason>`。分发释放与
        resume 命令两条道共用这一份判读，避免命令道把"什么都没重启"报成成功
        （2026-09-22 node:abaf5c7d6e11：暂停标志被清、执行器从未复活，幽灵态悬空
        82 分钟后才被孤儿收尸）。`missing` 只回报不重建，由调用方决定要不要建
        新 entry——释放道对无 entry 的节点保持不介入。
        """
        entry = dispatcher._entries.get(node_id)
        if entry is None:
            return 'missing'
        node = self._store.get_node(node_id)
        if node is None:
            return 'skipped:node_missing'
        if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
            return 'skipped:node_terminal'
        if self._node_operator_paused(node):
            # 人工暂停的节点保持暂停，不因分发释放被唤醒。
            try:
                logger.info('release skip (operator paused): task={} node={}', task_id, node_id)
            except Exception:
                pass
            return 'skipped:operator_paused'
        if entry.future.done():
            self._log_stranded_entry_future(task_id, node_id, entry)
            if entry.task is not None and not entry.task.done():
                # future 已解析但协程还在跑：不重建（防双跑），执行器仍然活着。
                try:
                    logger.warning(
                        'node resume triage deferred (future resolved while coroutine still runs): '
                        'task={} node={}',
                        task_id,
                        node_id,
                    )
                except Exception:
                    pass
                return 'deferred'
            # B5 兜底：future 被取消/异常搁浅而节点非终态——弹出残骸重建
            # entry 复活（resume_node 对已弹出节点走 _get_or_create_entry）。
            dispatcher._entries.pop(node_id, None)
            await dispatcher.resume_node(node_id)
            return 'resumed'
        await dispatcher.resume_node(node_id)
        return 'resumed'

    async def resume_node_entry(self, task_id: str, node_id: str, *, arm_verification: bool = True) -> str:
        """复活一个非终态节点的执行器入口：保证留下活执行器，并回报现场判读。

        `arm_verification=False` 供延迟校验清扫自身调用——它已在 sweep 协程里，
        再装填会把正在跑的清扫 cancel 掉。
        """
        normalized_task_id = str(task_id or '').strip()
        normalized_node_id = str(node_id or '').strip()
        dispatcher = self._dispatchers.get(normalized_task_id)
        if dispatcher is None:
            return 'no_dispatcher'
        outcome = await self._revive_dispatch_entry_for_resume(normalized_task_id, dispatcher, normalized_node_id)
        if outcome == 'missing':
            await dispatcher.resume_node(normalized_node_id)
            outcome = 'resumed'
        if arm_verification and outcome in {'resumed', 'deferred'}:
            self._schedule_release_verification(normalized_task_id, [normalized_node_id])
        try:
            logger.info(
                'node resume triage: task={} node={} outcome={}',
                normalized_task_id,
                normalized_node_id,
                outcome,
            )
        except Exception:
            pass
        return outcome

    def _log_stranded_entry_future(self, task_id: str, node_id: str, entry: _DispatchEntry) -> None:
        """A2：done future 若被取消或携带无人消费的异常，落日志（否则永久静默）。"""
        try:
            future = entry.future
            if future.cancelled():
                logger.warning(
                    'release: dispatch future was cancelled while node non-terminal (rebuilding entry): '
                    'task={} node={}',
                    task_id,
                    node_id,
                )
                return
            exc = future.exception()
            if exc is not None:
                logger.error(
                    'release: dispatch future already failed with stranded exception (rebuilding entry): '
                    'task={} node={} error={!r}',
                    task_id,
                    node_id,
                    exc,
                )
        except Exception:
            pass

    def _schedule_release_verification(self, task_id: str, node_ids: list[str]) -> None:
        """A3：释放后延迟校验（每任务替换式单飞；run_task finally 取消）。"""
        normalized = [str(item or '').strip() for item in list(node_ids or []) if str(item or '').strip()]
        if not normalized:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        previous = self._release_sweeps.pop(task_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        sweep = loop.create_task(
            self._verify_release_revival(task_id, normalized),
            name=f'task-release-verification:{task_id}',
        )
        self._release_sweeps[task_id] = sweep

        def _cleanup(completed_task: 'asyncio.Task[None]', *, tid: str = task_id) -> None:
            if self._release_sweeps.get(tid) is completed_task:
                self._release_sweeps.pop(tid, None)

        sweep.add_done_callback(_cleanup)

    async def _verify_release_revival(self, task_id: str, node_ids: list[str]) -> None:
        await asyncio.sleep(_RELEASE_VERIFICATION_DELAY_SECONDS)
        wedged = self._collect_wedged_release_nodes(task_id, node_ids)
        if not wedged:
            return
        dispatcher = self._dispatchers.get(task_id)
        if dispatcher is None:
            return
        for node_id in wedged:
            try:
                logger.warning(
                    'release verification: node still frozen after release, re-resuming once: '
                    'task={} node={}',
                    task_id,
                    node_id,
                )
            except Exception:
                pass
            await self.resume_node_entry(task_id, node_id, arm_verification=False)
        await asyncio.sleep(_RELEASE_VERIFICATION_DELAY_SECONDS)
        for node_id in self._collect_wedged_release_nodes(task_id, wedged):
            try:
                logger.error(
                    'release verification failed: node wedged after re-resume (needs attention): '
                    'task={} node={}',
                    task_id,
                    node_id,
                )
            except Exception:
                pass

    def _collect_wedged_release_nodes(self, task_id: str, node_ids: list[str]) -> list[str]:
        """卡死形态：future pending + entry task done + 非终态 + 非人工暂停。"""
        dispatcher = self._dispatchers.get(task_id)
        if dispatcher is None:
            return []
        wedged: list[str] = []
        for raw_node_id in list(node_ids or []):
            node_id = str(raw_node_id or '').strip()
            if not node_id:
                continue
            entry = dispatcher._entries.get(node_id)
            if entry is None or entry.future.done():
                continue
            if entry.task is not None and not entry.task.done():
                continue
            node = self._store.get_node(node_id)
            if node is None:
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            if self._node_operator_paused(node):
                continue
            wedged.append(node_id)
        return wedged

    async def _run_final_acceptance_if_needed(self, task_id: str) -> NodeFinalResult:
        task = self._store.get_task(task_id)
        if task is None:
            return NodeFinalResult(
                status='failed',
                delivery_status='blocked',
                summary='missing task',
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason='missing task',
            )
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        if not final_acceptance.required:
            root = self._store.get_node(task.root_node_id)
            if root is None:
                return NodeFinalResult(
                    status='failed',
                    delivery_status='blocked',
                    summary='missing root node',
                    answer='',
                    evidence=[],
                    remaining_work=[],
                    blocking_reason='missing root node',
                )
            return self._result_from_node(root)

        root = self._store.get_node(task.root_node_id)
        if root is None:
            return NodeFinalResult(
                status='failed',
                delivery_status='blocked',
                summary='missing root node',
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason='missing root node',
            )
        if str(final_acceptance.status or '').strip().lower() in {'waiting_acceptance', 'waiting_execution_retry'}:
            return self._result_from_node(root)

        # 最终验收只对真实提交开放：根仍是 in_progress（例如刚执行完 enter_wait
        # 控制回合，或恢复后尚未重新提交）时，创建/执行验收节点只会对不存在的
        # 交付下判词。真实提交落成后根状态为 success；握手 waiting_acceptance
        # 覆盖"提交已排队、节点行尚未刷新"的窗口。
        handshake = normalize_acceptance_handshake(
            (getattr(root, 'metadata', None) or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
        )
        root_status = str(getattr(root, 'status', '') or '').strip().lower()
        if root_status != 'success' and str(handshake.get('state') or '').strip() != ACCEPTANCE_STATE_WAITING_ACCEPTANCE:
            logger.info(
                'final acceptance deferred: execution node has not submitted: task={} root={} status={} handshake={}',
                task_id,
                root.node_id,
                root_status or '(empty)',
                str(handshake.get('state') or '(empty)'),
            )
            return NodeFinalResult(
                status='success',
                delivery_status='partial',
                summary='final acceptance deferred: execution node has not submitted',
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason='',
            )

        existing_acceptance_node_id = str(final_acceptance.node_id or '').strip()
        existing_acceptance = self._store.get_node(existing_acceptance_node_id) if existing_acceptance_node_id else None
        if existing_acceptance is not None:
            freeze_reason = self._node_runner._final_acceptance_freeze_reason(task=task, node=existing_acceptance)
            if freeze_reason:
                return NodeFinalResult(
                    status='success',
                    delivery_status='partial',
                    summary=freeze_reason,
                    answer='',
                    evidence=[],
                    remaining_work=[],
                    blocking_reason='',
                )

        acceptance = await self._get_or_create_final_acceptance_node(
            task_id=task_id,
            task=task,
            root=root,
            final_acceptance=final_acceptance,
        )
        acceptance_result = await self._execute_node(task_id, acceptance.node_id)
        acceptance = self._store.get_node(acceptance.node_id) or acceptance
        acceptance_result = await self._node_runner._handle_acceptance_node_result(
            task=task,
            acceptance=acceptance,
            result=acceptance_result,
        )
        if str(acceptance_result.delivery_status or '').strip() == 'partial':
            return acceptance_result
        acceptance = self._store.get_node(acceptance.node_id) or acceptance
        root = self._store.get_node(root.node_id) or root
        execution_output = str(root.final_output or '').strip()
        check_result = str(acceptance_result.summary or acceptance_result.output or acceptance.failure_reason or '').strip() or SKIPPED_CHECK_RESULT
        self._log_service.update_node_check_result(task_id, root.node_id, check_result)
        if acceptance_result.status == 'success':
            return NodeFinalResult(
                status='success',
                delivery_status='final',
                summary=check_result or execution_output or 'final acceptance passed',
                answer=execution_output,
                evidence=[],
                remaining_work=[],
                blocking_reason='',
            )

        failure_reason = acceptance_result.failure_text or check_result
        self._log_service.refresh_task_view(task_id, mark_unread=True)
        return NodeFinalResult(
            status='failed',
            delivery_status=(
                'blocked'
                if str(acceptance_result.delivery_status or '').strip().lower() == 'blocked'
                else 'final'
            ),
            summary=check_result,
            answer=execution_output,
            evidence=list(acceptance_result.evidence or []),
            remaining_work=[],
            blocking_reason=failure_reason,
        )

    async def _get_or_create_final_acceptance_node(self, *, task_id: str, task, root, final_acceptance) -> object:
        node_id = str(final_acceptance.node_id or '').strip()
        acceptance = self._store.get_node(node_id) if node_id else None
        if acceptance is None:
            # 指针丢失（恢复清洗/旧数据）时先收养现有根最终验收节点，不能直接
            # 新建第二颗；子节点验收的 `_ensure_spawn_acceptance_node` 同款顺序。
            acceptance = self._find_existing_root_final_acceptance_node(task_id=task_id, root=root)
            if acceptance is not None:
                logger.info(
                    'adopted existing root final acceptance node: task={} node={}',
                    task_id,
                    acceptance.node_id,
                )
        if acceptance is None:
            acceptance = self._node_runner.create_acceptance_node(
                task=task,
                accepted_node=root,
                goal=f'最终验收:{root.goal}',
                acceptance_prompt=final_acceptance.prompt,
                parent_node_id=root.node_id,
                metadata={'final_acceptance': True},
            )
        elif str(getattr(acceptance, 'status', '') or '').strip().lower() in {'success', 'failed'}:
            # 旧裁决可能是在本轮交付之前下的；新提交到达时作废重验。
            acceptance = self._node_runner._reset_terminal_acceptance_for_reverify(
                task=task,
                acceptance=acceptance,
            )
        self._update_final_acceptance_state(task_id, node_id=acceptance.node_id, status='running')
        return acceptance

    def _find_existing_root_final_acceptance_node(self, *, task_id: str, root) -> object | None:
        """按 `accepted_node_id=root` + `final_acceptance=true` 收养，不依赖指针。"""
        root_node_id = str(getattr(root, 'node_id', '') or '').strip()
        if not root_node_id:
            return None
        candidates = []
        for node in self._store.list_nodes(task_id) or []:
            if str(getattr(node, 'node_kind', '') or '').strip().lower() != KIND_ACCEPTANCE:
                continue
            if str(getattr(node, 'parent_node_id', '') or '').strip() != root_node_id:
                continue
            metadata = getattr(node, 'metadata', None) if isinstance(getattr(node, 'metadata', None), dict) else {}
            if str(metadata.get('accepted_node_id') or '').strip() != root_node_id:
                continue
            if not bool(metadata.get('final_acceptance')):
                continue
            candidates.append(node)
        if not candidates:
            return None

        def _sort_key(node) -> tuple[int, str, str]:
            status = str(getattr(node, 'status', '') or '').strip().lower()
            return (
                1 if status in {'success', 'failed'} else 0,
                str(getattr(node, 'created_at', '') or ''),
                str(getattr(node, 'node_id', '') or ''),
            )

        chosen = sorted(candidates, key=_sort_key)[0]
        if len(candidates) > 1:
            logger.warning(
                'multiple root final acceptance nodes; adopting one: task={} chosen={} count={}',
                task_id,
                getattr(chosen, 'node_id', ''),
                len(candidates),
            )
        return chosen

    def _update_final_acceptance_state(self, task_id: str, *, node_id: str | None = None, status: str | None = None) -> None:
        def _mutate(metadata):
            final_acceptance = normalize_final_acceptance_metadata(metadata.get('final_acceptance')).model_dump(mode='json')
            if node_id is not None:
                final_acceptance['node_id'] = str(node_id or '').strip()
            if status is not None:
                final_acceptance['status'] = str(status or final_acceptance.get('status') or 'pending').strip().lower() or 'pending'
            metadata['final_acceptance'] = final_acceptance
            return metadata

        self._log_service.update_task_metadata(task_id, _mutate, mark_unread=True)

    def _record_final_execution_output(self, task_id: str, value: str) -> None:
        execution_output = str(value or '').strip()

        def _mutate(metadata):
            if execution_output:
                metadata['final_execution_output'] = execution_output
            else:
                metadata.pop('final_execution_output', None)
            return metadata

        self._log_service.update_task_metadata(task_id, _mutate, mark_unread=False)

    @staticmethod
    def _result_from_node(node) -> NodeFinalResult:
        payload = normalize_result_payload((getattr(node, 'metadata', None) or {}).get('result_payload'))
        if payload is not None:
            return payload
        final_output = str(node.final_output or '').strip()
        failure_reason = str(node.failure_reason or '').strip()
        return NodeFinalResult(
            status='success' if str(node.status or '') == 'success' else 'failed',
            delivery_status='final' if str(node.status or '') == 'success' else 'blocked',
            summary=failure_reason or final_output or 'node finished',
            answer=final_output,
            evidence=[],
            remaining_work=[],
            blocking_reason=failure_reason if str(node.status or '') == 'failed' else '',
        )

    def _terminal_result_after_notice_resume(self, task_id: str) -> NodeFinalResult | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        root = self._store.get_node(task.root_node_id)
        if root is None:
            return NodeFinalResult(
                status='failed',
                delivery_status='blocked',
                summary='missing root node',
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason='missing root node',
            )
        root_result = self._result_from_node(root)
        root_status = str(getattr(root, 'status', '') or '').strip().lower()
        if root_status in {'success', 'failed'}:
            return root_result
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        if not bool(final_acceptance.required):
            return None
        acceptance_node_id = str(final_acceptance.node_id or '').strip()
        acceptance_status = str(final_acceptance.status or '').strip().lower()
        execution_output = str(root_result.answer or root_result.summary or '').strip()
        check_result = str(getattr(root, 'check_result', '') or '').strip()

        if acceptance_status == 'passed':
            self._reconcile_root_acceptance_handshake_after_notice_resume(
                root=root,
                acceptance_node_id=acceptance_node_id,
                state=ACCEPTANCE_STATE_ACCEPTED,
                rejection_feedback='',
                root_result=root_result,
            )
            summary = check_result or execution_output or 'final acceptance passed'
            return NodeFinalResult(
                status='success',
                delivery_status='final',
                summary=summary,
                answer=execution_output,
                evidence=list(root_result.evidence or []),
                remaining_work=[],
                blocking_reason='',
            )
        if acceptance_status == 'failed':
            handshake = normalize_acceptance_handshake((root.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY))
            if str(handshake.get('state') or '').strip() == ACCEPTANCE_STATE_REJECTED_TERMINAL:
                failure_text = (
                    str(getattr(root, 'failure_reason', '') or '').strip()
                    or str(task.failure_reason or '').strip()
                    or str(final_acceptance.prompt or '').strip()
                    or 'final acceptance rejected without retry'
                )
                return NodeFinalResult(
                    status='failed',
                    delivery_status='blocked',
                    summary=failure_text,
                    answer=execution_output,
                    evidence=[],
                    remaining_work=[],
                    blocking_reason=failure_text,
                )
            # 普通 failed+final 拒收没有次数上限：它只是打回的中转态，
            # 交还控制权让驱动层复活执行节点重跑。
            return None

        if acceptance_status == ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE:
            failure_text = (
                str(getattr(root, 'failure_reason', '') or '').strip()
                or str(task.failure_reason or '').strip()
                or str(root_result.failure_text or '').strip()
                or 'execution retry failed'
            )
            self._reconcile_root_acceptance_handshake_after_notice_resume(
                root=root,
                acceptance_node_id=acceptance_node_id,
                state=ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
                rejection_feedback=failure_text,
                root_result=root_result,
            )
            return NodeFinalResult(
                status='failed',
                delivery_status='blocked',
                summary=failure_text,
                answer=str(getattr(root, 'final_output', '') or execution_output).strip(),
                evidence=[],
                remaining_work=[],
                blocking_reason=failure_text,
            )

        return None

    def _reconcile_root_acceptance_handshake_after_notice_resume(
        self,
        *,
        root,
        acceptance_node_id: str,
        state: str,
        rejection_feedback: str,
        root_result: NodeFinalResult,
    ) -> None:
        updater = getattr(self._node_runner, '_update_execution_acceptance_handshake', None)
        if not callable(updater):
            return
        metadata = dict(getattr(root, 'metadata', {}) or {})
        handshake = dict(metadata.get('acceptance_handshake') or {})
        result_ref = (
            str(handshake.get('latest_execution_result_ref') or '').strip()
            or str(metadata.get('result_payload_ref') or '').strip()
        )
        result_summary = (
            str(handshake.get('latest_execution_result_summary') or '').strip()
            or str(root_result.summary or root_result.answer or '').strip()
        )
        updater(
            node_id=str(getattr(root, 'node_id', '') or '').strip(),
            state=str(state or '').strip(),
            acceptance_node_id=acceptance_node_id or str(handshake.get('acceptance_node_id') or '').strip(),
            rejection_count=int(handshake.get('rejection_count') or 0),
            latest_execution_result_ref=result_ref,
            latest_execution_result_summary=result_summary,
            latest_rejection_feedback_ref='',
            latest_rejection_feedback_summary=str(rejection_feedback or '').strip(),
        )
