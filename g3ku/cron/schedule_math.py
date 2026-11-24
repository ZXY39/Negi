"""Shared schedule math for both wake lanes.

The session cron service and the node wake sweep both turn a ``CronSchedule``
into a next-fire timestamp; keeping one implementation avoids a second cron
parser drifting from the first. ``compute_next_run`` is the public form of the
cron service's historical ``_compute_next_run`` (which now delegates here).

``resolve_recurring_wake`` is the node lane's recurring-parameter resolver: it
validates the ``every_seconds`` / ``cron_expr`` / ``tz`` shape and computes the
first fire time. One-shot ``at`` / ``after_seconds`` resolution stays in
``g3ku.runtime.wake_clock`` (stdlib-only, shared with the frontdoor).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from g3ku.cron.timezones import resolve_timezone, validate_timezone_name
from g3ku.cron.types import CronSchedule
from g3ku.runtime.wake_clock import iso_from_ms

# Recurring wakes each cost a full node turn; a sub-minute loop burns tokens.
# cron expressions are minute-grained by construction, so this floor only bites
# `every_seconds`. Mechanism-level guard, not a prompt-side plea.
MIN_RECURRING_INTERVAL_SECONDS = 60


def compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute the next fire time in epoch ms, or None when not computable."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from croniter import croniter

            base_time = now_ms / 1000
            tz = resolve_timezone(schedule.tz)
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def resolve_recurring_wake(
    *,
    every_seconds: Any = None,
    cron_expr: Any = None,
    tz: Any = None,
    current_ms: int | None = None,
) -> tuple[CronSchedule | None, int, str]:
    """Normalize recurring params into ``(schedule, first_wake_ms, error)``.

    Exactly one of ``every_seconds`` / ``cron_expr``; ``tz`` only with
    ``cron_expr``; ``every_seconds`` floored at ``MIN_RECURRING_INTERVAL_SECONDS``.
    On any refusal the error text is model-readable and self-contained.
    """
    current = int(current_ms) if current_ms is not None else int(time.time() * 1000)
    expr = str(cron_expr or '').strip()
    tz_text = str(tz or '').strip()
    has_every = every_seconds not in (None, '')
    has_cron = bool(expr)

    if has_every and has_cron:
        return None, 0, 'Give either `every_seconds` or `cron_expr`, not both.'
    if not has_every and not has_cron:
        return None, 0, 'Give `every_seconds` or `cron_expr` for a recurring wake-up.'
    if tz_text and not has_cron:
        return None, 0, '`tz` can only be used with `cron_expr`.'

    if has_every:
        try:
            seconds = int(float(str(every_seconds).strip()))
        except (TypeError, ValueError):
            return None, 0, '`every_seconds` must be a whole number of seconds.'
        if seconds < MIN_RECURRING_INTERVAL_SECONDS:
            return None, 0, (
                f'`every_seconds` must be at least {MIN_RECURRING_INTERVAL_SECONDS}. '
                'Each recurring wake costs a full node turn; a sub-minute loop burns tokens.'
            )
        schedule = CronSchedule(kind='every', every_ms=seconds * 1000)
    else:
        if tz_text:
            try:
                validate_timezone_name(tz_text)
            except ValueError as exc:
                return None, 0, str(exc)
        schedule = CronSchedule(kind='cron', expr=expr, tz=tz_text or None)
        try:
            from croniter import croniter

            base_dt = datetime.fromtimestamp(current / 1000, tz=resolve_timezone(schedule.tz))
            croniter(expr, base_dt)
        except Exception as exc:
            return None, 0, f'`cron_expr` is not a valid cron expression: {expr!r} ({exc}).'

    first = compute_next_run(schedule, current)
    if not first or first <= current:
        return None, 0, (
            f'Could not compute a next fire time for this schedule. Current time: {iso_from_ms(current)}'
        )
    return schedule, int(first), ''


__all__ = [
    'MIN_RECURRING_INTERVAL_SECONDS',
    'compute_next_run',
    'resolve_recurring_wake',
]
