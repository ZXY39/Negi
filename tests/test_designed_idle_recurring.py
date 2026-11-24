"""Design-idle exemption counts an armed recurring timer as an external wake source.

A daily recurring node parks for ~24h between fires; without this it would draw a
stall notice every bucket. The unread gate stays: a wake that should have fired but
left the node parked with injectable unread is still reported as suspected stall.
"""

from __future__ import annotations

from types import SimpleNamespace

from main.runtime.designed_idle import task_is_designed_idle
from main.runtime.wait_graph import NODE_WAIT_EXTERNAL_KEY

TASK = 'task:idle'
NODE = 'node:root'


class _FakeStore:
    def __init__(self, nodes, timers=(), notifications=()):
        self._nodes = list(nodes)
        self._timers = list(timers)
        self._notifications = list(notifications)

    def list_nodes(self, task_id):
        return self._nodes

    def list_task_node_timers(self, task_id):
        return self._timers

    def list_task_node_notifications(self, task_id, node_id):
        return self._notifications


def _node(*, status='in_progress', paused=False, phase='waiting_explicit', metadata=None):
    return SimpleNamespace(
        node_id=NODE,
        node_kind='execution',
        status=status,
        is_paused=paused,
        pause_requested=False,
        metadata=dict(metadata or {}),
    ), {NODE: phase}


def _recurring():
    return SimpleNamespace(node_id=NODE, kind='recurring')


def _idle(store, phases):
    return task_is_designed_idle(
        store=store, log_service=None, task_id=TASK, runtime_state={'node_phases': phases},
    )


def test_recurring_timer_counts_as_external_wake_source():
    node, phases = _node()
    store = _FakeStore([node], timers=[_recurring()])
    assert _idle(store, phases) is True


def test_once_timer_alone_is_not_an_external_source():
    node, phases = _node()
    store = _FakeStore([node], timers=[SimpleNamespace(node_id=NODE, kind='once')])
    assert _idle(store, phases) is False


def test_recurring_with_injectable_unread_is_not_idle():
    node, phases = _node()
    unread = SimpleNamespace(status='delivered', held_at='')
    store = _FakeStore([node], timers=[_recurring()], notifications=[unread])
    assert _idle(store, phases) is False


def test_paused_node_is_not_idle_even_with_recurring():
    node, phases = _node(paused=True)
    store = _FakeStore([node], timers=[_recurring()])
    assert _idle(store, phases) is False


def test_running_phase_is_not_idle_even_with_recurring():
    node, phases = _node(phase='waiting_tool_results')
    store = _FakeStore([node], timers=[_recurring()])
    assert _idle(store, phases) is False


def test_wait_external_metadata_still_counts_without_timer():
    node, phases = _node(metadata={NODE_WAIT_EXTERNAL_KEY: True})
    store = _FakeStore([node])
    assert _idle(store, phases) is True


def test_terminal_node_alone_is_not_idle():
    node, phases = _node(status='success')
    store = _FakeStore([node], timers=[_recurring()])
    assert _idle(store, phases) is False
