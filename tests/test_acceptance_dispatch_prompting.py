"""验收派发的两个"催命"点：提交后重排一趟，以及每趟 pass 先扫一次。

实盘 task:4555863a6a49：五个执行节点提交后，验收节点被同一趟扫描在 38～41 分钟后才
一起拉起。原因有两条——提交不是唤醒源（交接信 `wake=False` 是设计），而唯一派发扫描
排在同一趟 pass 的串行通知复派之后。这两条各自钉一个用例。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from main.models import NodeRecord, TaskRecord
from main.runtime.node_runner import NodeRunner
from main.runtime.task_actor_service import TaskActorService

STAMP = '2026-10-25T20:00:00+08:00'


def _records() -> tuple[TaskRecord, NodeRecord]:
    task = TaskRecord(
        task_id='task:acc',
        title='acceptance prompting',
        user_request='acceptance prompting',
        root_node_id='node:root',
        created_at=STAMP,
        updated_at=STAMP,
    )
    root = NodeRecord(
        node_id='node:root',
        task_id='task:acc',
        root_node_id='node:root',
        goal='root',
        prompt='root',
        created_at=STAMP,
        updated_at=STAMP,
    )
    return task, root


class _Store:
    def __init__(self, task: TaskRecord, root: NodeRecord) -> None:
        self._task = task
        self._root = root

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._task if task_id == self._task.task_id else None

    def get_node(self, node_id: str) -> NodeRecord | None:
        return self._root if node_id == self._root.node_id else None


class _Dispatcher:
    def __init__(self) -> None:
        self.closed = False

    def schedule_node(self, node_id: str) -> None:
        _ = node_id

    async def execute_node(self, task_id: str, node_id: str) -> Any:
        raise AssertionError('parked root must not be executed in this scenario')

    async def close(self) -> None:
        self.closed = True


class TestScanRunsBeforeNoticeResume:
    def _service(self, order: list[str], dispatcher: _Dispatcher) -> TaskActorService:
        task, root = _records()
        service = object.__new__(TaskActorService)
        service._store = _Store(task, root)
        service._dispatchers = {}
        service._release_sweeps = {}
        service._stall_notifier = None
        service.node_watchdog = None
        service.web_role_blocked = None
        service._node_runner = SimpleNamespace(
            wait_pending_terminal_messages=lambda: asyncio.sleep(0),
            has_pending_terminal_messages=lambda: False,
        )
        service._log_service = SimpleNamespace(
            set_pause_state=lambda *a, **k: None,
            set_node_pause_state=lambda *a, **k: None,
            update_node_status=lambda *a, **k: None,
            refresh_task_view=lambda *a, **k: None,
        )
        service._create_dispatcher = lambda task_id: dispatcher

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        async def _dispatch(task_id: str, disp: Any) -> bool:
            order.append('dispatch')
            return False

        async def _resume(task_id: str) -> bool:
            order.append('resume-notice')
            return False

        async def _resume_acceptance(task_id: str) -> bool:
            order.append('resume-acceptance')
            return False

        async def _loop(*, task_id: str, dispatcher: Any, root_node_id: str, current_result: Any):
            order.append('scheduler-loop')
            return current_result, True

        service._resume_task_paused_nodes = _noop
        service._reconcile_orphan_in_progress_nodes = _noop
        service._dispatch_runnable_nodes = _dispatch
        service._resume_pending_notice_nodes = _resume
        service._resume_inflight_final_acceptance = _resume_acceptance
        service._terminal_result_after_notice_resume = lambda task_id: None
        service._frame_phase = lambda task_id, node_id: 'waiting_explicit'
        service._is_final_submission_result = lambda result: False
        service._run_final_acceptance_if_needed = _noop
        service._drive_scheduler_loop = _loop
        service._requeue_interrupted_task_best_effort = lambda task_id: None
        return service

    def test_runnable_scan_is_not_starved_by_the_notice_resume_phase(self):
        order: list[str] = []
        dispatcher = _Dispatcher()
        service = self._service(order, dispatcher)
        asyncio.run(service.run_task('task:acc'))
        assert order == ['dispatch', 'resume-notice', 'resume-acceptance', 'scheduler-loop']
        assert dispatcher.closed is True


class TestAcceptanceHandoffRequestsARequeue:
    def _runner(self) -> NodeRunner:
        return object.__new__(NodeRunner)

    def test_handoff_enqueues_one_pass(self):
        seen: list[str] = []
        runner = self._runner()
        runner.acceptance_handoff_requeue_callback = lambda task_id: seen.append(task_id)
        runner._request_acceptance_dispatch('task:acc')
        assert seen == ['task:acc']

    def test_requeue_failure_never_escapes_the_submission(self):
        def _boom(task_id: str) -> None:
            raise RuntimeError('scheduler closed')

        runner = self._runner()
        runner.acceptance_handoff_requeue_callback = _boom
        runner._request_acceptance_dispatch('task:acc')

    def test_missing_callback_is_a_no_op(self):
        runner = self._runner()
        runner.acceptance_handoff_requeue_callback = None
        runner._request_acceptance_dispatch('task:acc')
