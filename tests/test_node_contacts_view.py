"""Contacts view flags: family scope, self/verifier/controllable markers."""

from __future__ import annotations

import pytest

from main.models import NodeRecord
from main.runtime.node_runner import NodeRunner


def _node(node_id, *, parent=None, depth=0, kind='execution', accepted=''):
    return NodeRecord(
        node_id=node_id,
        task_id='task:view',
        parent_node_id=parent,
        root_node_id='node:root',
        depth=depth,
        node_kind=kind,
        goal=f'goal-{node_id}',
        prompt='p',
        created_at='2026-10-25T00:00:00+08:00',
        updated_at='2026-10-25T00:00:00+08:00',
        metadata={'accepted_node_id': accepted} if accepted else {},
    )


class _Store:
    def __init__(self, nodes):
        self._nodes = nodes

    def list_nodes(self, task_id):
        _ = task_id
        return list(self._nodes)

    def list_task_node_notifications(self, task_id, node_id):
        _ = task_id, node_id
        return []


class _Log:
    def read_runtime_frame(self, task_id, node_id):
        _ = task_id, node_id
        return {}


def _runner(nodes):
    runner = object.__new__(NodeRunner)
    runner._store = _Store(nodes)
    runner._log_service = _Log()
    return runner


@pytest.mark.asyncio
async def test_family_scope_marks_self_and_acceptance_targets():
    root = _node('node:root')
    self_node = _node('node:self', parent='node:root', depth=1)
    sibling = _node('node:sibling', parent='node:root', depth=1)
    child = _node('node:child', parent='node:self', depth=2)
    own_verifier = _node(
        'node:own-verifier', parent='node:self', depth=2, kind='acceptance', accepted='node:self'
    )
    parent_verifier = _node(
        'node:root-verifier', parent='node:root', depth=1, kind='acceptance', accepted='node:root'
    )
    child_verifier = _node(
        'node:child-verifier', parent='node:child', depth=3, kind='acceptance', accepted='node:child'
    )
    nodes = [root, self_node, sibling, child, own_verifier, parent_verifier, child_verifier]
    runner = _runner(nodes)

    payload = await runner._view_tree(
        task_id='task:view', node=self_node, depth=3, scope='family'
    )
    by_id = {item['node_id']: item for item in payload['nodes']}
    assert by_id['node:self']['relation'] == 'self'
    assert by_id['node:self']['is_self'] is True
    assert by_id['node:self']['messageable'] is False
    assert by_id['node:own-verifier']['messageable'] is False
    assert by_id['node:own-verifier']['controllable'] is False
    assert by_id['node:root-verifier']['messageable'] is False
    assert by_id['node:child-verifier']['messageable'] is True
    assert by_id['node:child']['messageable'] is True
    assert by_id['node:child']['controllable'] is True
    assert by_id['node:sibling']['messageable'] is True
    assert by_id['node:sibling']['controllable'] is False
    assert by_id['node:child']['acceptance'] is True
