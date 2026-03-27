from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.session_events import SessionHeartbeatEvent, SessionHeartbeatEventQueue
from g3ku.heartbeat.session_wake import SessionHeartbeatWakeQueue
from g3ku.runtime.web_ceo_sessions import (
    _extract_task_ids_from_text,
    clear_inflight_turn_snapshot,
    normalize_ceo_metadata,
    update_ceo_session_after_turn,
)
from main.models import TaskRecord
from main.protocol import build_envelope, now_iso
from main.service.task_stall_callback import normalize_task_stall_payload
from main.service.task_stall_notifier import stalled_minutes_since, stall_bucket_minutes
from main.service.task_terminal_callback import build_task_terminal_payload, enrich_task_terminal_payload, normalize_task_terminal_payload

HEARTBEAT_OK = "HEARTBEAT_OK"
HeartbeatReplyNotifier = Callable[[str, str], Awaitable[None] | None]
_TASK_TERMINAL_OUTPUT_INLINE_LIMIT = 4000


@lru_cache(maxsize=1)
def _bundled_heartbeat_rules_text() -> str:
    path = Path(__file__).resolve().parents[1] / "runtime" / "prompts" / "heartbeat_rules.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception:
        logger.debug("heartbeat rules read skipped")
        return ""


class WebSessionHeartbeatService:
    def __init__(
        self,
        *,
        workspace: Path | str,
        agent: Any,
        runtime_manager: Any,
        main_task_service: Any,
        session_manager: Any,
        reply_notifier: HeartbeatReplyNotifier | None = None,
    ) -> None:
        self._agent = agent
        self._runtime_manager = runtime_manager
        self._main_task_service = main_task_service
        self._session_manager = session_manager
        self._reply_notifier = reply_notifier if callable(reply_notifier) else None
        self._events = SessionHeartbeatEventQueue()
        self._wake = SessionHeartbeatWakeQueue(handler=self._run_session)
        self._started = False
        self._start_lock = asyncio.Lock()
        self._prompt_tasks: dict[str, asyncio.Task[Any]] = {}

    async def start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            self._started = True
            for session_id in self._events.session_ids():
                delay_s = self._events.next_delay(session_id)
                self._wake.request(session_id, delay_s=0.25 if delay_s is None else max(0.0, delay_s))

    async def stop(self) -> None:
        self._started = False
        await self._wake.close()
        prompt_tasks = list(self._prompt_tasks.values())
        self._prompt_tasks.clear()
        for task in prompt_tasks:
            task.cancel()
        if prompt_tasks:
            await asyncio.gather(*prompt_tasks, return_exceptions=True)
        self._events.clear_all()

    def enqueue_task_terminal(self, task: TaskRecord) -> None:
        record = task if isinstance(task, TaskRecord) else None
        if record is None:
            return
        self.enqueue_task_terminal_payload(
            enrich_task_terminal_payload(
                build_task_terminal_payload(record),
                task=record,
                node_detail_getter=getattr(self._main_task_service, 'get_node_detail_payload', None),
            )
        )

    def enqueue_task_terminal_payload(self, payload: dict[str, Any] | None) -> bool:
        normalized_payload = enrich_task_terminal_payload(
            payload,
            task_getter=getattr(self._main_task_service, 'get_task', None),
            node_detail_getter=getattr(self._main_task_service, 'get_node_detail_payload', None),
        )
        session_id = str(normalized_payload.get("session_id") or "").strip()
        task_id = str(normalized_payload.get("task_id") or "").strip()
        status = str(normalized_payload.get("status") or "").strip().lower()
        if not session_id or not task_id or status not in {"success", "failed"}:
            return False
        event = self._events.enqueue(
            session_id=session_id,
            source="main_runtime",
            reason="task_terminal",
            dedupe_key=str(normalized_payload.get("dedupe_key") or f"task-terminal:{task_id}:{status}").strip(),
            payload=dict(normalized_payload),
            delay_seconds=0.0,
        )
        if event is None:
            return False
        if self._started:
            self._wake.request(session_id, delay_s=0.25)
        return True

    def enqueue_task_stall_payload(self, payload: dict[str, Any] | None) -> bool:
        normalized_payload = normalize_task_stall_payload(payload)
        session_id = str(normalized_payload.get("session_id") or "").strip()
        task_id = str(normalized_payload.get("task_id") or "").strip()
        bucket_minutes = int(normalized_payload.get("bucket_minutes") or 0)
        if not session_id or not task_id or bucket_minutes <= 0:
            return False
        event = self._events.enqueue(
            session_id=session_id,
            source="main_runtime",
            reason="task_stall",
            dedupe_key=str(
                normalized_payload.get("dedupe_key")
                or f"task-stall:{task_id}:{bucket_minutes}:{normalized_payload.get('last_visible_output_at') or ''}"
            ).strip(),
            payload=dict(normalized_payload),
            delay_seconds=0.0,
        )
        if event is None:
            return False
        if self._started:
            self._wake.request(session_id, delay_s=0.25)
        return True

    def enqueue_tool_background(self, *, session_id: str, payload: dict[str, Any] | None) -> None:
        key = str(session_id or "").strip()
        raw_payload = dict(payload or {})
        execution_id = str(raw_payload.get("execution_id") or "").strip()
        if not key or not execution_id:
            return
        tool_name = str(raw_payload.get("tool_name") or "tool").strip() or "tool"
        delay_s = self._tool_background_delay_seconds(raw_payload)
        runtime_snapshot = raw_payload.get("runtime_snapshot") if isinstance(raw_payload.get("runtime_snapshot"), dict) else {}
        summary = str(runtime_snapshot.get("summary_text") or "").strip()
        poll_count = self._int_value(raw_payload.get("poll_count"))
        elapsed_seconds = self._float_value(raw_payload.get("elapsed_seconds"))
        dedupe_key = (
            f"tool-background:{execution_id}:{poll_count}:"
            f"{elapsed_seconds:.1f}:{summary[:120]}"
        )
        event = self._events.enqueue(
            session_id=key,
            source="tool_watchdog",
            reason="tool_background",
            dedupe_key=dedupe_key,
            payload={
                **raw_payload,
                "execution_id": execution_id,
                "tool_name": tool_name,
                "runtime_snapshot": runtime_snapshot,
                "recommended_wait_seconds": delay_s,
            },
            delay_seconds=delay_s,
        )
        if event is None:
            return
        if self._started:
            self._wake.request(key, delay_s=delay_s if delay_s > 0 else 0.25)

    def enqueue_tool_terminal(self, *, session_id: str, payload: dict[str, Any] | None) -> None:
        key = str(session_id or "").strip()
        raw_payload = dict(payload or {})
        execution_id = str(raw_payload.get("execution_id") or "").strip()
        if not key or not execution_id:
            return
        tool_name = str(raw_payload.get("tool_name") or "tool").strip() or "tool"
        status = str(raw_payload.get("status") or "completed").strip().lower() or "completed"
        self._events.remove_where(
            key,
            predicate=lambda event: str((event.payload or {}).get("execution_id") or "").strip() == execution_id,
        )
        event = self._events.enqueue(
            session_id=key,
            source="tool_watchdog",
            reason="tool_terminal",
            dedupe_key=f"tool-terminal:{execution_id}:{status}",
            payload={
                **raw_payload,
                "execution_id": execution_id,
                "tool_name": tool_name,
                "status": status,
            },
            delay_seconds=0.0,
        )
        if event is None:
            return
        if self._started:
            self._wake.request(key, delay_s=0.25)

    @staticmethod
    def _float_value(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _int_value(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _tool_background_delay_seconds(self, payload: dict[str, Any]) -> float:
        delay = self._float_value(payload.get("recommended_wait_seconds"), 30.0)
        return max(0.0, delay)

    def _tool_execution_manager(self) -> Any:
        manager = getattr(self._agent, "tool_execution_manager", None)
        if manager is not None:
            return manager
        loop = getattr(self._runtime_manager, "loop", None)
        if loop is not None:
            return getattr(loop, "tool_execution_manager", None)
        return None

    async def _refresh_tool_background_events(self, events: list[SessionHeartbeatEvent]) -> list[SessionHeartbeatEvent]:
        manager = self._tool_execution_manager()
        refreshed: list[SessionHeartbeatEvent] = []
        for event in events:
            if str(event.reason or "").strip().lower() != "tool_background":
                refreshed.append(event)
                continue
            payload = dict(event.payload or {})
            execution_id = str(payload.get("execution_id") or "").strip()
            if manager is None or not execution_id or not hasattr(manager, "wait_execution"):
                payload.update(
                    {
                        "status": "unavailable",
                        "execution_id": execution_id,
                        "tool_name": str(payload.get("tool_name") or "tool").strip() or "tool",
                        "message": "Background tool execution manager is unavailable.",
                    }
                )
                event.payload = payload
                event.reason = "tool_terminal"
                refreshed.append(event)
                continue
            try:
                latest = await manager.wait_execution(execution_id, wait_seconds=0.1)
            except Exception as exc:
                latest = {
                    "status": "failed",
                    "execution_id": execution_id,
                    "tool_name": str(payload.get("tool_name") or "tool").strip() or "tool",
                    "message": f"Failed to refresh background tool execution: {exc}",
                }
            merged = dict(payload)
            if isinstance(latest, dict):
                merged.update(latest)
            merged["execution_id"] = str(merged.get("execution_id") or execution_id).strip()
            merged["tool_name"] = str(merged.get("tool_name") or payload.get("tool_name") or "tool").strip() or "tool"
            status = str(merged.get("status") or "").strip().lower()
            event.payload = merged
            event.reason = "tool_background" if status == "background_running" else "tool_terminal"
            refreshed.append(event)
        return refreshed

    def _refresh_task_stall_events(
        self,
        events: list[SessionHeartbeatEvent],
    ) -> tuple[list[SessionHeartbeatEvent], set[str]]:
        service = self._main_task_service
        refreshed: list[SessionHeartbeatEvent] = []
        discarded_event_ids: set[str] = set()
        grouped: dict[str, list[SessionHeartbeatEvent]] = {}
        for event in events:
            if str(event.reason or "").strip().lower() != "task_stall":
                refreshed.append(event)
                continue
            task_id = str((event.payload or {}).get("task_id") or "").strip()
            if not task_id:
                discarded_event_ids.add(event.event_id)
                continue
            grouped.setdefault(task_id, []).append(event)
        for task_id, bucket_events in grouped.items():
            bucket_events.sort(
                key=lambda item: (
                    int((item.payload or {}).get("bucket_minutes") or 0),
                    str(item.created_at or ""),
                    str(item.event_id or ""),
                ),
                reverse=True,
            )
            latest = bucket_events[0]
            discarded_event_ids.update(event.event_id for event in bucket_events[1:])
            task = service.get_task(task_id) if service is not None and hasattr(service, "get_task") else None
            if task is None:
                discarded_event_ids.add(latest.event_id)
                continue
            if str(getattr(task, "status", "") or "").strip().lower() != "in_progress":
                discarded_event_ids.add(latest.event_id)
                continue
            if bool(getattr(task, "is_paused", False)) or bool(getattr(task, "pause_requested", False)):
                discarded_event_ids.add(latest.event_id)
                continue
            if bool(getattr(task, "cancel_requested", False)):
                discarded_event_ids.add(latest.event_id)
                continue
            runtime_state = getattr(getattr(service, "log_service", None), "read_runtime_state", lambda _task_id: None)(task_id) or {}
            last_visible_output_at = str(
                runtime_state.get("last_visible_output_at")
                or (latest.payload or {}).get("last_visible_output_at")
                or getattr(task, "created_at", "")
                or ""
            ).strip()
            current_bucket = stall_bucket_minutes(last_visible_output_at)
            payload_bucket = int((latest.payload or {}).get("bucket_minutes") or 0)
            if current_bucket <= 0 or current_bucket < payload_bucket:
                discarded_event_ids.add(latest.event_id)
                continue
            detail = service.get_task_detail_payload(task_id, mark_read=False) if hasattr(service, "get_task_detail_payload") else {}
            origin_session_id = (
                str(service._task_origin_session_id(task) or "").strip()
                if hasattr(service, "_task_origin_session_id")
                else str(getattr(task, "session_id", "") or "").strip()
            ) or "web:shared"
            latest.payload = normalize_task_stall_payload(
                {
                    **dict(latest.payload or {}),
                    "task_id": task_id,
                    "session_id": str((latest.payload or {}).get("session_id") or origin_session_id).strip() or "web:shared",
                    "title": str((latest.payload or {}).get("title") or getattr(task, "title", "") or task_id).strip() or task_id,
                    "stalled_minutes": stalled_minutes_since(last_visible_output_at),
                    "bucket_minutes": current_bucket,
                    "last_visible_output_at": last_visible_output_at,
                    "brief_text": str((latest.payload or {}).get("brief_text") or getattr(task, "brief_text", "") or "").strip(),
                    "latest_node_summary": (
                        service._task_stall_latest_node_summary(detail)
                        if hasattr(service, "_task_stall_latest_node_summary")
                        else str((latest.payload or {}).get("latest_node_summary") or "").strip()
                    ),
                    "runtime_summary_excerpt": (
                        service._task_stall_runtime_summary(detail)
                        if hasattr(service, "_task_stall_runtime_summary")
                        else str((latest.payload or {}).get("runtime_summary_excerpt") or "").strip()
                    ),
                }
            )
            if not latest.payload:
                discarded_event_ids.add(latest.event_id)
                continue
            refreshed.append(latest)
        return refreshed, discarded_event_ids

    def _requeue_running_background_events(self, session_id: str, events: list[SessionHeartbeatEvent]) -> None:
        for event in events:
            if str(event.reason or "").strip().lower() != "tool_background":
                continue
            payload = dict(event.payload or {})
            if str(payload.get("status") or "").strip().lower() != "background_running":
                continue
            self.enqueue_tool_background(session_id=session_id, payload=payload)

    def clear_session(self, session_id: str) -> None:
        key = str(session_id or "").strip()
        if not key:
            return
        self._events.clear_session(key)
        self._wake.clear_session(key)
        task = self._prompt_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    def _session_exists(self, session_id: str) -> bool:
        get_path = getattr(self._session_manager, "get_path", None)
        if not callable(get_path):
            return False
        try:
            return bool(get_path(session_id).exists())
        except Exception:
            return False

    def _build_prompt(self, events: list[SessionHeartbeatEvent]) -> str:
        has_tool_background = any(str(event.reason or "").strip().lower() == "tool_background" for event in events)
        has_task_stall = any(str(event.reason or "").strip().lower() == "task_stall" for event in events)
        lines = [
            "This is a background heartbeat. Do not explain internal mechanics.",
            f"If no user-facing update is needed, reply with exactly {HEARTBEAT_OK}.",
            "If a user-facing update is needed, output only the text to show the user.",
        ]
        if has_tool_background:
            lines.extend(
                [
                    "For tool_background events, the payload below has already been refreshed just now.",
                    "Do not call wait_tool_execution in this heartbeat turn.",
                    "Only call stop_tool_execution if you are certain the background execution should be terminated.",
                    f"If the tool is still running and no user-visible update is needed, reply with exactly {HEARTBEAT_OK}.",
                ]
            )
        if has_task_stall:
            lines.extend(
                [
                    "For task_stall events, first inspect the task with task_progress(task_id).",
                    "If the task appears stuck and must be stopped, you may call stop_tool_execution with the task_id.",
                    "After any stop decision, explain the likely cause and the next follow-up action.",
                ]
            )
        heartbeat_text = _bundled_heartbeat_rules_text()
        if heartbeat_text:
            lines.extend(["", heartbeat_text])
        lines.append("")
        lines.append("[SESSION EVENTS]")
        for event in events:
            payload = dict(event.payload or {})
            reason = str(event.reason or "").strip().lower()
            if reason == "tool_background":
                tool_name = str(payload.get("tool_name") or "tool").strip() or "tool"
                execution_id = str(payload.get("execution_id") or "").strip()
                status = str(payload.get("status") or "background_running").strip().lower() or "background_running"
                snapshot = payload.get("runtime_snapshot") if isinstance(payload.get("runtime_snapshot"), dict) else {}
                summary = str(snapshot.get("summary_text") or payload.get("message") or "").strip() or "No snapshot summary."
                elapsed_seconds = self._float_value(payload.get("elapsed_seconds"))
                wait_seconds = self._float_value(payload.get("recommended_wait_seconds"))
                lines.append(f"- Background tool {tool_name} ({execution_id}) is still running")
                lines.append(f"  Status: {status}")
                lines.append(f"  Elapsed: {elapsed_seconds:.1f}s")
                lines.append(f"  Next scheduled heartbeat: {wait_seconds:.1f}s")
                lines.append(f"  Snapshot: {summary}")
                lines.append("  Allowed tool: stop_tool_execution")
                continue
            if reason == "tool_terminal":
                tool_name = str(payload.get("tool_name") or "tool").strip() or "tool"
                execution_id = str(payload.get("execution_id") or "").strip()
                status = str(payload.get("status") or "completed").strip().lower() or "completed"
                summary = str(payload.get("message") or payload.get("final_result") or payload.get("error") or "").strip() or "No terminal summary."
                lines.append(f"- Background tool {tool_name} ({execution_id}) reached a terminal state")
                lines.append(f"  Status: {status}")
                lines.append(f"  Summary: {summary}")
                continue
            if reason == "task_stall":
                task_id = str(payload.get("task_id") or "").strip()
                title = str(payload.get("title") or task_id or "task").strip() or "task"
                stalled_minutes = self._int_value(payload.get("stalled_minutes"))
                bucket_minutes = self._int_value(payload.get("bucket_minutes"))
                brief_text = str(payload.get("brief_text") or "").strip() or "No task summary."
                latest_node_summary = str(payload.get("latest_node_summary") or "").strip() or "No latest node summary."
                runtime_excerpt = str(payload.get("runtime_summary_excerpt") or "").strip() or "No runtime summary."
                last_visible_output_at = str(payload.get("last_visible_output_at") or "").strip() or "unknown"
                lines.append(f"- Task {title} ({task_id}) may be stalled")
                lines.append(f"  Silent for: {stalled_minutes} min")
                lines.append(f"  Trigger bucket: {bucket_minutes} min")
                lines.append(f"  Last visible output at: {last_visible_output_at}")
                lines.append(f"  Brief: {brief_text}")
                lines.append(f"  Latest node: {latest_node_summary}")
                lines.append(f"  Runtime: {runtime_excerpt}")
                lines.append("  Suggested first step: task_progress(task_id)")
                lines.append("  If needed: stop_tool_execution(task_id)")
                continue
            title = str(payload.get("title") or payload.get("task_id") or "task").strip() or "task"
            task_id = str(payload.get("task_id") or "").strip()
            status = str(payload.get("status") or "").strip().lower() or "unknown"
            summary = str(payload.get("brief_text") or payload.get("failure_reason") or "").strip() or "No summary."
            lines.append(f"- Task {title} ({task_id}) completed")
            lines.append(f"  Status: {status}")
            lines.append(f"  Summary: {summary}")
            terminal_node_id = str(payload.get("terminal_node_id") or "").strip()
            terminal_node_kind = str(payload.get("terminal_node_kind") or "").strip() or 'execution'
            terminal_reason = str(payload.get("terminal_node_reason") or "").strip()
            terminal_output = str(payload.get("terminal_output") or "").strip()
            terminal_output_ref = str(payload.get("terminal_output_ref") or "").strip()
            terminal_check_result = str(payload.get("terminal_check_result") or "").strip()
            terminal_failure_reason = str(payload.get("terminal_failure_reason") or "").strip()
            if terminal_node_id:
                lines.append(f"  Result node: {terminal_node_kind} {terminal_node_id}")
            if terminal_reason:
                lines.append(f"  Result source: {terminal_reason}")
            if terminal_output:
                if len(terminal_output) > _TASK_TERMINAL_OUTPUT_INLINE_LIMIT:
                    lines.append(f"  Result output excerpt: {terminal_output[:_TASK_TERMINAL_OUTPUT_INLINE_LIMIT].rstrip()}...")
                else:
                    lines.append(f"  Result output: {terminal_output}")
            if terminal_output_ref:
                lines.append(f"  Result output ref: {terminal_output_ref}")
            if terminal_check_result:
                lines.append(f"  Result check: {terminal_check_result}")
            if terminal_failure_reason and terminal_failure_reason != summary:
                lines.append(f"  Result failure reason: {terminal_failure_reason}")
        return "\n".join(lines).strip()

    @staticmethod
    def _task_terminal_events(events: list[SessionHeartbeatEvent]) -> list[SessionHeartbeatEvent]:
        return [
            event
            for event in list(events or [])
            if str(event.reason or "").strip().lower() == "task_terminal"
        ]

    @staticmethod
    def _truncate_text(text: str, *, limit: int = 180) -> str:
        normalized = str(text or "").strip()
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: max(0, limit - 3)].rstrip()}..."

    def _build_task_terminal_fallback_reply(self, events: list[SessionHeartbeatEvent]) -> str:
        task_events = self._task_terminal_events(events)
        if not task_events:
            return ""
        lines: list[str] = []
        for event in task_events[:3]:
            payload = dict(event.payload or {})
            task_id = str(payload.get("task_id") or "task").strip() or "task"
            short_task_id = task_id[5:] if task_id.startswith("task:") else task_id
            status = str(payload.get("status") or "").strip().lower()
            summary = self._truncate_text(
                str(payload.get("brief_text") or payload.get("failure_reason") or "").strip() or "No summary.",
                limit=180,
            )
            if status == "success":
                lines.append(f"任务 `{short_task_id}` 已完成：{summary}")
            else:
                continuation_task = self._find_continuation_task_for_event(event)
                if continuation_task is not None:
                    continuation_task_id = str(getattr(continuation_task, "task_id", "") or "").strip()
                    short_continuation_id = (
                        continuation_task_id[5:]
                        if continuation_task_id.startswith("task:")
                        else continuation_task_id or "task"
                    )
                    lines.append(f"任务 `{short_task_id}` 已失败，已自动续跑为 `{short_continuation_id}`，我会继续推进。")
                else:
                    lines.append(f"任务 `{short_task_id}` 已失败：{summary}")
        remaining = len(task_events) - min(3, len(task_events))
        if remaining > 0:
            lines.append(f"另有 {remaining} 个任务终态已处理。")
        return "\n".join(lines).strip()

    def _find_continuation_task_for_event(self, event: SessionHeartbeatEvent) -> TaskRecord | None:
        payload = dict(event.payload or {})
        session_id = str(payload.get("session_id") or event.session_id or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        if not session_id or not task_id:
            return None
        finder = getattr(self._main_task_service, "find_reusable_continuation_task", None)
        if not callable(finder):
            return None
        try:
            return finder(session_id=session_id, continuation_of_task_id=task_id)
        except Exception:
            return None

    def _ack_task_terminal_events(self, events: list[SessionHeartbeatEvent]) -> None:
        task_events = self._task_terminal_events(events)
        if not task_events:
            return
        store = getattr(getattr(self._main_task_service, "store", None), "mark_task_terminal_outbox_delivered", None)
        if not callable(store):
            return
        delivered_at = now_iso()
        for event in task_events:
            dedupe_key = str(event.dedupe_key or "").strip()
            if not dedupe_key:
                continue
            try:
                store(dedupe_key, delivered_at=delivered_at)
            except Exception:
                logger.debug("task terminal outbox ack skipped for {}", dedupe_key)

    @staticmethod
    def _task_stall_events(events: list[SessionHeartbeatEvent]) -> list[SessionHeartbeatEvent]:
        return [
            event
            for event in list(events or [])
            if str(event.reason or "").strip().lower() == "task_stall"
        ]

    def _ack_task_stall_events(self, events: list[SessionHeartbeatEvent]) -> None:
        stall_events = self._task_stall_events(events)
        if not stall_events:
            return
        store = getattr(getattr(self._main_task_service, "store", None), "mark_task_stall_outbox_delivered", None)
        if not callable(store):
            return
        delivered_at = now_iso()
        for event in stall_events:
            dedupe_key = str(event.dedupe_key or "").strip()
            if not dedupe_key:
                continue
            try:
                store(dedupe_key, delivered_at=delivered_at)
            except Exception:
                logger.debug("task stall outbox ack skipped for {}", dedupe_key)

    def _serialize_tool_event(self, event: AgentEvent) -> dict[str, Any] | None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        tool_name = str(payload.get("tool_name") or "tool").strip() or "tool"
        text = str(payload.get("text") or "").strip()
        is_error = bool(payload.get("is_error"))
        if event.type == "tool_execution_start":
            status = "running"
        elif event.type == "tool_execution_end":
            status = "error" if is_error else "success"
        else:
            return None
        return {
            "status": status,
            "tool_name": tool_name,
            "text": text,
            "timestamp": event.timestamp,
            "tool_call_id": str(payload.get("tool_call_id") or ""),
            "is_error": is_error,
            "source": "heartbeat",
        }

    def _publish_ceo(self, session_id: str, event_type: str, data: dict[str, Any] | None = None) -> None:
        registry = getattr(self._main_task_service, "registry", None)
        if registry is None:
            return
        registry.publish_ceo(
            session_id,
            build_envelope(
                channel="ceo",
                session_id=session_id,
                seq=registry.next_ceo_seq(session_id),
                type=event_type,
                data=data or {},
            ),
        )

    def _clear_preserved_inflight_turn(self, session_id: str, session: Any) -> str | None:
        source: str | None = None
        getter = getattr(session, "inflight_turn_snapshot", None)
        if callable(getter):
            try:
                snapshot = getter()
            except Exception:
                snapshot = None
            if isinstance(snapshot, dict):
                raw_source = str(snapshot.get("source") or "").strip().lower()
                if raw_source != "heartbeat":
                    source = raw_source or "user"
        clearer = getattr(session, "clear_preserved_inflight_turn", None)
        if callable(clearer):
            try:
                clearer()
            except Exception:
                logger.debug("preserved inflight turn clear skipped for {}", session_id)
        else:
            clear_inflight_turn_snapshot(session_id)
        return source

    @staticmethod
    def _task_terminal_result_metadata(events: list[SessionHeartbeatEvent]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for event in WebSessionHeartbeatService._task_terminal_events(events):
            payload = dict(event.payload or {})
            task_id = str(payload.get('task_id') or '').strip()
            node_id = str(payload.get('terminal_node_id') or '').strip()
            if not task_id:
                continue
            key = (task_id, node_id)
            if key in seen:
                continue
            seen.add(key)
            item = {
                'task_id': task_id,
                'node_id': node_id,
                'node_kind': str(payload.get('terminal_node_kind') or '').strip(),
                'node_reason': str(payload.get('terminal_node_reason') or '').strip(),
                'output': str(payload.get('terminal_output') or '').strip(),
                'output_ref': str(payload.get('terminal_output_ref') or '').strip(),
                'check_result': str(payload.get('terminal_check_result') or '').strip(),
                'failure_reason': str(payload.get('terminal_failure_reason') or '').strip(),
            }
            items.append({key: value for key, value in item.items() if value})
        return items

    def _persist_assistant_reply(self, session_id: str, *, text: str, task_ids: list[str], reason: str, task_results: list[dict[str, str]] | None = None) -> None:
        if not self._session_exists(session_id):
            return
        session = self._session_manager.get_or_create(session_id)
        normalized_task_ids: list[str] = []
        for task_id in list(task_ids or []) + _extract_task_ids_from_text(text):
            task_id_text = str(task_id or "").strip()
            if not task_id_text or not task_id_text.startswith("task:") or task_id_text in normalized_task_ids:
                continue
            normalized_task_ids.append(task_id_text)
        metadata = {
            "source": "heartbeat",
            "reason": reason,
            "task_ids": normalized_task_ids,
        }
        normalized_results = [dict(item) for item in list(task_results or []) if isinstance(item, dict)]
        if normalized_results:
            metadata['task_results'] = normalized_results
        session.add_message(
            "assistant",
            text,
            metadata=metadata,
        )
        update_ceo_session_after_turn(session, user_text="", assistant_text=text)
        self._session_manager.save(session)

    async def _run_session(self, session_id: str) -> float | None:
        key = str(session_id or "").strip()
        if not self._started or not key:
            return None
        if not self._events.has_events(key):
            return None
        if not self._session_exists(key):
            self.clear_session(key)
            return None

        persisted_session = self._session_manager.get_or_create(key)
        normalized_metadata = normalize_ceo_metadata(getattr(persisted_session, "metadata", None), session_key=key)
        if normalized_metadata != getattr(persisted_session, "metadata", None):
            persisted_session.metadata = normalized_metadata
            self._session_manager.save(persisted_session)

        memory_scope = dict(normalized_metadata.get("memory_scope") or {})
        if ":" in key:
            channel, chat_id = key.split(":", 1)
        else:
            channel, chat_id = "web", key
        session = self._runtime_manager.get_or_create(
            session_key=key,
            channel=channel or "web",
            chat_id=chat_id or "shared",
            memory_channel=str(memory_scope.get("channel") or "web"),
            memory_chat_id=str(memory_scope.get("chat_id") or "shared"),
        )
        state = getattr(session, "state", None)
        status = str(getattr(state, "status", "") or "").strip().lower()
        next_delay = self._events.next_delay(key)
        if bool(getattr(state, "is_running", False)) or status == "running":
            if next_delay is None:
                return 1.0
            return min(1.0, max(0.1, next_delay))

        events = self._events.peek_ready(key)
        if not events:
            return next_delay
        events = await self._refresh_tool_background_events(events)
        events, discarded_task_stall_ids = self._refresh_task_stall_events(events)
        if discarded_task_stall_ids:
            discarded_events = self._events.pop_many(key, event_ids=discarded_task_stall_ids)
            self._ack_task_stall_events(discarded_events)
            events = [event for event in events if event.event_id not in discarded_task_stall_ids]
        if not events:
            return self._events.next_delay(key)
        reasons = sorted({str(event.reason or "").strip().lower() or "heartbeat" for event in events})
        heartbeat_reason = reasons[0] if len(reasons) == 1 else "mixed"
        user_input = UserInputMessage(
            content=self._build_prompt(events),
            metadata={
                "heartbeat_internal": True,
                "heartbeat_reason": heartbeat_reason,
                "heartbeat_task_ids": [str((event.payload or {}).get("task_id") or "").strip() for event in events],
            },
        )

        async def _relay(event: AgentEvent) -> None:
            if event.type == "state_snapshot":
                state_payload = dict((event.payload or {}).get("state") or {})
                self._publish_ceo(key, "ceo.state", {"state": state_payload, "source": "heartbeat"})
                return
            serialized = self._serialize_tool_event(event)
            if serialized is not None:
                self._publish_ceo(key, "ceo.agent.tool", serialized)

        unsubscribe = session.subscribe(_relay)
        prompt_task = asyncio.create_task(session.prompt(user_input, persist_transcript=False))
        self._prompt_tasks[key] = prompt_task
        register_task = getattr(self._agent, "_register_active_task", None)
        if callable(register_task):
            register_task(key, prompt_task)
        try:
            result = await prompt_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("heartbeat session run failed for {}", key)
            self._publish_ceo(key, "ceo.turn.discard", {"source": "heartbeat"})
            return 10.0
        finally:
            unsubscribe()
            current = self._prompt_tasks.get(key)
            if current is prompt_task:
                self._prompt_tasks.pop(key, None)

        output = str(getattr(result, "output", "") or "").strip()
        task_terminal_events = self._task_terminal_events(events)
        if (not output or output == HEARTBEAT_OK) and task_terminal_events:
            output = self._build_task_terminal_fallback_reply(task_terminal_events)

        if not self._session_exists(key):
            event_ids = {event.event_id for event in events}
            popped = self._events.pop_many(key, event_ids=event_ids)
            self._requeue_running_background_events(key, events)
            self._ack_task_stall_events(popped)
            self.clear_session(key)
            return None
        if not output or output == HEARTBEAT_OK:
            event_ids = {event.event_id for event in events}
            popped = self._events.pop_many(key, event_ids=event_ids)
            self._requeue_running_background_events(key, events)
            self._ack_task_stall_events(popped)
            next_delay = self._events.next_delay(key)
            self._publish_ceo(key, "ceo.turn.discard", {"source": "heartbeat"})
            return next_delay

        task_ids = [
            str((event.payload or {}).get("task_id") or "").strip()
            for event in events
            if str((event.payload or {}).get("task_id") or "").strip()
        ]
        for event in task_terminal_events:
            continuation_task = self._find_continuation_task_for_event(event)
            continuation_task_id = str(getattr(continuation_task, "task_id", "") or "").strip()
            if continuation_task_id and continuation_task_id not in task_ids:
                task_ids.append(continuation_task_id)
        task_results = self._task_terminal_result_metadata(events)
        preserved_source = self._clear_preserved_inflight_turn(key, session)
        if preserved_source:
            self._publish_ceo(key, "ceo.turn.discard", {"source": preserved_source})
        self._persist_assistant_reply(
            key,
            text=output,
            task_ids=task_ids,
            reason=heartbeat_reason,
            task_results=task_results,
        )
        self._publish_ceo(key, "ceo.reply.final", {"text": output, "source": "heartbeat"})
        await self._notify_reply(key, output)
        event_ids = {event.event_id for event in events}
        popped = self._events.pop_many(key, event_ids=event_ids)
        self._requeue_running_background_events(key, events)
        self._ack_task_terminal_events(events)
        self._ack_task_stall_events(popped)
        next_delay = self._events.next_delay(key)
        return next_delay

    async def _notify_reply(self, session_id: str, text: str) -> None:
        notifier = self._reply_notifier
        payload = str(text or "").strip()
        if notifier is None or not payload:
            return
        try:
            maybe = notifier(str(session_id or "").strip(), payload)
            if hasattr(maybe, "__await__"):
                await maybe
        except Exception:
            logger.debug("heartbeat reply notify skipped for {}", session_id)
