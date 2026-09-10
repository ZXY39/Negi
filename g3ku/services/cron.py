"""Cron service for scheduling agent tasks."""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from g3ku.core.task_diagnostics import format_task_await_chain
from g3ku.cron.timezones import resolve_timezone, validate_timezone_name
from g3ku.cron.types import (
    CRON_STORE_VERSION,
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronStore,
)


_FAILED_AT_RETRY_DELAY_MS = 60_000

# 投递级看门狗默认预算：必须大于最长合理回合时长（provider 单次尝试默认上限
# 10 分钟 + 退避重试轮），只对真正楔死的 dispatch 兜底。<=0 表示关闭看门狗
# （无限等待，旧行为）。可被构造参数或 config.json 的 cron 节覆盖。
DEFAULT_DISPATCH_TIMEOUT_SECONDS = 1800.0
# 看门狗取消 dispatch 后等待其展开（CancelledError 收尾）的宽限；超过即视为
# 抗取消任务，脱离调度器单独放弃（保留 in-flight 直到任务真正终结）。
DEFAULT_DISPATCH_CANCEL_GRACE_SECONDS = 10.0
# 定时器任务意外死亡后的自愈重臂延迟（避免损坏状态下的 0 延迟热循环）。
_TIMER_SELF_HEAL_DELAY_SECONDS = 5.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _configured_cron_value(name: str) -> float | None:
    """Read one tunable from the live runtime config `cron` section, if any.

    Mirrors the lazy-read pattern of external_events._configured_buffer_size:
    the cron service stays importable without the config subsystem (CLI shells,
    unit tests), and any resolution failure falls back to module defaults.
    """
    try:
        from g3ku.config.live_runtime import get_runtime_config

        config = get_runtime_config(force=False)[0]
        value = getattr(getattr(config, "cron", None), name, None)
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _format_local_time_ms(now_ms: int) -> str:
    dt = datetime.fromtimestamp(now_ms / 1000).astimezone().replace(microsecond=0)
    offset = dt.strftime("%z")
    if len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {offset}".strip()


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        # Next interval from now
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from croniter import croniter
            # Use caller-provided reference time for deterministic scheduling
            base_time = now_ms / 1000
            tz = resolve_timezone(schedule.tz)
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            validate_timezone_name(schedule.tz)
        except ValueError as exc:
            raise ValueError(str(exc)) from None


def _normalize_max_runs(*, schedule: CronSchedule, max_runs: int | None) -> int:
    if schedule.kind == "at":
        return 1
    if max_runs is None:
        return 1
    normalized = int(max_runs)
    if normalized <= 0:
        raise ValueError("max_runs must be a positive integer")
    return normalized


