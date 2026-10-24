"""R2: the mailbox is the only thing that wakes a parked task node.

Until this module existed a notification could land in the mailbox and nothing
would start a turn — a node parked after ``wait`` (or paused by an operator)
only moved again when a worker happened to pick the task up. That is the single
mechanism this module adds: a delivered message reaches a parked node.

Three things are deliberately *not* done here:

* a running node is not woken a second time — its in-flight notice refresh folds
  new messages into the turn already in progress (single-flight);
* a terminal node is not woken and not messaged (terminal is absorbing);
* nothing here decides *whether* the message should wake: any injectable unread
  wakes; ``priority=high`` is the unconditional bypass. Batch holds are a
  delivery-time state on ``child_final`` rows, not a wake policy.

Dependencies are injected so the decision and the dispatch loop can be tested
without a runtime, a database, or an event loop full of workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

TERMINAL_STATUSES = frozenset({'success', 'failed'})

# Frame phases in which a node owns no turn and is waiting for an event.
PARKED_PHASES = frozenset({'waiting_explicit', 'waiting_acceptance'})

# outcomes
OUTCOME_TERMINAL = 'skipped_terminal'
OUTCOME_IN_FLIGHT = 'in_flight'
OUTCOME_RESUMED = 'resumed'
OUTCOME_SCHEDULED = 'scheduled'
OUTCOME_TASK_PAUSED = 'skipped_task_paused'
OUTCOME_TASK_TERMINAL = 'skipped_task_terminal'
OUTCOME_NO_NODE = 'skipped_missing_node'
OUTCOME_NO_RUNNING_LOOP = 'skipped_no_loop'


def node_park_state(*, status: str, is_paused: bool, phase: str) -> str:
    """Classify a node as ``terminal`` / ``paused`` / ``waiting`` / ``running``."""
    if str(status or '').strip().lower() in TERMINAL_STATUSES:
        return 'terminal'
    if bool(is_paused):
        return 'paused'
    if str(phase or '').strip() in PARKED_PHASES:
        return 'waiting'
    return 'running'


def wake_ready(
    *,
    unread_count: int,
    has_high_priority: bool = False,
) -> bool:
    """The one judgement every revival path must share.

    ``enter_wait`` is always ``any``: any injectable unread wakes the node.
    Batch batching is a delivery-time hold on ``child_final`` rows, not a wake
    policy; held rows never reach this counter.
    """
    return bool(has_high_priority) or int(unread_count or 0) > 0


@dataclass(frozen=True)
class WakeDeps:
    """Everything :func:`wake_parked_node` needs, injected for testability."""

    get_node: Callable[[str], Any]
    get_phase: Callable[[str, str], str]
    get_task: Callable[[str], Any]
    dispatcher_for: Callable[[str], Any]
    resume_node_entry: Callable[[str, str], Awaitable[str]]
    enqueue_task: Callable[[str], Awaitable[None]]
    clear_pause: Callable[[str, str], Awaitable[None]] | None = None


async def wake_parked_node(
    *,
    task_id: str,
    node_id: str,
    deps: WakeDeps,
) -> str:
    """Wake ``node_id`` if it is parked. Returns an outcome string for logs/tests."""
    normalized_task_id = str(task_id or '').strip()
    normalized_node_id = str(node_id or '').strip()
    if not normalized_task_id or not normalized_node_id:
        return OUTCOME_NO_NODE

    node = deps.get_node(normalized_node_id)
    if node is None:
        return OUTCOME_NO_NODE
    status = str(getattr(node, 'status', '') or '')
    if str(getattr(node, 'task_id', '') or '').strip() != normalized_task_id:
        return OUTCOME_NO_NODE

    state = node_park_state(
        status=status,
        is_paused=bool(getattr(node, 'is_paused', False) or getattr(node, 'pause_requested', False)),
        phase=deps.get_phase(normalized_task_id, normalized_node_id),
    )
    if state == 'terminal':
        # P3: terminal is absorbing. The delivery path rejects before reaching
        # here; this is the structural backstop.
        return OUTCOME_TERMINAL
    if state == 'running':
        # P7 single-flight: the in-flight notice refresh folds this message into
        # the turn already running. Waking would start a second one.
        return OUTCOME_IN_FLIGHT

    task = deps.get_task(normalized_task_id)
    if task is not None:
        if str(getattr(task, 'status', '') or '').strip().lower() in TERMINAL_STATUSES:
            return OUTCOME_TASK_TERMINAL
        if bool(getattr(task, 'is_paused', False)):
            return OUTCOME_TASK_PAUSED

    if state == 'paused' and deps.clear_pause is not None:
        # Clearing the flag is not resuming; it only lets the executor start.
        await deps.clear_pause(normalized_task_id, normalized_node_id)

    dispatcher = deps.dispatcher_for(normalized_task_id)
    if dispatcher is not None:
        await deps.resume_node_entry(normalized_task_id, normalized_node_id)
        return OUTCOME_RESUMED
    await deps.enqueue_task(normalized_task_id)
    return OUTCOME_SCHEDULED


def enter_wait_verdict(
    *,
    unread_count: int,
) -> tuple[bool, str]:
    """Whether ``enter_wait`` may park, and why not when it may not.

    A qualifying unread means "do not park, keep going": the caller injects the
    mail and the turn continues. A node with no children may park — it is waiting
    for a message, not for a settled child set.
    """
    if int(unread_count or 0) > 0:
        return False, 'unread_present'
    return True, ''


def schedule_wake(*, task_id: str, node_id: str, deps: WakeDeps) -> str:
    """Fire-and-forget variant for synchronous delivery sites.

    Returns ``OUTCOME_NO_RUNNING_LOOP`` when called outside an event loop instead
    of dropping the wake silently — the caller can log that as a defect, because
    every delivery site in the runtime runs inside one.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return OUTCOME_NO_RUNNING_LOOP
    loop.create_task(wake_parked_node(task_id=task_id, node_id=node_id, deps=deps))
    return 'scheduled_async'
