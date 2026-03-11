"""Legacy direct-turn execution helpers kept for one-release compatibility."""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

from g3ku.bus.events import InboundMessage
from g3ku.runtime.turns import RunTurnRequest, RunTurnResult

if TYPE_CHECKING:
    from g3ku.core.messages import UserInputMessage
    from g3ku.runtime.engine import AgentRuntimeEngine


async def run_turn(
    engine: AgentRuntimeEngine,
    *,
    user_input: UserInputMessage,
    session_key: str,
    channel: str,
    chat_id: str,
    on_progress: Callable[..., Awaitable[None]] | None = None,
) -> RunTurnResult:
    """Run the legacy direct-turn compatibility path via the runtime engine."""
    request = RunTurnRequest(
        user_input=user_input,
        session_key=session_key,
        channel=channel,
        chat_id=chat_id,
        on_progress=on_progress,
    )
    await engine._connect_mcp()
    if request.user_input.attachments or request.user_input.metadata:
        inbound = InboundMessage(
            channel=request.channel,
            sender_id="user",
            chat_id=request.chat_id,
            content=request.user_input.content,
            media=list(request.user_input.attachments),
            metadata=dict(request.user_input.metadata),
        )
        response = await engine._process_message(
            inbound,
            session_key=request.session_key,
            on_progress=request.on_progress,
        )
        return RunTurnResult(output=response.content if response else "", response=response)

    output = await engine.process_direct(
        content=request.user_input.content,
        session_key=request.session_key,
        channel=request.channel,
        chat_id=request.chat_id,
        on_progress=request.on_progress,
    )
    return RunTurnResult(output=output)


async def process_direct(
    engine: AgentRuntimeEngine,
    content: str,
    session_key: str = "cli:direct",
    channel: str = "cli",
    chat_id: str = "direct",
    on_progress: Callable[..., Awaitable[None]] | None = None,
) -> str:
    """Process a message through the legacy direct runtime path."""
    await engine._connect_mcp()
    msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
    response = await engine._process_message(msg, session_key=session_key, on_progress=on_progress)
    return response.content if response else ""


__all__ = ["process_direct", "run_turn"]
