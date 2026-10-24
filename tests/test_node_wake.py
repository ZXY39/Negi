"""Wake-trigger contract: only a parked node wakes, and acceptance never blocks it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from main.runtime.node_wake import (
    OUTCOME_IN_FLIGHT,
    OUTCOME_NO_NODE,
    OUTCOME_NO_RUNNING_LOOP,
    OUTCOME_RESUMED,
    OUTCOME_SCHEDULED,
    OUTCOME_TASK_PAUSED,
    OUTCOME_TASK_TERMINAL,
    OUTCOME_TERMINAL,
    WakeDeps,
    node_park_state,
    schedule_wake,
    wake_parked_node,
    wake_ready,
)


def _node(*, status='in_progress', is_paused=False, task_id='task:1', pause_requested=False):
    return SimpleNamespace(
        status=status,
        is_paused=is_paused,
        pause_requested=pause_requested,
        task_id=task_id,
    )


class _Recorder:
    def __init__(self):
        self.resumed: list[tuple[str, str]] = []
        self.enqueued: list[str] = []
        self.cleared: list[tuple[str, str]] = []


def _deps(
    *,
    node,
    phase='before_model',
    task=None,
    dispatcher=object(),
    recorder=None,
):
    record = recorder or _Recorder()
    task_obj = task if task is not None else SimpleNamespace(status='in_progress', is_paused=False)

    async def resume(task_id, node_id):
        record.resumed.append((task_id, node_id))
        return 'resumed'

    async def enqueue(task_id):
        record.enqueued.append(task_id)

    async def clear_pause(task_id, node_id):
        record.cleared.append((task_id, node_id))

    deps = WakeDeps(
        get_node=lambda node_id: node,
        get_phase=lambda task_id, node_id: phase,
        get_task=lambda task_id: task_obj,
        dispatcher_for=lambda task_id: dispatcher,
        resume_node_entry=resume,
        enqueue_task=enqueue,
        clear_pause=clear_pause,
    )
    return deps, record


class TestParkState:
    @pytest.mark.parametrize('status', ['success', 'failed', 'SUCCESS', ' Failed '])
    def test_terminal_wins_over_everything(self, status):
        assert node_park_state(status=status, is_paused=True, phase='waiting_explicit') == 'terminal'

    def test_paused_beats_phase(self):
        assert node_park_state(status='in_progress', is_paused=True, phase='waiting_acceptance') == 'paused'

    @pytest.mark.parametrize('phase', ['waiting_explicit', 'waiting_acceptance'])
    def test_waiting_phases(self, phase):
        assert node_park_state(status='in_progress', is_paused=False, phase=phase) == 'waiting'

    @pytest.mark.parametrize('phase', ['before_model', 'after_model', 'waiting_tool_results', ''])
    def test_everything_else_is_running(self, phase):
        assert node_park_state(status='in_progress', is_paused=False, phase=phase) == 'running'


class TestWakeReady:
    def test_one_unread_wakes(self):
        assert wake_ready(unread_count=1) is True
        assert wake_ready(unread_count=0) is False

    def test_high_priority_wakes_without_unread(self):
        assert wake_ready(unread_count=0, has_high_priority=True) is True


class TestWakeParkedNode:
    @pytest.mark.asyncio
    async def test_terminal_node_is_not_woken(self):
        deps, record = _deps(node=_node(status='success'))
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps) == OUTCOME_TERMINAL
        assert record.resumed == [] and record.enqueued == []

    @pytest.mark.asyncio
    async def test_running_node_is_not_woken_twice(self):
        deps, record = _deps(node=_node(), phase='before_model')
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps) == OUTCOME_IN_FLIGHT
        assert record.resumed == [] and record.enqueued == []

    @pytest.mark.asyncio
    async def test_waiting_node_resumes_through_the_dispatcher(self):
        deps, record = _deps(node=_node(), phase='waiting_explicit')
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps) == OUTCOME_RESUMED
        assert record.resumed == [('task:1', 'n')]

    @pytest.mark.asyncio
    async def test_no_dispatcher_schedules_the_task(self):
        deps, record = _deps(node=_node(), phase='waiting_acceptance', dispatcher=None)
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps) == OUTCOME_SCHEDULED
        assert record.enqueued == ['task:1']

    @pytest.mark.asyncio
    async def test_paused_node_clears_the_flag_first(self):
        deps, record = _deps(node=_node(is_paused=True), phase='before_model')
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps) == OUTCOME_RESUMED
        assert record.cleared == [('task:1', 'n')]
        assert record.resumed == [('task:1', 'n')]

    @pytest.mark.asyncio
    async def test_paused_task_blocks_the_wake(self):
        deps, record = _deps(
            node=_node(is_paused=True),
            task=SimpleNamespace(status='in_progress', is_paused=True),
        )
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps) == OUTCOME_TASK_PAUSED
        assert record.resumed == [] and record.cleared == []

    @pytest.mark.asyncio
    async def test_terminal_task_blocks_the_wake(self):
        deps, record = _deps(
            node=_node(),
            phase='waiting_explicit',
            task=SimpleNamespace(status='failed', is_paused=False),
        )
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps) == OUTCOME_TASK_TERMINAL
        assert record.resumed == []

    @pytest.mark.asyncio
    async def test_missing_node_or_foreign_task(self):
        deps, _ = _deps(node=None)
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps) == OUTCOME_NO_NODE
        deps2, _ = _deps(node=_node(task_id='task:2'))
        assert await wake_parked_node(task_id='task:1', node_id='n', deps=deps2) == OUTCOME_NO_NODE


def test_schedule_wake_outside_loop_is_reported_not_dropped():
    deps, _ = _deps(node=_node(), phase='waiting_explicit')
    assert schedule_wake(task_id='task:1', node_id='n', deps=deps) == OUTCOME_NO_RUNNING_LOOP
