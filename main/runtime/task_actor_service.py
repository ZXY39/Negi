from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from main.errors import DistributionHoldError, NodePausedError, TaskPausedError, describe_exception
from main.models import (
    NodeFinalResult,
    normalize_final_acceptance_metadata,
    normalize_result_payload,
)
from main.protocol import now_iso
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
    ACCEPTANCE_STATE_REJECTED_TERMINAL,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    ACCEPTANCE_STATE_WAITING_BLOCK_VERIFICATION,
    normalize_acceptance_handshake,
)
from main.runtime.append_notice_context import NOTICE_ORIGIN_SYSTEM_RELAY
from main.runtime.node_runner import SKIPPED_CHECK_RESULT
from main.runtime.pending_notice_state import RESUME_MODE_WAIT_FOR_CHILDREN
from main.runtime.subtree_hold import (
    DISTRIBUTION_ACTIVE_STATES,
    DISTRIBUTION_HOLD_STATES,
    NOTICE_ACTION_RESUME_EXECUTION,
    NOTICE_INTERRUPT_REASON,
    node_in_target_subtree,
    spawn_entry_child_fully_materialized,
)
from main.types import KIND_ACCEPTANCE

_DEFAULT_NODE_DISPATCH_LIMITS = {
    'execution': 8,
    'inspection': 4,
}
_DISTRIBUTION_BARRIER_SAFE_PHASES = {
    'before_model',
    'waiting_tool_results',
    'after_model',
    'waiting_children',
    'waiting_acceptance',
}
# 分发进行中的状态集合（hold 谓词与各门控共用，单一来源：subtree_hold；
# hold 阻塞集另含 'failed'——失败后子树保持冻结直到显式恢复降级）。
_DISTRIBUTION_ACTIVE_STATES = DISTRIBUTION_ACTIVE_STATES
# hold 阻塞态（含 failed 冻结）：孤儿收尸在该状态下整体不介入（单一来源：subtree_hold）。
_DISTRIBUTION_HOLD_STATES = DISTRIBUTION_HOLD_STATES
_DISTRIBUTION_DRIVER_POLL_SECONDS = 1.0
# 波次崩溃预算：驱动器是子树屏障唯一的释放者，静默退出＝永久冻结，所以崩溃计数
# 落库（epoch payload.wave_crash_count）并跨接管累计，超限显式 failed 交给操作员。
_DISTRIBUTION_WAVE_CRASH_RETRY_LIMIT = 2
_DISTRIBUTION_WAVE_CRASH_RETRY_SECONDS = 5.0
# drain 自愈的重复踢起冷却（秒）：踢了但没进展的轮必须能再踢，但不能逐秒重复
# resume 同一个未缓存 review 的轮。
_DRAIN_KICK_RETRY_SECONDS = 30.0
# A3：释放后校验清扫的两段延迟（秒）。第一段后仍卡死则再 resume 一次，
# 第二段后仍卡死则落 ERROR（冻结→释放的终点必须可见）。
_RELEASE_VERIFICATION_DELAY_SECONDS = 5.0
# 决策回合 action 词表（单一来源：main.runtime.subtree_hold）：驱动器据此从
# decision_records 反推「该做但没做」的副作用。
_NOTICE_ACTION_RESUME_EXECUTION = NOTICE_ACTION_RESUME_EXECUTION
# epoch payload.wave_effects 的副作用名：驱动器的执行账，与决策账分离。
_WAVE_EFFECT_ACCEPTANCE_INTERRUPT = 'acceptance_interrupt'
# 合成验收中断结果的 blocking_reason（单一来源：main.runtime.subtree_hold）。
_NOTICE_INTERRUPT_REASON = NOTICE_INTERRUPT_REASON
_CURRENT_DISPATCH_LEASE: ContextVar['_DispatchLease | None'] = ContextVar(
    'task_node_dispatch_lease',
    default=None,
)


