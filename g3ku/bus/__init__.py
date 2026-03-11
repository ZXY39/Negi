"""Message bus module for decoupled channel-agent communication."""

from g3ku.bus.events import InboundMessage, OutboundMessage
from g3ku.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]

