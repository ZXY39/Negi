from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from loguru import logger

from main.errors import DistributionHoldError, NodePausedError, TaskPausedError, describe_exception
from main.models import NodeFinalResult, normalize_final_acceptance_metadata, normalize_result_payload
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
from main.runtime.node_runner import SKIPPED_CHECK_RESULT
from main.runtime.pending_notice_state import RESUME_MODE_WAIT_FOR_CHILDREN
from main.runtime.subtree_hold import DISTRIBUTION_ACTIVE_STATES, INSPECTION_RESUME_MARKER, NOTICE_INTERRUPT_REASON

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
_DISTRIBUTION_DRIVER_POLL_SECONDS = 1.0
# 决策回合 resume_execution 的结果标记（单一来源：main.runtime.subtree_hold）。
_INSPECTION_RESUME_MARKER = INSPECTION_RESUME_MARKER
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
            return await future
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
        return await entry.future

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
        waits = [entry.future for entry in entries if not entry.future.done()]
        if waits:
            await asyncio.gather(*[asyncio.shield(future) for future in waits], return_exceptions=True)

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
        except DistributionHoldError:
            # 子树分发屏障冻结：对包括根在内的所有节点保持 future pending。
            # 父管线自然停摆；分发驱动器在释放时对每个 held entry 调
            # resume_node 在原 future 上重跑。绝不能 set_exception——那会被
            # 父管线的通用错误处理转成 spawn 运行时错误、杀死整条分支。
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
        # 每任务单飞的子树分发驱动器（side asyncio.Task）。
        self._epoch_drivers: dict[str, asyncio.Task[None]] = {}
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
        for node_id in pending_node_ids:
            await self._execute_node(task_id, node_id)
        refreshed = self._distribution_runtime_state(task_id)
        if any(str(item or '').strip() for item in list(refreshed.get('pending_notice_node_ids') or [])):
            await self._resume_distribution_if_needed(task_id)
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
                resumed_pending_notices = await self._resume_pending_notice_nodes(task_id)
                if resumed_pending_notices:
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
        if not child_node_id:
            return False
        child = self._store.get_node(child_node_id)
        if child is None or str(child.task_id or '').strip() != str(task_id or '').strip():
            return False
        metadata = dict(child.metadata or {}) if isinstance(child.metadata, dict) else {}
        if str(metadata.get('spawn_owner_kind') or '').strip().lower() != 'child':
            return False
        if str(metadata.get('spawn_owner_parent_node_id') or '').strip() != str(parent_node_id or '').strip():
            return False
        if str(metadata.get('spawn_owner_round_id') or '').strip() != str(round_id or '').strip():
            return False
        try:
            owner_entry_index = int(metadata.get('spawn_owner_entry_index'))
        except (TypeError, ValueError):
            return False
        if owner_entry_index != int(entry_index):
            return False
        return bool(self._node_runner.node_is_in_live_distribution_tree(task_id=task_id, node_id=child_node_id))

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
        advanced/promoted 立即续波。崩溃只记日志退出——状态全部持久化，
        下一次 ensure（追加/恢复/run_task 入口）会重建驱动器续跑。
        """
        deferred_polls = 0
        while True:
            try:
                outcome = await self._run_distribution_epoch(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('distribution driver wave crashed for {}', task_id)
                return
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

    async def _release_scoped_epoch_holds(self, task_id: str, barrier_node_ids: list[str]) -> None:
        """释放子树冻结：对每个 held entry 重跑（meta 已先行清除，hold 谓词关闭）。"""
        dispatcher = self._dispatchers.get(task_id)
        if dispatcher is None:
            return
        for raw_node_id in list(barrier_node_ids or []):
            node_id = str(raw_node_id or '').strip()
            if not node_id:
                continue
            entry = dispatcher._entries.get(node_id)
            if entry is None or entry.future.done():
                continue
            node = self._store.get_node(node_id)
            if node is None:
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            if self._node_operator_paused(node):
                # 人工暂停的节点保持暂停，不因分发释放被唤醒。
                continue
            await dispatcher.resume_node(node_id)

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
            if str(getattr(turn_result, 'delivery_status', '') or '').strip() == _INSPECTION_RESUME_MARKER:
                await self._interrupt_acceptance_for_notice(
                    task_id=task_id,
                    execution_node_id=node_id,
                    epoch_id=epoch_id,
                )
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
        await self._release_scoped_epoch_holds(task_id, barrier_node_ids)
        self._reset_stall_clock(task_id)
        await self._resume_distribution_if_needed(task_id)
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
            delivery_status='final',
            summary=check_result,
            answer=execution_output,
            evidence=[],
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

        if acceptance_status in {'passed', 'failed'}:
            self._reconcile_root_acceptance_handshake_after_notice_resume(
                root=root,
                acceptance_node_id=acceptance_node_id,
                state=ACCEPTANCE_STATE_ACCEPTED if acceptance_status == 'passed' else ACCEPTANCE_STATE_REJECTED_TERMINAL,
                rejection_feedback='' if acceptance_status == 'passed' else (check_result or str(task.failure_reason or '').strip()),
                root_result=root_result,
            )
            summary = check_result or execution_output or (
                'final acceptance passed' if acceptance_status == 'passed' else 'final acceptance failed'
            )
            return NodeFinalResult(
                status='success',
                delivery_status='final',
                summary=summary,
                answer=execution_output,
                evidence=list(root_result.evidence or []),
                remaining_work=[],
                blocking_reason='',
            )

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
            max_rejections=int(handshake.get('max_rejections') or 3),
            latest_execution_result_ref=result_ref,
            latest_execution_result_summary=result_summary,
            latest_rejection_feedback_ref='',
            latest_rejection_feedback_summary=str(rejection_feedback or '').strip(),
        )
