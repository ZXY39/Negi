from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from g3ku.agent.context import ContextBuilder
from g3ku.bus.events import OutboundMessage


class TurnLifecycleBridge:
    """Turn-level progress, persistence, ingest, and commit lifecycle glue."""

    def __init__(self, loop):
        self._loop = loop

    def build_bus_progress_callback(self, *, msg, session_key: str):
        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            deep_progress: bool = False,
            event_kind: str | None = None,
            event_data: dict[str, Any] | None = None,
        ) -> None:
            if self._loop.debug_trace:
                logger.info(
                    "[debug:pipeline:progress] session={} tool_hint={} deep_progress={} content={}",
                    session_key,
                    tool_hint,
                    deep_progress,
                    self._loop._preview(content, max_chars=1200),
                )
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            meta["_deep_progress"] = deep_progress
            if event_kind:
                meta["_event_kind"] = event_kind
            if event_data is not None:
                meta["_event_data"] = event_data
            await self._loop.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _bus_progress

    def persist_turn(
        self,
        *,
        session,
        all_messages: list[dict[str, Any]],
        history_count: int,
        msg,
        final_content: str,
    ) -> None:
        user_content_override = None
        if msg.metadata and (
            msg.metadata.get("uploaded_placeholders") or msg.metadata.get("interleaved_content")
        ):
            user_content_override = msg.content

        if not self._loop._checkpointer_enabled:
            self._loop._save_turn(
                session,
                all_messages,
                1 + history_count,
                user_content_override=user_content_override,
            )
            self._loop.sessions.save(session)
            return

        self._loop._save_checkpoint_turn_snapshot(
            session,
            user_content=msg.content,
            assistant_content=final_content,
        )
        self._loop.sessions.save(session)

    async def ingest_rag_memory(
        self,
        *,
        session_key: str,
        msg,
        all_messages: list[dict[str, Any]],
        history_count: int,
    ) -> None:
        if not (self._loop.memory_manager and self._loop._use_rag_memory()):
            return
        ingest_slice = all_messages[(1 + history_count) :] if all_messages else []
        sanitized_ingest_slice = [
            message
            for message in ingest_slice
            if not (
                message.get("role") == "user"
                and isinstance(message.get("content"), str)
                and message.get("content", "").startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            )
        ]
        try:
            await self._loop.memory_manager.ingest_turn(
                session_key=session_key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                messages=sanitized_ingest_slice,
            )
        except Exception:
            logger.exception("RAG memory ingest failed")

    def schedule_background_commit(self, *, session, channel: str, chat_id: str) -> None:
        if self._loop.commit_service is None:
            return

        async def _commit_background() -> None:
            try:
                artifact = await self._loop.commit_service.maybe_commit(
                    session=session,
                    channel=channel,
                    chat_id=chat_id,
                )
                if artifact is not None:
                    self._loop.sessions.save(session)
            except Exception:
                logger.exception("Background commit pipeline failed for {}", session.key)
            finally:
                task = asyncio.current_task()
                if task is not None:
                    self._loop._commit_tasks.discard(task)

        task = asyncio.create_task(_commit_background())
        self._loop._commit_tasks.add(task)

