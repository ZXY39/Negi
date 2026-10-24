"""Final-submission guard: children must settle and their results must be consumed."""

from __future__ import annotations

from types import SimpleNamespace

from main.runtime.react_loop import ReActToolLoop


def _node(node_id, *, parent='', kind='execution', status='in_progress'):
    return SimpleNamespace(
        node_id=node_id,
        parent_node_id=parent or None,
        node_kind=kind,
        status=status,
        metadata={},
    )


def _notification(notification_id, *, source, kind='child_final', status='delivered', merged_at=''):
    return SimpleNamespace(
        notification_id=notification_id,
        source_node_id=source,
        kind=kind,
        status=status,
        merged_at=merged_at,
    )


class _Store:
    def __init__(self, nodes, notifications):
        self._nodes = nodes
        self._notifications = notifications

    def list_nodes(self, task_id):
        _ = task_id
        return list(self._nodes)

    def list_task_node_notifications(self, task_id, node_id):
        _ = task_id, node_id
        return list(self._notifications)


def _loop(nodes, notifications):
    loop = object.__new__(ReActToolLoop)
    loop._log_service = SimpleNamespace(_store=_Store(nodes, notifications))
    return loop


def test_non_terminal_execution_descendant_blocks():
    task = SimpleNamespace(task_id='task:1')
    parent = _node('node:p', parent='node:r', status='in_progress')
    child = _node('node:c', parent='node:p', status='in_progress')
    loop = _loop([parent, child], [])
    blockers = loop._final_submission_blockers(task=task, node=parent)
    assert blockers['non_terminal_node_ids'] == ['node:c']


def test_non_terminal_acceptance_descendant_does_not_block():
    task = SimpleNamespace(task_id='task:1')
    parent = _node('node:p', parent='node:r', status='in_progress')
    verifier = _node('node:v', parent='node:p', kind='acceptance', status='in_progress')
    loop = _loop([parent, verifier], [])
    blockers = loop._final_submission_blockers(task=task, node=parent)
    assert blockers['non_terminal_node_ids'] == []


def test_unread_and_held_child_messages_block():
    task = SimpleNamespace(task_id='task:1')
    parent = _node('node:p', parent='node:r', status='in_progress')
    child = _node('node:c', parent='node:p', status='success')
    notifications = [
        _notification('notif:1', source='node:c', status='delivered'),
        _notification('notif:2', source='node:c', status='consumed', merged_at=''),
        _notification('notif:3', source='node:c', status='consumed', merged_at='2026-10-25T00:00:00+00:00'),
    ]
    loop = _loop([parent, child], notifications)
    blockers = loop._final_submission_blockers(task=task, node=parent)
    assert blockers['pending_message_ids'] == ['notif:1', 'notif:2']


def test_settled_and_merged_children_do_not_block():
    task = SimpleNamespace(task_id='task:1')
    parent = _node('node:p', parent='node:r', status='in_progress')
    child = _node('node:c', parent='node:p', status='success')
    notifications = [
        _notification('notif:1', source='node:c', status='consumed', merged_at='2026-10-25T00:00:00+00:00'),
    ]
    loop = _loop([parent, child], notifications)
    blockers = loop._final_submission_blockers(task=task, node=parent)
    assert blockers == {'non_terminal_node_ids': [], 'pending_message_ids': []}
