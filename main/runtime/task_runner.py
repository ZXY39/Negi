from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from main.errors import TaskPausedError
from main.models import NodeFinalResult, normalize_final_acceptance_metadata, normalize_result_payload
from main.runtime.node_runner import SKIPPED_CHECK_RESULT


class TaskRunner:
    def __init__(self, *, store, log_service, node_runner) -> None:
        self._store = store
        self._log_service = log_service
        self._node_runner = node_runner
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

    def start_background(self, task_id: str) -> None:
        task = self._active_tasks.get(task_id)
        if task is not None and not task.done():
            return
        self._active_tasks[task_id] = asyncio.create_task(self.run_task(task_id), name=f'main-task:{task_id}')

    async def run_task(self, task_id: str) -> None:
        task_record = self._store.get_task(task_id)
        if task_record is None:
            return
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
            self._active_tasks.pop(task_id, None)
            return
        except asyncio.CancelledError:
            latest = self._store.get_task(task_id)
            if latest is not None and bool(latest.pause_requested) and not bool(latest.cancel_requested):
                self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
                self._active_tasks.pop(task_id, None)
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
            self._active_tasks.pop(task_id, None)

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

        acceptance = await self._get_or_create_final_acceptance_node(task_id=task_id, task=task, root=root, final_acceptance=final_acceptance)
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

    async def _get_or_create_final_acceptance_node(self, *, task_id: str, task, root, final_acceptance) -> Any:
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
        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
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

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            if execution_output:
                metadata['final_execution_output'] = execution_output
            else:
                metadata.pop('final_execution_output', None)
            return metadata

        self._log_service.update_task_metadata(task_id, _mutate, mark_unread=False)

    @staticmethod
    def _result_from_node(node) -> NodeFinalResult:
        payload = normalize_result_payload((node.metadata or {}).get('result_payload'))
        if payload is not None and str(payload.status or '').strip().lower() == str(node.status or '').strip().lower():
            return payload
        final_output = str(node.final_output or '').strip()
        failure_reason = str(node.failure_reason or '').strip()
        return NodeFinalResult(
            status='success' if str(node.status or '') == 'success' else 'failed',
            delivery_status='final' if str(node.status or '') == 'failed' and (failure_reason or str(node.check_result or '').strip() or final_output) else 'blocked' if str(node.status or '') == 'failed' else 'final',
            summary=failure_reason or final_output or 'node finished',
            answer=final_output,
            evidence=[],
            remaining_work=[],
            blocking_reason=failure_reason if str(node.status or '') == 'failed' else '',
        )

    async def wait(self, task_id: str) -> None:
        task = self._active_tasks.get(task_id)
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return
            raise

    def is_active(self, task_id: str) -> bool:
        task = self._active_tasks.get(task_id)
        return bool(task is not None and not task.done())

    async def cancel(self, task_id: str) -> None:
        task_record = self._store.get_task(task_id)
        if task_record is not None:
            self._log_service.request_cancel(task_id)
        active = self._active_tasks.get(task_id)
        if active is not None and not active.done():
            active.cancel()

    async def pause(self, task_id: str) -> None:
        self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
        active = self._active_tasks.get(task_id)
        if active is not None and not active.done():
            active.cancel()
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(active), timeout=1.0)

    async def resume(self, task_id: str) -> None:
        task_record = self._store.get_task(task_id)
        if task_record is None:
            return
        self._log_service.set_pause_state(task_id, pause_requested=False, is_paused=False)
        self.start_background(task_id)

    async def close(self) -> None:
        tasks = [task for task in self._active_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()
