"""Pure target matrix shared by delivery and the contacts view."""

from __future__ import annotations

from types import SimpleNamespace

from main.runtime.node_relationships import (
    execution_has_acceptance,
    message_relation,
    messageable_target_ids,
)


def _node(node_id, *, parent='', kind='execution', accepted='', metadata=None):
    return SimpleNamespace(
        node_id=node_id,
        parent_node_id=parent or None,
        node_kind=kind,
        metadata={'accepted_node_id': accepted, **(metadata or {})} if accepted else dict(metadata or {}),
    )


def _tree():
    root = _node('node:r')
    child = _node('node:c', parent='node:r')
    sibling = _node('node:s', parent='node:r')
    grandchild = _node('node:g', parent='node:c')
    root_verifier = _node('node:rv', parent='node:r', kind='acceptance', accepted='node:r')
    child_verifier = _node('node:cv', parent='node:c', kind='acceptance', accepted='node:c')
    grandchild_verifier = _node('node:gv', parent='node:g', kind='acceptance', accepted='node:g')
    nodes = [root, child, sibling, grandchild, root_verifier, child_verifier, grandchild_verifier]
    return {item.node_id: item for item in nodes}


def test_relation_matrix():
    nodes = _tree()
    sender = nodes['node:c']
    assert message_relation(sender=sender, target=nodes['node:r'], node_map=nodes) == ('child', '')
    assert message_relation(sender=sender, target=nodes['node:g'], node_map=nodes) == ('parent', '')
    assert message_relation(sender=sender, target=nodes['node:s'], node_map=nodes) == ('peer', '')
    assert message_relation(sender=sender, target=nodes['node:c'], node_map=nodes) == ('', 'self_target')
    assert message_relation(sender=sender, target=nodes['node:cv'], node_map=nodes) == (
        '',
        'own_acceptance_node',
    )
    assert message_relation(sender=sender, target=nodes['node:rv'], node_map=nodes) == (
        '',
        'parent_acceptance_node',
    )
    assert message_relation(sender=sender, target=nodes['node:gv'], node_map=nodes) == ('parent', '')


def test_messageable_targets_exclude_self_and_own_verifier():
    nodes = _tree()
    sender = nodes['node:c']
    targets, truncated = messageable_target_ids(
        sender=sender, node_map=nodes, sender_is_root=False
    )
    assert truncated is False
    assert 'node:r' in targets
    assert 'node:s' in targets
    assert 'node:g' in targets
    assert 'node:gv' in targets
    assert 'node:c' not in targets
    assert 'node:cv' not in targets
    assert 'node:rv' not in targets


def test_root_gets_frontdoor_target():
    nodes = _tree()
    targets, _ = messageable_target_ids(
        sender=nodes['node:r'], node_map=nodes, sender_is_root=True
    )
    assert 'frontdoor' in targets


def test_execution_has_acceptance():
    nodes = _tree()
    assert execution_has_acceptance(nodes['node:c'], nodes) is True
    assert execution_has_acceptance(nodes['node:s'], nodes) is False
