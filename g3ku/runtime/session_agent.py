from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from g3ku.prompt_trace import render_output_trace
from g3ku.core.events import AgentEvent
from g3ku.core.messages import AssistantMessage, UserInputMessage
from g3ku.core.results import RunResult
from g3ku.core.state import AgentState, StructuredError


class RuntimeAgentSession:
    """Primary AgentSession implementation backed by the runtime engine."""

    def __init__(self, loop, *, session_key: str, channel: str, chat_id: str):
        self._loop = loop
        self._channel = channel
        self._chat_id = chat_id
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
        self._tool_seq: int = 0

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
        return data

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

    async def _emit_state_snapshot(self):
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

        if kind in {"tool_plan", "browser_runtime_bootstrap", "browser_command_status"}:
            await self._emit(
                "tool_execution_update",
                kind=kind,
                tool_name=data.get("tool_name"),
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

    async def _run_message(self, user_input: UserInputMessage) -> str:
        self._multi_agent_runner = getattr(self._loop, "multi_agent_runner", None)
        if self._multi_agent_runner is None:
            raise RuntimeError("Main frontdoor runtime is required but was not initialized.")
        return await self._multi_agent_runner.run_turn(
            user_input=user_input,
            session=self,
            on_progress=self._handle_progress,
        )

    async def prompt(self, message: str | UserInputMessage) -> RunResult:
        from g3ku.shells.web import refresh_web_agent_runtime

        await refresh_web_agent_runtime(force=False, reason="prompt")
        user_input = message if isinstance(message, UserInputMessage) else UserInputMessage(content=str(message))
        self._last_prompt = user_input
        self._event_log = []
        self._pending_tool_names.clear()
        self._state.is_running = True
        self._state.paused = False
        self._state.status = "running"
        self._state.latest_message = ""
        self._state.last_error = None
        self._state.pending_tool_calls.clear()

        await self._emit("agent_start", session_key=self._state.session_key, trigger="prompt")
        await self._emit("turn_start", session_key=self._state.session_key)
        await self._emit_state_snapshot()

        try:
            output = await self._run_message(user_input)
        except asyncio.CancelledError:
            self._state.is_running = False
            self._state.paused = True
            self._state.status = "paused"
            await self._emit("control_ack", action="pause", accepted=True)
            await self._emit("agent_end", session_key=self._state.session_key, status="paused")
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

        assistant = AssistantMessage(content=output, timestamp=self._now())
        self._state.messages.append(assistant)
        self._state.latest_message = output
        self._state.is_running = False
        self._state.status = "completed"
        self._state.pending_tool_calls.clear()
        user_text = self._history_text(user_input.content)
        if getattr(self._loop, "prompt_trace", False):
            logger.info(render_output_trace(output))
        persisted_session = None
        try:
            persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
            persisted_session.add_message(
                "user",
                user_text,
                attachments=list(user_input.attachments or []),
                metadata=dict(user_input.metadata or {}),
            )
            persisted_session.add_message("assistant", output)
            self._loop.sessions.save(persisted_session)
        except Exception:
            await self._emit(
                "message_delta",
                channel="analysis",
                kind="persistence_warning",
                text="Session transcript persistence failed; response is still available in-memory.",
            )
        if getattr(self._loop, "memory_manager", None) is not None and self._loop._use_rag_memory():
            try:
                await self._loop.memory_manager.ingest_turn(
                    session_key=self._state.session_key,
                    channel=self._channel,
                    chat_id=self._chat_id,
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
                    text="RAG memory ingest failed; turn history is still available in session transcript.",
                )
        if persisted_session is not None and getattr(self._loop, "commit_service", None) is not None:
            try:
                artifact = await self._loop.commit_service.maybe_commit(
                    session=persisted_session,
                    channel=self._channel,
                    chat_id=self._chat_id,
                )
                if artifact is not None:
                    self._loop.sessions.save(persisted_session)
            except Exception:
                await self._emit(
                    "message_delta",
                    channel="analysis",
                    kind="persistence_warning",
                    text="RAG memory commit pipeline failed; new turns remain available in session transcript.",
                )
        await self._emit("message_end", role="assistant", text=output)
        await self._emit("turn_end", session_key=self._state.session_key, status="completed")
        await self._emit("agent_end", session_key=self._state.session_key, status="completed")
        await self._emit_state_snapshot()
        return RunResult(output=output, events=list(self._event_log))

    async def continue_(self) -> RunResult:
        return await self.prompt(self._last_prompt)

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
        await self._loop.cancel_session_tasks(self._state.session_key)
        self._state.is_running = False
        self._state.paused = False
        self._state.status = "idle"
        self._state.pending_tool_calls.clear()
        await self._emit("control_ack", action="cancel", accepted=True, reason=reason)
        await self._emit_state_snapshot()

    def set_model(self, model: str) -> None:
        self._state.model = model

    def set_reasoning_effort(self, level: str | None) -> None:
        self._state.reasoning_effort = level

