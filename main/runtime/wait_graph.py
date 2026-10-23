"""Wait-for graph: structural cycle detection for parked nodes (P6 fallback).

A parked node can normally be woken by a mailbox message. The only structural
deadlock the runtime can prove without guessing about future user input is a
closed waiting cycle:

* an execution node waiting for an acceptance verdict while the acceptance node
  is itself parked waiting for the next submission (a verdict nobody will
  produce), or
* a parent parked on ``all_children`` while a child is parked on
  ``all_children`` and both closures only point at each other.

Nodes parked with ``wake_on=any`` are treated as having an external wake source
(any sender may deliver a message), so they never form an edge. Terminal nodes
settle and therefore also break the chain. The detector is deliberately
conservative: a false positive would wake a legitimately parked node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PARKED_PHASES = frozenset({'waiting_explicit', 'waiting_acceptance'})
TERMINAL_STATUSES = frozenset({'success', 'failed'})


@dataclass(frozen=True)
class WaitNode:
    node_id: str
    parent_node_id: str
    depth: int
    node_kind: str
    status: str
    phase: str
    wake_on: str = 'any'
    acceptance_node_id: str = ''
    subject_node_id: str = ''
    handshake_state: str = ''


def build_wait_nodes(
    *,
    records: list[Any],
    frames: dict[str, dict[str, Any]],
    handshake_lookup: Any = None,
) -> list[WaitNode]:
    """Turn store records + runtime frames into the detector's input."""
    nodes: list[WaitNode] = []
    for record in list(records or []):
        node_id = str(getattr(record, 'node_id', '') or '').strip()
        if not node_id:
            continue
        metadata = dict(getattr(record, 'metadata', None) or {}) if isinstance(getattr(record, 'metadata', None), dict) else {}
        frame = dict(frames.get(node_id) or {})
        node_kind = str(getattr(record, 'node_kind', '') or '').strip().lower()
        handshake = {}
        if node_kind == 'execution' and callable(handshake_lookup):
            handshake = dict(handshake_lookup(record) or {})
        nodes.append(
            WaitNode(
                node_id=node_id,
                parent_node_id=str(getattr(record, 'parent_node_id', '') or '').strip(),
                depth=int(getattr(record, 'depth', 0) or 0),
                node_kind=node_kind,
                status=str(getattr(record, 'status', '') or '').strip().lower(),
                phase=str(frame.get('phase') or '').strip(),
                wake_on=str(metadata.get('node_wait_wake_on') or 'any').strip() or 'any',
                acceptance_node_id=str(handshake.get('acceptance_node_id') or '').strip(),
                subject_node_id=str(metadata.get('accepted_node_id') or getattr(record, 'parent_node_id', '') or '').strip(),
                handshake_state=str(handshake.get('state') or '').strip(),
            )
        )
    return nodes


def _is_terminal(node: WaitNode) -> bool:
    return node.status in TERMINAL_STATUSES


def _is_parked(node: WaitNode) -> bool:
    return node.phase in PARKED_PHASES


def _wait_edges(nodes_by_id: dict[str, WaitNode], children_by_parent: dict[str, list[str]]) -> dict[str, list[str]]:
    edges: dict[str, list[str]] = {}
    for node in nodes_by_id.values():
        if _is_terminal(node) or not _is_parked(node):
            continue
        out: list[str] = []
        if (
            node.node_kind == 'execution'
            and node.handshake_state == 'waiting_acceptance'
            and node.acceptance_node_id
        ):
            acceptance = nodes_by_id.get(node.acceptance_node_id)
            if acceptance is not None and not _is_terminal(acceptance) and _is_parked(acceptance):
                out.append(acceptance.node_id)
        if node.node_kind == 'acceptance' and node.subject_node_id:
            subject = nodes_by_id.get(node.subject_node_id)
            if (
                subject is not None
                and not _is_terminal(subject)
                and subject.handshake_state == 'waiting_acceptance'
                and _is_parked(subject)
            ):
                out.append(subject.node_id)
        if node.node_kind == 'execution' and node.wake_on == 'all_children':
            for child_id in list(children_by_parent.get(node.node_id) or []):
                child = nodes_by_id.get(child_id)
                if child is None or _is_terminal(child) or child.node_kind != 'execution':
                    continue
                # `any` 停靠有外部唤醒源；只有同样 `all_children` 的子节点才构成闭合等待。
                if _is_parked(child) and child.wake_on == 'all_children':
                    out.append(child.node_id)
        if out:
            edges[node.node_id] = list(dict.fromkeys(out))
    return edges


def find_wait_cycles(nodes: list[WaitNode]) -> list[list[str]]:
    """Return the strongly-connected waiting components that form a cycle."""
    nodes_by_id = {node.node_id: node for node in list(nodes or [])}
    children_by_parent: dict[str, list[str]] = {}
    for node in nodes_by_id.values():
        if node.parent_node_id:
            children_by_parent.setdefault(node.parent_node_id, []).append(node.node_id)
    edges = _wait_edges(nodes_by_id, children_by_parent)

    index_counter = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def strongconnect(node_id: str) -> None:
        nonlocal index_counter
        indexes[node_id] = index_counter
        lowlinks[node_id] = index_counter
        index_counter += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for target in edges.get(node_id, []):
            if target not in indexes:
                strongconnect(target)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[target])
            elif target in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indexes[target])
        if lowlinks[node_id] != indexes[node_id]:
            return
        component: list[str] = []
        while True:
            current = stack.pop()
            on_stack.discard(current)
            component.append(current)
            if current == node_id:
                break
        if len(component) > 1 or node_id in edges.get(node_id, []):
            # 按深度排序：最靠近根的节点排第一，调用方据此选择强制唤醒对象。
            component.sort(key=lambda item: (nodes_by_id[item].depth, item))
            cycles.append(component)

    for node_id in list(edges.keys()):
        if node_id not in indexes:
            strongconnect(node_id)
    return cycles


@dataclass
class WaitCycleBreaker:
    """Apply the detector to one task (thin wrapper for testability)."""

    nodes: list[WaitNode] = field(default_factory=list)

    def cycles(self) -> list[list[str]]:
        return find_wait_cycles(self.nodes)
