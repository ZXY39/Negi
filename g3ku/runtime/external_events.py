"""Per-session event hub for the External Agent API.

One hub per external session: a bounded replay buffer (monotonic ``seq``) plus
live fan-out to SSE subscribers. Producers are the turn executor
(``g3ku.runtime.api.external_turns``), the outbound bus drain branch, and the
heartbeat reply notifier. Consumers are ``GET /api/v1/sessions/{id}/events``
streams.

Event shape: ``{type, seq, ts, turn_id?, ...payload}``. The AgentEvent →
external event mapping lives in ``make_session_event_relay`` and mirrors the
legacy China transport semantics (progress lines via ``cli_event_text``,
authoritative final text on ``message_end``).

Lives outside ``g3ku/runtime/api`` on purpose: ``g3ku/shells/web.py``
publishes outbound events here, and importing the ``g3ku.runtime.api``
package would re-enter the web shell through ``ceo_sessions`` (circular
import). The ``ceo_media`` rewrite is imported lazily for the same reason.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from g3ku.config.live_runtime import get_runtime_config
from g3ku.core.events import AgentEvent
from g3ku.runtime.bridge import cli_event_text
from g3ku.runtime.session_keys import sanitize_channel_outbound_text

DEFAULT_EVENT_BUFFER_SIZE = 512
SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0
# 等待终稿时 live 队列的单次 get 上限：每轮醒来重查 deadline / should_stop。
_WAIT_POLL_SECONDS = 5.0


def _now_iso() -> str:
    return datetime.now().isoformat()


def build_external_event(
    event_type: str,
    *,
    seq: int,
    turn_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {"type": str(event_type), "seq": int(seq), "ts": _now_iso()}
    if turn_id:
        event["turn_id"] = str(turn_id)
    event.update(payload)
    return event


class SessionEventHub:
    """Bounded replay buffer + live fan-out for one session's events."""

    def __init__(self, session_key: str, buffer_size: int):
        self.session_key = str(session_key)
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max(16, int(buffer_size or DEFAULT_EVENT_BUFFER_SIZE)))
        self._seq = 0
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    def publish(self, event_type: str, *, turn_id: str | None = None, **payload: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = build_external_event(event_type, seq=self._seq, turn_id=turn_id, **payload)
            self._buffer.append(event)
            subscribers = list(self._subscribers)
        for queue in subscribers:
            queue.put_nowait(event)
        return event

    def replay(self, last_seq: int = 0) -> list[dict[str, Any]]:
        threshold = int(last_seq or 0)
        with self._lock:
            return [event for event in self._buffer if int(event.get("seq") or 0) > threshold]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)


_HUBS: dict[str, SessionEventHub] = {}
_HUBS_LOCK = threading.RLock()


def _configured_buffer_size() -> int:
    try:
        config = get_runtime_config(force=False)[0]
        value = getattr(getattr(config, "external_api", None), "event_buffer_size", DEFAULT_EVENT_BUFFER_SIZE)
        return int(value or DEFAULT_EVENT_BUFFER_SIZE)
    except Exception:
        return DEFAULT_EVENT_BUFFER_SIZE


def get_session_event_hub(session_key: str) -> SessionEventHub:
    raw = str(session_key or "").strip()
    with _HUBS_LOCK:
        hub = _HUBS.get(raw)
        if hub is None:
            hub = SessionEventHub(raw, _configured_buffer_size())
            _HUBS[raw] = hub
        return hub


def reset_session_event_hubs() -> None:
    """Test hook: drop all in-memory hubs."""
    with _HUBS_LOCK:
        _HUBS.clear()


def _rewrite_media_signed(text: str) -> str:
    # Lazy import: ceo_media lives in g3ku.runtime.api, which must not be
    # imported at module load time here (see module docstring).
    from g3ku.runtime.api.ceo_media import rewrite_media_links_signed

    rewritten = rewrite_media_links_signed(text)
    return rewritten if isinstance(rewritten, str) else text


def _turn_usage(session: Any, turn_id: str) -> dict[str, Any] | None:
    usage_map = getattr(session, "_frontdoor_turn_usage", None)
    if not isinstance(usage_map, dict) or not turn_id:
        return None
    usage = usage_map.get(turn_id)
    return dict(usage) if isinstance(usage, dict) else None


def make_session_event_relay(
    session_key: str,
    *,
    turn_id: str,
    session: Any | None = None,
) -> Callable[[AgentEvent], Awaitable[None]]:
    """Build an AgentEvent listener mapping runtime events to external hub events.

    Mapping (see docs/architecture/external-agent-api.md):
    - ``assistant_stream_delta`` → ``reply.delta`` (latest-segment authoritative text)
    - ``message_delta`` (progress/analysis) and tool start/error → ``progress``
    - ``message_end`` → ``reply.final`` (sanitized, media rewritten to signed URLs)
    Turn terminal events (``turn.completed`` / ``turn.failed``) are emitted by
    the executor, never by this relay.
    """
    hub = get_session_event_hub(session_key)

    async def relay(event: AgentEvent) -> None:
        try:
            event_type = str(getattr(event, "type", "") or "")
            payload = dict(getattr(event, "payload", {}) or {})
            if event_type == "assistant_stream_delta":
                text = str(payload.get("text") or "")
                if not text:
                    return
                hub.publish(
                    "reply.delta",
                    turn_id=str(payload.get("turn_id") or turn_id),
                    text=text,
                    source=str(payload.get("source") or "user"),
                )
                return
            if event_type == "message_end":
                if payload.get("heartbeat_internal"):
                    return
                text = sanitize_channel_outbound_text(str(payload.get("text") or ""))
                if not text:
                    return
                text = _rewrite_media_signed(text)
                end_turn_id = str(payload.get("turn_id") or turn_id)
                final_payload: dict[str, Any] = {
                    "text": text,
                    "source": str(payload.get("source") or "user"),
                }
                usage = _turn_usage(session, end_turn_id)
                if usage:
                    final_payload["usage"] = usage
                hub.publish("reply.final", turn_id=end_turn_id, **final_payload)
                return
            kind, text = cli_event_text(event)
            text = str(text or "").strip()
            if not text:
                return
            if kind == "tool":
                hub.publish("progress", turn_id=turn_id, kind="tool", text=f"🔧 {text}")
            elif kind == "tool_error":
                hub.publish("progress", turn_id=turn_id, kind="tool_error", text=f"⚠️ {text}")
            elif kind in {"progress", "analysis"}:
                hub.publish("progress", turn_id=turn_id, kind="milestone", text=text)
        except Exception:
            # Relays run on the session event dispatch path: never block, never raise.
            return

    return relay


