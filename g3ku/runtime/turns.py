from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from g3ku.bus.events import OutboundMessage
from g3ku.core.messages import UserInputMessage


@dataclass(slots=True)
class RunTurnRequest:
    user_input: UserInputMessage
    session_key: str
    channel: str
    chat_id: str
    on_progress: Callable[..., Awaitable[None]] | None = None


@dataclass(slots=True)
class RunTurnResult:
    output: str
    response: OutboundMessage | None = None

