"""Shared schedule math: next-run computation and recurring-wake resolution.

Both wake lanes (session cron service, node wake sweep) compute next-fire times
from one implementation; these guard the public helper and the node recurring
resolver, plus parity with the cron service's delegating private wrapper.
"""

from __future__ import annotations

from datetime import datetime, timezone

from g3ku.cron.schedule_math import (
    MIN_RECURRING_INTERVAL_SECONDS,
    compute_next_run,
    resolve_recurring_wake,
)
from g3ku.cron.types import CronSchedule
from g3ku.services.cron import _compute_next_run

# 2026-11-24T05:00:00Z, a fixed reference so cron assertions are tz-deterministic.
NOW_MS = int(datetime(2026, 11, 24, 5, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_compute_next_run_every_is_now_plus_interval():
    schedule = CronSchedule(kind='every', every_ms=90_000)
    assert compute_next_run(schedule, NOW_MS) == NOW_MS + 90_000


def test_compute_next_run_every_nonpositive_is_none():
    assert compute_next_run(CronSchedule(kind='every', every_ms=0), NOW_MS) is None
    assert compute_next_run(CronSchedule(kind='every', every_ms=-5), NOW_MS) is None


def test_compute_next_run_at_future_and_past():
    assert compute_next_run(CronSchedule(kind='at', at_ms=NOW_MS + 1000), NOW_MS) == NOW_MS + 1000
    assert compute_next_run(CronSchedule(kind='at', at_ms=NOW_MS - 1000), NOW_MS) is None


def test_compute_next_run_cron_daily_six_utc():
    schedule = CronSchedule(kind='cron', expr='0 6 * * *', tz='UTC')
    nxt = compute_next_run(schedule, NOW_MS)
    assert nxt is not None and nxt > NOW_MS
    dt = datetime.fromtimestamp(nxt / 1000, tz=timezone.utc)
    assert (dt.hour, dt.minute) == (6, 0)
    # 05:00 -> next 06:00 is the same day, one hour out.
    assert nxt == NOW_MS + 3_600_000


def test_compute_next_run_cron_invalid_is_none():
    assert compute_next_run(CronSchedule(kind='cron', expr='not a cron'), NOW_MS) is None


def test_cron_service_private_wrapper_delegates():
    for schedule in (
        CronSchedule(kind='every', every_ms=60_000),
        CronSchedule(kind='cron', expr='*/5 * * * *', tz='UTC'),
        CronSchedule(kind='at', at_ms=NOW_MS + 5000),
    ):
        assert _compute_next_run(schedule, NOW_MS) == compute_next_run(schedule, NOW_MS)


def test_resolve_recurring_every_valid():
    schedule, first, error = resolve_recurring_wake(every_seconds=120, current_ms=NOW_MS)
    assert error == ''
    assert schedule is not None and schedule.kind == 'every' and schedule.every_ms == 120_000
    assert first == NOW_MS + 120_000


def test_resolve_recurring_every_below_floor_rejected():
    schedule, first, error = resolve_recurring_wake(every_seconds=5, current_ms=NOW_MS)
    assert schedule is None and first == 0
    assert str(MIN_RECURRING_INTERVAL_SECONDS) in error


def test_resolve_recurring_every_non_integer_rejected():
    schedule, _, error = resolve_recurring_wake(every_seconds='abc', current_ms=NOW_MS)
    assert schedule is None and 'whole number' in error


def test_resolve_recurring_cron_valid():
    schedule, first, error = resolve_recurring_wake(cron_expr='0 6 * * *', tz='UTC', current_ms=NOW_MS)
    assert error == ''
    assert schedule is not None and schedule.kind == 'cron' and schedule.expr == '0 6 * * *'
    assert first == NOW_MS + 3_600_000


def test_resolve_recurring_cron_invalid_rejected():
    schedule, _, error = resolve_recurring_wake(cron_expr='61 * * * *', current_ms=NOW_MS)
    assert schedule is None and 'cron_expr' in error


def test_resolve_recurring_tz_only_with_cron():
    schedule, _, error = resolve_recurring_wake(every_seconds=120, tz='UTC', current_ms=NOW_MS)
    assert schedule is None and 'tz' in error


def test_resolve_recurring_invalid_tz_rejected():
    schedule, _, error = resolve_recurring_wake(cron_expr='0 6 * * *', tz='Mars/Olympus', current_ms=NOW_MS)
    assert schedule is None and error


def test_resolve_recurring_both_modes_rejected():
    schedule, _, error = resolve_recurring_wake(every_seconds=120, cron_expr='0 6 * * *', current_ms=NOW_MS)
    assert schedule is None and 'not both' in error


def test_resolve_recurring_neither_mode_rejected():
    schedule, _, error = resolve_recurring_wake(current_ms=NOW_MS)
    assert schedule is None and error
