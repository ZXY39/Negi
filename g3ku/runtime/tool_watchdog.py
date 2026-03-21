from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class ToolWatchdogConfig:
    enabled: bool = True
    poll_interval_seconds: float = 5.0
    handoff_after_seconds: float = 30.0
    stop_grace_seconds: float = 2.0
    text_char_limit: int = 280
    list_limit: int = 3


@dataclass(slots=True)
class ToolWatchdogRunResult:
    completed: bool
    value: Any
    elapsed_seconds: float
    poll_count: int
    snapshot: dict[str, Any] | None = None
    execution_id: str = ""


@dataclass(slots=True)
class DetachedToolExecution:
    execution_id: str
    tool_name: str
    arguments: dict[str, Any]
    task: asyncio.Task[Any]
    snapshot_supplier: Callable[[], Any] | None
    cancel_token: Any | None
    started_at: float
    created_at: float
    session_key: str = ""
    terminal_notifier: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    terminal_notified: bool = False
    handoff_count: int = 1


DEFAULT_WAIT_WINDOWS_SECONDS: tuple[float, ...] = (30.0, 60.0, 120.0, 240.0, 600.0)


class ToolExecutionManager:
    def __init__(self) -> None:
        self._counter = 0
        self._executions: dict[str, DetachedToolExecution] = {}
        self._lock = asyncio.Lock()

    async def register_execution(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        task: asyncio.Task[Any],
        snapshot_supplier: Callable[[], Any] | None,
        cancel_token: Any | None,
        started_at: float,
        session_key: str = "",
        terminal_notifier: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> DetachedToolExecution:
        async with self._lock:
            for entry in self._executions.values():
                if entry.task is task:
                    return entry
            self._counter += 1
            execution_id = f"tool-exec:{self._counter}"
            entry = DetachedToolExecution(
                execution_id=execution_id,
                tool_name=str(tool_name or "tool"),
                arguments=dict(arguments or {}),
                task=task,
                snapshot_supplier=snapshot_supplier,
                cancel_token=cancel_token,
                started_at=float(started_at),
                created_at=time.monotonic(),
                session_key=str(session_key or "").strip(),
                terminal_notifier=terminal_notifier if callable(terminal_notifier) else None,
                handoff_count=1,
            )
            self._executions[execution_id] = entry
            task.add_done_callback(
                lambda _task, stored_execution_id=execution_id: self._schedule_terminal_notification(
                    stored_execution_id
                )
            )
            return entry

    async def wait_execution(
        self,
        execution_id: str,
        *,
        wait_seconds: float = 20.0,
        poll_interval_seconds: float = 5.0,
        text_char_limit: int = 280,
        list_limit: int = 3,
        on_poll: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        entry = await self._get_execution(execution_id)
        if entry is None:
            return {
                "status": "not_found",
                "execution_id": str(execution_id or ""),
                "message": "没有找到对应的后台工具执行记录，可能已经完成并被清理，或者 execution_id 无效。",
            }

        effective_wait_seconds = float(wait_seconds) if wait_seconds and float(wait_seconds) > 0 else self.recommended_wait_seconds(entry)
        try:
            result = await _wait_for_task_window(
                task=entry.task,
                tool_name=entry.tool_name,
                started_at=entry.started_at,
                snapshot_supplier=entry.snapshot_supplier,
                poll_interval_seconds=poll_interval_seconds,
                handoff_after_seconds=effective_wait_seconds,
                text_char_limit=text_char_limit,
                list_limit=list_limit,
                on_poll=on_poll,
            )
        except asyncio.CancelledError:
            await self._remove_execution(entry.execution_id)
            return {
                "status": "stopped",
                "execution_id": entry.execution_id,
                "tool_name": entry.tool_name,
                "message": "后台工具已经停止。",
            }
        except Exception as exc:
            await self._remove_execution(entry.execution_id)
            return {
                "status": "failed",
                "execution_id": entry.execution_id,
                "tool_name": entry.tool_name,
                "message": "后台工具执行失败。",
                "error": str(exc),
            }
        if result.completed:
            await self._remove_execution(entry.execution_id)
            return _build_completion_payload(entry=entry, result=result.value)
        entry.handoff_count += 1
        return _build_handoff_payload(
            tool_name=entry.tool_name,
            arguments=entry.arguments,
            execution_id=entry.execution_id,
            elapsed_seconds=result.elapsed_seconds,
            poll_count=result.poll_count,
            snapshot=result.snapshot,
            continued_wait=True,
            recommended_wait_seconds=self.recommended_wait_seconds(entry),
        )

    async def stop_execution(
        self,
        execution_id: str,
        *,
        reason: str = "agent_requested_stop",
        stop_grace_seconds: float = 2.0,
    ) -> dict[str, Any]:
        entry = await self._get_execution(execution_id)
        if entry is None:
            return {
                "status": "not_found",
                "execution_id": str(execution_id or ""),
                "message": "没有找到对应的后台工具执行记录，可能已经完成并被清理，或者 execution_id 无效。",
            }

        if entry.task.done():
            await self._remove_execution(entry.execution_id)
            return _build_completion_payload(entry=entry, result=await _await_finished_task(entry.task))

        await request_tool_cancellation(
            entry.task,
            cancel_token=entry.cancel_token,
            reason=reason,
            grace_seconds=stop_grace_seconds,
        )
        snapshot = summarize_runtime_snapshot(
            await _maybe_await_callable(entry.snapshot_supplier),
            text_char_limit=280,
            list_limit=3,
        )
        payload = {
            "status": "stopped",
            "execution_id": entry.execution_id,
            "tool_name": entry.tool_name,
            "message": "已按要求停止该后台工具执行；如果工具启动了子进程，也会一起尝试结束。",
            "elapsed_seconds": round(max(0.0, time.monotonic() - entry.started_at), 1),
            "runtime_snapshot": snapshot,
        }
        await self._remove_execution(entry.execution_id)
        return payload

    async def stop_session_executions(
        self,
        session_key: str,
        *,
        reason: str = "session_deleted",
        stop_grace_seconds: float = 2.0,
    ) -> list[dict[str, Any]]:
        key = str(session_key or "").strip()
        if not key:
            return []
        async with self._lock:
            execution_ids = [
                entry.execution_id
                for entry in self._executions.values()
                if str(entry.session_key or "").strip() == key
            ]
        results: list[dict[str, Any]] = []
        for execution_id in execution_ids:
            results.append(
                await self.stop_execution(
                    execution_id,
                    reason=reason,
                    stop_grace_seconds=stop_grace_seconds,
                )
            )
        return results

    async def _get_execution(self, execution_id: str) -> DetachedToolExecution | None:
        async with self._lock:
            return self._executions.get(str(execution_id or "").strip())

    async def _remove_execution(self, execution_id: str) -> None:
        async with self._lock:
            self._executions.pop(str(execution_id or "").strip(), None)

    def _schedule_terminal_notification(self, execution_id: str) -> None:
        key = str(execution_id or "").strip()
        if not key:
            return
        try:
            asyncio.get_running_loop().create_task(self._emit_terminal_notification(key))
        except RuntimeError:
            return

    async def _emit_terminal_notification(self, execution_id: str) -> None:
        async with self._lock:
            entry = self._executions.get(str(execution_id or "").strip())
            if entry is None or entry.terminal_notified or entry.terminal_notifier is None:
                return
            entry.terminal_notified = True
            notifier = entry.terminal_notifier
        payload = await _build_terminal_payload(entry)
        try:
            await _maybe_await(notifier(payload))
        except Exception:
            return

    @staticmethod
    def recommended_wait_seconds(entry: DetachedToolExecution | None) -> float:
        if entry is None:
            return DEFAULT_WAIT_WINDOWS_SECONDS[1]
        index = max(0, min(int(entry.handoff_count), len(DEFAULT_WAIT_WINDOWS_SECONDS) - 1))
        return float(DEFAULT_WAIT_WINDOWS_SECONDS[index])


def runtime_context_value(runtime_context: Any, key: str, default: Any = None) -> Any:
    if isinstance(runtime_context, dict):
        return runtime_context.get(key, default)
    return getattr(runtime_context, key, default)


def actor_role_allows_watchdog(runtime_context: Any) -> bool:
    role = str(runtime_context_value(runtime_context, "actor_role", "") or "").strip().lower()
    return role == "ceo"


def resolve_tool_watchdog_config(runtime_context: Any) -> ToolWatchdogConfig:
    raw = runtime_context_value(runtime_context, "tool_watchdog", None)
    payload = dict(raw) if isinstance(raw, dict) else {}
    enabled = payload.get("enabled", True)
    poll_interval = payload.get("poll_interval_seconds", 5.0)
    handoff_after = payload.get("handoff_after_seconds", payload.get("stale_after_seconds", DEFAULT_WAIT_WINDOWS_SECONDS[0]))
    stop_grace = payload.get("stop_grace_seconds", payload.get("cancel_grace_seconds", 2.0))
    text_char_limit = payload.get("text_char_limit", 280)
    list_limit = payload.get("list_limit", 3)
    return ToolWatchdogConfig(
        enabled=bool(enabled),
        poll_interval_seconds=max(0.2, float(poll_interval or 5.0)),
        handoff_after_seconds=max(0.1, float(handoff_after or DEFAULT_WAIT_WINDOWS_SECONDS[0])),
        stop_grace_seconds=max(0.0, float(stop_grace or 0.0)),
        text_char_limit=max(80, int(text_char_limit or 280)),
        list_limit=max(1, int(list_limit or 3)),
    )


def resolve_snapshot_supplier(runtime_context: Any) -> Callable[[], Any] | None:
    supplier = runtime_context_value(runtime_context, "tool_snapshot_supplier", None)
    return supplier if callable(supplier) else None


def resolve_terminal_notifier(
    runtime_context: Any,
) -> Callable[[dict[str, Any]], Awaitable[None] | None] | None:
    session_key = str(runtime_context_value(runtime_context, "session_key", "") or "").strip()
    if not session_key:
        return None
    notifier = runtime_context_value(runtime_context, "tool_terminal_notifier", None)
    if callable(notifier):
        return notifier
    heartbeat = runtime_context_value(runtime_context, "web_session_heartbeat", None)
    if heartbeat is None:
        loop = runtime_context_value(runtime_context, "loop", None)
        heartbeat = getattr(loop, "web_session_heartbeat", None) if loop is not None else None
    if heartbeat is None or not hasattr(heartbeat, "enqueue_tool_terminal"):
        return None

    def _notify(payload: dict[str, Any]) -> None:
        heartbeat.enqueue_tool_terminal(session_id=session_key, payload=dict(payload or {}))

    return _notify


async def request_tool_cancellation(
    execution_task: asyncio.Task[Any],
    *,
    cancel_token: Any | None,
    reason: str,
    grace_seconds: float,
) -> None:
    if cancel_token is not None and hasattr(cancel_token, "cancel"):
        try:
            cancel_token.cancel(reason=reason)
        except Exception:
            pass

    if execution_task.done():
        return

    if grace_seconds > 0:
        try:
            await asyncio.wait_for(asyncio.shield(execution_task), timeout=grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return
        except Exception:
            return

    execution_task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(execution_task), timeout=max(0.1, grace_seconds))
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        return


def summarize_runtime_snapshot(
    payload: Any,
    *,
    text_char_limit: int = 280,
    list_limit: int = 3,
) -> dict[str, Any] | None:
    if payload is None:
        return {
            "snapshot_type": "empty",
            "summary_text": "当前还没有可用的运行快照。",
        }

    if isinstance(payload, dict) and isinstance(payload.get("task"), dict) and isinstance(payload.get("progress"), dict):
        task = payload["task"]
        progress = payload["progress"]
        latest_node = progress.get("latest_node") if isinstance(progress.get("latest_node"), dict) else {}
        root = progress.get("root") if isinstance(progress.get("root"), dict) else {}
        live_state = progress.get("live_state") if isinstance(progress.get("live_state"), dict) else payload.get("runtime_summary")
        tool_steps = []
        execution_trace = latest_node.get("execution_trace") if isinstance(latest_node, dict) else None
        if isinstance(execution_trace, dict):
            tool_steps = list(execution_trace.get("tool_steps") or [])
        if not tool_steps:
            tool_steps = _runtime_summary_tool_steps(live_state)
        recent_tools = [
            {
                "tool_name": str(item.get("tool_name") or "tool"),
                "status": str(item.get("status") or "unknown"),
                "output_text": _clip_text(item.get("output_text", ""), limit=text_char_limit // 2),
            }
            for item in tool_steps[-list_limit:]
            if isinstance(item, dict)
        ]
        latest_summary_source = latest_node.get("output") or _runtime_summary_tool_calls_summary(
            live_state,
            preferred_node_id=latest_node.get("node_id"),
            limit=list_limit,
        )
        latest_summary = _clip_text(
            latest_summary_source,
            limit=text_char_limit,
        )
        node_title = str(latest_node.get("title") or root.get("goal") or latest_node.get("node_id") or "当前节点")
        node_status = str(latest_node.get("status") or task.get("status") or "in_progress")
        summary_text = f"任务仍在进行中；最近节点“{node_title}”状态为 {node_status}。"
        if latest_summary:
            summary_text = f"{summary_text} 最近输出：{latest_summary}"
        return {
            "snapshot_type": "main_task_detail",
            "summary_text": summary_text,
            "task_id": str(task.get("task_id") or ""),
            "task_status": str(task.get("status") or ""),
            "updated_at": str(task.get("updated_at") or ""),
            "latest_node": {
                "node_id": str(latest_node.get("node_id") or ""),
                "status": node_status,
                "title": node_title,
                "updated_at": str(latest_node.get("updated_at") or ""),
                "output_excerpt": latest_summary,
            },
            "recent_tool_steps": recent_tools,
        }

    if isinstance(payload, dict) and ("tool_events" in payload or "assistant_text" in payload or "status" in payload):
        tool_events = [item for item in list(payload.get("tool_events") or []) if isinstance(item, dict)]
        recent_events = [
            {
                "tool_name": str(item.get("tool_name") or "tool"),
                "status": str(item.get("status") or "running"),
                "text": _clip_text(item.get("text", ""), limit=text_char_limit // 2),
            }
            for item in tool_events[-list_limit:]
        ]
        latest_event = recent_events[-1] if recent_events else {}
        latest_text = str(latest_event.get("text") or "")
        assistant_text = _clip_text(payload.get("assistant_text", ""), limit=text_char_limit // 2)
        summary_text = f"会话仍在运行，最近阶段状态为 {str(payload.get('status') or 'running')}。"
        if latest_text:
            summary_text = f"{summary_text} 最近进度：{latest_text}"
        elif assistant_text:
            summary_text = f"{summary_text} 助手最近输出：{assistant_text}"
        return {
            "snapshot_type": "ceo_inflight_turn",
            "summary_text": summary_text,
            "status": str(payload.get("status") or "running"),
            "assistant_text_excerpt": assistant_text,
            "recent_tool_events": recent_events,
            "last_error": _compact_error(payload.get("last_error")),
        }

    if isinstance(payload, dict):
        compact: dict[str, Any] = {}
        for key, value in list(payload.items())[:list_limit]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                compact[str(key)] = value
            elif isinstance(value, dict):
                compact[str(key)] = {
                    str(inner_key): inner_value
                    for inner_key, inner_value in list(value.items())[:list_limit]
                    if isinstance(inner_value, (str, int, float, bool)) or inner_value is None
                }
            elif isinstance(value, list):
                compact[str(key)] = [_clip_text(item, limit=text_char_limit // 4) for item in value[:list_limit]]
            else:
                compact[str(key)] = _clip_text(value, limit=text_char_limit // 4)
        return {
            "snapshot_type": "generic",
            "summary_text": _clip_text(json.dumps(compact, ensure_ascii=False), limit=text_char_limit),
            "payload": compact,
        }

    return {
        "snapshot_type": "scalar",
        "summary_text": _clip_text(payload, limit=text_char_limit),
    }


def _runtime_summary_tool_steps(runtime_summary: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime_summary, dict):
        return []
    collected: list[dict[str, Any]] = []
    for frame in list(runtime_summary.get("frames") or []):
        if not isinstance(frame, dict):
            continue
        for item in list(frame.get("tool_calls") or []):
            if not isinstance(item, dict):
                continue
            collected.append(
                {
                    "tool_name": str(item.get("tool_name") or "tool"),
                    "status": str(item.get("status") or "unknown"),
                    "output_text": "",
                }
            )
    return collected


def _runtime_summary_tool_calls_summary(
    runtime_summary: Any,
    *,
    preferred_node_id: Any = "",
    limit: int = 3,
) -> str:
    if not isinstance(runtime_summary, dict):
        return ""
    frames = [item for item in list(runtime_summary.get("frames") or []) if isinstance(item, dict)]
    if not frames:
        return ""
    selected = None
    preferred = str(preferred_node_id or "").strip()
    if preferred:
        selected = next((frame for frame in frames if str(frame.get("node_id") or "").strip() == preferred), None)
    if selected is None:
        frames_by_node = {str(frame.get("node_id") or "").strip(): frame for frame in frames if str(frame.get("node_id") or "").strip()}
        for node_id in [
            *list(runtime_summary.get("active_node_ids") or []),
            *list(runtime_summary.get("runnable_node_ids") or []),
            *list(runtime_summary.get("waiting_node_ids") or []),
        ]:
            selected = frames_by_node.get(str(node_id or "").strip())
            if selected is not None:
                break
    if selected is None:
        selected = frames[0]
    lines: list[str] = []
    tool_calls = [item for item in list(selected.get("tool_calls") or []) if isinstance(item, dict) and str(item.get("tool_name") or "").strip()]
    if tool_calls:
        lines.append("Recent tool calls:")
        for item in tool_calls[-max(1, int(limit or 1)) :]:
            tool_name = str(item.get("tool_name") or "tool").strip() or "tool"
            status = str(item.get("status") or "queued").strip() or "queued"
            lines.append(f"- {tool_name} [{status}]")
    return "\n".join(lines)


async def run_tool_with_watchdog(
    awaitable: Awaitable[Any],
    *,
    tool_name: str,
    arguments: dict[str, Any],
    runtime_context: Any,
    snapshot_supplier: Callable[[], Any] | None = None,
    manager: ToolExecutionManager | None = None,
    on_poll: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> ToolWatchdogRunResult:
    config = resolve_tool_watchdog_config(runtime_context)
    if not config.enabled:
        value = await awaitable
        return ToolWatchdogRunResult(
            completed=True,
            value=value,
            elapsed_seconds=0.0,
            poll_count=0,
            snapshot=None,
            execution_id="",
        )

    supplier = snapshot_supplier or resolve_snapshot_supplier(runtime_context)
    execution_task = asyncio.create_task(awaitable, name=f"tool-watchdog:{tool_name}")
    cancel_token = runtime_context_value(runtime_context, "cancel_token", None)
    session_key = str(runtime_context_value(runtime_context, "session_key", "") or "").strip()
    terminal_notifier = resolve_terminal_notifier(runtime_context)
    started_at = time.monotonic()

    try:
        if manager is None:
            result = await _wait_for_task_window(
                task=execution_task,
                tool_name=tool_name,
                started_at=started_at,
                snapshot_supplier=supplier,
                poll_interval_seconds=config.poll_interval_seconds,
                handoff_after_seconds=10_000_000.0,
                text_char_limit=config.text_char_limit,
                list_limit=config.list_limit,
                on_poll=on_poll,
            )
            return ToolWatchdogRunResult(
                completed=True,
                value=result.value,
                elapsed_seconds=result.elapsed_seconds,
                poll_count=result.poll_count,
                snapshot=result.snapshot,
                execution_id="",
            )

        wait_result = await _wait_for_task_window(
            task=execution_task,
            tool_name=tool_name,
            started_at=started_at,
            snapshot_supplier=supplier,
            poll_interval_seconds=config.poll_interval_seconds,
            handoff_after_seconds=config.handoff_after_seconds,
            text_char_limit=config.text_char_limit,
            list_limit=config.list_limit,
            on_poll=on_poll,
        )
        if wait_result.completed:
            return ToolWatchdogRunResult(
                completed=True,
                value=wait_result.value,
                elapsed_seconds=wait_result.elapsed_seconds,
                poll_count=wait_result.poll_count,
                snapshot=wait_result.snapshot,
                execution_id="",
            )

        entry = await manager.register_execution(
            tool_name=tool_name,
            arguments=arguments,
            task=execution_task,
            snapshot_supplier=supplier,
            cancel_token=cancel_token,
            started_at=started_at,
            session_key=session_key,
            terminal_notifier=terminal_notifier,
        )
        payload = _build_handoff_payload(
            tool_name=tool_name,
            arguments=arguments,
            execution_id=entry.execution_id,
            elapsed_seconds=wait_result.elapsed_seconds,
            poll_count=wait_result.poll_count,
            snapshot=wait_result.snapshot,
            continued_wait=False,
            recommended_wait_seconds=manager.recommended_wait_seconds(entry),
        )
        return ToolWatchdogRunResult(
            completed=False,
            value=payload,
            elapsed_seconds=wait_result.elapsed_seconds,
            poll_count=wait_result.poll_count,
            snapshot=wait_result.snapshot,
            execution_id=entry.execution_id,
        )
    except BaseException:
        if not execution_task.done():
            await request_tool_cancellation(
                execution_task,
                cancel_token=cancel_token,
                reason=f"watchdog_aborted:{tool_name}",
                grace_seconds=config.stop_grace_seconds,
            )
        raise


async def _wait_for_task_window(
    *,
    task: asyncio.Task[Any],
    tool_name: str,
    started_at: float,
    snapshot_supplier: Callable[[], Any] | None,
    poll_interval_seconds: float,
    handoff_after_seconds: float,
    text_char_limit: int,
    list_limit: int,
    on_poll: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> ToolWatchdogRunResult:
    poll_count = 0
    last_snapshot: dict[str, Any] | None = None
    deadline = time.monotonic() + max(0.1, float(handoff_after_seconds))
    while True:
        remaining_to_handoff = deadline - time.monotonic()
        if remaining_to_handoff <= 0:
            snapshot = summarize_runtime_snapshot(
                await _maybe_await_callable(snapshot_supplier),
                text_char_limit=text_char_limit,
                list_limit=list_limit,
            )
            elapsed = max(0.0, time.monotonic() - started_at)
            return ToolWatchdogRunResult(
                completed=False,
                value=None,
                elapsed_seconds=elapsed,
                poll_count=poll_count,
                snapshot=snapshot or last_snapshot,
            )

        wait_timeout = min(max(0.05, float(poll_interval_seconds)), remaining_to_handoff)
        try:
            value = await asyncio.wait_for(asyncio.shield(task), timeout=wait_timeout)
            elapsed = max(0.0, time.monotonic() - started_at)
            return ToolWatchdogRunResult(
                completed=True,
                value=value,
                elapsed_seconds=elapsed,
                poll_count=poll_count,
                snapshot=last_snapshot,
            )
        except asyncio.TimeoutError:
            poll_count += 1
            last_snapshot = summarize_runtime_snapshot(
                await _maybe_await_callable(snapshot_supplier),
                text_char_limit=text_char_limit,
                list_limit=list_limit,
            )
            if on_poll is not None:
                elapsed = max(0.0, time.monotonic() - started_at)
                await _maybe_await(
                    on_poll(
                        {
                            "tool_name": str(tool_name or "tool"),
                            "elapsed_seconds": round(elapsed, 1),
                            "poll_count": poll_count,
                            "snapshot": last_snapshot,
                            "next_handoff_in_seconds": round(max(0.0, deadline - time.monotonic()), 1),
                        }
                    )
                )


def _build_handoff_payload(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    execution_id: str,
    elapsed_seconds: float,
    poll_count: int,
    snapshot: dict[str, Any] | None,
    continued_wait: bool,
    recommended_wait_seconds: float,
) -> dict[str, Any]:
    message = "工具仍在后台运行，我先把当前运行快照交给你判断。"
    if continued_wait:
        message = "工具仍在后台运行；你刚才选择继续等待，我把新的运行快照再交给你判断。"
    return {
        "status": "background_running",
        "tool_name": str(tool_name or "tool"),
        "execution_id": str(execution_id or ""),
        "message": f"{message} 如果决定继续等待，请调用 wait_tool_execution；如果决定停止，请调用 stop_tool_execution。",
        "elapsed_seconds": round(float(elapsed_seconds), 1),
        "poll_count": int(poll_count),
        "argument_preview": _preview_arguments(arguments),
        "runtime_snapshot": snapshot,
        "agent_guidance": "根据 runtime_snapshot 判断是继续等待、改用别的工具，还是调用 stop_tool_execution 主动结束当前后台执行。",
        "next_actions": ["wait_tool_execution", "stop_tool_execution"],
        "recommended_wait_seconds": round(float(recommended_wait_seconds), 1),
    }


def _build_completion_payload(*, entry: DetachedToolExecution, result: Any) -> dict[str, Any]:
    normalized = _normalize_detached_result(result)
    return {
        "status": "completed",
        "execution_id": entry.execution_id,
        "tool_name": entry.tool_name,
        "message": "后台工具已经完成，final_result 字段就是该工具的最终输出。",
        "elapsed_seconds": round(max(0.0, time.monotonic() - entry.started_at), 1),
        "final_result": normalized,
    }


async def _build_terminal_payload(entry: DetachedToolExecution) -> dict[str, Any]:
    try:
        result = await asyncio.shield(entry.task)
    except asyncio.CancelledError:
        snapshot = summarize_runtime_snapshot(
            await _maybe_await_callable(entry.snapshot_supplier),
            text_char_limit=280,
            list_limit=3,
        )
        return {
            "status": "stopped",
            "execution_id": entry.execution_id,
            "tool_name": entry.tool_name,
            "message": "后台工具执行已停止。",
            "elapsed_seconds": round(max(0.0, time.monotonic() - entry.started_at), 1),
            "runtime_snapshot": snapshot,
        }
    except Exception as exc:
        snapshot = summarize_runtime_snapshot(
            await _maybe_await_callable(entry.snapshot_supplier),
            text_char_limit=280,
            list_limit=3,
        )
        return {
            "status": "failed",
            "execution_id": entry.execution_id,
            "tool_name": entry.tool_name,
            "message": "后台工具执行失败。",
            "error": str(exc),
            "elapsed_seconds": round(max(0.0, time.monotonic() - entry.started_at), 1),
            "runtime_snapshot": snapshot,
        }
    return _build_completion_payload(entry=entry, result=result)


def _normalize_detached_result(value: Any) -> Any:
    if hasattr(value, "content"):
        content = getattr(value, "content", "")
        name = str(getattr(value, "name", "") or "")
        status = str(getattr(value, "status", "") or "")
        payload: dict[str, Any] = {"content": _json_safe(content)}
        if name:
            payload["name"] = name
        if status:
            payload["status"] = status
        return payload
    return _json_safe(value)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return str(value)


async def _await_finished_task(task: asyncio.Task[Any]) -> Any:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        return {"status": "stopped", "message": "后台任务已取消。"}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _maybe_await_callable(callback: Callable[[], Any] | None) -> Any:
    if callback is None:
        return None
    return await _maybe_await(callback())


def _clip_text(value: Any, *, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}..."


def _compact_error(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return {
            "code": str(value.get("code") or ""),
            "message": _clip_text(value.get("message", ""), limit=180),
        }
    return None


def _preview_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for key, value in list((arguments or {}).items())[:5]:
        if isinstance(value, dict):
            preview[str(key)] = {
                str(inner_key): _clip_text(inner_value, limit=80)
                for inner_key, inner_value in list(value.items())[:3]
            }
        elif isinstance(value, list):
            preview[str(key)] = [_clip_text(item, limit=80) for item in value[:3]]
        else:
            preview[str(key)] = _clip_text(value, limit=120)
    return preview
