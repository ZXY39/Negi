from __future__ import annotations

import asyncio

from main.errors import TaskPausedError
from main.models import NodeFinalResult


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
        result = NodeFinalResult(status='failed', output='task failed')
        root_node = self._store.get_node(task_record.root_node_id)
        try:
            if root_node is None:
                result = NodeFinalResult(status='failed', output='missing root node')
            else:
                result = await self._node_runner.run_node(task_id, root_node.node_id)
        except TaskPausedError:
            self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            self._active_tasks.pop(task_id, None)
            return
        except asyncio.CancelledError:
            result = NodeFinalResult(status='failed', output='canceled')
        except Exception as exc:
            result = NodeFinalResult(status='failed', output=str(exc))
        finally:
            latest = self._store.get_task(task_id)
            if latest is not None and not latest.is_paused and result.status in {'success', 'failed'}:
                self._log_service.update_node_status(
                    task_id,
                    latest.root_node_id,
                    status=result.status,
                    final_output=result.output,
                    failure_reason='' if result.status == 'success' else result.output,
                )
            self._active_tasks.pop(task_id, None)

    async def wait(self, task_id: str) -> None:
        task = self._active_tasks.get(task_id)
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            return

    async def cancel(self, task_id: str) -> None:
        task_record = self._store.get_task(task_id)
        if task_record is not None:
            self._log_service.request_cancel(task_id)
        active = self._active_tasks.get(task_id)
        if active is not None and not active.done():
            active.cancel()

    async def pause(self, task_id: str) -> None:
        self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)

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
