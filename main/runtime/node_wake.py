"""R2: the mailbox is the only thing that wakes a parked task node.

Until this module existed a notification could land in the mailbox and nothing
would start a turn — a node parked after ``wait`` (or paused by an operator)
only moved again when a worker happened to pick the task up. That is the single
mechanism this module adds: a delivered message reaches a parked node.

Three things are deliberately *not* done here:

* a running node is not woken a second time — its in-flight notice refresh folds
  new messages into the turn already in progress (single-flight);
* a terminal node is not woken and not messaged (terminal is absorbing);
* nothing here decides *whether* the message should wake: that is priority
  (``node_message_kind``) plus the node's own ``wake_on`` policy, both evaluated
  by :func:`wait_satisfied` / the caller.

Dependencies are injected so the decision and the dispatch loop can be tested
without a runtime, a database, or an event loop full of workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

TERMINAL_STATUSES = frozenset({'success', 'failed'})
EXECUTION_NODE_KIND = 'execution'
ACCEPTANCE_NODE_KIND = 'acceptance'

# Frame phases in which a node owns no turn and is waiting for an event.
PARKED_PHASES = frozenset({'waiting_explicit', 'waiting_acceptance'})

WAKE_ON_ANY = 'any'
WAKE_ON_ALL_CHILDREN = 'all_children'
WAKE_ON_VALUES = frozenset({WAKE_ON_ANY, WAKE_ON_ALL_CHILDREN})

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


def _settled(record: Mapping[str, Any]) -> bool:
    value = record.get('settled')
    if value is not None:
        return bool(value)
    return str(record.get('status') or '').strip().lower() in TERMINAL_STATUSES


def _node_kind(record: Mapping[str, Any]) -> str:
    return str(record.get('node_kind') or EXECUTION_NODE_KIND).strip().lower()


def execution_children(children: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Acceptance children are excluded from every settled-based judgement.

    An acceptance node stays un-settled until the node it inspects terminates
    (it terminates *with* its subject), so counting it would make the parent's
    ``all_children`` wait impossible to satisfy — parent waits for acceptance,
    acceptance waits for parent. That is not a wait-for cycle, so cycle
    detection cannot catch it; the exclusion is the only defence.
    """
    return [child for child in children if _node_kind(child) != ACCEPTANCE_NODE_KIND]


def all_children_satisfied(
    children: Iterable[Mapping[str, Any]],
    *,
    unread_final_sources: Iterable[str],
) -> bool:
    """``wake_on=all_children`` predicate.

    True when every direct execution child is settled *and* each of them already
    has its terminal message in this node's mailbox. A child that settled without
    delivering (crash window) keeps the parent waiting on purpose: waking without
    the result would make the parent re-derive a conclusion it cannot see.
    """
    execution = list(execution_children(children))
    if not execution:
        return True
    unread = {str(item or '').strip() for item in unread_final_sources if str(item or '').strip()}
    for child in execution:
        if not _settled(child):
            return False
        node_id = str(child.get('node_id') or '').strip()
        if node_id and node_id not in unread:
            return False
    return True


def wait_satisfied(
    *,
    wake_on: str,
    unread_count: int,
    children: Iterable[Mapping[str, Any]] = (),
    unread_final_sources: Iterable[str] = (),
) -> bool:
    """Whether a parked node's wake condition is met.

    ``wake_on=any`` — any unread message. ``wake_on=all_children`` — every direct
    execution child settled and reported; with no execution children the
    condition is vacuously true, which is what makes an empty park impossible.
    """
    policy = str(wake_on or '').strip() or WAKE_ON_ANY
    if policy not in WAKE_ON_VALUES:
        raise ValueError(f'unknown_wake_on:{policy}')
    if policy == WAKE_ON_ANY:
        return int(unread_count or 0) > 0
    return all_children_satisfied(children, unread_final_sources=unread_final_sources)


def wake_ready(
    *,
    wake_on: str,
    unread_count: int,
    has_high_priority: bool = False,
    children: Iterable[Mapping[str, Any]] = (),
    unread_final_sources: Iterable[str] = (),
) -> bool:
    """The one judgement every revival path must share.

    Two callers used to decide independently whether a parked node may run:
    delivery-time wake (``runtime_service._delivery_wakes_target``) and the
    dispatch-time runnable scan (``task_actor_service._dispatch_runnable_nodes``).
    The scan only asked "is there unread mail", so a parent parked on
    ``all_children`` was re-dispatched the moment its first child reported while
    the second was still running (live ``task:eb3f3a7753f7``: child A reported
    at 10:38:38, the root ran a turn at 10:38:43, child B finished at 10:45:36).

    ``has_high_priority`` is the P6 bypass: mail the recipient must act on
    (user / front door / abnormal pause / reject_no_progress) wakes regardless
    of the node's own policy. Everything else follows ``wake_on``.
    """
    if has_high_priority:
        return True
    return wait_satisfied(
        wake_on=wake_on,
        unread_count=unread_count,
        children=children,
        unread_final_sources=unread_final_sources,
    )


def wait_inputs(
    *,
    children: Iterable[Any],
    notifications: Iterable[Any],
    parent_node_id: str = '',
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the two ``wait_satisfied`` inputs from store rows.

    ``children`` are node records; ``notifications`` are mailbox rows. A
    child_final counts as "reported" once it is in the box — delivered or
    already consumed — because consuming the message is exactly what a waking
    turn does, and a re-park must not then treat the same child as unreported.
    """
    wanted_parent = str(parent_node_id or '').strip()
    child_rows: list[dict[str, Any]] = []
    for child in list(children or []):
        child_node_id = str(getattr(child, 'node_id', '') or '').strip()
        if not child_node_id:
            continue
        if wanted_parent and str(getattr(child, 'parent_node_id', '') or '').strip() != wanted_parent:
            continue
        child_rows.append(
            {
                'node_id': child_node_id,
                'status': str(getattr(child, 'status', '') or ''),
                'node_kind': str(getattr(child, 'node_kind', '') or ''),
            }
        )
    final_sources: list[str] = []
    for item in list(notifications or []):
        if str(getattr(item, 'kind', '') or '').strip() != 'child_final':
            continue
        if str(getattr(item, 'status', '') or '').strip() not in {'delivered', 'consumed'}:
            continue
        source = str(getattr(item, 'source_node_id', '') or '').strip()
        if source:
            final_sources.append(source)
    return child_rows, final_sources


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
    wake_on: str,
    unread_count: int,
    children: Iterable[Mapping[str, Any]] = (),
    unread_final_sources: Iterable[str] = (),
) -> tuple[bool, str]:
    """Whether ``enter_wait`` may park, and why not when it may not.

    Two refusals, both structural rather than advisory:

    * qualifying mail is already waiting — parking would strand a message that
      arrived a moment earlier, and the node would wait for a wake that already
      happened;
    * ``all_children`` with nothing left to wait for — an empty park is a node
      nobody can ever wake.

    Refusing means "do not park, keep going": the caller injects the mail and the
    turn continues.
    """
    policy = str(wake_on or '').strip() or WAKE_ON_ANY
    if policy not in WAKE_ON_VALUES:
        return False, f'unknown_wake_on:{policy}'
    if policy == WAKE_ON_ANY:
        if int(unread_count or 0) > 0:
            return False, 'unread_present'
        return True, ''
    if all_children_satisfied(children, unread_final_sources=unread_final_sources):
        return False, 'no_unsettled_children'
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
