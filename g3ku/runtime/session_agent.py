from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from g3ku.prompt_trace import render_output_trace
from g3ku.core.events import AgentEvent
from g3ku.core.messages import AssistantMessage, UserInputMessage
from g3ku.core.results import RunResult
from g3ku.core.state import AgentState, StructuredError
from g3ku.runtime.cancellation import ToolCancellationToken

_CONTROL_TOOL_NAMES = {"wait_tool_execution", "stop_tool_execution"}


class RuntimeAgentSession:
    """Primary AgentSession implementation backed by the runtime engine."""

    def __init__(
        self,
        loop,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        memory_channel: str | None = None,
        memory_chat_id: str | None = None,
    ):
        self._loop = loop
        self._channel = channel
        self._chat_id = chat_id
        self._memory_channel = str(memory_channel or channel or "unknown")
        self._memory_chat_id = str(memory_chat_id or chat_id or "unknown")
        self._multi_agent_runner = getattr(loop, "multi_agent_runner", None)
        self._state = AgentState(
            session_key=session_key,
            system_prompt="",
            model=str(getattr(loop, "model", "")),
            reasoning_effort=getattr(loop, "reasoning_effort", None),
        )
        self._listeners: set[Callable[[AgentEvent], Awaitable[None] | None]] = set()
        self._last_prompt: str | UserInputMessage = ""
        self._event_log: list[dict] = []
        self._pending_tool_names: dict[str, str] = {}
        self._background_tool_targets: dict[str, dict[str, str]] = {}
        self._tool_seq: int = 0
        self._active_cancel_token: ToolCancellationToken | None = None
        self._preserved_inflight_turn: dict[str, Any] | None = None
        self._interaction_trace: dict[str, Any] | None = None
        self._current_stage: dict[str, Any] | None = None
        self._turn_lock = asyncio.Lock()

    @property
    def state(self) -> AgentState:
        return self._state

    def subscribe(self, listener: Callable[[AgentEvent], Awaitable[None] | None]):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    def state_dict(self) -> dict:
        data = asdict(self._state)
        data["session_id"] = self._state.session_key
        data["pending_tool_calls"] = sorted(self._state.pending_tool_calls)
        if self._state.last_error is not None:
            data["last_error"] = asdict(self._state.last_error)
        if isinstance(self._current_stage, dict) and self._current_stage:
            data["stage"] = copy.deepcopy(self._current_stage)
        return data

    def set_interaction_trace(self, trace: dict[str, Any] | None, *, stage: dict[str, Any] | None = None) -> None:
        self._interaction_trace = copy.deepcopy(trace) if isinstance(trace, dict) and trace else None
        self._current_stage = copy.deepcopy(stage) if isinstance(stage, dict) and stage else None

    def clear_interaction_trace(self) -> None:
        self._interaction_trace = None
        self._current_stage = None

    def interaction_trace_snapshot(self) -> dict[str, Any] | None:
        if not isinstance(self._interaction_trace, dict) or not self._interaction_trace:
            return None
        return copy.deepcopy(self._interaction_trace)

    def current_stage_snapshot(self) -> dict[str, Any] | None:
        if not isinstance(self._current_stage, dict) or not self._current_stage:
            return None
        return copy.deepcopy(self._current_stage)

    def _normalize_live_context(self, live_context: dict[str, str] | None) -> dict[str, str]:
        current_channel = str(getattr(self, "_channel", "") or "cli").strip() or "cli"
        current_chat_id = str(getattr(self, "_chat_id", "") or "direct").strip() or "direct"
        current_memory_channel = (
            str(getattr(self, "_memory_channel", "") or current_channel).strip() or current_channel
        )
        current_memory_chat_id = (
            str(getattr(self, "_memory_chat_id", "") or current_chat_id).strip() or current_chat_id
        )
        payload = live_context if isinstance(live_context, dict) else {}
        return {
            "channel": str(payload.get("channel") or current_channel).strip() or current_channel,
            "chat_id": str(payload.get("chat_id") or current_chat_id).strip() or current_chat_id,
            "memory_channel": str(payload.get("memory_channel") or current_memory_channel).strip()
            or current_memory_channel,
            "memory_chat_id": str(payload.get("memory_chat_id") or current_memory_chat_id).strip()
            or current_memory_chat_id,
        }

    def _apply_live_context(self, live_context: dict[str, str] | None) -> None:
        normalized = self._normalize_live_context(live_context)
        self._channel = normalized["channel"]
        self._chat_id = normalized["chat_id"]
        self._memory_channel = normalized["memory_channel"]
        self._memory_chat_id = normalized["memory_chat_id"]

    def _now(self) -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _history_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        parts.append(text)
                    continue
                if not isinstance(item, dict):
                    continue
                text = item.get("text", item.get("content", ""))
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts).strip()
        return str(content or "")

    @staticmethod
    def _normalize_web_uploads(uploads: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in list(uploads or []):
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("path") or "").strip()
            if not path:
                continue
            item = {
                "path": path,
                "name": str(raw.get("name") or "").strip() or path,
                "mime_type": str(raw.get("mime_type") or raw.get("mimeType") or "").strip(),
                "kind": str(raw.get("kind") or "").strip(),
            }
            size = raw.get("size")
            if isinstance(size, (int, float)):
                item["size"] = int(size)
            items.append(item)
        return items

    def _pending_user_message_snapshot(self) -> dict[str, Any] | None:
        prompt = self._last_prompt
        attachments: list[dict[str, Any]] = []
        timestamp: str | None = None
        if isinstance(prompt, UserInputMessage):
            metadata = dict(prompt.metadata or {})
            if bool(metadata.get('heartbeat_internal')):
                return None
            raw_text = metadata.get("web_ceo_raw_text")
            text = str(raw_text) if isinstance(raw_text, str) else self._history_text(prompt.content)
            attachments = self._normalize_web_uploads(metadata.get("web_ceo_uploads"))
            timestamp = prompt.timestamp
        else:
            text = self._history_text(prompt)
        if not text.strip() and not attachments:
            return None
        payload: dict[str, Any] = {"role": "user", "content": text}
        if attachments:
            payload["attachments"] = attachments
        if isinstance(timestamp, str) and timestamp.strip():
            payload["timestamp"] = timestamp.strip()
        return payload

    def _is_heartbeat_internal_prompt(self, prompt: Any | None = None) -> bool:
        current = self._last_prompt if prompt is None else prompt
        if not isinstance(current, UserInputMessage):
            return False
        metadata = dict(current.metadata or {})
        return bool(metadata.get("heartbeat_internal"))

    def _interaction_flow_snapshot(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in self._event_log:
            if not isinstance(raw, dict):
                continue
            event_type = str(raw.get("type") or "").strip()
            payload = raw.get("payload")
            event_payload = payload if isinstance(payload, dict) else {}
            event_data = event_payload.get("data") if isinstance(event_payload.get("data"), dict) else {}
            if event_type == "tool_execution_update" and bool(event_data.get("watchdog")):
                continue
            if event_type == "tool_execution_start":
                status = "running"
                is_update = False
            elif event_type == "tool_execution_update":
                status = "running"
                is_update = True
            elif event_type == "tool_execution_end":
                status = "error" if bool(event_payload.get("is_error")) else "success"
                is_update = False
            else:
                continue
            items.append(
                {
                    "status": status,
                    "tool_name": str(event_payload.get("tool_name") or event_data.get("tool_name") or "tool").strip() or "tool",
                    "text": str(event_payload.get("text") or "").strip(),
                    "timestamp": str(raw.get("timestamp") or "").strip(),
                    "tool_call_id": str(event_payload.get("tool_call_id") or event_data.get("tool_call_id") or "").strip(),
                    "is_error": bool(event_payload.get("is_error")),
                    "is_update": is_update,
                    "kind": str(event_payload.get("kind") or "").strip(),
                }
            )
        return items

    def _resolve_progress_tool_target(self, data: dict[str, Any]) -> tuple[str, str]:
        tool_name = str(data.get("tool_name") or "").strip()
        tool_call_id = str(data.get("tool_call_id") or "").strip()
        if tool_name and not tool_call_id:
            tool_call_id = self._pending_tool_names.get(tool_name, "")
        if not tool_name and len(self._pending_tool_names) == 1:
            tool_name, tool_call_id = next(iter(self._pending_tool_names.items()))
        return tool_name or "tool", tool_call_id

    def _remember_background_tool_target(self, *, execution_id: str, tool_name: str, tool_call_id: str) -> None:
        key = str(execution_id or "").strip()
        if not key:
            return
        self._background_tool_targets[key] = {
            "tool_name": str(tool_name or "tool").strip() or "tool",
            "tool_call_id": str(tool_call_id or "").strip(),
        }

    def _forget_background_tool_target(self, execution_id: str) -> None:
        self._background_tool_targets.pop(str(execution_id or "").strip(), None)

    def _resolve_control_tool_target(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        execution_id = str((payload or {}).get("execution_id") or "").strip()
        mapped = self._background_tool_targets.get(execution_id, {})
        target_tool_name = str(mapped.get("tool_name") or "").strip()
        target_tool_call_id = str(mapped.get("tool_call_id") or "").strip()
        if not target_tool_name:
            target_tool_name = str((payload or {}).get("tool_name") or "").strip()
        if not target_tool_call_id and target_tool_name:
            target_tool_call_id = str(self._pending_tool_names.get(target_tool_name) or "").strip()
        return (
            target_tool_name or str(tool_name or "tool").strip() or "tool",
            target_tool_call_id,
            execution_id,
        )

    def _current_inflight_turn_snapshot(self) -> dict[str, Any] | None:
        status = str(self._state.status or "").strip().lower()
        if not (self._state.is_running or status in {"paused", "error"}):
            return None
        snapshot: dict[str, Any] = {
            "status": status or ("running" if self._state.is_running else "idle"),
            "tool_events": self._interaction_flow_snapshot(),
        }
        prompt = self._last_prompt
        if isinstance(prompt, UserInputMessage):
            metadata = dict(prompt.metadata or {})
            if bool(metadata.get("heartbeat_internal")):
                snapshot["source"] = "heartbeat"
        user_message = self._pending_user_message_snapshot()
        if user_message is not None:
            snapshot["user_message"] = user_message
        if self._state.latest_message:
            snapshot["assistant_text"] = str(self._state.latest_message)
        if self._state.last_error is not None:
            snapshot["last_error"] = asdict(self._state.last_error)
        interaction_trace = self.interaction_trace_snapshot()
        if interaction_trace is not None:
            snapshot["interaction_trace"] = interaction_trace
        stage = self.current_stage_snapshot()
        if stage is not None:
            snapshot["stage"] = stage
        if (
            not snapshot["tool_events"]
            and "user_message" not in snapshot
            and "assistant_text" not in snapshot
            and "last_error" not in snapshot
            and "interaction_trace" not in snapshot
            and "stage" not in snapshot
        ):
            return None
        return snapshot

    def inflight_turn_snapshot(self) -> dict[str, Any] | None:
        snapshot = self._current_inflight_turn_snapshot()
        if snapshot is not None:
            source = str(snapshot.get("source") or "").strip().lower()
            if source == "heartbeat" and self._preserved_inflight_turn is not None:
                return copy.deepcopy(self._preserved_inflight_turn)
            return snapshot
        if self._preserved_inflight_turn is not None:
            return copy.deepcopy(self._preserved_inflight_turn)
        return None

    def clear_preserved_inflight_turn(self) -> None:
        if self._preserved_inflight_turn is None:
            return
        self._preserved_inflight_turn = None
        self._sync_persisted_inflight_turn()

    @staticmethod
    def _parse_progress_payload(content: Any) -> dict[str, Any] | None:
        if not isinstance(content, str):
            return None
        text = content.strip()
        if not text or text[:1] not in {"{", "["}:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _allocate_tool_call_id(self, tool_name: str) -> str:
        normalized = str(tool_name or "tool").strip() or "tool"
        self._tool_seq += 1
        call_id = f"{normalized}:{self._tool_seq}"
        self._pending_tool_names[normalized] = call_id
        return call_id

    def _pop_tool_call_id(self, tool_name: str) -> str:
        normalized = str(tool_name or "tool").strip() or "tool"
        return self._pending_tool_names.pop(normalized, f"{normalized}:{self._tool_seq + 1}")

    async def _emit(self, event_type: str, **payload):
        event = AgentEvent(type=event_type, timestamp=self._now(), payload=payload)
        self._state.event_count += 1
        self._event_log.append({"type": event.type, "timestamp": event.timestamp, "payload": dict(event.payload)})
        for listener in list(self._listeners):
            result = listener(event)
            if hasattr(result, "__await__"):
                await result
        return event

    def _sync_persisted_inflight_turn(self) -> None:
        session_key = str(self._state.session_key or "").strip()
        if not session_key.startswith("web:"):
            return
        try:
            from g3ku.runtime.web_ceo_sessions import write_inflight_turn_snapshot

            write_inflight_turn_snapshot(session_key, self.inflight_turn_snapshot())
        except Exception:
            logger.debug("Skipped persisted inflight turn sync for {}", session_key)

    async def _emit_state_snapshot(self):
        self._sync_persisted_inflight_turn()
        await self._emit("state_snapshot", state=self.state_dict())

    async def _handle_progress(
        self,
        content: str,
        *,
        tool_hint: bool = False,
        deep_progress: bool = False,
        event_kind: str | None = None,
        event_data=None,
    ) -> None:
        kind = event_kind or ("tool_plan" if tool_hint else "deep_progress" if deep_progress else "progress")
        data = event_data if isinstance(event_data, dict) else {}
        tool_name = str(data.get("tool_name") or "").strip() or "tool"

        if kind == "tool_start":
            if tool_name in _CONTROL_TOOL_NAMES:
                return
            call_id = self._allocate_tool_call_id(tool_name)
            self._state.pending_tool_calls.add(call_id)
            await self._emit(
                "tool_execution_start",
                tool_name=tool_name,
                tool_call_id=call_id,
                text=str(content or ""),
                kind=kind,
                data=data,
            )
            await self._emit_state_snapshot()
            return

        if kind == "tool_result":
            payload = self._parse_progress_payload(content)
            payload_status = str((payload or {}).get("status") or "").strip().lower()
            if payload_status == "background_running":
                if tool_name in _CONTROL_TOOL_NAMES:
                    resolved_tool_name, call_id, execution_id = self._resolve_control_tool_target(
                        tool_name=tool_name,
                        payload=payload,
                    )
                else:
                    resolved_tool_name, call_id = self._resolve_progress_tool_target(data)
                    execution_id = str((payload or {}).get("execution_id") or "").strip()
                if execution_id:
                    self._remember_background_tool_target(
                        execution_id=execution_id,
                        tool_name=resolved_tool_name,
                        tool_call_id=call_id,
                    )
                self._enqueue_background_tool_heartbeat(payload=payload, tool_name=resolved_tool_name)
                await self._emit(
                    "tool_execution_update",
                    kind="tool_background",
                    tool_name=resolved_tool_name,
                    tool_call_id=call_id,
                    text=str(content or ""),
                    data=data,
                )
                await self._emit_state_snapshot()
                return
            if tool_name in _CONTROL_TOOL_NAMES:
                resolved_tool_name, call_id, execution_id = self._resolve_control_tool_target(
                    tool_name=tool_name,
                    payload=payload,
                )
                if execution_id and payload_status in {"completed", "stopped", "failed", "error", "not_found", "unavailable"}:
                    self._forget_background_tool_target(execution_id)
                if call_id:
                    self._state.pending_tool_calls.discard(call_id)
                await self._emit(
                    "tool_execution_end",
                    tool_name=resolved_tool_name,
                    tool_call_id=call_id,
                    text=str(content or ""),
                    kind=kind,
                    is_error=payload_status in {"stopped", "failed", "error", "not_found", "unavailable"},
                    data=data,
                )
                await self._emit_state_snapshot()
                return
            call_id = self._pop_tool_call_id(tool_name)
            self._state.pending_tool_calls.discard(call_id)
            await self._emit(
                "tool_execution_end",
                tool_name=tool_name,
                tool_call_id=call_id,
                text=str(content or ""),
                kind=kind,
                is_error=False,
                data=data,
            )
            await self._emit_state_snapshot()
            return

        if kind == "tool_error":
            if tool_name in _CONTROL_TOOL_NAMES:
                payload = self._parse_progress_payload(content)
                resolved_tool_name, call_id, execution_id = self._resolve_control_tool_target(
                    tool_name=tool_name,
                    payload=payload,
                )
                if execution_id:
                    self._forget_background_tool_target(execution_id)
                if call_id:
                    self._state.pending_tool_calls.discard(call_id)
                error = StructuredError(
                    code="tool_error",
                    message=str(content or f"{resolved_tool_name} failed"),
                    recoverable=True,
                    source="tool",
                    details={"tool_name": resolved_tool_name, "tool_call_id": call_id, **data},
                )
                self._state.last_error = error
                await self._emit(
                    "tool_execution_end",
                    tool_name=resolved_tool_name,
                    tool_call_id=call_id,
                    text=error.message,
                    kind=kind,
                    is_error=True,
                    data=data,
                )
                await self._emit(
                    "error",
                    code=error.code,
                    message=error.message,
                    recoverable=error.recoverable,
                    source=error.source,
                    details=error.details,
                )
                await self._emit_state_snapshot()
                return
            call_id = self._pop_tool_call_id(tool_name)
            self._state.pending_tool_calls.discard(call_id)
            error = StructuredError(
                code="tool_error",
                message=str(content or f"{tool_name} failed"),
                recoverable=True,
                source="tool",
                details={"tool_name": tool_name, "tool_call_id": call_id, **data},
            )
            self._state.last_error = error
            await self._emit(
                "tool_execution_end",
                tool_name=tool_name,
                tool_call_id=call_id,
                text=error.message,
                kind=kind,
                is_error=True,
                data=data,
            )
            await self._emit(
                "error",
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
                source=error.source,
                details=error.details,
            )
            await self._emit_state_snapshot()
            return

        if kind in {"tool_plan", "browser_runtime_bootstrap", "browser_command_status", "tool"}:
            resolved_tool_name, call_id = self._resolve_progress_tool_target(data)
            await self._emit(
                "tool_execution_update",
                kind=kind,
                tool_name=resolved_tool_name,
                tool_call_id=call_id,
                text=str(content or ""),
                data=data,
            )
            return

        channel = "analysis" if kind == "analysis" else "deep_progress" if (deep_progress or kind == "deep_progress") else "progress"
        await self._emit(
            "message_delta",
            channel=channel,
            kind=kind,
            text=str(content or ""),
            data=data,
        )

    def _enqueue_background_tool_heartbeat(self, *, payload: dict[str, Any] | None, tool_name: str) -> None:
        heartbeat = getattr(self._loop, "web_session_heartbeat", None)
        if heartbeat is None or not hasattr(heartbeat, "enqueue_tool_background"):
            return
        session_key = str(self._state.session_key or "").strip()
        execution_id = str((payload or {}).get("execution_id") or "").strip()
        if not session_key or not execution_id:
            return
        handoff_payload = dict(payload or {})
        handoff_payload["tool_name"] = str(handoff_payload.get("tool_name") or tool_name or "tool").strip() or "tool"
        try:
            heartbeat.enqueue_tool_background(session_id=session_key, payload=handoff_payload)
        except Exception:
            logger.debug("Background tool heartbeat enqueue skipped for {}", session_key)

    async def _run_message(self, user_input: UserInputMessage) -> str:
        self._multi_agent_runner = getattr(self._loop, "multi_agent_runner", None)
        if self._multi_agent_runner is None:
            raise RuntimeError("Main frontdoor runtime is required but was not initialized.")
        return await self._multi_agent_runner.run_turn(
            user_input=user_input,
            session=self,
            on_progress=self._handle_progress,
        )

    async def prompt(
        self,
        message: str | UserInputMessage,
        *,
        persist_transcript: bool = True,
        live_context: dict[str, str] | None = None,
    ) -> RunResult:
        from g3ku.shells.web import refresh_web_agent_runtime

        async with self._turn_lock:
            self._apply_live_context(live_context)
            await refresh_web_agent_runtime(force=False, reason="prompt")
            user_input = message if isinstance(message, UserInputMessage) else UserInputMessage(content=str(message))
            heartbeat_internal = self._is_heartbeat_internal_prompt(user_input)
            if heartbeat_internal:
                current_snapshot = self._current_inflight_turn_snapshot()
                current_source = str((current_snapshot or {}).get("source") or "").strip().lower()
                if current_snapshot is not None and current_source != "heartbeat":
                    self._preserved_inflight_turn = copy.deepcopy(current_snapshot)
            else:
                self._preserved_inflight_turn = None
            cancel_token = self._loop.create_session_cancellation_token(self._state.session_key)
            self._active_cancel_token = cancel_token
            try:
                self._last_prompt = user_input
                self._event_log = []
                self._pending_tool_names.clear()
                self._state.is_running = True
                self._state.paused = False
                self._state.status = "running"
                self._state.latest_message = ""
                self._state.last_error = None
                self._state.pending_tool_calls.clear()
                if not heartbeat_internal:
                    self.clear_interaction_trace()

                await self._emit("agent_start", session_key=self._state.session_key, trigger="prompt")
                await self._emit("turn_start", session_key=self._state.session_key)
                await self._emit_state_snapshot()

                output = await self._run_message(user_input)
            except asyncio.CancelledError:
                already_paused = bool(self._state.paused) or str(self._state.status or "").strip().lower() == "paused"
                self._state.is_running = False
                self._state.paused = True
                self._state.status = "paused"
                if not already_paused:
                    await self._emit("control_ack", action="pause", accepted=True)
                await self._emit("agent_end", session_key=self._state.session_key, status="paused")
                if not already_paused:
                    await self._emit_state_snapshot()
                raise
            except Exception as exc:
                self._state.is_running = False
                self._state.status = "error"
                self._state.last_error = StructuredError(code="legacy_session_error", message=str(exc), recoverable=True)
                await self._emit(
                    "error",
                    code="legacy_session_error",
                    message=str(exc),
                    recoverable=True,
                    source="runtime",
                )
                await self._emit("agent_end", session_key=self._state.session_key, status="error")
                await self._emit_state_snapshot()
                raise
            else:
                assistant = AssistantMessage(content=output, timestamp=self._now())
                self._state.messages.append(assistant)
                self._state.latest_message = output
                self._state.is_running = False
                self._state.status = "completed"
                self._state.pending_tool_calls.clear()
                user_text = self._history_text(user_input.content)
                interaction_flow = self._interaction_flow_snapshot()
                interaction_trace = self.interaction_trace_snapshot()
                if getattr(self._loop, "prompt_trace", False):
                    logger.info(render_output_trace(output))
                persisted_session = None
                if persist_transcript:
                    try:
                        persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
                        persisted_session.add_message(
                            "user",
                            user_text,
                            attachments=list(user_input.attachments or []),
                            metadata=dict(user_input.metadata or {}),
                        )
                        assistant_payload: dict[str, Any] = {}
                        if interaction_flow:
                            assistant_payload["tool_events"] = interaction_flow
                        if interaction_trace is not None:
                            assistant_payload["interaction_trace"] = interaction_trace
                        persisted_session.add_message("assistant", output, **assistant_payload)
                        if self._state.session_key.startswith("web:"):
                            from g3ku.runtime.web_ceo_sessions import update_ceo_session_after_turn

                            update_ceo_session_after_turn(
                                persisted_session,
                                user_text=user_text,
                                assistant_text=output,
                                route_kind=str(getattr(self, "_last_route_kind", "") or ""),
                            )
                        self._loop.sessions.save(persisted_session)
                    except Exception:
                        await self._emit(
                            "message_delta",
                            channel="analysis",
                            kind="persistence_warning",
                            text="Session transcript persistence failed; response is still available in-memory.",
                        )
                    if getattr(self._loop, "memory_manager", None) is not None:
                        try:
                            await self._loop.memory_manager.ingest_turn(
                                session_key=self._state.session_key,
                                channel=self._memory_channel,
                                chat_id=self._memory_chat_id,
                                messages=[
                                    {"role": "user", "content": user_text},
                                    {"role": "assistant", "content": output},
                                ],
                            )
                        except Exception:
                            await self._emit(
                                "message_delta",
                                channel="analysis",
                                kind="persistence_warning",
                                text="Memory ingest failed; turn history is still available in session transcript.",
                            )
                    if persisted_session is not None and getattr(self._loop, "commit_service", None) is not None:
                        try:
                            artifact = await self._loop.commit_service.maybe_commit(
                                session=persisted_session,
                                channel=self._memory_channel,
                                chat_id=self._memory_chat_id,
                            )
                            if artifact is not None:
                                self._loop.sessions.save(persisted_session)
                        except Exception:
                            await self._emit(
                                "message_delta",
                                channel="analysis",
                                kind="persistence_warning",
                                text="Memory commit pipeline failed; new turns remain available in session transcript.",
                            )
                stage_snapshot = self.current_stage_snapshot()
                await self._emit(
                    "message_end",
                    role="assistant",
                    text=output,
                    heartbeat_internal=self._is_heartbeat_internal_prompt(user_input),
                    interaction_trace=interaction_trace,
                    stage=stage_snapshot,
                )
                self.clear_interaction_trace()
                await self._emit("turn_end", session_key=self._state.session_key, status="completed")
                await self._emit("agent_end", session_key=self._state.session_key, status="completed")
                await self._emit_state_snapshot()
                return RunResult(output=output, events=list(self._event_log))
            finally:
                if self._active_cancel_token is cancel_token:
                    self._active_cancel_token = None
                self._loop.release_session_cancellation_token(self._state.session_key, cancel_token)

    async def continue_(self, *, live_context: dict[str, str] | None = None) -> RunResult:
        return await self.prompt(self._last_prompt, live_context=live_context)

    def steer(self, message: str | UserInputMessage) -> None:
        content = message.content if isinstance(message, UserInputMessage) else str(message)
        self._state.queued_steering_messages.append(UserInputMessage(content=content))

    def follow_up(self, message: str | UserInputMessage) -> None:
        content = message.content if isinstance(message, UserInputMessage) else str(message)
        self._state.queued_follow_up_messages.append(UserInputMessage(content=content))

    async def pause(self) -> None:
        self._state.paused = True
        self._state.is_running = False
        self._state.status = "paused"
        await self._emit_safe_stop_notice("pause")
        if self._active_cancel_token is not None:
            self._active_cancel_token.cancel(reason="用户已请求暂停，正在安全停止...")
        await self._loop.cancel_session_tasks(self._state.session_key)
        await self._emit("control_ack", action="pause", accepted=True)
        await self._emit_state_snapshot()

    async def resume(self, *, replan: bool = False, additional_context: str | None = None) -> RunResult:
        self._state.paused = False
        self._state.status = "running"
        await self._emit("control_ack", action="resume", accepted=True, replan=replan)
        await self._emit_state_snapshot()
        if additional_context:
            return await self.prompt(additional_context)
        return RunResult(output="", events=list(self._event_log))

    async def cancel(self, *, reason: str = "user_cancelled") -> None:
        await self._emit_safe_stop_notice("cancel")
        if self._active_cancel_token is not None:
            self._active_cancel_token.cancel(reason=reason or "用户已请求停止，正在安全停止...")
        await self._loop.cancel_session_tasks(self._state.session_key)
        self._preserved_inflight_turn = None
        self.clear_interaction_trace()
        self._state.is_running = False
        self._state.paused = False
        self._state.status = "idle"
        self._state.pending_tool_calls.clear()
        await self._emit("control_ack", action="cancel", accepted=True, reason=reason)
        await self._emit_state_snapshot()

    async def _emit_safe_stop_notice(self, action: str) -> None:
        message = "用户已请求暂停，正在安全停止..." if action == "pause" else "用户已请求停止，正在安全停止..."
        if self._pending_tool_names:
            for tool_name, call_id in list(self._pending_tool_names.items()):
                await self._handle_progress(
                    message,
                    event_kind="tool",
                    event_data={"tool_name": tool_name, "tool_call_id": call_id},
                )
            return
        await self._emit(
            "message_delta",
            channel="progress",
            kind="progress",
            text=message,
            data={"action": action},
        )

    def set_model(self, model: str) -> None:
        self._state.model = model

    def set_reasoning_effort(self, level: str | None) -> None:
        self._state.reasoning_effort = level

