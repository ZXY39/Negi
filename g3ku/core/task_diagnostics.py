"""Diagnostics helpers for stuck asyncio tasks.

Used by watchdogs (cron dispatch watchdog, bridge slow-prompt watchdog) to
dump the await chain of a task that has not returned, so a production hang
can be located from the log alone instead of requiring a debugger attached
to a live process.

Why not ``Task.get_stack()``: on the C-accelerated task implementation it
returns only the task's own coroutine frame — the docstring promise that
"stacks for all awaited coroutines are also returned" does not hold there.
The reliable chain is ``cr_await`` (coroutine → coroutine → ... → terminal
future/iterator). One hop is not expressible via ``cr_await``: ``await
some_task`` parks on an opaque ``FutureIter`` that exposes no reference to
the awaited task. For that hop the walker falls back to scanning the
suspended frame's locals for live ``asyncio.Task`` objects and continues
inside them — the bridge keeps the awaited session-prompt task in a local,
which is exactly the boundary that mattered in the 2026-09 cron hang
(dispatch → bridge.prompt → [opaque] → session.prompt → _prompt_locked).

This is a best-effort forensic dump, not a semantic contract: locals
crossing can pick up unrelated live tasks held by the frame (they are
rendered as their own short chains), and depth/width are hard-capped so a
pathological graph can never flood the log.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Hard caps so a pathological chain can never flood the log.
_MAX_CHAIN_LINES = 60
_MAX_LOCAL_TASKS_PER_FRAME = 3


def _label(obj: Any) -> str:
    name = getattr(obj, "__qualname__", "") or getattr(obj, "__name__", "")
    return name or type(obj).__name__


def _frame_of(obj: Any) -> Any:
    return getattr(obj, "cr_frame", None) or getattr(obj, "gi_frame", None)


def _live_local_tasks(frame: Any) -> list["asyncio.Task[Any]"]:
    """Live (not done) asyncio.Task objects referenced by a suspended frame."""
    found: list[asyncio.Task[Any]] = []
    try:
        values = list(frame.f_locals.values())
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return found
    for value in values:
        if isinstance(value, asyncio.Task) and not value.done():
            found.append(value)
        if len(found) >= _MAX_LOCAL_TASKS_PER_FRAME:
            break
    return found


def _walk_chain(root: Any, lines: list[str], seen: set[int]) -> None:
    current = root
    while current is not None and id(current) not in seen and len(lines) < _MAX_CHAIN_LINES:
        seen.add(id(current))
        if isinstance(current, asyncio.Task):
            coro = current.get_coro()
            lines.append(f"-> [awaited task {current.get_name()!r}] {_label(coro)}")
            current = coro
            continue

        frame = _frame_of(current)
        if frame is None:
            # Futures, opaque await-iterators, frameless generators: terminal.
            lines.append(f"<suspended on {type(current).__name__}>")
            return

        code = frame.f_code
        lines.append(f"{_label(current)} @ {code.co_filename}:{frame.f_lineno} in {code.co_name}")
        nxt = getattr(current, "cr_await", None)
        if nxt is None:
            nxt = getattr(current, "gi_yieldfrom", None)

        if isinstance(nxt, asyncio.Task):
            current = nxt  # crosses the boundary cleanly when visible
            continue
        if nxt is None or _frame_of(nxt) is None:
            # Deepest visible frame. Either suspended on an opaque
            # future/iterator (possibly an awaited Task) or with nothing
            # visible at all: try to continue through live task locals.
            if nxt is not None:
                lines.append(f"  <suspended on {type(nxt).__name__}>")
            for local_task in _live_local_tasks(frame):
                if id(local_task) in seen or len(lines) >= _MAX_CHAIN_LINES:
                    continue
                _walk_chain(local_task, lines, seen)
            return
        current = nxt


def format_task_await_chain(task: "asyncio.Task[Any]") -> str:
    """Render the current await chain of a task as log-friendly text.

    Ordered outermost-first; the deepest (currently suspended) frame is
    printed last — that is the await to inspect first. Diagnostics must never
    raise into the watchdog path, so any failure degrades to a placeholder.
    """
    try:
        coro = task.get_coro()
        header = f"task={task.get_name()!r} coro={_label(coro)}"
        lines: list[str] = []
        _walk_chain(coro, lines, set())
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return f"<await chain unavailable: {exc}>"
    if not lines:
        return f"{header}\n<empty await chain>"
    return header + "\n" + "\n".join(f"  {line}" for line in lines)