def _normalize_dispatch_limit(value: int | None, *, default: int) -> int | None:
    if value is None:
        return None
    return max(1, int(value or default or 1))


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
    ) -> None:
        self._task_id = str(task_id or '').strip()
        self._store = store
        self._log_service = log_service
        self._node_runner = node_runner
        self._limits = {
            'execution': _normalize_dispatch_limit(execution_limit, default=_DEFAULT_NODE_DISPATCH_LIMITS['execution']),
            'inspection': _normalize_dispatch_limit(inspection_limit, default=_DEFAULT_NODE_DISPATCH_LIMITS['inspection']),
        }
        self._semaphores = {
            role: (asyncio.Semaphore(limit) if limit is not None else None)
            for role, limit in self._limits.items()
        }
        self._entries: dict[str, _DispatchEntry] = {}
        self._closed = False
        self._last_snapshot_fingerprint: tuple[tuple[str, int], ...] | None = None
        self._publish_dispatch_state(force=True)

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {
            'dispatch_limits': dict(self._limits),
            'dispatch_running': {
                role: sum(1 for entry in self._entries.values() if entry.role == role and entry.running_counted)
                for role in self._limits
            },
            'dispatch_queued': {
                role: sum(1 for entry in self._entries.values() if entry.role == role and entry.queued_counted)
                for role in self._limits
            },
        }

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

    async def cancel_nodes(self, node_ids: list[str]) -> None:
        entries: list[_DispatchEntry] = []
        seen: set[str] = set()
        for raw_node_id in list(node_ids or []):
            node_id = str(raw_node_id or '').strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            entry = self._entries.get(node_id)
            if entry is None:
                continue
            entries.append(entry)
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
            elif not entry.future.done():
                try:
                    entry.future.set_result(self._node_runner.fail_paused_node(self._task_id, node_id, 'canceled'))
                except Exception:
                    entry.future.set_exception(asyncio.CancelledError())
        # 先等被取消的协程真停：run_node 的 CancelledError 在活动 hold 下会转
        # DistributionHoldError 冻结（future 保持 pending），直接 shield-await
        # future 将永远等不到解析——supersede-kill 会死锁（B3 配套修复）。
        running_tasks = [entry.task for entry in entries if entry.task is not None and not entry.task.done()]
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)
        # 协程停稳后仍未解析的 future 强制落终态，保持取消语义权威。
        for entry in entries:
            if entry.future.done():
                continue
            try:
                entry.future.set_result(self._node_runner.fail_paused_node(self._task_id, entry.node_id, 'canceled'))
            except Exception:
                entry.future.set_exception(asyncio.CancelledError())

    async def resume_node(self, node_id: str) -> None:
        normalized_node_id = str(node_id or '').strip()
        entry = self._entries.get(normalized_node_id)
        if entry is None:
            entry = self._get_or_create_entry(normalized_node_id)
        if entry.future.done():
            return
        if entry.task is None or entry.task.done():
            entry.task = asyncio.create_task(
                self._run_entry(entry),
                name=f'task-node-resume:{self._task_id}:{normalized_node_id}',
            )

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

    async def fail_node(self, node_id: str, reason: str = '') -> NodeFinalResult:
        normalized_node_id = str(node_id or '').strip()
        entry = self._entries.get(normalized_node_id)
        result = self._node_runner.fail_paused_node(self._task_id, normalized_node_id, reason)
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
        except DistributionHoldError as exc:
            # 子树分发屏障冻结：对包括根在内的所有节点保持 future pending。
            # 父管线自然停摆；分发驱动器在释放时对每个 held entry 调
            # resume_node 在原 future 上重跑。绝不能 set_exception——那会被
            # 父管线的通用错误处理转成 spawn 运行时错误、杀死整条分支。
            # A2：冻结不再静默——落 WARN 供排查"复活失败"类事故（2026-09-15）。
            try:
                logger.warning(
                    'node frozen by distribution hold (future kept pending): task={} node={} epoch={}',
                    self._task_id,
                    entry.node_id,
                    str(getattr(exc, 'epoch_id', '') or ''),
                )
            except Exception:
                pass
            return
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

    def _publish_dispatch_state(self, *, force: bool = False) -> None:
        snapshot = self.snapshot()
        fingerprint = (
            ('limits_execution', int(snapshot['dispatch_limits']['execution'] or 0)),
            ('limits_inspection', int(snapshot['dispatch_limits']['inspection'] or 0)),
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
    ) -> None:
        self._store = store
        self._log_service = log_service
        self._node_runner = node_runner
        self._stall_notifier = stall_notifier
        self._dispatchers: dict[str, TaskNodeDispatcher] = {}
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
        self.distribution_resume_callback = None
        self.distribution_failure_notifier = None
        # 引擎级中断（无取消/暂停标志的 CancelledError）后的尽力即时重排钩子：
        # 由 MainRuntimeService 接到 global_scheduler.enqueue_task。进程退出中
        # 调度器关闭时自然无效，任务由下一个 worker 启动恢复兜底。
        self.interrupted_task_requeue_callback = None
        # 每任务单飞的子树分发驱动器（side asyncio.Task）。
        self._epoch_drivers: dict[str, asyncio.Task[None]] = {}
        # A3：释放后校验清扫任务（每任务替换式单飞，run_task finally 取消）。
        self._release_sweeps: dict[str, asyncio.Task[None]] = {}
        self._node_runner.nested_node_executor = self._execute_nested_node
        self._node_runner.cancel_node_subtree_executor = self._cancel_node_subtree

    async def _resume_distribution_if_needed(self, task_id: str) -> None:
        resume_callback = self.distribution_resume_callback
        if not callable(resume_callback):
            return
        result = resume_callback(task_id)
        if asyncio.iscoroutine(result):
            await result

    def _notify_distribution_failure(self, *, task_id: str, epoch_id: str, error_text: str) -> None:
        notifier = self.distribution_failure_notifier
        if not callable(notifier):
            return
        try:
            result = notifier(
                task_id=str(task_id or '').strip(),
                epoch_id=str(epoch_id or '').strip(),
                error_text=str(error_text or '').strip(),
            )
            if asyncio.iscoroutine(result):
                asyncio.get_running_loop().create_task(result)
        except Exception:
            logger.debug("distribution failure notifier callback failed for {}")

    async def _resume_pending_notice_nodes(self, task_id: str) -> bool:
        distribution = self._distribution_runtime_state(task_id)
        state = str(distribution.get('state') or '').strip()
        if state in _DISTRIBUTION_ACTIVE_STATES:
            return False
        pending_node_ids = [
            str(item or '').strip()
            for item in list(distribution.get('pending_notice_node_ids') or [])
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
            # 其他节点消费了通知但最终验收仍过早：补一次入队，避免
            # control_only_return 后无人再驱动根节点继续执行。
            await self._enqueue_acceptance_followup_if_needed(task_id)
        refreshed = self._distribution_runtime_state(task_id)
        if any(str(item or '').strip() for item in list(refreshed.get('pending_notice_node_ids') or [])):
            await self._resume_distribution_if_needed(task_id)
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

        仅当握手处于 waiting_acceptance / waiting_block_verification（执行节点
        已提交结果、正等待验收或阻塞核验）时放行；idle（从未提交）、
        waiting_execution_retry（应先跑执行节点）及各类终态一律拦截。
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
            ACCEPTANCE_STATE_WAITING_BLOCK_VERIFICATION,
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
        handled = self._node_runner._handle_acceptance_node_result(task=task, acceptance=acceptance, result=result)
        if str(handled.delivery_status or '').strip() != 'partial':
            return
        # 打回（或通知中断）：执行节点回到 waiting_execution_retry 并持有拒绝
        # 反馈通知；刷新分发态使根节点进入待恢复清单，并显式补入队驱动重跑。
        self._node_runner._refresh_resume_ready_distribution_state(task_id=task_id)
        await self._enqueue_acceptance_followup_if_needed(task_id)

    async def _resume_inflight_final_acceptance(self, task_id: str) -> bool:
        """重派发「握手在等、但回合半路断掉」的最终验收节点——通知账本之外的兜底路径。

        验收节点进入回合的第一步就消费并合并自己的交接通知，所以
        `_resume_pending_notice_nodes` 在重启后永远看不到它。这里以握手状态为凭据
        把它重新派发，并复用同一套结算路径（`_settle_final_acceptance_notice_result`）
        与终态判定（`_terminal_result_after_notice_resume`）：命中即返回 True，
        一次 run_task 不会同时驱动根节点与验收节点。
        """
        distribution = self._distribution_runtime_state(task_id)
        if str(distribution.get('state') or '').strip() in _DISTRIBUTION_HOLD_STATES:
            # 分发屏障/失败冻结期间节点生命周期由驱动器持有，不在此插手。
            return False
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

    async def run_task(self, task_id: str) -> None:
        task_record = self._store.get_task(task_id)
        if task_record is None:
            return
        if self._stall_notifier is not None and hasattr(self._stall_notifier, 'start_task'):
            self._stall_notifier.start_task(task_id)
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
                distribution = self._distribution_runtime_state(task_id)
                if str(distribution.get('state') or '').strip() in _DISTRIBUTION_ACTIVE_STATES:
                    # 子树分发进行中：确保单飞驱动器在跑，然后继续普通执行路径。
                    # - 根在屏障内（根定向=原全局冻结）：根 entry 被 hold 谓词
                    #   挂起为 pending future，run_task 自然等待到驱动器释放；
                    # - 根不在屏障内：无关分支照常执行，驱动器并发推进 epoch。
                    # 不再 control_only_return：那会把整任务冻结，违背局部暂停。
                    self.ensure_scoped_epoch_driver(task_id)
                # C：孤儿收尸/重派发——每次 worker 拾取都决断"DB 非终态但无执行器
                # 也无重放路径"的节点，杜绝幽灵 in_progress（2026-09-15 事故 L3）。
                await self._reconcile_orphan_in_progress_nodes(task_id, dispatcher)
                resumed_offroot_nodes = await self._resume_pending_notice_nodes(task_id)
                if not resumed_offroot_nodes:
                    # 通知账本里没人可派发时，再看握手：半路断掉的验收回合已经从
                    # 账本里消失，落回根节点会让被检验节点白跑一轮新回合。
                    resumed_offroot_nodes = await self._resume_inflight_final_acceptance(task_id)
                if resumed_offroot_nodes:
                    resumed_result = self._terminal_result_after_notice_resume(task_id)
                    if resumed_result is None:
                        control_only_return = True
                        return
                    result = resumed_result
                else:
                    result = await dispatcher.execute_node(task_id, root_node.node_id)
                    if str(result.delivery_status or '').strip() == 'partial':
                        await self._enqueue_acceptance_followup_if_needed(task_id)
                        control_only_return = True
                        return
                    if result.status == 'success':
                        result = await self._run_final_acceptance_if_needed(task_id)
                        if str(result.delivery_status or '').strip() == 'partial':
                            await self._enqueue_acceptance_followup_if_needed(task_id)
                            control_only_return = True
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

    async def _reconcile_orphan_in_progress_nodes(self, task_id: str, dispatcher: 'TaskNodeDispatcher') -> None:
        """C：孤儿收尸/重派发（每次 run_task 拾取时执行，幂等）。

        不变式：DB 里的 in_progress 执行节点必须对应"在跑/即将被派发"或
        "可由根重放恢复"，否则显式落 failed——杜绝"显示进行中但没人在跑"
        的幽灵态（2026-09-15 孤儿子节点事故 L3）。

        范围：只决断派生树内的 execution 节点（metadata 带 spawn_owner_kind='child'
        且 spawn_owner_round_id 非空）；acceptance 节点的生命周期由验收握手与
        `_resume_pending_notice_nodes`/最终验收机制管理，不在此收尸。
        分发进行中（含 failed 冻结）由屏障/驱动器持有节点生命周期，整体不介入。
        """
        distribution = self._distribution_runtime_state(task_id)
        if str(distribution.get('state') or '').strip() in _DISTRIBUTION_HOLD_STATES:
            return
        task = self._store.get_task(task_id)
        if task is None:
            return
        root_node_id = str(getattr(task, 'root_node_id', '') or '').strip()
        try:
            records = list(self._store.list_task_nodes(task_id) or [])
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
            entry = dispatcher._entries.get(node_id)
            if entry is not None and entry.task is not None and not entry.task.done():
                continue  # 已有活执行器
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
            reason = 'orphan reaped at task resume: in_progress without executor or replay path'
            try:
                logger.warning('orphan node reaped: task={} node={}', task_id, node_id)
            except Exception:
                pass
            try:
                self._node_runner.fail_paused_node(task_id, node_id, reason)
            except Exception:
                try:
                    logger.exception('orphan reap failed: task={} node={}', task_id, node_id)
                except Exception:
                    pass
                continue
            try:
                self._log_service.append_task_error_log(
                    task_id=task_id,
                    node_id=node_id,
                    node_title=str(getattr(node, 'title', '') or getattr(node, 'goal', '') or ''),
                    error_text=reason,
                )
            except Exception:
                pass

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

    async def _cancel_node_subtree(self, task_id: str, node_ids: list[str]) -> None:
        dispatcher = self._dispatchers.get(str(task_id or '').strip())
        if dispatcher is None:
            return
        await dispatcher.cancel_nodes(node_ids)

    async def _execute_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        return await self._execute_nested_node(task_id, node_id)

    async def _enqueue_acceptance_followup_if_needed(self, task_id: str) -> None:
        resume_callback = self.distribution_resume_callback
        if not callable(resume_callback):
            return
        result = resume_callback(task_id)
        if asyncio.iscoroutine(result):
            await result

    def _create_dispatcher(self, task_id: str) -> TaskNodeDispatcher:
        return TaskNodeDispatcher(
            task_id=task_id,
            store=self._store,
            log_service=self._log_service,
            node_runner=self._node_runner,
            execution_limit=self._node_dispatch_limits['execution'],
            inspection_limit=self._node_dispatch_limits['inspection'],
        )

    def _distribution_runtime_state(self, task_id: str) -> dict[str, object]:
        runtime_meta = self._log_service.read_task_runtime_meta(task_id) or {}
        return dict(runtime_meta.get('distribution') or {})

    def _barrier_materialize_pending_entries(
        self,
        *,
        task_id: str,
        barrier_node_ids: list[str],
    ) -> list[dict[str, Any]]:
        task = self._store.get_task(task_id)
        if task is None:
            return []
        pending_entries: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int]] = set()
        for raw_node_id in list(barrier_node_ids or []):
            node_id = str(raw_node_id or '').strip()
            if not node_id:
                continue
            node = self._store.get_node(node_id)
            if node is None or str(node.task_id or '').strip() != str(task.task_id or '').strip():
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            self._node_runner.reconcile_spawn_entry_child_bindings(task_id=task.task_id, parent_node_id=node_id)
            node = self._store.get_node(node_id) or node
            operations = (node.metadata or {}).get('spawn_operations') if isinstance(node.metadata, dict) else {}
            if not isinstance(operations, dict):
                continue
            for round_id, payload in reversed(list(operations.items())):
                if not isinstance(payload, dict) or bool(payload.get('completed')):
                    continue
                for entry_index, entry in enumerate(list(payload.get('entries') or [])):
                    if not isinstance(entry, dict):
                        continue
                    review_decision = str(entry.get('review_decision') or '').strip().lower()
                    if review_decision == 'blocked':
                        continue
                    status = str(entry.get('status') or '').strip().lower()
                    if status not in {'queued', 'running'}:
                        continue
                    normalized_round_id = str(round_id or '').strip()
                    normalized_index = int(entry.get('index') or entry_index)
                    if self._spawn_entry_child_is_fully_materialized(
                        task_id=task.task_id,
                        parent_node_id=node_id,
                        round_id=normalized_round_id,
                        entry_index=normalized_index,
                        entry=entry,
                    ):
                        continue
                    key = (node_id, normalized_round_id, normalized_index)
                    if key in seen:
                        continue
                    seen.add(key)
                    pending_entries.append(
                        {
                            'parent_node_id': node_id,
                            'round_id': normalized_round_id,
                            'entry_index': normalized_index,
                            'goal': str(entry.get('goal') or '').strip(),
                            'status': status,
                        }
                    )
        return pending_entries

    def _spawn_entry_child_is_fully_materialized(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        round_id: str,
        entry_index: int,
        entry: dict[str, Any],
    ) -> bool:
        child_node_id = str(entry.get('child_node_id') or '').strip()
        child = self._store.get_node(child_node_id) if child_node_id else None
        # 字段级判定与检查点侧共用单一真源（main.runtime.subtree_hold），避免漂移；
        # 驱动器额外要求子节点仍在活动分发树内。
        return spawn_entry_child_fully_materialized(
            task_id=task_id,
            parent_node_id=parent_node_id,
            round_id=round_id,
            entry_index=entry_index,
            entry=entry,
            child=child,
            is_in_live_tree=lambda node_id: self._node_runner.node_is_in_live_distribution_tree(
                task_id=task_id,
                node_id=node_id,
            ),
        )

    @staticmethod
    def _drain_kick_ledger(payload: dict[str, Any]) -> dict[str, str]:
        """把 `drain_kick_rounds` 读成 {父节点::轮: 上次踢起时刻}。

        兼容早期列表形态：无时刻即视为冷却已过，允许重试（踢了但没进展的轮
        必须能再踢，否则一次失败就永久卡住）。
        """
        raw = payload.get('drain_kick_rounds')
        ledger: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                normalized = str(key or '').strip()
                if normalized:
                    ledger[normalized] = str(value or '').strip()
        elif isinstance(raw, list):
            for item in raw:
                normalized = str(item or '').strip()
                if normalized:
                    ledger[normalized] = ''
        return ledger

    @staticmethod
    def _drain_kick_cooldown_active(last_kicked_at: str) -> bool:
        """同一 父节点+轮 的重复踢起是否仍在冷却期内。时刻不可解析时按可重试处理。"""
        text = str(last_kicked_at or '').strip()
        if not text:
            return False
        try:
            last_dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
            now_dt = datetime.fromisoformat(now_iso().replace('Z', '+00:00'))
        except Exception:
            return False
        return (now_dt - last_dt).total_seconds() < _DRAIN_KICK_RETRY_SECONDS

    async def _kick_stalled_spawn_round_parents(
        self,
        *,
        task_id: str,
        entries: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        """drain 自愈：把「持有未物化 spawn 轮、但已被屏障停摆」的父节点踢起来。

        这类节点没有存活 entry（例如进程重启后、或它的协程早已被 hold 中止），
        屏障的 drain 却在等它物化子节点——不踢就是永久互等。踢之前要求帧里
        仍保留该轮的重放入口（``_parent_frame_intends_round_replay``，B4），
        否则重放不成立、只会让模型发一轮新 spawn。

        同一 父节点+轮 有冷却期（``_DRAIN_KICK_RETRY_SECONDS``）：踢了但没进展的轮
        会再次被踢（不能一次失败就永久卡住），但不会逐秒重复 resume 同一个未缓存
        review 的轮。账记在 epoch payload 的 ``drain_kick_rounds``（键 → 上次踢起时刻）。
        """
        dispatcher = self._dispatchers.get(str(task_id or '').strip())
        if dispatcher is None:
            return
        ledger = self._drain_kick_ledger(payload)
        for raw_item in list(entries or []):
            item = dict(raw_item) if isinstance(raw_item, dict) else {}
            parent_node_id = str(item.get('parent_node_id') or '').strip()
            round_id = str(item.get('round_id') or '').strip()
            if not parent_node_id or not round_id:
                continue
            key = f'{parent_node_id}::{round_id}'
            if self._drain_kick_cooldown_active(ledger.get(key, '')):
                continue
            entry = dispatcher._entries.get(parent_node_id)
            if entry is not None and entry.task is not None and not entry.task.done():
                continue  # 已有活执行器：它会自己把物化跑完
            parent = self._store.get_node(parent_node_id)
            if parent is None or self._node_operator_paused(parent):
                continue
            if str(getattr(parent, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            if not self._parent_frame_intends_round_replay(
                task_id=task_id,
                parent=parent,
                round_id=round_id,
            ):
                continue
            ledger[key] = now_iso()
            logger.warning(
                'barrier drain self-heal: kicking stalled spawn-round parent: task={} node={} round={}',
                task_id,
                parent_node_id,
                round_id,
            )
            try:
                await self.resume_node_entry(task_id, parent_node_id)
            except Exception:
                logger.exception(
                    'barrier drain self-heal: resume failed: task={} node={}',
                    task_id,
                    parent_node_id,
                )
        payload['drain_kick_rounds'] = ledger

    def _queue_root_distribution_notices(self, *, epoch, created_at: str) -> None:
        self._node_runner._queue_pending_root_distribution_notices(epoch=epoch, created_at=created_at)

    def _fail_distribution_epoch(
        self,
        task_id: str,
        *,
        epoch_id: str,
        failed_node_id: str,
        failure_reason: str,
    ) -> bool:
        epoch = self._store.get_task_message_distribution_epoch(task_id, epoch_id)
        if epoch is None:
            return True
        failed_at = now_iso()
        error_text = (str(failure_reason or '').strip() or 'distribution turn failed')[:500]
        payload = dict(epoch.payload or {})
        payload['frontier_node_ids'] = []
        payload['next_frontier_node_ids'] = []
        payload['failure_node_id'] = str(failed_node_id or '').strip()
        failed_epoch = self._store.upsert_task_message_distribution_epoch(
            epoch.model_copy(
                update={
                    'state': 'failed',
                    'completed_at': failed_at,
                    'error_text': error_text,
                    'payload': payload,
                }
            )
        )
        # Message durability: the queued messages were never distributed, so keep
        # them as pending notices on the epoch targets; an explicit resume still
        # merges them instead of silently dropping the appended requirement.
        self._queue_root_distribution_notices(epoch=failed_epoch, created_at=failed_at)
        queued_epoch_count = sum(
            1
            for item in list(self._store.list_active_task_message_distribution_epochs(task_id) or [])
            if str(item.state or '').strip() == 'queued'
        )
        # 子树保持冻结：hold 谓词把 'failed' 计入阻塞态，屏障与目标保留在
        # meta 中（红色横幅 + barrier_blocked 标记），直到显式恢复降级。
        failed_barrier = [
            str(item or '').strip()
            for item in list(payload.get('barrier_node_ids') or [])
            if str(item or '').strip()
        ]
        failed_targets = self._epoch_target_node_ids(failed_epoch, self._store.get_task(task_id))
        failed_pending = list(dict.fromkeys([
            *failed_targets,
            *self._node_runner.nodes_with_pending_distribution_notices(task_id=task_id),
        ]))
        self._publish_distribution_meta(
            task_id,
            epoch_id=epoch_id,
            state='failed',
            mode='subtree_barrier' if failed_barrier else '',
            targets=failed_targets,
            frontier=[],
            blocked=failed_barrier,
            pending_notice=failed_pending,
            queued_epoch_count=queued_epoch_count,
            error_text=error_text,
        )
        # The task is now stuck in a failed distribution: mark it paused at the
        # task level so the task hall shows 「任务暂停」 instead of a running task.
        # Resume (operator or CEO) clears this via set_pause_state(False, False).
        self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
        # Deliberately no clear_pause / _resume_distribution_if_needed here: the task
        # stays paused until an operator or the CEO explicitly resumes it or appends a
        # new notice (which queues a fresh epoch and re-runs distribution).
        self._notify_distribution_failure(
            task_id=task_id,
            epoch_id=epoch_id,
            error_text=error_text,
        )
        return True

    def _epoch_target_node_ids(self, epoch, task) -> list[str]:
        payload = dict(epoch.payload or {}) if isinstance(getattr(epoch, 'payload', None), dict) else {}
        targets = [
            str(item or '').strip()
            for item in list(payload.get('target_node_ids') or [])
            if str(item or '').strip()
        ]
        if not targets:
            # 兼容旧 epoch 数据：无目标视为根定向（等价旧全局模式）。
            root_id = (
                str(getattr(epoch, 'root_node_id', '') or '').strip()
                or str(getattr(task, 'root_node_id', '') or '').strip()
            )
            if root_id:
                targets = [root_id]
        return targets

    def _derive_barrier_node_ids(self, task_id: str, targets: list[str]) -> list[str]:
        """每波重推屏障集：目标子树并集 ∩ 存活分发树 + 目标自身。

        入队快照会漏掉 drain 期间新物化的子孙，因此 hold 判定与 blocked
        展示都以重推结果为准；快照仅保留在 epoch payload 里做取证。
        """
        live_ids = set(
            getattr(self._node_runner, 'live_distribution_tree_node_ids', lambda **_: [])(task_id=task_id) or []
        )
        scope: list[str] = []
        for target_id in targets:
            if not target_id:
                continue
            scope.append(target_id)
            scope.extend(sorted(self._node_runner._collect_descendant_node_ids([target_id])))
        deduped = [item for item in dict.fromkeys(scope) if item]
        target_set = {item for item in targets if item}
        return [node_id for node_id in deduped if node_id in live_ids or node_id in target_set]

    @staticmethod
    def _node_operator_paused(node) -> bool:
        return bool(getattr(node, 'pause_requested', False)) or bool(getattr(node, 'is_paused', False))

    def _scoped_drain_pending_node_ids(self, *, task_id: str, barrier_node_ids: list[str]) -> list[str]:
        """屏障内节点"协程确已停摆"才算 drain 完成。

        与旧全局 drain 的区别：局部冻结不取消 actor，节点协程可能仍在跑。
        entry.task 未结束 = 还没到 hold 检查点；entry 存在且 task 已结束
        （held/已解析）= 已停摆；无 entry 时按 frame 相位兜底（冷路径）。
        """
        dispatcher = self._dispatchers.get(task_id)
        frame_map = {
            str(item.node_id or '').strip(): item
            for item in list(self._store.list_task_runtime_frames(task_id) or [])
            if str(item.node_id or '').strip()
        }
        pending_node_ids: list[str] = []
        seen: set[str] = set()
        for raw_node_id in list(barrier_node_ids or []):
            node_id = str(raw_node_id or '').strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            node = self._store.get_node(node_id)
            if node is None:
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            entry = dispatcher._entries.get(node_id) if dispatcher is not None else None
            if entry is not None:
                if entry.task is not None and not entry.task.done():
                    pending_node_ids.append(node_id)
                continue
            frame = frame_map.get(node_id)
            if frame is None:
                continue
            phase = str(getattr(frame, 'phase', '') or '').strip()
            if phase not in _DISTRIBUTION_BARRIER_SAFE_PHASES:
                pending_node_ids.append(node_id)
        return pending_node_ids

    def _queued_epoch_count(self, task_id: str) -> int:
        return sum(
            1
            for item in list(self._store.list_active_task_message_distribution_epochs(task_id) or [])
            if str(item.state or '').strip() == 'queued'
        )

    def _publish_distribution_meta(
        self,
        task_id: str,
        *,
        epoch_id: str,
        state: str,
        mode: str = 'subtree_barrier',
        targets: list[str] | None = None,
        frontier: list[str] | None = None,
        blocked: list[str] | None = None,
        pending_notice: list[str] | None = None,
        queued_epoch_count: int | None = None,
        error_text: str = '',
    ) -> None:
        """分发 runtime meta 的驱动侧唯一写入口（全量重算式覆盖）。"""
        normalized_state = str(state or '').strip()
        blocked_ids = [str(item or '').strip() for item in list(blocked or []) if str(item or '').strip()]
        if normalized_state == 'pause_requested' and blocked_ids:
            normalized_state = 'barrier_requested'
        self._log_service.update_task_runtime_meta(
            task_id,
            distribution={
                'active_epoch_id': str(epoch_id or '').strip(),
                'state': normalized_state,
                'mode': str(mode or '').strip(),
                'target_node_ids': [str(item or '').strip() for item in list(targets or []) if str(item or '').strip()],
                'frontier_node_ids': [str(item or '').strip() for item in list(frontier or []) if str(item or '').strip()],
                'blocked_node_ids': blocked_ids,
                'pending_notice_node_ids': (
                    [str(item or '').strip() for item in list(pending_notice or []) if str(item or '').strip()]
                    if pending_notice is not None
                    else list(self._node_runner.nodes_with_pending_distribution_notices(task_id=task_id))
                ),
                'queued_epoch_count': int(
                    queued_epoch_count if queued_epoch_count is not None else self._queued_epoch_count(task_id)
                ),
                'pending_mailbox_count': self._node_runner.pending_distribution_mailbox_count(task_id=task_id),
                'error_text': str(error_text or '').strip(),
            },
        )

    def _reset_stall_clock(self, task_id: str) -> None:
        notifier = self._stall_notifier
        if notifier is not None and hasattr(notifier, 'reset_visible_output'):
            try:
                notifier.reset_visible_output(task_id)
            except Exception:
                pass

    def ensure_scoped_epoch_driver(self, task_id: str) -> None:
        """确保该任务的子树分发驱动器在跑（每任务单飞，幂等）。"""
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return
        distribution = self._distribution_runtime_state(normalized_task_id)
        if str(distribution.get('state') or '').strip() not in _DISTRIBUTION_ACTIVE_STATES:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        existing = self._epoch_drivers.get(normalized_task_id)
        if existing is not None and not existing.done():
            return
        driver = loop.create_task(
            self._drive_scoped_epoch(normalized_task_id),
            name=f'task-distribution-driver:{normalized_task_id}',
        )
        self._epoch_drivers[normalized_task_id] = driver

        def _cleanup(completed_task: 'asyncio.Task[None]', *, tid: str = normalized_task_id) -> None:
            if self._epoch_drivers.get(tid) is completed_task:
                self._epoch_drivers.pop(tid, None)

        driver.add_done_callback(_cleanup)

    async def _drive_scoped_epoch(self, task_id: str) -> None:
        """单飞驱动器：波次循环直到 epoch 终态。

        deferred/draining 轮询等待（任务/目标人工暂停、节点未到安全停点），
        advanced/promoted 立即续波。波次异常按落库的 crash 计数有界重试，超限显式
        失败并出告警——不再「记一条日志就退出」：屏障的唯一释放者就是这个驱动器，
        它一死子树就永久冻结（2026-09-20 task:e5d3d0c2fbe1）。
        """
        deferred_polls = 0
        while True:
            try:
                outcome = await self._run_distribution_epoch(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._account_wave_crash(task_id, error_text=describe_exception(exc)):
                    return
                await asyncio.sleep(_DISTRIBUTION_WAVE_CRASH_RETRY_SECONDS)
                continue
            if outcome in {'idle', 'completed', 'failed'}:
                return
            if outcome == 'deferred':
                deferred_polls += 1
                await asyncio.sleep(min(5.0 * deferred_polls, 30.0))
                continue
            if outcome == 'draining':
                deferred_polls = 0
                await asyncio.sleep(_DISTRIBUTION_DRIVER_POLL_SECONDS)
                continue
            deferred_polls = 0

    def _account_wave_crash(self, task_id: str, *, error_text: str) -> bool:
        """波次崩溃记账，返回驱动器该「继续重试」还是「收口退出」。"""
        distribution = self._distribution_runtime_state(task_id)
        epoch_id = str(distribution.get('active_epoch_id') or '').strip()
        epoch = self._store.get_task_message_distribution_epoch(task_id, epoch_id) if epoch_id else None
        if epoch is None:
            logger.error(
                'distribution wave crashed with no active epoch (nothing to retry): task={} error={}',
                task_id,
                error_text,
            )
            return False
        payload = dict(epoch.payload or {})
        crash_count = int(payload.get('wave_crash_count') or 0) + 1
        payload['wave_crash_count'] = crash_count
        self._store.upsert_task_message_distribution_epoch(epoch.model_copy(update={'payload': payload}))
        logger.exception(
            'distribution driver wave crashed for {} (crash_count={}): {}',
            task_id,
            crash_count,
            error_text,
        )
        if crash_count <= _DISTRIBUTION_WAVE_CRASH_RETRY_LIMIT:
            return True
        self._fail_distribution_epoch(
            task_id,
            epoch_id=epoch_id,
            failed_node_id='',
            failure_reason=f'distribution wave crashed {crash_count}x: {error_text}',
        )
        return False

    async def reconcile_distribution_drivers(self) -> list[str]:
        """接管分发收尾的两个缺席面：驱动器没了 / 释放账没销。

        - epoch 处于活跃分发态而该任务没有在跑的驱动器 → 重新 ensure（幂等由
          ``ensure_scoped_epoch_driver`` 保证，有活驱动器即返回）；
        - epoch 已终态但 ``payload.release_pending`` 仍有值 → 完成序列在「清 meta」之后
          半路抛过，hold 谓词已关、对账器第一条判据再也看不见它，重跑释放补销账。
          A3 的「冻结→释放的终点只能是在跑或显式告警」由这条才真正闭合。

        只接管本进程正在执行的任务（``_dispatchers`` 有该任务的派发器）：分发收尾对象
        都是进程内资源，扫全库会在多 worker 部署里替别的 worker 武装波次。进程真死过的
        任务由 worker 重启的 ``run_task`` 入口负责 ensure。返回本轮动过接管的 task_id。
        """
        touched: list[str] = []
        for task_id, dispatcher in list(self._dispatchers.items()):
            if dispatcher is None:
                continue
            task = self._store.get_task(task_id)
            if task is None or str(getattr(task, 'status', '') or '').strip().lower() != 'in_progress':
                continue
            distribution = self._distribution_runtime_state(task_id)
            state = str(distribution.get('state') or '').strip()
            if state in _DISTRIBUTION_ACTIVE_STATES:
                driver = self._epoch_drivers.get(task_id)
                if driver is not None and not driver.done():
                    continue
                try:
                    logger.warning(
                        'distribution driver missing for active epoch, re-arming: task={} epoch={} state={}',
                        task_id,
                        str(distribution.get('active_epoch_id') or '').strip(),
                        state,
                    )
                except Exception:
                    pass
                self.ensure_scoped_epoch_driver(task_id)
                touched.append(task_id)
                continue
            unreleased = self._unreleased_release_ledger(task_id)
            if not unreleased:
                continue
            pending_epoch_id, barrier_node_ids, target_node_ids = unreleased
            try:
                logger.warning(
                    'distribution release ledger unfinished, re-releasing: task={} epoch={} barrier_nodes={}',
                    task_id,
                    pending_epoch_id,
                    len(barrier_node_ids),
                )
            except Exception:
                pass
            await self._release_scoped_epoch_holds(task_id, barrier_node_ids, target_node_ids=target_node_ids)
            # 销账在释放之后：再抛就留给下一轮对账重试，不会既漏释放又反复空跑。
            self._clear_release_pending(task_id, epoch_id=pending_epoch_id)
            touched.append(task_id)
        return touched

    def _unreleased_release_ledger(self, task_id: str) -> tuple[str, list[str], list[str]] | None:
        """最近一个还挂着未销释放账的 epoch（完成序列半路中断的形态）。

        连同 ``target_node_ids`` 一起回，让重跑的释放能按冻结面（目标子树）宽算，
        而不是只重放 barrier 快照。
        """
        epochs = list(self._store.list_active_task_message_distribution_epochs(task_id) or [])
        for epoch in reversed(epochs):
            payload = dict(epoch.payload or {}) if isinstance(epoch.payload, dict) else {}
            barrier = [
                str(item or '').strip()
                for item in list(payload.get('release_pending') or [])
                if str(item or '').strip()
            ]
            if barrier:
                targets = [
                    str(item or '').strip()
                    for item in list(payload.get('target_node_ids') or [])
                    if str(item or '').strip()
                ]
                return str(epoch.epoch_id or '').strip(), barrier, targets
        return None

    def _clear_release_pending(self, task_id: str, *, epoch_id: str) -> None:
        epoch = self._store.get_task_message_distribution_epoch(task_id, epoch_id)
        if epoch is None or not list(dict(epoch.payload or {}).get('release_pending') or []):
            return
        payload = dict(epoch.payload or {})
        payload['release_pending'] = []
        self._store.upsert_task_message_distribution_epoch(epoch.model_copy(update={'payload': payload}))

    async def _run_frontier_turn(self, task, epoch, node_id: str) -> NodeFinalResult | None:
        """按目标节点自身状态选择接收方式（需求一.1/.2/.3 + Q2）。"""
        node = self._store.get_node(node_id)
        if node is None:
            return None
        metadata = dict(node.metadata or {}) if isinstance(node.metadata, dict) else {}
        handshake = normalize_acceptance_handshake(metadata.get(ACCEPTANCE_HANDSHAKE_KEY))
        handshake_state = str(handshake.get('state') or '').strip()
        if handshake_state in {ACCEPTANCE_STATE_WAITING_ACCEPTANCE, ACCEPTANCE_STATE_WAITING_BLOCK_VERIFICATION}:
            # 被验收检验中：决策回合（打断验收恢复执行 / 继续验收并告知验收节点）。
            return await self._node_runner.run_notice_inspection_decision(task=task, node=node)
        resume_mode, _holding_round_id = self._node_runner._pending_notice_resume_target(node=node)
        live_children = self._node_runner.live_distribution_child_node_ids(
            task_id=task.task_id,
            parent_node_id=node_id,
        )
        if resume_mode == RESUME_MODE_WAIT_FOR_CHILDREN and live_children:
            # 等子节点：现有控制回合决定下传/放弃；直接走 run_node 的分发分支
            # （不经 dispatcher，避免解析被 hold 的 entry future）。
            return await self._node_runner.run_node(task.task_id, node_id)
        # 自处理中/尚未启动/叶子：无存活子节点可决策。epoch 目标把自己的通知
        # 落为待处理记录；级联接收者的转发消息已在信箱（父回合投递时 stamp 过
        # pending_notice_state），释放后由恢复路径或在途刷新并入，不重启节点。
        if self._node_runner._node_is_epoch_target(node=node, epoch=epoch):
            self._node_runner.queue_pending_target_distribution_notices(epoch=epoch, node_ids=[node_id])
        self._record_direct_local_merge(task.task_id, epoch, node_id)
        return None

    def _record_direct_local_merge(self, task_id: str, epoch, node_id: str) -> None:
        """直接并入分支的记账：决策记录 + distributed 标记（防止重复回合）。"""
        refreshed = self._store.get_task_message_distribution_epoch(task_id, str(epoch.epoch_id or '').strip())
        if refreshed is None:
            return
        payload = dict(refreshed.payload or {})
        decision_records = list(payload.get('decision_records') or [])
        decision_records.append(
            {
                'source_node_id': str(node_id or '').strip(),
                'turn': 'direct_local_merge',
                'notes': 'no live children to decide; notice merged locally without a model turn',
                'local_notice_kept': self._node_runner._node_is_epoch_target(node=self._store.get_node(node_id), epoch=refreshed),
                'created_at': now_iso(),
            }
        )
        distributed_node_ids = [
            str(item or '').strip()
            for item in list(payload.get('distributed_node_ids') or [])
            if str(item or '').strip()
        ]
        if node_id not in distributed_node_ids:
            distributed_node_ids.append(node_id)
        payload['decision_records'] = decision_records
        payload['distributed_node_ids'] = distributed_node_ids
        self._store.upsert_task_message_distribution_epoch(
            refreshed.model_copy(update={'payload': payload})
        )

    async def _release_scoped_epoch_holds(
        self,
        task_id: str,
        barrier_node_ids: list[str],
        *,
        target_node_ids: list[str] | None = None,
    ) -> None:
        """释放子树冻结：对每个 held entry 重跑（meta 已先行清除，hold 谓词关闭）。

        A2：每个跳过项落原因；done future 携带未消费异常时把异常打出来——
        父链路已死时这是搁浅异常的唯一痕迹（2026-09-15 孤儿子节点事故 L2）。
        A3：成功复活的节点进入延迟校验清扫，保证"冻结→释放"的终点只能是
        在跑或显式终态/告警，不允许静默 pending。

        释放集 = ``barrier_node_ids`` ∪ 「本进程 entry 落在本 epoch 目标子树内的节点」。
        只按 barrier 释放是 2026-09-22 task:2311f30ddace 的根因：barrier 由
        ``live_distribution_tree_node_ids`` 重推，而它对验收节点直接判否，于是沿祖先
        上溯被冻住的验收子节点整批落在释放面之外——future 永久 pending、零 ERROR、
        零告警。子树归属复用冻结面同一函数 ``node_in_target_subtree``，两侧不再漂移。
        """
        dispatcher = self._dispatchers.get(task_id)
        if dispatcher is None:
            try:
                logger.info('release skip: dispatcher missing (task not running here): task={}', task_id)
            except Exception:
                pass
            return
        candidates = [str(item or '').strip() for item in list(barrier_node_ids or []) if str(item or '').strip()]
        candidates.extend(self._entry_node_ids_in_target_subtrees(dispatcher, target_node_ids=target_node_ids))
        resumed_node_ids: list[str] = []
        for node_id in dict.fromkeys(candidates):
            outcome = await self._revive_dispatch_entry_for_resume(task_id, dispatcher, node_id)
            if outcome == 'resumed':
                resumed_node_ids.append(node_id)
        if resumed_node_ids:
            self._schedule_release_verification(task_id, resumed_node_ids)

    def _entry_node_ids_in_target_subtrees(
        self,
        dispatcher: 'TaskNodeDispatcher',
        *,
        target_node_ids: list[str] | None,
    ) -> list[str]:
        """本进程已登记、且落在本 epoch 目标子树内的节点 id（= 冻结面的真实作用集）。

        只认 entry：冻结只可能发生在 ``_run_entry`` 里，无 entry 即无 pending future。
        正常在跑的 entry 由 ``TaskNodeDispatcher.resume_node`` 自身的双跑保护兜住
        （协程未停即 no-op），因此这里宽算集合不会重启任何活执行器。
        """
        targets = {
            str(item or '').strip() for item in list(target_node_ids or []) if str(item or '').strip()
        }
        if not targets:
            return []
        try:
            entry_ids = [str(node_id or '').strip() for node_id in list(dispatcher._entries) if str(node_id or '').strip()]
        except Exception:
            return []
        return [
            node_id
            for node_id in dict.fromkeys(entry_ids)
            if node_in_target_subtree(get_node=self._store.get_node, node_id=node_id, targets=targets)
        ]

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
                    'release verification: node still frozen after epoch release, re-resuming once: '
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
        """卡死形态：future pending + entry task done + 非终态 + 非人工暂停 + 无活动 hold。"""
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
            if self._node_runner._subtree_hold_epoch_id(task_id=task_id, node_id=node_id):
                # 新一轮屏障又冻上了——合法冻结，不算卡死。
                continue
            wedged.append(node_id)
        return wedged

    def _ancestor_node_ids(self, node_id: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current = self._store.get_node(str(node_id or '').strip())
        while current is not None:
            parent_id = str(getattr(current, 'parent_node_id', '') or '').strip()
            if not parent_id or parent_id in seen:
                break
            seen.add(parent_id)
            chain.append(parent_id)
            current = self._store.get_node(parent_id)
        return chain

    def _acceptance_companion_node_id(self, node) -> str:
        """该执行节点当前存活的验收子节点（无则空串）。"""
        metadata = dict(getattr(node, 'metadata', None) or {}) if isinstance(getattr(node, 'metadata', None), dict) else {}
        handshake = normalize_acceptance_handshake(metadata.get(ACCEPTANCE_HANDSHAKE_KEY))
        acceptance_id = str(handshake.get('acceptance_node_id') or '').strip()
        if not acceptance_id:
            return ''
        acceptance = self._store.get_node(acceptance_id)
        if acceptance is None:
            return ''
        if str(getattr(acceptance, 'node_kind', '') or '').strip().lower() != 'acceptance':
            return ''
        if str(getattr(acceptance, 'status', '') or '').strip().lower() in {'success', 'failed'}:
            return ''
        return acceptance_id

    def _propagate_notice_upward(self, *, task_id: str, epoch, targets: list[str]) -> None:
        """需求三：分发完成后沿路径向上告知祖先（含验收节点与最终验收节点）。

        信箱投递、无控制回合：祖先下次执行/恢复时自然消费；正在运行的
        祖先经由在途刷新并入。失败 epoch 不上传播（消息未被接受）。
        """
        task = self._store.get_task(task_id)
        if task is None:
            return
        payload = dict(epoch.payload or {}) if isinstance(epoch.payload, dict) else {}
        messages = [
            str(item or '').strip()
            for item in list(payload.get('queued_root_messages') or [])
            if str(item or '').strip()
        ]
        if not messages:
            fallback = str(epoch.root_message or '').strip()
            messages = [fallback] if fallback else []
        if not messages:
            return
        combined = '\n\n'.join(messages)
        epoch_id = str(epoch.epoch_id or '').strip()
        delivered: set[tuple[str, str]] = set()

        def _deliver(target_node_id: str, source_node_id: str, message: str) -> None:
            normalized_target = str(target_node_id or '').strip()
            if not normalized_target or (normalized_target, message) in delivered:
                return
            delivered.add((normalized_target, message))
            self._node_runner._persist_node_notification_direct(
                task_id=task_id,
                epoch_id=epoch_id,
                source_node_id=source_node_id,
                target_node_id=normalized_target,
                message=message,
                # 转述包装是系统自动生成的通知（非真实消息内容），
                # 展示层据此从节点消息列表里过滤。
                origin=NOTICE_ORIGIN_SYSTEM_RELAY,
            )

        for target_id in targets:
            target_node = self._store.get_node(target_id)
            target_title = ' '.join(str(getattr(target_node, 'goal', '') or target_id).split())[:60] or target_id
            relay = (
                f'【后代节点收到用户定向通知】你的后代节点「{target_title}」（{target_id}）收到了用户追加的通知，'
                f'内容如下，请参考并决定是否调整自己的任务内容与验收口径：\n{combined}'
            )
            for ancestor_id in self._ancestor_node_ids(target_id):
                _deliver(ancestor_id, target_id, relay)
                ancestor_node = self._store.get_node(ancestor_id)
                if ancestor_node is None:
                    continue
                acceptance_id = self._acceptance_companion_node_id(ancestor_node)
                if acceptance_id:
                    _deliver(
                        acceptance_id,
                        target_id,
                        f'【被检验节点收到用户定向通知】{relay}',
                    )
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        final_node_id = str(final_acceptance.node_id or '').strip()
        if final_node_id:
            final_node = self._store.get_node(final_node_id)
            if final_node is not None and str(getattr(final_node, 'status', '') or '').strip().lower() not in {'success', 'failed'}:
                target_lines = []
                for target_id in targets:
                    target_node = self._store.get_node(target_id)
                    target_title = ' '.join(str(getattr(target_node, 'goal', '') or target_id).split())[:60] or target_id
                    target_lines.append(f'「{target_title}」（{target_id}）')
                _deliver(
                    final_node_id,
                    targets[0] if targets else '',
                    '【任务收到用户定向通知】以下节点收到了用户追加的通知：'
                    + '、'.join(target_lines)
                    + f'。通知内容如下，请在最终验收判定时纳入参考：\n{combined}',
                )

    async def _interrupt_acceptance_for_notice(self, *, task_id: str, execution_node_id: str, epoch_id: str) -> None:
        """决策回合 resume_execution：打断验收节点并作废其轮次。

        顺序不可颠倒：cancel 协程并等它真正停下 → invalidate 权威重置
        （run_node 的取消路径可能先把验收节点标 canceled）+ 丢弃其
        runtime frame（避免下一轮验收从被作废轮次的 react 状态恢复）→
        最后以合成结果解析 future，父管线走「拒绝重试但不计预算」分支。
        """
        node = self._store.get_node(execution_node_id)
        if node is None:
            return
        metadata = dict(node.metadata or {}) if isinstance(node.metadata, dict) else {}
        handshake = normalize_acceptance_handshake(metadata.get(ACCEPTANCE_HANDSHAKE_KEY))
        acceptance_id = str(handshake.get('acceptance_node_id') or '').strip()
        if not acceptance_id:
            return
        synthetic = NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary='acceptance interrupted by user notice',
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=_NOTICE_INTERRUPT_REASON,
        )

        def _invalidate_and_discard() -> None:
            self._log_service.invalidate_acceptance_node(
                task_id,
                acceptance_id,
                epoch_id=epoch_id,
                reason='notice_inspection_resume',
            )
            remove_frame = getattr(self._log_service, 'remove_frame', None)
            if callable(remove_frame):
                try:
                    remove_frame(task_id, acceptance_id)
                except Exception:
                    pass

        dispatcher = self._dispatchers.get(task_id)
        entry = dispatcher._entries.get(acceptance_id) if dispatcher is not None else None
        if entry is not None:
            # 验收协程在跑/在等：打断并由父管线的拒绝重试分支恢复执行节点。
            await dispatcher.interrupt_node(acceptance_id, synthetic, on_stopped=_invalidate_and_discard)
        else:
            # 冷路径：验收没有存活 entry——作废后直接把执行节点恢复为等待重试。
            _invalidate_and_discard()
            task_record = self._store.get_task(task_id)
            acceptance_record = self._store.get_node(acceptance_id)
            if task_record is not None and acceptance_record is not None:
                self._node_runner._interrupt_acceptance_for_notice_retry(
                    task=task_record,
                    execution=node,
                    acceptance=acceptance_record,
                    handshake=handshake,
                )

    async def _apply_notice_interrupt_effects(self, *, task_id: str, epoch_id: str) -> None:
        """按「决策账 × 执行账」的差额补做验收打断，让波次重放幂等。

        ``decision_records`` 只记模型决定了什么，``wave_effects`` 记驱动器真正做过的
        副作用；落在两本账之间的任何中断（构造结果抛异常、进程被杀）都不会再把动作
        弄丢（2026-09-20 task:e5d3d0c2fbe1：决定落库后驱动器 ValidationError 打死，
        打断从未执行、通知也没并入，子树屏障永久冻结）。
        """
        epoch = self._store.get_task_message_distribution_epoch(task_id, epoch_id)
        if epoch is None:
            return
        payload = dict(epoch.payload or {})
        effects = [dict(item) for item in list(payload.get('wave_effects') or []) if isinstance(item, dict)]
        applied = {
            (str(item.get('effect') or '').strip(), str(item.get('node_id') or '').strip())
            for item in effects
        }
        decided = [
            str(item.get('source_node_id') or '').strip()
            for item in list(payload.get('decision_records') or [])
            if isinstance(item, dict)
            and str(item.get('action') or '').strip().lower() == _NOTICE_ACTION_RESUME_EXECUTION
            and str(item.get('source_node_id') or '').strip()
        ]
        missing = list(dict.fromkeys(node_id for node_id in decided if (_WAVE_EFFECT_ACCEPTANCE_INTERRUPT, node_id) not in applied))
        if not missing:
            return
        # 每落一个副作用立刻记账：波次若在两个副作用之间再崩，重放不会重复作废
        # 已处理过的验收（漏做打断正是本次事故的形态）。
        for execution_node_id in missing:
            skipped = ''
            execution = self._store.get_node(execution_node_id)
            metadata = dict(execution.metadata or {}) if execution is not None and isinstance(execution.metadata, dict) else {}
            handshake = normalize_acceptance_handshake(metadata.get(ACCEPTANCE_HANDSHAKE_KEY))
            acceptance_id = str(handshake.get('acceptance_node_id') or '').strip()
            acceptance = self._store.get_node(acceptance_id) if acceptance_id else None
            if acceptance is not None and str(getattr(acceptance, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                # 验收已终态：补打断只会作废一个已完成的判定，记为跳过。
                skipped = 'acceptance_terminal'
            else:
                await self._interrupt_acceptance_for_notice(
                    task_id=task_id,
                    execution_node_id=execution_node_id,
                    epoch_id=epoch_id,
                )
            effects.append(
                {
                    'effect': _WAVE_EFFECT_ACCEPTANCE_INTERRUPT,
                    'node_id': execution_node_id,
                    'skipped': skipped,
                    'created_at': now_iso(),
                }
            )
            payload['wave_effects'] = effects
            epoch = self._store.upsert_task_message_distribution_epoch(epoch.model_copy(update={'payload': payload}))

    async def _run_distribution_epoch(self, task_id: str) -> str:
        """执行一个子树分发波次；返回驱动器节奏控制用的波次结果。

        返回值：idle / deferred / draining / advanced / promoted / completed / failed。
        """
        task = self._store.get_task(task_id)
        if task is None:
            return 'idle'
        distribution = self._distribution_runtime_state(task_id)
        epoch_id = str(distribution.get('active_epoch_id') or '').strip()
        meta_state = str(distribution.get('state') or '').strip()
        if not epoch_id or meta_state not in _DISTRIBUTION_ACTIVE_STATES:
            return 'idle'
        epoch = self._store.get_task_message_distribution_epoch(task_id, epoch_id)
        if epoch is None:
            return 'idle'
        epoch_state = str(epoch.state or '').strip()
        if epoch_state in {'completed', 'failed', 'cancelled', 'cancelled_by_task_delete'}:
            return 'idle'
        payload = dict(epoch.payload or {})
        targets = self._epoch_target_node_ids(epoch, task)
        if not targets:
            self._fail_distribution_epoch(
                task_id,
                epoch_id=epoch_id,
                failed_node_id='',
                failure_reason='distribution targets missing',
            )
            return 'failed'
        # N2：任务人工暂停 → 延迟分发，绝不自动恢复任务。
        if bool(task.pause_requested) or bool(task.is_paused):
            return 'deferred'
        barrier_node_ids = self._derive_barrier_node_ids(task_id, targets)
        previous_barrier = [str(item or '').strip() for item in list(payload.get('barrier_node_ids') or [])]
        if previous_barrier != barrier_node_ids or list(payload.get('target_node_ids') or []) != targets:
            payload['barrier_node_ids'] = list(barrier_node_ids)
            payload['target_node_ids'] = list(targets)
            epoch = self._store.upsert_task_message_distribution_epoch(
                epoch.model_copy(update={'payload': payload})
            )
        if epoch_state != 'distributing' or meta_state != 'distributing':
            # N3：人工暂停的目标延迟等待（不拒绝）；其余目标照常推进。
            still_deferred: list[str] = []
            active_targets: list[str] = []
            for target_id in targets:
                target_node = self._store.get_node(target_id)
                if target_node is not None and self._node_operator_paused(target_node):
                    still_deferred.append(target_id)
                else:
                    active_targets.append(target_id)
            payload['deferred_frontier_node_ids'] = list(still_deferred)
            if not active_targets:
                epoch = self._store.upsert_task_message_distribution_epoch(
                    epoch.model_copy(update={'payload': payload})
                )
                self._publish_distribution_meta(
                    task_id,
                    epoch_id=epoch_id,
                    state='barrier_requested',
                    targets=targets,
                    blocked=barrier_node_ids,
                )
                return 'deferred'
            materialize_pending_entries = self._barrier_materialize_pending_entries(
                task_id=task_id,
                barrier_node_ids=barrier_node_ids,
            )
            drain_pending_node_ids = self._scoped_drain_pending_node_ids(
                task_id=task_id,
                barrier_node_ids=barrier_node_ids,
            )
            for item in materialize_pending_entries:
                parent_node_id = str(item.get('parent_node_id') or '').strip()
                if parent_node_id and parent_node_id not in drain_pending_node_ids:
                    drain_pending_node_ids.append(parent_node_id)
            # 自愈：未物化批次的父节点若已被屏障停摆（无存活 entry），它自己不会再
            # 跑，而物化只能由它自己的协程产出——干等就是互等死锁（要点 5）。
            await self._kick_stalled_spawn_round_parents(
                task_id=task_id,
                entries=materialize_pending_entries,
                payload=payload,
            )
            payload['drain_pending_node_ids'] = list(drain_pending_node_ids)
            payload['materialize_pending_entries'] = [dict(item) for item in materialize_pending_entries]
            if drain_pending_node_ids:
                self._store.upsert_task_message_distribution_epoch(
                    epoch.model_copy(update={'state': 'barrier_draining', 'payload': payload})
                )
                self._publish_distribution_meta(
                    task_id,
                    epoch_id=epoch_id,
                    state='barrier_draining',
                    targets=targets,
                    blocked=barrier_node_ids,
                )
                return 'draining'
            payload['frontier_node_ids'] = list(active_targets)
            payload.setdefault('distributed_node_ids', [])
            payload['next_frontier_node_ids'] = []
            epoch = self._store.upsert_task_message_distribution_epoch(
                epoch.model_copy(update={'state': 'distributing', 'payload': payload})
            )
            self._publish_distribution_meta(
                task_id,
                epoch_id=epoch_id,
                state='distributing',
                targets=targets,
                frontier=list(active_targets),
                blocked=barrier_node_ids,
            )
        else:
            # distributing 续波：恢复已解除暂停的延迟目标。
            deferred_targets = [
                str(item or '').strip()
                for item in list(payload.get('deferred_frontier_node_ids') or [])
                if str(item or '').strip()
            ]
            frontier_ids = [
                str(item or '').strip()
                for item in list(payload.get('frontier_node_ids') or [])
                if str(item or '').strip()
            ]
            distributed_ids = {
                str(item or '').strip()
                for item in list(payload.get('distributed_node_ids') or [])
                if str(item or '').strip()
            }
            remaining_deferred: list[str] = []
            rejoined = False
            for deferred_id in deferred_targets:
                deferred_node = self._store.get_node(deferred_id)
                if deferred_node is not None and self._node_operator_paused(deferred_node):
                    remaining_deferred.append(deferred_id)
                elif deferred_id not in frontier_ids and deferred_id not in distributed_ids:
                    frontier_ids.append(deferred_id)
                    rejoined = True
            if rejoined or remaining_deferred != deferred_targets:
                payload['frontier_node_ids'] = list(frontier_ids)
                payload['deferred_frontier_node_ids'] = list(remaining_deferred)
                epoch = self._store.upsert_task_message_distribution_epoch(
                    epoch.model_copy(update={'payload': payload})
                )
        payload = dict(epoch.payload or {})
        frontier = [
            str(item or '').strip()
            for item in list(payload.get('frontier_node_ids') or [])
            if str(item or '').strip()
        ]
        failure_reason = ''
        failure_node_id = ''
        turn_deferred: list[str] = []
        for node_id in list(frontier):
            node = self._store.get_node(node_id)
            if node is None:
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                # 防御：追加时已拒绝终态目标；级联中途漂移为终态则跳过，
                # 不做任何程序化下传（下传只能由节点自己的回合决定）。
                continue
            if self._node_operator_paused(node):
                turn_deferred.append(node_id)
                continue
            try:
                turn_result = await self._run_frontier_turn(task, epoch, node_id)
            except NodePausedError:
                # 竞态：回合前一刻节点被人工暂停 → 转为延迟目标。
                turn_deferred.append(node_id)
                continue
            if turn_result is None:
                continue
            if str(getattr(turn_result, 'status', '') or '').strip().lower() == 'failed':
                failure_reason = (
                    str(getattr(turn_result, 'blocking_reason', '') or '').strip()
                    or 'distribution turn failed'
                )
                failure_node_id = node_id
                break
        merged_deferred = list(dict.fromkeys([
            *([str(item or '').strip() for item in list(payload.get('deferred_frontier_node_ids') or []) if str(item or '').strip()]),
            *turn_deferred,
        ]))
        if failure_reason:
            refreshed = self._store.get_task_message_distribution_epoch(task_id, epoch_id)
            if refreshed is not None:
                refreshed_payload = dict(refreshed.payload or {})
                refreshed_payload['deferred_frontier_node_ids'] = merged_deferred
                self._store.upsert_task_message_distribution_epoch(
                    refreshed.model_copy(update={'payload': refreshed_payload})
                )
            self._fail_distribution_epoch(
                task_id,
                epoch_id=epoch_id,
                failed_node_id=failure_node_id,
                failure_reason=failure_reason,
            )
            return 'failed'
        # 决策与副作用分账执行：本波新落的 resume_execution 与崩溃重放漏做的副作用
        # 走同一条补齐路径，幂等由 wave_effects 保证。
        await self._apply_notice_interrupt_effects(task_id=task_id, epoch_id=epoch_id)
        refreshed_epoch = self._store.get_task_message_distribution_epoch(task_id, epoch_id)
        if refreshed_epoch is None:
            return 'idle'
        payload = dict(refreshed_epoch.payload or {})
        next_frontier = [
            str(item or '').strip()
            for item in list(payload.pop('next_frontier_node_ids', None) or [])
            if str(item or '').strip()
        ]
        payload['frontier_node_ids'] = list(next_frontier)
        payload['deferred_frontier_node_ids'] = merged_deferred
        if next_frontier or merged_deferred:
            self._store.upsert_task_message_distribution_epoch(
                refreshed_epoch.model_copy(update={'state': 'distributing', 'payload': payload})
            )
            self._publish_distribution_meta(
                task_id,
                epoch_id=epoch_id,
                state='distributing',
                targets=targets,
                frontier=list(next_frontier),
                blocked=barrier_node_ids,
            )
            return 'advanced' if next_frontier else 'deferred'
        queued_epochs = [
            item
            for item in list(self._store.list_active_task_message_distribution_epochs(task_id) or [])
            if str(item.state or '').strip() == 'queued'
        ]
        if queued_epochs:
            # 提升排队 epoch：按它自己的目标重建屏障与 frontier（不得继承
            # 旧 epoch 的 blocked 快照，也不得硬编码任务根）。
            next_epoch = queued_epochs[0]
            next_payload = dict(next_epoch.payload or {})
            next_targets = self._epoch_target_node_ids(next_epoch, task)
            next_barrier = self._derive_barrier_node_ids(task_id, next_targets)
            next_payload['target_node_ids'] = list(next_targets)
            next_payload['barrier_node_ids'] = list(next_barrier)
            next_payload['drain_pending_node_ids'] = list(next_barrier)
            next_payload['frontier_node_ids'] = []
            next_payload['deferred_frontier_node_ids'] = []
            next_payload.setdefault('distributed_node_ids', [])
            next_payload['next_frontier_node_ids'] = []
            self._store.upsert_task_message_distribution_epoch(
                next_epoch.model_copy(update={'state': 'pause_requested', 'payload': next_payload})
            )
            self._publish_distribution_meta(
                task_id,
                epoch_id=str(next_epoch.epoch_id or '').strip(),
                state='barrier_requested',
                targets=next_targets,
                blocked=next_barrier,
            )
            return 'promoted'
        completed_at = now_iso()
        # 释放账先落：完成序列的后半段（清 meta → 释放 held entries → 续跑分发）任何一处
        # 抛异常，meta 已清、hold 谓词关闭，「活跃态」判据再也看不见这个任务；这条账
        # 让对账器仍能认出「冻结过但没被释放」的形态并补跑释放。
        payload['release_pending'] = list(barrier_node_ids)
        completed_epoch = self._store.upsert_task_message_distribution_epoch(
            refreshed_epoch.model_copy(
                update={
                    'state': 'completed',
                    'completed_at': completed_at,
                    'payload': payload,
                }
            )
        )
        # 兜底：确保每个目标自己的通知都有本地记录（幂等，按 id 去重）。
        self._node_runner.queue_pending_target_distribution_notices(
            epoch=completed_epoch,
            created_at=completed_at,
        )
        # 需求三：先上传播（在 hold 释放前落库，计数与释放原子可见）。
        self._propagate_notice_upward(task_id=task_id, epoch=completed_epoch, targets=targets)
        pending_notice_node_ids = list(self._node_runner.nodes_with_pending_distribution_notices(task_id=task_id))
        # 清 meta（hold 谓词随之关闭）→ 释放 held entries → 复位失速时钟。
        self._publish_distribution_meta(
            task_id,
            epoch_id='',
            state='',
            mode='',
            targets=[],
            frontier=[],
            blocked=[],
            pending_notice=pending_notice_node_ids,
        )
        await self._release_scoped_epoch_holds(task_id, barrier_node_ids, target_node_ids=targets)
        self._reset_stall_clock(task_id)
        await self._resume_distribution_if_needed(task_id)
        self._clear_release_pending(task_id, epoch_id=epoch_id)
        return 'completed'

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
        if str(final_acceptance.status or '').strip().lower() in {'waiting_acceptance', 'waiting_execution_retry', 'waiting_block_verification'}:
            return self._result_from_node(root)

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
        acceptance_result = self._node_runner._handle_acceptance_node_result(
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
            acceptance = self._node_runner.create_acceptance_node(
                task=task,
                accepted_node=root,
                goal=f'最终验收:{root.goal}',
                acceptance_prompt=final_acceptance.prompt,
                parent_node_id=root.node_id,
                metadata={'final_acceptance': True},
            )
        self._update_final_acceptance_state(task_id, node_id=acceptance.node_id, status='running')
        return acceptance

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
