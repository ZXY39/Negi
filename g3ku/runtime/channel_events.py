from __future__ import annotations

from typing import Any, Awaitable, Callable

from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.core.events import AgentEvent


async def publish_channel_event(
    *,
    bus: MessageBus,
    event: AgentEvent,
    session,
    channel: str,
    chat_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
    base_metadata: dict[str, Any] | None = None,
) -> OutboundMessage | None:
    outbound = build_channel_outbound_message(
        event=event,
        session=session,
        channel=channel,
        chat_id=chat_id,
        run_id=run_id,
        turn_id=turn_id,
        seq=seq,
        base_metadata=base_metadata,
    )
    if outbound is not None:
        await bus.publish_outbound(outbound)
    return outbound


def build_channel_outbound_message(
    *,
    event: AgentEvent,
    session,
    channel: str,
    chat_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
    base_metadata: dict[str, Any] | None = None,
) -> OutboundMessage | None:
    _ = event, session, channel, chat_id, run_id, turn_id, seq, base_metadata
    return None


def make_channel_event_listener(
    *,
    bus: MessageBus,
    session,
    channel: str,
    chat_id: str,
    run_id: str,
    turn_id: str,
    base_metadata: dict[str, Any] | None = None,
) -> Callable[[AgentEvent], Awaitable[None]]:
    seq = 0

    async def _listener(event: AgentEvent) -> None:
        nonlocal seq
        seq += 1
        await publish_channel_event(
            bus=bus,
            event=event,
            session=session,
            channel=channel,
            chat_id=chat_id,
            run_id=run_id,
            turn_id=turn_id,
            seq=seq,
            base_metadata=base_metadata,
        )

    return _listener

