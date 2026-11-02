"""前门工具后台作业的统一生命周期。

工具调用从第一秒就是一个 job：前门最多等 ``auto_background_seconds``（默认 60s），
等不到就把 job 留在进程内继续跑，并给模型一个合成结果。只有真正后台化的调用会落
``tool_jobs`` 行；快工具不产生任何行。

本模块不做工具分档，也不改工具 schema。它只负责：

- 进程内 task 强引用与 ``wait_first``；
- 交棒后的持久化、终态计算、outbox 事件与启动恢复；
- 用户手动停止（取消 task + 取消令牌 + 立即投递 ``stopped_by_user``）。

会话级暂停/停止**不**取消后台 job（2026-11-02 操作者裁决）：作业继续跑完再报事件。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

from main.protocol import now_iso

TOOL_JOB_STATUS_RUNNING = "running"
TOOL_JOB_STATUS_STOPPING = "stopping"
TOOL_JOB_STATUS_SUCCESS = "success"
TOOL_JOB_STATUS_ERROR = "error"
TOOL_JOB_STATUS_TIMEOUT = "timeout"
TOOL_JOB_STATUS_INTERRUPTED = "interrupted"
TOOL_JOB_STATUS_STOPPED_BY_USER = "stopped_by_user"

TOOL_JOB_TERMINAL_STATUSES = frozenset(
    {
        TOOL_JOB_STATUS_SUCCESS,
        TOOL_JOB_STATUS_ERROR,
        TOOL_JOB_STATUS_TIMEOUT,
        TOOL_JOB_STATUS_INTERRUPTED,
        TOOL_JOB_STATUS_STOPPED_BY_USER,
    }
)
TOOL_JOB_ACTIVE_STATUSES = (TOOL_JOB_STATUS_RUNNING, TOOL_JOB_STATUS_STOPPING)
TOOL_JOB_EVENT_REASON = "tool_job"
TOOL_JOB_RESULT_PREVIEW_CHARS = 1200
TOOL_JOB_STOP_ABANDON_GRACE_SECONDS = 5.0
TOOL_JOB_RETENTION_SECONDS = 7 * 24 * 3600


class ToolJobNotFoundError(RuntimeError):
    pass


class ToolJobFinishedError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolJobOutcome:
    status: str = TOOL_JOB_STATUS_SUCCESS
    result_text: str = ""
    result_preview: str = ""
    result_ref: str = ""
    log_path: str = ""
    elapsed_seconds: float = 0.0
    payload: Any = None


@dataclass
class ToolJob:
    job_id: str
    session_key: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    args_hash: str
    hint: str
    started_at: str
    started_monotonic: float
    timeout_seconds: float
    recovery_policy: str
    cancel_token: Any = None
    task: asyncio.Task[Any] | None = None
    status: str = TOOL_JOB_STATUS_RUNNING
    backgrounded: bool = False
    persisted: bool = False
    stop_requested_by_user: bool = False
    suppress_event: bool = False
    event_emitted: bool = False
    finalized: bool = False
    outcome: ToolJobOutcome | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def stable_tool_args_hash(arguments: Any) -> str:
    """稳定参数指纹，用于同一会话内的 in-flight 去重。"""
    try:
        text = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        text = repr(arguments)
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def tool_job_dedupe_key(job_id: str, status: str) -> str:
    return f"tool-job:{str(job_id or '').strip()}:{str(status or '').strip()}"


def _clip_preview(text: Any, limit: int = TOOL_JOB_RESULT_PREVIEW_CHARS) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 1)]}..."


def build_tool_job_event_payload(
    job: ToolJob,
    *,
    status: str,
    finished_at: str,
    elapsed_seconds: float,
    outcome: ToolJobOutcome | None = None,
    stopped_by: str = "",
) -> dict[str, Any]:
    preview_source = ""
    result_ref = ""
    log_path = ""
    if outcome is not None:
        preview_source = outcome.result_preview or outcome.result_text
        result_ref = outcome.result_ref
        log_path = outcome.log_path
    return {
        "event_reason": TOOL_JOB_EVENT_REASON,
        "reason": TOOL_JOB_EVENT_REASON,
        "session_id": job.session_key,
        "job_id": job.job_id,
        "tool_name": job.tool_name,
        "status": status,
        "started_at": job.started_at,
        "finished_at": finished_at,
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds or 0.0)), 1),
        "timeout_seconds": round(max(0.0, float(job.timeout_seconds or 0.0)), 1),
        "turn_id": job.turn_id,
        "tool_call_id": job.tool_call_id,
        "result_preview": _clip_preview(preview_source),
        "result_ref": str(result_ref or ""),
        "log_path": str(log_path or ""),
        "recovery_policy": str(job.recovery_policy or "model_decide"),
        "stopped_by": str(stopped_by or ""),
    }


class ToolJobRunner:
    """一个会话内的后台作业索引。job task 不注册进会话 task 集合。

    会话暂停/停止只取消会话 task，不会碰到这里；只有用户点停止、会话删除或进程
    退出才会取消 job task。
    """

    def __init__(
        self,
        *,
        session_key: str,
        store_getter: Callable[[], Any],
        heartbeat_getter: Callable[[], Any] | None = None,
        changed_callback: Callable[[list[dict[str, Any]]], Any] | None = None,
        stop_abandon_grace_seconds: float = TOOL_JOB_STOP_ABANDON_GRACE_SECONDS,
    ) -> None:
        self._session_key = str(session_key or "").strip()
        self._store_getter = store_getter
        self._heartbeat_getter = heartbeat_getter or (lambda: None)
        self._changed_callback = changed_callback
        self._stop_abandon_grace_seconds = max(0.0, float(stop_abandon_grace_seconds or 0.0))
        self._jobs: dict[str, ToolJob] = {}
        self._inflight: dict[tuple[str, str], str] = {}
        self._finalize_tasks: set[asyncio.Task[Any]] = set()

    def _store(self) -> Any:
        try:
            return self._store_getter()
        except Exception:
            return None

    def _heartbeat(self) -> Any:
        try:
            return self._heartbeat_getter()
        except Exception:
            return None

    def start_job(
        self,
        *,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        args_hash: str,
        hint: str,
        run: Callable[[], Awaitable[ToolJobOutcome]],
        timeout_seconds: float,
        recovery_policy: str = "model_decide",
        cancel_token: Any = None,
    ) -> tuple[ToolJob, bool]:
        """启动 job；同一 ``(tool, args_hash)`` 仍在跑时复用已有 job。

        返回 ``(job, reused)``。``run`` 必须在当前 asyncio context 里创建 task，
        这样工具执行读到的 ContextVar（runtime context / read roots）不会丢。
        """
        normalized_tool_name = str(tool_name or "tool").strip() or "tool"
        normalized_hash = str(args_hash or "").strip()
        dedupe_key = (normalized_tool_name, normalized_hash)
        existing_id = self._inflight.get(dedupe_key)
        if existing_id:
            existing = self._jobs.get(existing_id)
            if existing is not None and existing.status in TOOL_JOB_ACTIVE_STATUSES:
                return existing, True
            self._inflight.pop(dedupe_key, None)
        job = ToolJob(
            job_id=f"tooljob:{uuid.uuid4().hex}",
            session_key=self._session_key,
            turn_id=str(turn_id or "").strip(),
            tool_call_id=str(tool_call_id or "").strip(),
            tool_name=normalized_tool_name,
            args_hash=normalized_hash,
            hint=str(hint or "").strip(),
            started_at=now_iso(),
            started_monotonic=time.monotonic(),
            timeout_seconds=max(0.0, float(timeout_seconds or 0.0)),
            recovery_policy=str(recovery_policy or "model_decide").strip() or "model_decide",
            cancel_token=cancel_token,
        )
        job.task = asyncio.create_task(self._run_job(job, run), name=f"tool-job:{normalized_tool_name}")
        self._jobs[job.job_id] = job
        self._inflight[dedupe_key] = job.job_id
        job.task.add_done_callback(lambda _task, current=job: self._on_task_done(current))
        return job, False

    async def _run_job(self, job: ToolJob, run: Callable[[], Awaitable[ToolJobOutcome]]) -> ToolJobOutcome:
        try:
            outcome = await run()
            if not isinstance(outcome, ToolJobOutcome):
                outcome = ToolJobOutcome(result_text=str(outcome or ""))
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("tool job {} failed: {}", job.job_id, exc)
            text = f"Error executing {job.tool_name}: {exc}"
            return ToolJobOutcome(status=TOOL_JOB_STATUS_ERROR, result_text=text, result_preview=text)

    async def wait_first(self, job: ToolJob, timeout_seconds: float) -> tuple[bool, ToolJobOutcome | None]:
        """等 job 最多 ``timeout_seconds``。

        返回 ``(True, outcome)`` 表示在前台窗口内完成；``(False, None)`` 表示已交棒。
        前台等待被取消（会话暂停/停止）时取消 job task 并向上抛，保持现有回合语义。
        """
        task = job.task
        if task is None:
            return True, None
        wait_seconds = max(0.0, float(timeout_seconds or 0.0))
        try:
            if wait_seconds > 0:
                done, _pending = await asyncio.wait({task}, timeout=wait_seconds)
            else:
                done = set()
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            raise
        if done or task.done():
            return True, task.result()
        token = job.cancel_token
        if token is not None and getattr(token, "is_cancelled", lambda: False)():
            # 暂停与交棒撞在一起：工具已经不再属于前台，但也不该留下一个已被取消的 job。
            try:
                outcome = await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                outcome = None
            return True, outcome
        self._promote(job)
        return False, None

    def _promote(self, job: ToolJob) -> None:
        if job.backgrounded:
            return
        job.backgrounded = True
        job.status = TOOL_JOB_STATUS_RUNNING
        store = self._store()
        if store is None:
            logger.warning(
                "tool job {} backgrounded without a durable store (session={})",
                job.job_id,
                job.session_key,
            )
            self._notify_changed()
            return
        try:
            store.put_tool_job(
                job_id=job.job_id,
                session_id=job.session_key,
                turn_id=job.turn_id,
                tool_call_id=job.tool_call_id,
                tool_name=job.tool_name,
                args_hash=job.args_hash,
                hint=job.hint,
                status=TOOL_JOB_STATUS_RUNNING,
                started_at=job.started_at,
                updated_at=now_iso(),
                timeout_seconds=job.timeout_seconds,
                owner_pid=os.getpid(),
                recovery_policy=job.recovery_policy,
                payload={"hint": job.hint},
            )
            job.persisted = True
        except Exception:
            logger.exception("failed to persist tool job {} at background handoff", job.job_id)
        self._notify_changed()

    def _on_task_done(self, job: ToolJob) -> None:
        if not job.backgrounded:
            self._drop_job(job)
            return
        try:
            task = asyncio.create_task(self._finalize(job), name=f"tool-job-finalize:{job.job_id}")
        except RuntimeError:
            return
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    def _resolve_terminal(self, job: ToolJob) -> tuple[str, ToolJobOutcome | None, str]:
        task = job.task
        outcome = job.outcome
        if task is not None and task.done() and not task.cancelled():
            try:
                exc = task.exception()
            except Exception:
                exc = None
            if exc is not None:
                return TOOL_JOB_STATUS_ERROR, outcome, str(exc)
        if job.stop_requested_by_user:
            return TOOL_JOB_STATUS_STOPPED_BY_USER, outcome, ""
        if task is not None and task.cancelled():
            return TOOL_JOB_STATUS_INTERRUPTED, outcome, "cancelled"
        if outcome is None:
            return TOOL_JOB_STATUS_ERROR, None, "no outcome"
        status = str(outcome.status or "").strip().lower()
        if status not in TOOL_JOB_TERMINAL_STATUSES:
            status = TOOL_JOB_STATUS_ERROR
        return status, outcome, ""

    async def _finalize(self, job: ToolJob) -> None:
        async with job.lock:
            if job.finalized:
                return
            job.finalized = True
            task = job.task
            if task is not None and task.done() and not task.cancelled():
                try:
                    job.outcome = task.result()
                except Exception:
                    job.outcome = None
            try:
                exc = task.exception() if task is not None and task.done() and not task.cancelled() else None
            except Exception:
                exc = None
            status, outcome, error_text = self._resolve_terminal(job)
            if exc is not None and not outcome:
                outcome = ToolJobOutcome(
                    status=TOOL_JOB_STATUS_ERROR,
                    result_text=f"Error executing {job.tool_name}: {exc}",
                    result_preview=str(exc),
                )
            job.status = status
            finished_at = now_iso()
            elapsed = max(0.0, time.monotonic() - job.started_monotonic)
            if not job.event_emitted and not job.suppress_event:
                self._emit_terminal_event(
                    job,
                    status=status,
                    outcome=outcome,
                    finished_at=finished_at,
                    elapsed_seconds=elapsed,
                    stopped_by="user" if status == TOOL_JOB_STATUS_STOPPED_BY_USER else "",
                    error_text=error_text,
                )
            self._persist_terminal(
                job,
                status=status,
                outcome=outcome,
                finished_at=finished_at,
                error_text=error_text,
            )
            self._drop_job(job)
            self._notify_changed()

    def _persist_terminal(
        self,
        job: ToolJob,
        *,
        status: str,
        outcome: ToolJobOutcome | None,
        finished_at: str,
        error_text: str = "",
    ) -> None:
        if not job.persisted:
            return
        store = self._store()
        if store is None:
            return
        preview = ""
        result_ref = ""
        log_path = ""
        if outcome is not None:
            preview = outcome.result_preview or outcome.result_text
            result_ref = outcome.result_ref
            log_path = outcome.log_path
        if not preview and error_text:
            preview = error_text
        try:
            store.update_tool_job(
                job.job_id,
                status=status,
                finished_at=finished_at,
                updated_at=finished_at,
                result_preview=_clip_preview(preview),
                result_ref=result_ref,
                log_path=log_path,
            )
        except Exception:
            logger.exception("failed to persist terminal state for tool job {}", job.job_id)

    def _emit_terminal_event(
        self,
        job: ToolJob,
        *,
        status: str,
        outcome: ToolJobOutcome | None,
        finished_at: str,
        elapsed_seconds: float,
        stopped_by: str = "",
        error_text: str = "",
    ) -> None:
        job.event_emitted = True
        payload = build_tool_job_event_payload(
            job,
            status=status,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            outcome=outcome,
            stopped_by=stopped_by,
        )
        if not payload.get("result_preview") and error_text:
            payload["result_preview"] = _clip_preview(error_text)
        dedupe_key = tool_job_dedupe_key(job.job_id, status)
        store = self._store()
        if store is not None and job.persisted:
            try:
                store.put_tool_job_outbox(
                    dedupe_key=dedupe_key,
                    job_id=job.job_id,
                    session_id=job.session_key,
                    created_at=finished_at,
                    payload=payload,
                )
            except Exception:
                logger.exception("failed to persist tool job outbox row {}", dedupe_key)
        heartbeat = self._heartbeat()
        enqueue = getattr(heartbeat, "enqueue_tool_job", None) if heartbeat is not None else None
        if callable(enqueue):
            try:
                enqueue(payload)
                if store is not None and job.persisted:
                    store.mark_tool_job_outbox_enqueue_result(
                        dedupe_key,
                        accepted=True,
                        rejected_reason="",
                        updated_at=finished_at,
                    )
            except Exception:
                logger.exception("failed to enqueue tool job heartbeat event {}", dedupe_key)

    def _drop_job(self, job: ToolJob) -> None:
        self._jobs.pop(job.job_id, None)
        dedupe_key = (job.tool_name, job.args_hash)
        if self._inflight.get(dedupe_key) == job.job_id:
            self._inflight.pop(dedupe_key, None)

    def _notify_changed(self) -> None:
        callback = self._changed_callback
        if callback is None:
            return
        try:
            result = callback(self.list_jobs(self._session_key))
            if inspect.isawaitable(result):
                try:
                    asyncio.create_task(result)
                except RuntimeError:
                    pass
        except Exception:
            logger.debug("tool job change notification failed", exc_info=True)

    def list_jobs(self, session_key: str | None = None) -> list[dict[str, Any]]:
        normalized_session = str(session_key or "").strip()
        now_mono = time.monotonic()
        items: list[dict[str, Any]] = []
        for job in list(self._jobs.values()):
            if not job.backgrounded:
                continue
            if job.status not in TOOL_JOB_ACTIVE_STATUSES:
                continue
            if normalized_session and job.session_key != normalized_session:
                continue
            items.append(
                {
                    "job_id": job.job_id,
                    "tool_name": job.tool_name,
                    "status": job.status,
                    "started_at": job.started_at,
                    "elapsed_seconds": round(max(0.0, now_mono - job.started_monotonic), 1),
                    "turn_id": job.turn_id,
                    "tool_call_id": job.tool_call_id,
                    "hint": job.hint,
                }
            )
        items.sort(key=lambda item: (str(item.get("started_at") or ""), str(item.get("job_id") or "")))
        return items

    async def stop_job(self, job_id: str, *, reason: str = "user_manual_stop") -> dict[str, Any]:
        normalized_job_id = str(job_id or "").strip()
        job = self._jobs.get(normalized_job_id)
        if job is None:
            store = self._store()
            row = store.get_tool_job(normalized_job_id) if store is not None else None
            if row is None:
                raise ToolJobNotFoundError(normalized_job_id)
            if str(row.get("status") or "") in TOOL_JOB_TERMINAL_STATUSES:
                raise ToolJobFinishedError(normalized_job_id)
            raise ToolJobFinishedError(normalized_job_id)
        async with job.lock:
            if job.finalized or job.stop_requested_by_user:
                if job.stop_requested_by_user:
                    return {"job_id": job.job_id, "status": TOOL_JOB_STATUS_STOPPING}
                raise ToolJobFinishedError(job.job_id)
            if job.task is not None and job.task.done():
                raise ToolJobFinishedError(job.job_id)
            job.stop_requested_by_user = True
            job.status = TOOL_JOB_STATUS_STOPPING
            if job.persisted:
                store = self._store()
                if store is not None:
                    try:
                        store.update_tool_job(
                            job.job_id,
                            status=TOOL_JOB_STATUS_STOPPING,
                            stop_requested_by_user=True,
                            updated_at=now_iso(),
                        )
                    except Exception:
                        logger.exception("failed to persist stop request for tool job {}", job.job_id)
            self._emit_terminal_event(
                job,
                status=TOOL_JOB_STATUS_STOPPED_BY_USER,
                outcome=None,
                finished_at=now_iso(),
                elapsed_seconds=max(0.0, time.monotonic() - job.started_monotonic),
                stopped_by="user",
            )
            token = job.cancel_token
            if token is not None and hasattr(token, "cancel"):
                try:
                    token.cancel(reason=reason)
                except Exception:
                    pass
            if job.task is not None and not job.task.done():
                job.task.cancel()
        self._notify_changed()
        self._schedule_stop_abandon(job)
        return {"job_id": job.job_id, "status": TOOL_JOB_STATUS_STOPPING}

    def _schedule_stop_abandon(self, job: ToolJob) -> None:
        try:
            task = asyncio.create_task(
                self._abandon_if_needed(job),
                name=f"tool-job-stop-abandon:{job.job_id}",
            )
        except RuntimeError:
            return
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    async def _abandon_if_needed(self, job: ToolJob) -> None:
        await asyncio.sleep(self._stop_abandon_grace_seconds)
        async with job.lock:
            if job.finalized:
                return
            job.finalized = True
            job.status = TOOL_JOB_STATUS_STOPPED_BY_USER
            logger.warning(
                "tool job {} ignored cancellation for {:.1f}s; abandoning it after the user stop event",
                job.job_id,
                self._stop_abandon_grace_seconds,
            )
            self._persist_terminal(
                job,
                status=TOOL_JOB_STATUS_STOPPED_BY_USER,
                outcome=None,
                finished_at=now_iso(),
                error_text="stopped by user; task did not unwind in the grace window",
            )
            self._drop_job(job)
            self._notify_changed()

    async def cancel_all(self, *, reason: str = "session_deleted", suppress_event: bool = True) -> int:
        jobs = list(self._jobs.values())
        if not jobs:
            return 0
        for job in jobs:
            job.suppress_event = bool(suppress_event)
            token = job.cancel_token
            if token is not None and hasattr(token, "cancel"):
                try:
                    token.cancel(reason=reason)
                except Exception:
                    pass
            if job.task is not None and not job.task.done():
                job.task.cancel()
        tasks = [job.task for job in jobs if job.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in jobs:
            if job.finalized:
                continue
            job.finalized = True
            job.status = TOOL_JOB_STATUS_INTERRUPTED
            self._persist_terminal(
                job,
                status=TOOL_JOB_STATUS_INTERRUPTED,
                outcome=None,
                finished_at=now_iso(),
                error_text=reason,
            )
            self._drop_job(job)
        self._notify_changed()
        return len(jobs)


def recover_tool_jobs(store: Any, *, now: str | None = None) -> dict[str, int]:
    """Web 启动时把上一进程遗留的 running/stopping 行收成终态并补事件。

    在 ``start_web_session_heartbeat(replay_pending_outbox=True)`` 之前调用：本函数
    只落 outbox 行，心跳启动后的 replay 负责真正投递。
    """
    if store is None:
        return {"recovered": 0, "events": 0}
    recovered_at = str(now or now_iso())
    active_rows = store.list_tool_jobs(statuses=TOOL_JOB_ACTIVE_STATUSES, limit=1000)
    recovered = 0
    events = 0
    for row in active_rows:
        job_id = str(row.get("job_id") or "").strip()
        if not job_id:
            continue
        previous_status = str(row.get("status") or "").strip()
        stop_requested = bool(row.get("stop_requested_by_user"))
        existing = _find_existing_job_outbox(store, job_id)
        if existing is not None:
            # 终止写序是「先 outbox，后行状态」：事故可能停在两者之间，事件已经在
            # pending 里，重启只补行状态，不能再造第二条。
            status = str(existing.get("payload", {}).get("status") or "").strip()
            if status not in TOOL_JOB_TERMINAL_STATUSES:
                status = TOOL_JOB_STATUS_STOPPED_BY_USER if stop_requested else TOOL_JOB_STATUS_INTERRUPTED
            store.update_tool_job(
                job_id,
                status=status,
                finished_at=recovered_at,
                updated_at=recovered_at,
                result_preview=str(existing.get("payload", {}).get("result_preview") or ""),
                result_ref=str(existing.get("payload", {}).get("result_ref") or ""),
            )
            recovered += 1
            continue
        if previous_status == TOOL_JOB_STATUS_STOPPING:
            status = TOOL_JOB_STATUS_STOPPED_BY_USER if stop_requested else TOOL_JOB_STATUS_INTERRUPTED
        else:
            status = TOOL_JOB_STATUS_INTERRUPTED
        recover_text = (
            "The project process restarted while this background tool job was running. "
            "It may have stopped or may still be running; inspect the result/log before retrying."
        )
        if status == TOOL_JOB_STATUS_STOPPED_BY_USER:
            recover_text = "The user manually stopped this background tool job (用户手动停止)."
        store.update_tool_job(
            job_id,
            status=status,
            finished_at=recovered_at,
            updated_at=recovered_at,
            result_preview=recover_text,
        )
        payload = {
            "event_reason": TOOL_JOB_EVENT_REASON,
            "reason": TOOL_JOB_EVENT_REASON,
            "session_id": str(row.get("session_id") or ""),
            "job_id": job_id,
            "tool_name": str(row.get("tool_name") or "tool"),
            "status": status,
            "started_at": str(row.get("started_at") or ""),
            "finished_at": recovered_at,
            "elapsed_seconds": 0.0,
            "timeout_seconds": float(row.get("timeout_seconds") or 0.0),
            "turn_id": str(row.get("turn_id") or ""),
            "tool_call_id": str(row.get("tool_call_id") or ""),
            "result_preview": recover_text,
            "result_ref": "",
            "log_path": str(row.get("log_path") or ""),
            "recovery_policy": str(row.get("recovery_policy") or "model_decide"),
            "stopped_by": "user" if status == TOOL_JOB_STATUS_STOPPED_BY_USER else "",
        }
        dedupe_key = tool_job_dedupe_key(job_id, status)
        store.put_tool_job_outbox(
            dedupe_key=dedupe_key,
            job_id=job_id,
            session_id=str(row.get("session_id") or ""),
            created_at=recovered_at,
            payload=payload,
        )
        recovered += 1
        events += 1

    # 终止写序是「先 outbox，后行状态」：崩溃/写失败可能留下终态行却没有
    # outbox。扫最近保留窗口内的终态行，缺事件就补一条；已有任一状态的事件
    # 则跳过，避免重复。
    cutoff = _retention_cutoff_iso()
    terminal_rows = store.list_tool_jobs(statuses=tuple(TOOL_JOB_TERMINAL_STATUSES), limit=500)
    for row in terminal_rows:
        updated_at = str(row.get("updated_at") or "")
        if updated_at and updated_at < cutoff:
            continue
        job_id = str(row.get("job_id") or "").strip()
        if not job_id or _find_existing_job_outbox(store, job_id) is not None:
            continue
        status = str(row.get("status") or "").strip()
        if status not in TOOL_JOB_TERMINAL_STATUSES:
            continue
        payload = {
            "event_reason": TOOL_JOB_EVENT_REASON,
            "reason": TOOL_JOB_EVENT_REASON,
            "session_id": str(row.get("session_id") or ""),
            "job_id": job_id,
            "tool_name": str(row.get("tool_name") or "tool"),
            "status": status,
            "started_at": str(row.get("started_at") or ""),
            "finished_at": str(row.get("finished_at") or recovered_at),
            "elapsed_seconds": 0.0,
            "timeout_seconds": float(row.get("timeout_seconds") or 0.0),
            "turn_id": str(row.get("turn_id") or ""),
            "tool_call_id": str(row.get("tool_call_id") or ""),
            "result_preview": str(row.get("result_preview") or ""),
            "result_ref": str(row.get("result_ref") or ""),
            "log_path": str(row.get("log_path") or ""),
            "recovery_policy": str(row.get("recovery_policy") or "model_decide"),
            "stopped_by": "user" if status == TOOL_JOB_STATUS_STOPPED_BY_USER else "",
        }
        dedupe_key = tool_job_dedupe_key(job_id, status)
        store.put_tool_job_outbox(
            dedupe_key=dedupe_key,
            job_id=job_id,
            session_id=str(row.get("session_id") or ""),
            created_at=recovered_at,
            payload=payload,
        )
        events += 1
    return {"recovered": recovered, "events": events}


def _retention_cutoff_iso(*, now_epoch: float | None = None) -> str:
    import datetime as _datetime

    cutoff_epoch = float(now_epoch if now_epoch is not None else time.time()) - TOOL_JOB_RETENTION_SECONDS
    return _datetime.datetime.fromtimestamp(cutoff_epoch).astimezone().replace(microsecond=0).isoformat()


def _find_existing_job_outbox(store: Any, job_id: str) -> dict[str, Any] | None:
    getter = getattr(store, "get_tool_job_outbox", None)
    if not callable(getter):
        return None
    for status in (
        TOOL_JOB_STATUS_SUCCESS,
        TOOL_JOB_STATUS_ERROR,
        TOOL_JOB_STATUS_TIMEOUT,
        TOOL_JOB_STATUS_INTERRUPTED,
        TOOL_JOB_STATUS_STOPPED_BY_USER,
    ):
        try:
            row = getter(tool_job_dedupe_key(job_id, status))
        except Exception:
            row = None
        if row:
            return row
    return None


def prune_tool_job_rows(store: Any, *, now_epoch: float | None = None) -> int:
    if store is None:
        return 0
    cutoff = _retention_cutoff_iso(now_epoch=now_epoch)
    pruner = getattr(store, "prune_tool_jobs", None)
    if not callable(pruner):
        return 0
    return int(pruner(before_iso=cutoff) or 0)


__all__ = [
    "TOOL_JOB_ACTIVE_STATUSES",
    "TOOL_JOB_EVENT_REASON",
    "TOOL_JOB_STATUS_ERROR",
    "TOOL_JOB_STATUS_INTERRUPTED",
    "TOOL_JOB_STATUS_RUNNING",
    "TOOL_JOB_STATUS_STOPPED_BY_USER",
    "TOOL_JOB_STATUS_STOPPING",
    "TOOL_JOB_STATUS_SUCCESS",
    "TOOL_JOB_STATUS_TIMEOUT",
    "TOOL_JOB_TERMINAL_STATUSES",
    "ToolJob",
    "ToolJobFinishedError",
    "ToolJobNotFoundError",
    "ToolJobOutcome",
    "ToolJobRunner",
    "build_tool_job_event_payload",
    "prune_tool_job_rows",
    "recover_tool_jobs",
    "stable_tool_args_hash",
    "tool_job_dedupe_key",
]
