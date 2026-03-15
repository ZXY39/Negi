from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.core.results import RunResult
from g3ku.runtime.manager import SessionRuntimeManager
from g3ku.runtime.session_agent import RuntimeAgentSession

EventListener = Callable[[AgentEvent], Awaitable[None] | None]
TaskRegistrar = Callable[[str, asyncio.Task[Any]], None]


@dataclass(slots=True)
class SessionSubscription:
    session: RuntimeAgentSession
    unsubscribe: Callable[[], None]


class SessionRuntimeBridge:
    """Shared session glue for web, CLI, gateway, and background services."""

    def __init__(self, manager: SessionRuntimeManager):
        self._manager = manager

    def get_session(self, *, session_key: str, channel: str, chat_id: str) -> RuntimeAgentSession:
        return self._manager.get_or_create(session_key=session_key, channel=channel, chat_id=chat_id)

    def subscribe(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        listener: EventListener,
    ) -> SessionSubscription:
        session = self.get_session(session_key=session_key, channel=channel, chat_id=chat_id)
        unsubscribe = session.subscribe(listener)
        return SessionSubscription(session=session, unsubscribe=unsubscribe)

    async def prompt(
        self,
        message: str | UserInputMessage,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        listeners: Iterable[EventListener] | None = None,
        register_task: TaskRegistrar | None = None,
    ) -> RunResult:
        session = self.get_session(session_key=session_key, channel=channel, chat_id=chat_id)
        unsubscribers = self._subscribe_many(session, listeners)
        task = asyncio.create_task(session.prompt(message))
        if register_task is not None:
            active_session_key = getattr(getattr(session, "state", None), "session_key", None) or str(session_key or "").strip()
            register_task(active_session_key, task)
        try:
            return await task
        finally:
            self._unsubscribe_all(unsubscribers)

    async def continue_(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        listeners: Iterable[EventListener] | None = None,
        register_task: TaskRegistrar | None = None,
    ) -> RunResult:
        session = self.get_session(session_key=session_key, channel=channel, chat_id=chat_id)
        unsubscribers = self._subscribe_many(session, listeners)
        task = asyncio.create_task(session.continue_())
        if register_task is not None:
            active_session_key = getattr(getattr(session, "state", None), "session_key", None) or str(session_key or "").strip()
            register_task(active_session_key, task)
        try:
            return await task
        finally:
            self._unsubscribe_all(unsubscribers)

    async def cancel(self, session_key: str, *, reason: str = "user_cancelled") -> int:
        return await self._manager.cancel(session_key, reason=reason)

    @staticmethod
    def _subscribe_many(
        session: RuntimeAgentSession,
        listeners: Iterable[EventListener] | None,
    ) -> list[Callable[[], None]]:
        unsubscribers: list[Callable[[], None]] = []
        for listener in listeners or ():
            unsubscribers.append(session.subscribe(listener))
        return unsubscribers

    @staticmethod
    def _unsubscribe_all(unsubscribers: Iterable[Callable[[], None]]) -> None:
        for unsubscribe in reversed(list(unsubscribers)):
            unsubscribe()


def build_state_snapshot(
    session: RuntimeAgentSession,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    protocol_version: int = 1,
) -> dict[str, Any]:
    state = dict(session.state_dict())
    state.setdefault("session_id", session_id or state.get("session_key") or session.state.session_key)
    if run_id is not None:
        state["run_id"] = run_id
    if turn_id is not None:
        state["turn_id"] = turn_id
    state["protocol_version"] = protocol_version
    state["updated_at"] = datetime.now().isoformat()
    return state


def build_structured_event(
    event: AgentEvent,
    *,
    session_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
) -> dict[str, Any]:
    return {
        "type": event.type,
        "session_id": session_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "seq": seq,
        "timestamp": event.timestamp or datetime.now().isoformat(),
        "payload": dict(event.payload or {}),
    }


def cli_event_text(event: AgentEvent) -> tuple[str | None, str | None]:
    payload = dict(getattr(event, "payload", {}) or {})
    event_type = str(getattr(event, "type", "") or "")
    if event_type == "message_delta":
        kind = str(payload.get("kind") or payload.get("channel") or "progress")
        return kind, str(payload.get("text") or "")
    if event_type == "tool_execution_start":
        return "tool", str(payload.get("text") or f"{payload.get('tool_name') or 'tool'} started")
    if event_type == "tool_execution_update":
        return str(payload.get("kind") or "tool_plan"), str(payload.get("text") or "")
    if event_type == "tool_execution_end":
        return "tool_error" if payload.get("is_error") else "tool_result", str(payload.get("text") or "")
    if event_type == "control_ack":
        return "control", str(payload.get("text") or payload.get("action") or "control")
    if event_type == "error":
        return "tool_error", str(payload.get("message") or "Unknown error")
    return None, None