@dataclass(slots=True)
class ExternalReplyOutcome:
    """Result of waiting for a session's next authoritative assistant reply.

    kind:
    - ``reply``     — final text captured (``text``/``turn_id``/``usage``)
    - ``timeout``   — deadline or ``should_stop`` reached (``error`` marks which)
    - ``failed``    — ``turn.failed`` observed (``error`` is the readable text)
    - ``cancelled`` — turn ended paused/cancelled without a final reply
    - ``no_reply``  — turn completed without any user-visible final reply
    """

    kind: str
    text: str | None = None
    turn_id: str | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None
    last_seq: int = 0


async def wait_for_external_reply(
    session_key: str,
    *,
    after_seq: int,
    timeout: float,
    want_turn_id: str | None = None,
    queued: bool = False,
    should_stop: Callable[[], Awaitable[bool]] | None = None,
) -> ExternalReplyOutcome:
    """In-process "submit → wait for the reply" primitive for gateway surfaces
    (OpenAI-compatible endpoint; see docs/architecture/agent-gateway.md).

    Race-free ordering contract: the CALLER captures ``after_seq =
    hub.last_seq`` BEFORE ``ExternalTurnService.submit``; this helper
    subscribes first, then drains ``replay(after_seq)`` before the live queue,
    deduping the overlap with a seen-seq set (publish fans out outside the hub
    lock, so arrival order is not guaranteed — matching is by type/turn_id and
    selection by max seq, never by arrival).

    ``queued=True`` (message joined a running turn's chain): the chain's own
    final for the PREDECESSOR message arrives first, and drained follow-ups
    publish additional ``reply.final`` events under the same turn_id before the
    single terminal ``turn.completed``. The rule is therefore: collect finals,
    stop at the first terminal, return the max-seq final.

    Known bound: more than ``event_buffer_size`` events between the caller's
    ``after_seq`` capture and the replay pass can evict the reply.
    """
    hub = get_session_event_hub(session_key)
    queue = hub.subscribe()
    threshold = int(after_seq or 0)
    seen: set[int] = set()
    finals: list[dict[str, Any]] = []
    state = {"last_seq": threshold}

    def _reply_outcome(event: dict[str, Any]) -> ExternalReplyOutcome:
        usage = event.get("usage")
        return ExternalReplyOutcome(
            kind="reply",
            text=str(event.get("text") or ""),
            turn_id=str(event.get("turn_id") or "") or None,
            usage=dict(usage) if isinstance(usage, dict) else None,
            last_seq=state["last_seq"],
        )

    def _consider(event: dict[str, Any]) -> ExternalReplyOutcome | None:
        seq = int(event.get("seq") or 0)
        if seq <= threshold or seq in seen:
            return None
        seen.add(seq)
        state["last_seq"] = max(state["last_seq"], seq)
        event_type = str(event.get("type") or "")
        event_turn_id = str(event.get("turn_id") or "")
        if event_type == "reply.final":
            if queued:
                finals.append(dict(event))
                return None
            if want_turn_id is None or event_turn_id == str(want_turn_id):
                return _reply_outcome(event)
            return None
        if event_type == "turn.completed":
            if queued:
                if finals:
                    return _reply_outcome(max(finals, key=lambda item: int(item.get("seq") or 0)))
                return ExternalReplyOutcome(
                    kind="cancelled" if event.get("cancelled") else "no_reply",
                    turn_id=event_turn_id or None,
                    last_seq=state["last_seq"],
                )
            if want_turn_id is not None and event_turn_id != str(want_turn_id):
                return None
            return ExternalReplyOutcome(
                kind="cancelled" if event.get("cancelled") else "no_reply",
                turn_id=event_turn_id or None,
                last_seq=state["last_seq"],
            )
        if event_type == "turn.failed":
            if want_turn_id is not None and event_turn_id != str(want_turn_id):
                return None
            return ExternalReplyOutcome(
                kind="failed",
                error=str(event.get("error") or ""),
                turn_id=event_turn_id or None,
                last_seq=state["last_seq"],
            )
        return None

    try:
        for event in hub.replay(threshold):
            outcome = _consider(event)
            if outcome is not None:
                return outcome
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return ExternalReplyOutcome(kind="timeout", last_seq=state["last_seq"])
            if should_stop is not None and await should_stop():
                return ExternalReplyOutcome(
                    kind="timeout", error="client_disconnected", last_seq=state["last_seq"]
                )
            try:
                event = await asyncio.wait_for(queue.get(), min(remaining, _WAIT_POLL_SECONDS))
            except asyncio.TimeoutError:
                continue
            outcome = _consider(event)
            if outcome is not None:
                return outcome
    finally:
        hub.unsubscribe(queue)
