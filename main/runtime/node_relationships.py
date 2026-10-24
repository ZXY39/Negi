"""Pure relationship/target rules for node-to-node messages.

Both the delivery service and the node-facing contacts view read these rules so
the id list the model sees and the targets the runtime accepts cannot drift.
"""

from __future__ import annotations

from typing import Any

from main.runtime.node_message_kind import RELATION_CHILD, RELATION_PARENT, RELATION_PEER


def node_id(record: Any) -> str:
    return str(getattr(record, 'node_id', '') or '').strip()


def node_parent_id(record: Any) -> str:
    return str(getattr(record, 'parent_node_id', '') or '').strip()


def node_kind(record: Any) -> str:
    return str(getattr(record, 'node_kind', '') or '').strip().lower()


def node_metadata(record: Any) -> dict[str, Any]:
    metadata = getattr(record, 'metadata', None)
    return dict(metadata) if isinstance(metadata, dict) else {}


def accepted_node_id(record: Any) -> str:
    return str(node_metadata(record).get('accepted_node_id') or '').strip()


def is_descendant(target: Any, ancestor_id: str, node_map: dict[str, Any]) -> bool:
    current = node_parent_id(target)
    wanted = str(ancestor_id or '').strip()
    seen: set[str] = set()
    while current and current not in seen:
        if current == wanted:
            return True
        seen.add(current)
        parent = node_map.get(current)
        if parent is None:
            return False
        current = node_parent_id(parent)
    return False


def message_relation(
    *,
    sender: Any,
    target: Any,
    node_map: dict[str, Any],
) -> tuple[str, str]:
    """Return ``(relation, reject_reason)`` for one node-authored target."""
    sender_id = node_id(sender)
    target_id = node_id(target)
    if not sender_id or not target_id:
        return '', 'target_missing'
    if target_id == sender_id:
        return '', 'self_target'

    target_kind = node_kind(target)
    sender_parent_id = node_parent_id(sender)
    target_parent_id = node_parent_id(target)
    descendant = is_descendant(target, sender_id, node_map)

    if target_kind == 'acceptance':
        accepted = accepted_node_id(target)
        if accepted and accepted == sender_id:
            return '', 'own_acceptance_node'
        if accepted and sender_parent_id and accepted == sender_parent_id:
            return '', 'parent_acceptance_node'
        if not descendant:
            return '', 'acceptance_node_not_messageable'
        return RELATION_PARENT, ''

    if target_id == sender_parent_id:
        return RELATION_CHILD, ''
    if descendant or target_parent_id == sender_id:
        return RELATION_PARENT, ''
    if sender_parent_id and target_parent_id == sender_parent_id:
        return RELATION_PEER, ''
    return '', 'not_messageable'


def messageable_target_ids(
    *,
    sender: Any,
    node_map: dict[str, Any],
    sender_is_root: bool,
    limit: int = 50,
) -> tuple[list[str], bool]:
    sender_id = node_id(sender)
    sender_parent_id = node_parent_id(sender)
    targets: list[str] = []
    if sender_parent_id:
        targets.append(sender_parent_id)
    for item in node_map.values():
        item_id = node_id(item)
        if not item_id or item_id == sender_id:
            continue
        relation, reject_reason = message_relation(sender=sender, target=item, node_map=node_map)
        if reject_reason or not relation:
            continue
        targets.append(item_id)
    if sender_is_root:
        targets.append('frontdoor')
    ordered: list[str] = []
    seen: set[str] = set()
    for item in targets:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    truncated = len(ordered) > limit
    return ordered[:limit], truncated


def execution_has_acceptance(node: Any, node_map: dict[str, Any], *, task_final_node_id: str = '') -> bool:
    """Whether an execution node has a dedicated acceptance node watching it."""
    target_id = node_id(node)
    if not target_id:
        return False
    if task_final_node_id and target_id == task_final_node_id:
        return True
    for item in node_map.values():
        if node_kind(item) != 'acceptance':
            continue
        if accepted_node_id(item) == target_id:
            return True
    return False


__all__ = [
    'accepted_node_id',
    'execution_has_acceptance',
    'is_descendant',
    'message_relation',
    'messageable_target_ids',
    'node_id',
    'node_kind',
    'node_metadata',
    'node_parent_id',
]