class CronService:
    """Service for managing and executing scheduled jobs."""

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        *,
        dispatch_timeout_s: float | None = None,
        dispatch_cancel_grace_s: float | None = None,
    ):
        self.store_path = store_path
        self.on_job = on_job
        self._store: CronStore | None = None
        self._last_mtime_ns: int = 0
        self._timer_task: asyncio.Task | None = None
        self._heal_task: asyncio.Task | None = None
        self._running = False
        # Job ids currently inside a claimed dispatch; blocks concurrent
        # dispatch of the same job from overlapping entry points (timer tick vs
        # run_job) and excludes the job's stale next_run from wake arithmetic
        # while the run is unresolved. An abandoned (cancel-resistant) dispatch
        # keeps its id here until the detached task really ends.
        self._in_flight: set[str] = set()
        # Independent per-job dispatch tasks spawned by _on_timer. Tracked so
        # stop() can cancel in-flight dispatches instead of orphaning them.
        self._dispatch_tasks: set[asyncio.Task] = set()
        # Explicit constructor values win; None resolves per-dispatch from the
        # live runtime config `cron` section, then module defaults.
        self._dispatch_timeout_s = dispatch_timeout_s
        self._dispatch_cancel_grace_s = dispatch_cancel_grace_s

    def _resolve_dispatch_timeout_s(self) -> float:
        if self._dispatch_timeout_s is not None:
            return float(self._dispatch_timeout_s)
        configured = _configured_cron_value("dispatch_timeout_seconds")
        if configured is not None:
            return configured
        return DEFAULT_DISPATCH_TIMEOUT_SECONDS

    def _resolve_dispatch_cancel_grace_s(self) -> float:
        if self._dispatch_cancel_grace_s is not None:
            return float(self._dispatch_cancel_grace_s)
        configured = _configured_cron_value("dispatch_cancel_grace_seconds")
        if configured is not None:
            return configured
        return DEFAULT_DISPATCH_CANCEL_GRACE_SECONDS

    def _load_store(self) -> CronStore:
        """Load jobs from disk. Reloads automatically if file was modified externally."""
        if self._store and self.store_path.exists():
            mtime_ns = self.store_path.stat().st_mtime_ns
            if mtime_ns != self._last_mtime_ns:
                logger.info("Cron: jobs.json modified externally, reloading")
                self._store = None
        if self._store:
            return self._store

        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                version = int(data.get("version", 0) or 0)
                if version < CRON_STORE_VERSION:
                    logger.warning(
                        "Cron: dropping incompatible legacy store version {} at {}",
                        version,
                        self.store_path,
                    )
                    self._store = CronStore()
                    self._save_store()
                    return self._store
                jobs = []
                for j in data.get("jobs", []):
                    jobs.append(CronJob(
                        id=j["id"],
                        name=j["name"],
                        enabled=j.get("enabled", True),
                        schedule=CronSchedule(
                            kind=j["schedule"]["kind"],
                            at_ms=j["schedule"].get("atMs"),
                            every_ms=j["schedule"].get("everyMs"),
                            expr=j["schedule"].get("expr"),
                            tz=j["schedule"].get("tz"),
                        ),
                        payload=CronPayload(
                            kind=j["payload"].get("kind", "agent_turn"),
                            message=j["payload"].get("message", ""),
                            stop_condition=j["payload"].get("stopCondition"),
                            max_runs=max(1, int(j["payload"].get("maxRuns", 1) or 1)),
                            deliver=j["payload"].get("deliver", False),
                            channel=j["payload"].get("channel"),
                            to=j["payload"].get("to"),
                            session_key=j["payload"].get("sessionKey"),
                        ),
                        state=CronJobState(
                            next_run_at_ms=j.get("state", {}).get("nextRunAtMs"),
                            last_run_at_ms=j.get("state", {}).get("lastRunAtMs"),
                            delivered_runs=max(0, int(j.get("state", {}).get("deliveredRuns", 0) or 0)),
                            last_delivered_at_ms=j.get("state", {}).get("lastDeliveredAtMs"),
                            last_status=j.get("state", {}).get("lastStatus"),
                            last_error=j.get("state", {}).get("lastError"),
                        ),
                        created_at_ms=j.get("createdAtMs", 0),
                        updated_at_ms=j.get("updatedAtMs", 0),
                        delete_after_run=j.get("deleteAfterRun", False),
                    ))
                self._store = CronStore(version=CRON_STORE_VERSION, jobs=jobs)
                self._last_mtime_ns = self.store_path.stat().st_mtime_ns
            except Exception as e:
                logger.warning("Failed to load cron store: {}", e)
                self._store = CronStore()
                self._last_mtime_ns = self.store_path.stat().st_mtime_ns
        else:
            self._store = CronStore()
            self._last_mtime_ns = 0

        return self._store

    def _save_store(self) -> None:
        """Save jobs to disk atomically.

        Writes to a temp file then os.replace()s it over the store so a crash
        mid-write can never truncate jobs.json. A truncated store would be
        treated as corrupt by _load_store and reset to an empty store, losing
        persisted claims and re-arming already-dispatched jobs.
        """
        if not self._store:
            return

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "stopCondition": j.payload.stop_condition,
                        "maxRuns": j.payload.max_runs,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                        "sessionKey": j.payload.session_key,
                    },
                    "state": {
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "deliveredRuns": j.state.delivered_runs,
                        "lastDeliveredAtMs": j.state.last_delivered_at_ms,
                        "lastStatus": j.state.last_status,
                        "lastError": j.state.last_error,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "deleteAfterRun": j.delete_after_run,
                }
                for j in self._store.jobs
            ]
        }

        tmp_path = self.store_path.with_name(self.store_path.name + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self.store_path)
        self._last_mtime_ns = self.store_path.stat().st_mtime_ns
    
    async def start(self) -> None:
        """Start the cron service."""
        if self._running:
            return
        if not callable(self.on_job):
            raise RuntimeError("cron job handler is not configured")
        self._running = True
        self._load_store()
        self._recover_interrupted_runs()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        logger.info("Cron service started with {} jobs", len(self._store.jobs if self._store else []))

    def stop(self) -> None:
        """Stop the cron service.

        Cancels the timer and every independent dispatch task. Cancelled
        dispatches finalize themselves as ``interrupted`` on their way out (see
        _dispatch_claimed), so the store never stays in ``running`` across a
        shutdown.
        """
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        if self._heal_task:
            self._heal_task.cancel()
            self._heal_task = None
        for task in list(self._dispatch_tasks):
            task.cancel()

    def _recover_interrupted_runs(self) -> None:
        """Reconcile jobs whose claim was persisted but dispatch never finalized.

        A job still marked "running" at startup means the process restarted
        between the claim write and the finalize write. The dispatch may
        already have reached the downstream handler, so one-shot "at" jobs are
        suppressed (at-most-once): a duplicate reminder is worse than a missed
        one. Recurring jobs simply resume their schedule.
        """
        if not self._store:
            return
        for job in self._store.jobs:
            if job.state.last_status != "running":
                continue
            job.state.last_status = "interrupted"
            job.updated_at_ms = _now_ms()
            if job.schedule.kind == "at":
                job.state.next_run_at_ms = None
                job.state.last_error = (
                    "dispatch was interrupted by a runtime restart; "
                    "suppressed to avoid duplicate delivery"
                )
                if job.delete_after_run or max(1, int(job.payload.max_runs or 1)) <= 1:
                    job.enabled = False
                logger.warning(
                    "Cron: one-shot job '{}' ({}) interrupted by restart; suppressed",
                    job.name,
                    job.id,
                )
            else:
                job.state.last_error = None
                logger.info(
                    "Cron: recurring job '{}' ({}) interrupted by restart; resuming schedule",
                    job.name,
                    job.id,
                )

    def _recompute_next_runs(self) -> None:
        """Restore next run times after startup without replaying every missed tick."""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            max_runs = max(1, int(getattr(job.payload, "max_runs", 1) or 1))
            if int(job.state.delivered_runs or 0) >= max_runs:
                job.enabled = False
                job.state.next_run_at_ms = None
                continue
            if not job.enabled:
                job.state.next_run_at_ms = None
                continue
            current_next = job.state.next_run_at_ms
            if job.schedule.kind == "at":
                if job.state.last_run_at_ms:
                    job.state.next_run_at_ms = None
                elif job.schedule.at_ms is not None and job.schedule.at_ms <= now:
                    job.state.next_run_at_ms = now
                else:
                    job.state.next_run_at_ms = job.schedule.at_ms
                continue
            if current_next is not None and current_next <= now:
                job.state.next_run_at_ms = now
                continue
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs.

        In-flight jobs are excluded: their next_run is still the stale (past)
        claim value until the dispatch finalizes, and arming on it would spin
        a zero-delay tick loop for the whole dispatch duration. Each finalize
        re-arms the timer itself, so the excluded job's next window is picked
        up as soon as its run resolves.
        """
        if not self._store:
            return None
        times = [
            j.state.next_run_at_ms for j in self._store.jobs
            if j.enabled and j.state.next_run_at_ms and j.id not in self._in_flight
        ]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return

        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick(), name="cron-timer-tick")
        self._timer_task.add_done_callback(self._on_timer_task_done)

    def _on_timer_task_done(self, task: asyncio.Task) -> None:
        """Liveness watchdog for the scheduler heartbeat itself.

        A tick task must never die silently: without this callback an
        unexpected exception on the tick path (e.g. a store save failure inside
        _on_timer's finally) would end the task with no re-arm and no log —
        the whole scheduler going dark was failure mode #3 of the 2026-09
        production stall. Cancelled ticks are normal: every re-arm cancels its
        predecessor, including the tick that triggered it.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error(
            "Cron: timer task died unexpectedly ({!r}); self-heal re-arm in {:.0f}s",
            exc,
            _TIMER_SELF_HEAL_DELAY_SECONDS,
        )
        if not self._running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _heal():
            await asyncio.sleep(_TIMER_SELF_HEAL_DELAY_SECONDS)
            if self._running:
                self._arm_timer()

        if self._heal_task is not None and not self._heal_task.done():
            self._heal_task.cancel()
        self._heal_task = loop.create_task(_heal(), name="cron-timer-self-heal")

    async def _on_timer(self) -> None:
        """Handle timer tick - dispatch due jobs as independent tasks.

        Each due job is claimed synchronously and then dispatched in its own
        task: one wedged dispatch can never pin the tick (and therefore every
        other job's schedule) — the 2026-09 production stall had exactly that
        signature, where a single hung session.prompt kept the whole scheduler
        silent for 40 minutes until a remove_job cancelled the timer task.
        Save + re-arm run in a finally so the scheduler survives even a broken
        tick body.
        """
        try:
            self._load_store()
            if not self._store:
                return

            now = _now_ms()
            due_jobs = [
                j for j in self._store.jobs
                if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
            ]

            for job in due_jobs:
                if job.id in self._in_flight:
                    logger.warning(
                        "Cron: job '{}' ({}) is due but its previous dispatch is still in flight; skipping this tick",
                        job.name,
                        job.id,
                    )
                    continue
                if not self._claim_job(job):
                    continue
                task = asyncio.create_task(
                    self._dispatch_claimed(job), name=f"cron-job:{job.id}"
                )
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)
        finally:
            self._save_store()
            self._arm_timer()

    def _claim_job(self, job: CronJob) -> bool:
        """Phase 1 (claim): mark the run started and persist it BEFORE any side
        effect. Once last_run_at_ms is on disk, a restart can never re-arm an
        "at" job (see _recompute_next_runs), so a crash mid-dispatch cannot
        double-trigger it. Returns False when the job is already in flight
        (overlapping timer tick vs manual run_job).
        """
        if job.id in self._in_flight:
            logger.warning(
                "Cron: job '{}' ({}) is already dispatching; skipping duplicate",
                job.name,
                job.id,
            )
            return False

        self._in_flight.add(job.id)
        start_ms = _now_ms()
        job.state.last_run_at_ms = start_ms
        job.state.last_status = "running"
        job.state.last_error = None
        job.updated_at_ms = start_ms
        self._save_store()
        return True

    async def _dispatch_claimed(self, job: CronJob) -> None:
        """Phase 2 (dispatch under watchdog) + Phase 3 (finalize, always persisted).

        Must only run for a job that _claim_job just claimed. The handler runs
        in its own task so a wedge inside session.prompt is observable and
        bounded instead of silently pinning the scheduler:

        - watchdog timeout: dump the dispatch task's live await chain (this is
          the primary post-mortem for hung turns), cancel it, wait a bounded
          grace for the CancelledError unwind;
        - cancel-resistant task: abandon it detached with a loud error and keep
          the job in-flight until the task really ends, so later ticks cannot
          stack a second dispatch on the same wedged session;
        - every exit path (ok / error / timeout / interrupted) finalizes the
          claim in the finally block, so jobs.json can never stay frozen in
          last_status="running" while the process lives.
        """
        dispatch_task: asyncio.Task | None = None
        abandoned = False
        delivered = False
        outcome = "ok"
        error_text: str | None = None
        try:
            logger.info("Cron: executing job '{}' ({})", job.name, job.id)
            try:
                if not callable(self.on_job):
                    raise RuntimeError("cron job handler is not configured")
                dispatch_task = asyncio.create_task(
                    self.on_job(job), name=f"cron-dispatch:{job.id}"
                )
                timeout_s = self._resolve_dispatch_timeout_s()
                grace_s = self._resolve_dispatch_cancel_grace_s()
                done, _pending = await asyncio.wait(
                    {dispatch_task},
                    timeout=timeout_s if timeout_s > 0 else None,
                )
                if not done:
                    logger.error(
                        "Cron: dispatch watchdog timeout for job '{}' ({}) after {:.0f}s; current await chain:\n{}",
                        job.name,
                        job.id,
                        timeout_s,
                        format_task_await_chain(dispatch_task),
                    )
                    dispatch_task.cancel()
                    _unwound, still_pending = await asyncio.wait(
                        {dispatch_task}, timeout=max(0.0, grace_s)
                    )
                    if still_pending:
                        abandoned = True
                        logger.error(
                            "Cron: dispatch task for job '{}' ({}) ignored cancellation for {:.0f}s; "
                            "abandoning it detached (job stays in-flight until the task ends)",
                            job.name,
                            job.id,
                            grace_s,
                        )
                    outcome = "timeout"
                    error_text = f"dispatch watchdog timeout after {timeout_s:.0f}s"
                    if abandoned:
                        error_text += "; task ignored cancellation and was abandoned"
                elif dispatch_task.cancelled():
                    outcome = "interrupted"
                    error_text = (
                        "dispatch cancelled before completion "
                        "(session cancel/pause or runtime shutdown)"
                    )
                else:
                    exc = dispatch_task.exception()
                    if exc is None:
                        delivered = True
                    else:
                        outcome = "error"
                        error_text = str(exc) or exc.__class__.__name__
                        logger.error("Cron: job '{}' failed: {}", job.name, exc)
            except Exception as e:
                outcome = "error"
                error_text = str(e) or e.__class__.__name__
                logger.error("Cron: job '{}' failed: {}", job.name, e)
        except asyncio.CancelledError:
            # _dispatch_claimed itself was cancelled (service stop / shutdown).
            # Cancel the inner dispatch too — asyncio.wait does not propagate
            # cancellation to the tasks it watches — then let the finally block
            # persist the interrupted finalize before propagating.
            outcome = "interrupted"
            error_text = (
                "dispatch interrupted by cancellation (service stop or timer teardown)"
            )
            if dispatch_task is not None and not dispatch_task.done():
                dispatch_task.cancel()
            raise
        finally:
            self._finalize_dispatched_job(
                job,
                delivered=delivered,
                outcome=outcome,
                error_text=error_text,
                abandoned=abandoned,
                dispatch_task=dispatch_task,
            )

    def _finalize_dispatched_job(
        self,
        job: CronJob,
        *,
        delivered: bool,
        outcome: str,
        error_text: str | None,
        abandoned: bool,
        dispatch_task: asyncio.Task | None,
    ) -> None:
        """Phase 3 (finalize): persist the terminal state of a claimed run.

        Runs on EVERY exit path so the store never stays in last_status
        "running" — a frozen claim is what silently broke at-most-once
        bookkeeping and next_run arithmetic after the 2026-09 stall. The
        startup-only _recover_interrupted_runs remains the crash-path
        equivalent; this is the live-process equivalent.
        """
        # 派发期间 store 可能因外部修改被重载（CLI 进程写同一 jobs.json），
        # 原 job 对象随之脱钩：把收尾写回当前 store 里的同 id 副本，保证
        # finalize 永远落在会被 _save_store 持久化的对象上。已被外部移除
        # 时保持 detached（后续写入只影响临时对象，属预期）。
        if self._store:
            for candidate in self._store.jobs:
                if candidate.id == job.id:
                    job = candidate
                    break
        finish_ms = _now_ms()
        try:
            if delivered:
                job.state.delivered_runs = max(0, int(job.state.delivered_runs or 0)) + 1
                job.state.last_delivered_at_ms = finish_ms
                job.state.last_status = "ok"
                job.state.last_error = None
                logger.info("Cron: job '{}' completed", job.name)
            elif outcome == "timeout":
                job.state.last_status = "timeout"
                job.state.last_error = error_text
                logger.warning(
                    "Cron: job '{}' ({}) finalized as timeout: {}",
                    job.name,
                    job.id,
                    error_text,
                )
            elif outcome == "interrupted":
                job.state.last_status = "interrupted"
                job.state.last_error = error_text
                logger.warning(
                    "Cron: job '{}' ({}) finalized as interrupted: {}",
                    job.name,
                    job.id,
                    error_text,
                )
            else:
                job.state.last_status = "error"
                job.state.last_error = error_text
            job.updated_at_ms = finish_ms

            max_runs = max(1, int(getattr(job.payload, "max_runs", 1) or 1))
            terminal_delivery = delivered and int(job.state.delivered_runs or 0) >= max_runs

            if terminal_delivery:
                if self._delete_job_in_memory(job.id):
                    logger.info(
                        "Cron: job '{}' ({}) reached max_runs={}; removed",
                        job.name,
                        job.id,
                        max_runs,
                    )
                else:
                    # 派发期间 job 已被移除（如运维 remove_job）：删除按 id 过滤，
                    # 返回 False 即“已不在 store”， detached 对象的后续状态是只写
                    # 的，不构成失败，也不得伪造 removal-failed 错误。
                    logger.info(
                        "Cron: job '{}' ({}) reached terminal delivery but is no longer in the store; removal skipped",
                        job.name,
                        job.id,
                    )
            else:
                job.state.next_run_at_ms = self._next_run_after_execution(
                    job, delivered=delivered, outcome=outcome
                )
                # At-most-once for unfinished one-shot dispatches (mirrors the
                # startup reconciliation in _recover_interrupted_runs): the
                # dispatch may already have reached the downstream handler, so
                # a duplicate reminder is worse than a missed one.
                if (
                    not delivered
                    and outcome in {"timeout", "interrupted"}
                    and job.schedule.kind == "at"
                    and (job.delete_after_run or max_runs <= 1)
                    and job.enabled
                ):
                    job.enabled = False
                    logger.warning(
                        "Cron: one-shot job '{}' ({}) ended as {}; suppressed to avoid duplicate delivery",
                        job.name,
                        job.id,
                        outcome,
                    )
        except Exception:
            logger.exception(
                "Cron: failed to compute finalize state for job '{}' ({})",
                job.name,
                job.id,
            )
        finally:
            try:
                self._save_store()
            except Exception:
                logger.exception(
                    "Cron: failed to persist finalize state for job '{}' ({})",
                    job.name,
                    job.id,
                )
            if abandoned and dispatch_task is not None:
                def _release(task: asyncio.Task) -> None:
                    self._in_flight.discard(job.id)
                    logger.warning(
                        "Cron: abandoned dispatch task for job '{}' ({}) finally ended (cancelled={})",
                        job.name,
                        job.id,
                        task.cancelled(),
                    )
                    if self._running:
                        self._arm_timer()

                dispatch_task.add_done_callback(_release)
            else:
                self._in_flight.discard(job.id)
            # The tick armed the timer BEFORE this run resolved (in-flight jobs
            # are excluded from wake arithmetic), so every finalize re-arms to
            # pick up this job's freshly advanced next_run.
            if self._running:
                self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job with claim-before-dispatch semantics.

        Phase 1 (claim):    mark the run started and persist it BEFORE any side
                            effect (see _claim_job).
        Phase 2 (dispatch): invoke the job handler in an independent task under
                            a delivery watchdog (see _dispatch_claimed).
        Phase 3 (finalize): persist delivery counters and the next run on every
                            exit path (see _finalize_dispatched_job).

        Kept as the inline entry point for manual run_job and tests; the timer
        path spawns this same sequence per due job as an independent task.
        """
        if not self._claim_job(job):
            return
        await self._dispatch_claimed(job)

    def _next_run_after_execution(
        self, job: CronJob, *, delivered: bool, outcome: str = "ok"
    ) -> int | None:
        now = _now_ms()
        if delivered:
            if job.schedule.kind == "at":
                return None
            return _compute_next_run(job.schedule, now)
        if job.schedule.kind == "at":
            if outcome in {"timeout", "interrupted"}:
                # Unfinished one-shot: suppress instead of retrying (at-most-once,
                # see _finalize_dispatched_job / _recover_interrupted_runs).
                return None
            return now + _FAILED_AT_RETRY_DELAY_MS
        return _compute_next_run(job.schedule, now)

    def _delete_job_in_memory(self, job_id: str) -> bool:
        if not self._store:
            return False
        before = len(self._store.jobs)
        self._store.jobs = [j for j in self._store.jobs if j.id != job_id]
        return len(self._store.jobs) < before

    # ========== Public API ==========

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))

    def _find_conflicting_at_job(self, at_ms: int, session_key: str | None) -> CronJob | None:
        """Return an enabled one-shot job for the same instant and session, if any.

        Two enabled one-shot reminders targeting the same session at the exact
        same time are treated as a duplicate registration (a common model
        failure mode), so the second add is rejected instead of later
        double-firing. Different sessions may legitimately share a time, and
        recurring schedules are never compared here.
        """
        store = self._load_store()
        for existing in store.jobs:
            if not existing.enabled:
                continue
            if existing.schedule.kind != "at":
                continue
            if int(existing.schedule.at_ms or 0) != at_ms:
                continue
            existing_session = str(existing.payload.session_key or "").strip() or None
            if existing_session == session_key:
                return existing
        return None

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        session_key: str | None = None,
        stop_condition: str | None = None,
        max_runs: int | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        """Add a new job."""
        store = self._load_store()
        _validate_schedule_for_add(schedule)
        now = _now_ms()
        if schedule.kind == "at":
            at_ms = int(schedule.at_ms or 0)
            if at_ms <= now:
                current_time_text = _format_local_time_ms(now)
                raise ValueError(
                    f"任务定时已过期，当前时间为{current_time_text}，请立即执行或视情况废弃而不要创建过期任务"
                )
            effective_session_key = str(session_key or "").strip() or None
            conflict = self._find_conflicting_at_job(at_ms, effective_session_key)
            if conflict is not None:
                raise ValueError(
                    f"同一会话在 {_format_local_time_ms(at_ms)} 已存在一次性提醒 (id: {conflict.id})，"
                    f"请勿重复创建；如需修改请先用 remove 删除旧任务，或改用其他时间"
                )
        _ = stop_condition
        effective_max_runs = _normalize_max_runs(schedule=schedule, max_runs=max_runs)

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                stop_condition=None,
                max_runs=effective_max_runs,
                deliver=deliver,
                channel=channel,
                to=to,
                session_key=str(session_key or "").strip() or None,
            ),
            state=CronJobState(
                next_run_at_ms=_compute_next_run(schedule, now),
                delivered_runs=0,
            ),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )

        store.jobs.append(job)
        self._save_store()
        self._arm_timer()

        logger.info("Cron: added job '{}' ({})", name, job.id)
        return job

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        store = self._load_store()
        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id]
        removed = len(store.jobs) < before

        if removed:
            self._save_store()
            self._arm_timer()
            logger.info("Cron: removed job {}", job_id)

        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        if not callable(self.on_job):
            return False
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }


