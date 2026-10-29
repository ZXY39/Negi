"""Wait-for graph: structural cycle detection for parked nodes (P6 fallback).

A parked node can normally be woken by a mailbox message. The runtime can prove
two kinds of structural deadlock without guessing about future user input:

* an execution node waiting for an acceptance verdict while the acceptance node
  is itself parked waiting for the next submission (a verdict nobody will
  produce);
* a closed cycle of ``enter_wait`` declarations — ``waiting_for`` names who may
  wake the node, so A waiting for B while B waits for A is a real cycle rather
  than a guess.

Every parked node is externally wakeable by any mailbox message, so spawn-batch
waits are a tree-shaped chain rather than a cycle. Declarations that name
``frontdoor`` produce no edge at all: waiting on a human is a legitimate
unbounded wait. Terminal nodes settle and therefore also break the chain. The
detector is deliberately conservative: a false positive would wake a
legitimately parked node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Node metadata key carrying the resolved `enter_wait.waiting_for` ids. It lives on
# the node row (not the frame) so the reverse lookup — "who declared they wait for
# this node" — is one `list_nodes` pass with no per-node frame reads. Edges are only
# built for parked nodes, so a stale value on a node that already woke is inert.
NODE_WAIT_TARGETS_KEY = 'node_wait_targets'
# Companion flag: the declaration named `frontdoor`, i.e. a human is expected to
# wake this node. An all-parked task is legitimate only if someone set this.
NODE_WAIT_EXTERNAL_KEY = 'node_wait_external'

PARKED_PHASES = frozenset({'waiting_explicit', 'waiting_acceptance'})
# Every phase in which an execution node owns no turn and will not produce a
# message by itself. The watchdog uses the same set for the same reason.
IDLE_PHASES = frozenset({'waiting_explicit', 'waiting_children', 'waiting_acceptance'})
TERMINAL_STATUSES = frozenset({'success', 'failed'})


@dataclass(frozen=True)
class WaitNode:
    node_id: str
    parent_node_id: str
    depth: int
    node_kind: str
    status: str
    phase: str
    acceptance_node_id: str = ''
    subject_node_id: str = ''
    handshake_state: str = ''
    wait_targets: tuple[str, ...] = ()
    wait_external: bool = False


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
        raw_targets = metadata.get(NODE_WAIT_TARGETS_KEY)
        wait_targets = tuple(
            str(item or '').strip() for item in list(raw_targets or []) if str(item or '').strip()
        )
        nodes.append(
            WaitNode(
                node_id=node_id,
                parent_node_id=str(getattr(record, 'parent_node_id', '') or '').strip(),
                depth=int(getattr(record, 'depth', 0) or 0),
                node_kind=node_kind,
                status=str(getattr(record, 'status', '') or '').strip().lower(),
                phase=str(frame.get('phase') or '').strip(),
                acceptance_node_id=str(handshake.get('acceptance_node_id') or '').strip(),
                subject_node_id=str(metadata.get('accepted_node_id') or getattr(record, 'parent_node_id', '') or '').strip(),
                handshake_state=str(handshake.get('state') or '').strip(),
                wait_targets=wait_targets,
                wait_external=bool(metadata.get(NODE_WAIT_EXTERNAL_KEY)),
            )
        )
    return nodes


def _is_terminal(node: WaitNode) -> bool:
    return node.status in TERMINAL_STATUSES


def _is_parked(node: WaitNode) -> bool:
    return node.phase in PARKED_PHASES


def _wait_edges(nodes_by_id: dict[str, WaitNode]) -> dict[str, list[str]]:
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
        if node.phase == 'waiting_explicit' and node.wait_targets:
            # 声明式等待：只连"还停靠着"的目标。运行中的目标在推进，成不了环；
            # 终态的目标由定向唤醒负责，不在这里补一条假边。
            for target_id in node.wait_targets:
                target = nodes_by_id.get(target_id)
                if target is None or _is_terminal(target) or not _is_parked(target):
                    continue
                out.append(target_id)
        if out:
            edges[node.node_id] = list(dict.fromkeys(out))
    return edges


def find_all_parked_waits(nodes: list[WaitNode]) -> list[str] | None:
    """Return the ids to force-wake when a task has no reachable waker.

    A task is provably stuck when every non-terminal execution node sits in an
    idle phase and none of the explicitly parked ones declared an external
    (``frontdoor``) waker. Any execution node outside those phases is either
    running or queued, so it can still produce a message and the task is not
    stuck. Acceptance nodes are excluded: they are driven by the handshake and
    never wake anyone by sending.

    Returns the explicitly parked ids ordered shallowest-first, or ``None``.
    """
    explicit: list[WaitNode] = []
    external = False
    for node in list(nodes or []):
        if node.node_kind != 'execution' or _is_terminal(node):
            continue
        if node.phase == 'waiting_explicit':
            explicit.append(node)
            if node.wait_external:
                external = True
            continue
        if node.phase in IDLE_PHASES:
            continue
        return None
    if not explicit or external:
        return None
    explicit.sort(key=lambda item: (item.depth, item.node_id))
    return [item.node_id for item in explicit]


def find_wait_cycles(nodes: list[WaitNode]) -> list[list[str]]:
    """Return the strongly-connected waiting components that form a cycle."""
    nodes_by_id = {node.node_id: node for node in list(nodes or [])}
    edges = _wait_edges(nodes_by_id)

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
