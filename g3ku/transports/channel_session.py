"""Transport adapter that routes channel inbound messages into SessionRuntimeBridge."""

from __future__ import annotations

import asyncio
import uuid
from typing import Callable

from loguru import logger

from g3ku.bus.events import InboundMessage, OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.core.messages import UserInputMessage
from g3ku.runtime.bridge import SessionRuntimeBridge
from g3ku.runtime.channel_events import make_channel_event_listener


class ChannelSessionTransport:
    """Bridge channel inbound traffic directly into the session runtime."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        runtime_bridge: SessionRuntimeBridge,
        register_task: Callable[[str | None, asyncio.Task], None] | None = None,
    ):
        self.bus = bus
        self.runtime_bridge = runtime_bridge
        self._register_task = register_task

    async def handle_inbound(self, msg: InboundMessage) -> None:
        task = asyncio.create_task(self._process_message(msg))
        if callable(self._register_task):
            self._register_task(msg.session_key, task)
        else:
            task.add_done_callback(self._log_task_error)

    async def _process_message(self, msg: InboundMessage) -> None:
        try:
            if msg.content.strip().lower() == "/stop":
                total = await self.runtime_bridge.cancel(msg.session_key, reason="user_stop")
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Stopped {total} task(s)." if total else "No active task to stop.",
                    )
                )
                return

            user_message: str | UserInputMessage = msg.content
            if msg.metadata or msg.media:
                user_message = UserInputMessage(
                    content=msg.content,
                    attachments=list(msg.media or []),
                    metadata=dict(msg.metadata or {}),
                )

            run_id = uuid.uuid4().hex
            turn_id = uuid.uuid4().hex[:12]
            session = self.runtime_bridge.get_session(
                session_key=msg.session_key,
                channel=msg.channel,
                chat_id=msg.chat_id,
            )
            event_listener = make_channel_event_listener(
                bus=self.bus,
                session=session,
                channel=msg.channel,
                chat_id=msg.chat_id,
                run_id=run_id,
                turn_id=turn_id,
                base_metadata=dict(msg.metadata or {}),
            )
            result = await self.runtime_bridge.prompt(
                user_message,
                session_key=msg.session_key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                listeners=[event_listener],
                register_task=self._register_task if callable(self._register_task) else None,
            )
            if result.output:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=str(result.output),
                        metadata=msg.metadata or {},
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error processing channel message for {}", msg.session_key)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                )
            )

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Unhandled channel transport task failure")

