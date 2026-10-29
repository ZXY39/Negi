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


def resolve_wait_targets(
    *,
    waiter: Any,
    waiting_for: Any,
    node_map: dict[str, Any],
) -> tuple[list[str], bool, list[dict[str, str]]]:
    """Resolve one ``enter_wait.waiting_for`` declaration.

    Returns ``(targets, external, problems)``:

    * ``targets`` — in-task node ids the waiter is waiting for. These are what the
      wait-for graph turns into edges, so they must be nodes that can actually
      address the waiter; a target that cannot is a guaranteed dead wait and is
      reported as a problem instead.
    * ``external`` — the declaration named ``frontdoor`` (or ``parent`` on a root
      node). Waiting on a human is a legitimate unbounded wait and never becomes
      an edge.
    * ``problems`` — one entry per rejected value. Non-empty means the caller must
      refuse the park and hand the reasons back to the model.

    Terminal targets are rejected here rather than turned into an instant wake:
    the answer is ready now, so parking would be a wasted round.
    """
    raw_items = waiting_for if isinstance(waiting_for, (list, tuple, set)) else [waiting_for]
    waiter_parent_id = node_parent_id(waiter)
    targets: list[str] = []
    seen: set[str] = set()
    external = False
    problems: list[dict[str, str]] = []
    for raw in raw_items:
        value = str(raw or '').strip()
        if not value:
            continue
        if value == 'frontdoor':
            external = True
            continue
        if value == 'parent':
            if waiter_parent_id:
                value = waiter_parent_id
            else:
                # 根节点没有父节点可等，它对上的唯一地址就是前门。
                external = True
                continue
        if value in seen:
            continue
        seen.add(value)
        target = node_map.get(value)
        if target is None:
            problems.append({'target': value, 'reason': 'wait_target_not_in_task'})
            continue
        if str(getattr(target, 'status', '') or '').strip().lower() in {'success', 'failed'}:
            problems.append({'target': value, 'reason': 'wait_target_terminal'})
            continue
        _, reject_reason = message_relation(sender=target, target=waiter, node_map=node_map)
        if reject_reason:
            problems.append({'target': value, 'reason': f'wait_target_not_messageable:{reject_reason}'})
            continue
        targets.append(value)
    return targets, external, problems


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
    'resolve_wait_targets',
]
