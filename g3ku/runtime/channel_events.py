from __future__ import annotations

from typing import Any, Awaitable, Callable

from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.core.events import AgentEvent
from g3ku.runtime.bridge import build_state_snapshot, build_structured_event, cli_event_text


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
    event_type = str(getattr(event, "type", "") or "")
    if event_type in {"state_snapshot", "agent_start", "agent_end", "turn_start", "turn_end", "message_start"}:
        return None
    if event_type == "message_end" and str((event.payload or {}).get("role") or "") == "assistant":
        return None

    kind, text = cli_event_text(event)
    if not text:
        return None

    metadata = dict(base_metadata or {})
    metadata.update(
        {
            "_session_event": True,
            "_progress": True,
            "_tool_hint": kind == "tool_plan",
            "_progress_kind": kind or "progress",
            "_agent_event": build_structured_event(
                event,
                session_id=str(getattr(getattr(session, "state", None), "session_key", "") or ""),
                run_id=run_id,
                turn_id=turn_id,
                seq=seq,
            ),
            "_state_snapshot": build_state_snapshot(
                session,
                session_id=str(getattr(getattr(session, "state", None), "session_key", "") or ""),
                run_id=run_id,
                turn_id=turn_id,
            ),
        }
    )
    return OutboundMessage(
        channel=channel,
        chat_id=chat_id,
        content=text,
        metadata=metadata,
    )


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

