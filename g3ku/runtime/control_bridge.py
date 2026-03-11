from __future__ import annotations

import asyncio

from loguru import logger

from g3ku.bus.events import OutboundMessage
from g3ku.session.manager import Session


class SessionControlBridge:
    """System-message and session-control command bridge."""

    HELP_TEXT = (
        "g3ku commands:\n"
        "/new - Start a new conversation\n"
        "/stop - Stop the current task\n"
        "/help - Show available commands"
    )

    def __init__(self, loop):
        self._loop = loop

    async def handle_system_message(self, msg) -> OutboundMessage:
        channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id))
        logger.info("Processing system message from {}", msg.sender_id)
        key = f"{channel}:{chat_id}"
        session = self._loop.sessions.get_or_create(key)
        self._loop._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
        ensure_checkpointer = getattr(self._loop, "_ensure_checkpointer_ready", None)
        if callable(ensure_checkpointer):
            await ensure_checkpointer()
        history_messages = [] if self._loop._checkpointer_enabled else session.get_history_messages(max_messages=self._loop.memory_window)
        messages = self._loop._transform_context(
            history_messages=history_messages,
            current_message=msg.content,
            channel=channel,
            chat_id=chat_id,
            include_legacy_memory=self._loop._use_legacy_memory(),
            temp_dir=str(self._loop.temp_dir),
        )
        final_content, _, all_msgs = await self._loop._run_agent_loop(
            messages,
            session_key=key,
            channel=channel,
            chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
        )
        if not self._loop._checkpointer_enabled:
            self._loop._save_turn(session, all_msgs, 1 + len(history_messages))
            self._loop.sessions.save(session)
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=final_content or "Background task completed.",
        )

    async def handle_command(self, *, msg, session: Session) -> OutboundMessage | None:
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            return await self._handle_new_command(msg=msg, session=session)
        if cmd == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self.HELP_TEXT,
            )
        return None

    def schedule_legacy_consolidation_if_needed(self, *, session: Session) -> None:
        unconsolidated = len(session.messages) - session.last_consolidated
        if not (
            self._loop._use_legacy_memory()
            and unconsolidated >= self._loop.memory_window
            and session.key not in self._loop._consolidating
        ):
            return

        self._loop._consolidating.add(session.key)
        lock = self._loop._consolidation_locks.setdefault(session.key, asyncio.Lock())

        async def _consolidate_and_unlock():
            try:
                async with lock:
                    baseline_last = session.last_consolidated
                    result = await self._loop._consolidate_memory(session)
                    if result is not False and session.last_consolidated == baseline_last:
                        keep_count = self._loop.memory_window // 2
                        if len(session.messages) > keep_count:
                            session.last_consolidated = len(session.messages) - keep_count
            finally:
                self._loop._consolidating.discard(session.key)
                task = asyncio.current_task()
                if task is not None:
                    self._loop._consolidation_tasks.discard(task)

        task = asyncio.create_task(_consolidate_and_unlock())
        self._loop._consolidation_tasks.add(task)

    async def _handle_new_command(self, *, msg, session: Session) -> OutboundMessage:
        if self._loop._use_legacy_memory():
            lock = self._loop._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._loop._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._loop._consolidate_memory(temp, archive_all=True):
                            return OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._loop._consolidating.discard(session.key)

        if self._loop.commit_service is not None:
            try:
                await self._loop.commit_service.commit_for_new_session(
                    session=session,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                )
                self._loop.sessions.save(session)
            except Exception:
                logger.exception("Commit pipeline failed on /new for {}", session.key)

        if self._loop._checkpointer is not None and hasattr(self._loop._checkpointer, "adelete_thread"):
            try:
                await self._loop._checkpointer.adelete_thread(session.key)
            except Exception:
                logger.exception("Failed to clear checkpoint thread {}", session.key)
        elif self._loop._checkpointer is not None and hasattr(self._loop._checkpointer, "delete_thread"):
            try:
                await asyncio.to_thread(self._loop._checkpointer.delete_thread, session.key)
            except Exception:
                logger.exception("Failed to clear checkpoint thread {}", session.key)

        session.clear()
        self._loop.sessions.save(session)
        self._loop.sessions.invalidate(session.key)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="New session started.")

