from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from main.errors import TaskPausedError, describe_exception
from main.models import NodeFinalResult, normalize_final_acceptance_metadata
from main.protocol import now_iso
from main.runtime.node_runner import SKIPPED_CHECK_RESULT

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
        waits = [entry.future for entry in entries if not entry.future.done()]
        if waits:
            await asyncio.gather(*[asyncio.shield(future) for future in waits], return_exceptions=True)

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
        entry = self._entries.get(normalized_node_id)
        if entry is not None:
            return entry
        node = self._store.get_node(normalized_node_id)
        if node is None:
            raise ValueError(f'node not found: {normalized_node_id}')
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
        except asyncio.CancelledError:
            if not entry.future.done():
                entry.future.cancel()
            raise
        except Exception as exc:
            if not entry.future.done():
                entry.future.set_exception(exc)
        else:
            if not entry.future.done():
                entry.future.set_result(result)
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
        self._node_runner.nested_node_executor = self._execute_nested_node
        self._node_runner.cancel_node_subtree_executor = self._cancel_node_subtree

    async def _resume_distribution_if_needed(self, task_id: str) -> None:
        resume_callback = self.distribution_resume_callback
        if not callable(resume_callback):
            return
        result = resume_callback(task_id)
        if asyncio.iscoroutine(result):
            await result

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
                if str(distribution.get('state') or '').strip() in {'pause_requested', 'barrier_requested', 'paused', 'distributing'}:
                    distribution_result = await self._run_distribution_epoch(task_id)
                    if distribution_result is not None:
                        control_only_return = True
                        return
                result = await dispatcher.execute_node(task_id, root_node.node_id)
                if result.status == 'success':
                    result = await self._run_final_acceptance_if_needed(task_id)
        except TaskPausedError:
            self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            return
        except asyncio.CancelledError:
            latest = self._store.get_task(task_id)
            if latest is not None and bool(latest.pause_requested) and not bool(latest.cancel_requested):
                self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
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

    def _barrier_drain_pending_node_ids(self, *, task_id: str, barrier_node_ids: list[str]) -> list[str]:
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
            frame = frame_map.get(node_id)
            if frame is None:
                continue
            phase = str(getattr(frame, 'phase', '') or '').strip()
            if phase not in _DISTRIBUTION_BARRIER_SAFE_PHASES:
                pending_node_ids.append(node_id)
        return pending_node_ids

    def _queue_root_distribution_notices(self, *, epoch, created_at: str) -> None:
        self._node_runner._queue_pending_root_distribution_notices(epoch=epoch, created_at=created_at)

    async def _run_distribution_epoch(self, task_id: str) -> bool | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        distribution = self._distribution_runtime_state(task_id)
        epoch_id = str(distribution.get('active_epoch_id') or '').strip()
        if not epoch_id:
            return None
        epoch = self._store.get_task_message_distribution_epoch(task_id, epoch_id)
        if epoch is None:
            return None
        state = str(distribution.get('state') or '').strip()
        payload = dict(epoch.payload or {})
        barrier_node_ids = [
            str(item or '').strip()
            for item in list(payload.get('barrier_node_ids') or [])
            if str(item or '').strip()
        ]
        pending_notice_node_ids = [
            str(item or '').strip()
            for item in list(distribution.get('pending_notice_node_ids') or [])
            if str(item or '').strip()
        ]
        if not pending_notice_node_ids:
            root_node_id = str(epoch.root_node_id or '').strip()
            if root_node_id:
                pending_notice_node_ids = [root_node_id]
        frontier = [
            str(item or '').strip()
            for item in list(distribution.get('frontier_node_ids') or [])
            if str(item or '').strip()
        ]
        if barrier_node_ids and state in {'pause_requested', 'barrier_requested', 'paused', 'barrier_draining'}:
            drain_pending_node_ids = self._barrier_drain_pending_node_ids(
                task_id=task_id,
                barrier_node_ids=barrier_node_ids,
            )
            payload['drain_pending_node_ids'] = list(drain_pending_node_ids)
            if drain_pending_node_ids:
                self._store.upsert_task_message_distribution_epoch(
                    epoch.model_copy(update={'state': 'barrier_draining', 'payload': payload})
                )
                self._log_service.update_task_runtime_meta(
                    task_id,
                    distribution={
                        'active_epoch_id': epoch_id,
                        'state': 'barrier_draining',
                        'mode': 'task_wide_barrier',
                        'frontier_node_ids': [],
                        'blocked_node_ids': list(barrier_node_ids),
                        'pending_notice_node_ids': list(pending_notice_node_ids),
                        'queued_epoch_count': int(distribution.get('queued_epoch_count') or 0),
                        'pending_mailbox_count': int(distribution.get('pending_mailbox_count') or 0),
                    },
                )
                return True
        if state in {'pause_requested', 'barrier_requested', 'paused', 'barrier_draining'}:
            frontier = frontier or [str(task.root_node_id or '').strip()]
            payload['frontier_node_ids'] = list(frontier)
            payload.setdefault('distributed_node_ids', [])
            payload['next_frontier_node_ids'] = []
            epoch = self._store.upsert_task_message_distribution_epoch(
                epoch.model_copy(update={'state': 'distributing', 'payload': payload})
            )
            distribution['state'] = 'distributing'
            distribution['frontier_node_ids'] = list(frontier)
            if barrier_node_ids:
                distribution['mode'] = 'task_wide_barrier'
                distribution['blocked_node_ids'] = list(barrier_node_ids)
                distribution['pending_notice_node_ids'] = list(pending_notice_node_ids)
            self._log_service.update_task_runtime_meta(task_id, distribution=distribution)
        if not frontier:
            return True
        for node_id in frontier:
            await self._execute_node(task_id, node_id)
        refreshed_epoch = self._store.get_task_message_distribution_epoch(task_id, epoch_id)
        if refreshed_epoch is None:
            return True
        payload = dict(refreshed_epoch.payload or {})
        next_frontier = [
            str(item or '').strip()
            for item in list(payload.pop('next_frontier_node_ids') or [])
            if str(item or '').strip()
        ]
        payload['frontier_node_ids'] = list(next_frontier)
        if next_frontier:
            self._store.upsert_task_message_distribution_epoch(
                refreshed_epoch.model_copy(update={'state': 'distributing', 'payload': payload})
            )
            self._log_service.update_task_runtime_meta(
                task_id,
                distribution={
                    'active_epoch_id': epoch_id,
                    'state': 'distributing',
                    'mode': str(distribution.get('mode') or '').strip(),
                    'frontier_node_ids': list(next_frontier),
                    'blocked_node_ids': list(distribution.get('blocked_node_ids') or []),
                    'pending_notice_node_ids': list(distribution.get('pending_notice_node_ids') or []),
                    'queued_epoch_count': int(distribution.get('queued_epoch_count') or 0),
                    'pending_mailbox_count': int(distribution.get('pending_mailbox_count') or 0),
                },
            )
            await self._resume_distribution_if_needed(task_id)
            return True
        queued_epochs = [
            item
            for item in list(self._store.list_active_task_message_distribution_epochs(task_id) or [])
            if str(item.state or '').strip() == 'queued'
        ]
        if queued_epochs:
            next_epoch = queued_epochs[0]
            next_payload = dict(next_epoch.payload or {})
            next_payload['frontier_node_ids'] = [str(task.root_node_id or '').strip()]
            next_payload.setdefault('distributed_node_ids', [])
            next_payload['next_frontier_node_ids'] = []
            self._store.upsert_task_message_distribution_epoch(
                next_epoch.model_copy(update={'state': 'distributing', 'payload': next_payload})
            )
            remaining_queued = max(0, len(queued_epochs) - 1)
            self._log_service.update_task_runtime_meta(
                task_id,
                distribution={
                    'active_epoch_id': next_epoch.epoch_id,
                    'state': 'distributing',
                    'mode': str(distribution.get('mode') or '').strip(),
                    'frontier_node_ids': [str(task.root_node_id or '').strip()],
                    'blocked_node_ids': list(distribution.get('blocked_node_ids') or []),
                    'pending_notice_node_ids': list(distribution.get('pending_notice_node_ids') or []),
                    'queued_epoch_count': remaining_queued,
                    'pending_mailbox_count': int(distribution.get('pending_mailbox_count') or 0),
                },
            )
            return await self._run_distribution_epoch(task_id)
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
        self._queue_root_distribution_notices(epoch=completed_epoch, created_at=completed_at)
        self.clear_pause(task_id)
        pending_notice_node_ids = list(self._node_runner.nodes_with_pending_distribution_notices(task_id=task_id))
        if pending_notice_node_ids:
            self._log_service.update_task_runtime_meta(
                task_id,
                distribution={
                    'active_epoch_id': epoch_id,
                    'state': 'resume_ready',
                    'mode': 'task_wide_barrier',
                    'frontier_node_ids': [],
                    'blocked_node_ids': [],
                    'pending_notice_node_ids': pending_notice_node_ids,
                    'queued_epoch_count': 0,
                    'pending_mailbox_count': self._node_runner.pending_distribution_mailbox_count(task_id=task_id),
                },
            )
        else:
            self._log_service.update_task_runtime_meta(
                task_id,
                distribution={
                    'active_epoch_id': '',
                    'state': '',
                    'mode': '',
                    'frontier_node_ids': [],
                    'blocked_node_ids': [],
                    'pending_notice_node_ids': [],
                    'queued_epoch_count': 0,
                    'pending_mailbox_count': 0,
                },
            )
        await self._resume_distribution_if_needed(task_id)
        return True

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

        acceptance = await self._get_or_create_final_acceptance_node(
            task_id=task_id,
            task=task,
            root=root,
            final_acceptance=final_acceptance,
        )
        acceptance_result = await self._execute_node(task_id, acceptance.node_id)
        acceptance = self._store.get_node(acceptance.node_id) or acceptance
        execution_output = str(root.final_output or '').strip()
        check_result = str(acceptance_result.summary or acceptance_result.output or acceptance.failure_reason or '').strip() or SKIPPED_CHECK_RESULT
        self._log_service.update_node_check_result(task_id, root.node_id, check_result)
        self._update_final_acceptance_state(
            task_id,
            node_id=acceptance.node_id,
            status='passed' if acceptance_result.status == 'success' else 'failed',
        )
        if acceptance_result.status == 'success':
            self._record_final_execution_output(task_id, '')
            return NodeFinalResult(
                status='success',
                delivery_status='final',
                summary=check_result or execution_output or 'final acceptance passed',
                answer=execution_output,
                evidence=[],
                remaining_work=[],
                blocking_reason='',
            )

        self._record_final_execution_output(task_id, execution_output)
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
