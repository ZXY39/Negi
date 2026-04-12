from __future__ import annotations

import asyncio
from typing import Any

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.session_agent import RuntimeAgentSession


class SessionRuntimeManager:
    """Cache and route AgentSession instances for channels, web, and background jobs."""

    def __init__(self, loop):
        self._loop = loop
        self._sessions: dict[str, RuntimeAgentSession] = {}
        self._session_cls: type[RuntimeAgentSession] | None = None
        self._meta: dict[str, tuple[str, str]] = {}

    @property
    def loop(self):
        return self._loop

    def _resolve_session_cls(self) -> type[RuntimeAgentSession]:
        if self._session_cls is not None:
            return self._session_cls
        return RuntimeAgentSession

    def get_or_create(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        memory_channel: str | None = None,
        memory_chat_id: str | None = None,
    ) -> RuntimeAgentSession:
        key = str(session_key or "").strip() or f"{channel}:{chat_id}"
        channel_value = str(channel or "cli")
        chat_value = str(chat_id or "direct")
        session = self._sessions.get(key)
        if session is None:
            session_cls = self._resolve_session_cls()
            session = session_cls(
                self._loop,
                session_key=key,
                channel=channel_value,
                chat_id=chat_value,
                memory_channel=memory_channel,
                memory_chat_id=memory_chat_id,
            )
            self._sessions[key] = session
        else:
            if memory_channel:
                setattr(session, "_memory_channel", str(memory_channel or "").strip() or channel_value)
            if memory_chat_id:
                setattr(session, "_memory_chat_id", str(memory_chat_id or "").strip() or chat_value)
        self._meta[key] = (channel_value, chat_value)
        return session

    def bind_live_context(
        self,
        session: RuntimeAgentSession,
        *,
        channel: str,
        chat_id: str,
        memory_channel: str | None = None,
        memory_chat_id: str | None = None,
    ) -> dict[str, str]:
        channel_value = str(channel or "cli").strip() or "cli"
        chat_value = str(chat_id or "direct").strip() or "direct"
        memory_channel_value = str(memory_channel or channel_value).strip() or channel_value
        memory_chat_value = str(memory_chat_id or chat_value).strip() or chat_value
        key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        if key:
            self._meta[key] = (channel_value, chat_value)
        return {
            "channel": channel_value,
            "chat_id": chat_value,
            "memory_channel": memory_channel_value,
            "memory_chat_id": memory_chat_value,
        }

    async def prompt(
        self,
        message: str | UserInputMessage,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        memory_channel: str | None = None,
        memory_chat_id: str | None = None,
        runtime_channel: str | None = None,
        runtime_chat_id: str | None = None,
        runtime_memory_channel: str | None = None,
        runtime_memory_chat_id: str | None = None,
        persist_transcript: bool = True,
    ) -> Any:
        session = self.get_or_create(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            memory_channel=memory_channel,
            memory_chat_id=memory_chat_id,
        )
        live_context = self.bind_live_context(
            session,
            channel=runtime_channel or channel,
            chat_id=runtime_chat_id or chat_id,
            memory_channel=runtime_memory_channel or memory_channel,
            memory_chat_id=runtime_memory_chat_id or memory_chat_id,
        )
        return await session.prompt(
            message,
            persist_transcript=persist_transcript,
            live_context=live_context,
        )

    async def continue_(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        memory_channel: str | None = None,
        memory_chat_id: str | None = None,
        runtime_channel: str | None = None,
        runtime_chat_id: str | None = None,
        runtime_memory_channel: str | None = None,
        runtime_memory_chat_id: str | None = None,
    ) -> Any:
        session = self.get_or_create(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            memory_channel=memory_channel,
            memory_chat_id=memory_chat_id,
        )
        live_context = self.bind_live_context(
            session,
            channel=runtime_channel or channel,
            chat_id=runtime_chat_id or chat_id,
            memory_channel=runtime_memory_channel or memory_channel,
            memory_chat_id=runtime_memory_chat_id or memory_chat_id,
        )
        return await session.continue_(live_context=live_context)

    async def cancel(self, session_key: str, *, reason: str = "user_cancelled") -> int:
        key = str(session_key or "").strip()
        session = self._sessions.get(key)
        if session is not None:
            await session.cancel(reason=reason)
        return await self._loop.cancel_session_tasks(key)

    async def pause(self, session_key: str, *, manual: bool = False) -> int:
        key = str(session_key or "").strip()
        session = self._sessions.get(key)
        if session is None:
            return 0
        await session.pause(manual=manual)
        return 1

    def session_meta(self, session_key: str) -> tuple[str, str] | None:
        return self._meta.get(str(session_key or "").strip())

    def get(self, session_key: str) -> RuntimeAgentSession | None:
        return self._sessions.get(str(session_key or "").strip())

    def remove(self, session_key: str) -> RuntimeAgentSession | None:
        key = str(session_key or "").strip()
        self._meta.pop(key, None)
        return self._sessions.pop(key, None)

    def list_sessions(self) -> list[str]:
        return sorted(self._sessions.keys())

