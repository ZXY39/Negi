"""Base channel interface for chat platforms."""

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from loguru import logger

from g3ku.bus.events import InboundMessage, OutboundMessage
from g3ku.bus.queue import MessageBus


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the g3ku message bus.
    """

    name: str = "base"

    def __init__(self, config: Any, bus: MessageBus):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.bus = bus
        self._running = False
        self._inbound_handler: Callable[[InboundMessage], Awaitable[None]] | None = None

    def set_inbound_handler(self, handler: Callable[[InboundMessage], Awaitable[None]] | None) -> None:
        """Attach an explicit inbound handler instead of the legacy bus queue path."""
        self._inbound_handler = handler

    @abstractmethod
    async def start(self) -> None:
        """
        Start the channel and begin listening for messages.

        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.

        Args:
            msg: The message to send.
        """
        pass

    def is_allowed(self, sender_id: str) -> bool:
        """Check if *sender_id* is permitted.  Empty list 鈫?deny all; ``"*"`` 鈫?allow all."""
        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            logger.warning("{}: allow_from is empty 鈥?all access denied", self.name)
            return False
        if "*" in allow_list:
            return True
        sender_str = str(sender_id)
        return sender_str in allow_list or any(
            p in allow_list for p in sender_str.split("|") if p
        )

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions and forwards to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            metadata: Optional channel-specific metadata.
            session_key: Optional session key override (e.g. thread-scoped sessions).
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                "Access denied for sender {} on channel {}. "
                "Add them to allowFrom list in config to grant access.",
                sender_id, self.name,
            )
            return

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=metadata or {},
            session_key_override=session_key,
        )

        if callable(self._inbound_handler):
            await self._inbound_handler(msg)
            return

        await self.bus.publish_inbound(msg)

    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running

