from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus


class OutboundRouter:
    def __init__(self, *, bus: MessageBus, legacy_manager: Any, china_transport: Any | None = None):
        self.bus = bus
        self.legacy_manager = legacy_manager
        self.china_transport = china_transport
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        logger.info("Outbound router started")
        while True:
            try:
                msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)
                await self._dispatch(msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _dispatch(self, msg: OutboundMessage) -> None:
        if msg.metadata.get("_progress"):
            if msg.metadata.get("_tool_hint") and not self.legacy_manager.config.channels.send_tool_hints:
                return
            if not msg.metadata.get("_tool_hint") and not self.legacy_manager.config.channels.send_progress:
                return

        if self.china_transport is not None and msg.channel in {"qqbot", "dingtalk", "wecom", "wecom-app", "feishu-china"}:
            if hasattr(self.china_transport, "send_outbound"):
                await self.china_transport.send_outbound(msg)
                return

        channel = self.legacy_manager.get_channel(msg.channel)
        if channel is None:
            logger.warning("Unknown channel: {}", msg.channel)
            return
        try:
            await channel.send(msg)
        except Exception as exc:
            logger.error("Error sending to {}: {}", msg.channel, exc)
