from __future__ import annotations

import asyncio

from main.errors import TaskPausedError
from main.models import NodeFinalResult, normalize_final_acceptance_metadata
from main.runtime.node_runner import SKIPPED_CHECK_RESULT


class TaskActorService:
    def __init__(self, *, store, log_service, node_runner, stall_notifier=None) -> None:
        self._store = store
        self._log_service = log_service
        self._node_runner = node_runner
        self._stall_notifier = stall_notifier

    async def run_task(self, task_id: str) -> None:
        task_record = self._store.get_task(task_id)
        if task_record is None:
            return
        if self._stall_notifier is not None and hasattr(self._stall_notifier, 'start_task'):
            self._stall_notifier.start_task(task_id)
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
                result = await self._node_runner.run_node(task_id, root_node.node_id)
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
            text = str(exc or 'task failed').strip() or 'task failed'
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
            latest = self._store.get_task(task_id)
            root = self._store.get_node(latest.root_node_id) if latest is not None else None
            if latest is not None and not latest.is_paused and result.status in {'success', 'failed'}:
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
        acceptance_result = await self._node_runner.run_node(task_id, acceptance.node_id)
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
        self._log_service.update_node_status(
            task_id,
            root.node_id,
            status='failed',
            final_output=execution_output,
            failure_reason=failure_reason,
        )
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
