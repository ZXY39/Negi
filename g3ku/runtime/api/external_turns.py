"""Turn executor for the External Agent API.

Generalized from the (since removed) China bridge turn runner with all
platform-specific branches dropped: turns execute through
``SessionRuntimeBridge`` and report to the per-session event hub. Carried
over verbatim in spirit:

- Terminal invariant: every turn emits exactly one ``turn.completed`` or
  ``turn.failed`` event on every path, including cancellation
  (``asyncio.CancelledError`` is a ``BaseException`` that slips past
  ``except Exception``; a missing terminal event wedged the legacy host's
  per-session dispatch queue, so this is contractual, not cosmetic).
- Running-session messages queue as follow-ups and are drained after the
  prompt returns, chaining turns under a single terminal event.
- Turn tasks register with a ``None`` key: registering under the real session
  key makes pause's ``cancel_session_tasks`` gather collect on itself and
  deadlock.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.external_events import get_session_event_hub, make_session_event_relay
from g3ku.runtime.bridge import SessionRuntimeBridge
from g3ku.runtime.external_sessions import EXTERNAL_OUTBOUND_CHANNEL, ExternalSessionEntry
from g3ku.runtime.session_agent import TURN_FAILED_FRIENDLY_TEXT

QUEUED_RECEIPT_TEXT = "收到，将在当前任务中一并处理。"
IDEMPOTENCY_MEMORY_LIMIT = 256


@dataclass(slots=True)
class TurnRecord:
    turn_id: str
    session_key: str
    bridge_id: str
    external_key: str
    status: str
    started_at: str
    idempotency_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ExternalTurnService:
    def __init__(
        self,
        *,
        runtime_bridge: SessionRuntimeBridge,
        register_task: Any | None = None,
    ):
        self._runtime_bridge = runtime_bridge
        self._register_task = register_task
        self._turns: dict[str, TurnRecord] = {}
        self._idempotency: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._submit_locks: dict[str, asyncio.Lock] = {}

    # -- registry -----------------------------------------------------------

    def get_turn(self, turn_id: str | None) -> TurnRecord | None:
        raw = str(turn_id or "").strip()
        return self._turns.get(raw) if raw else None

    def inflight_turn_id_for(self, session_key: str) -> str | None:
        raw = str(session_key or "").strip()
        for record in self._turns.values():
            if record.session_key == raw and record.status == "running":
                return record.turn_id
        return None

    # -- submission ---------------------------------------------------------

    async def submit(
        self,
        *,
        entry: ExternalSessionEntry,
        user_message: str | UserInputMessage,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        session_key = entry.session_key
        idem = str(idempotency_key or "").strip() or None
        lock = self._submit_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            if idem:
                existing_turn_id = self._idempotency.get((session_key, idem))
                if existing_turn_id and existing_turn_id in self._turns:
                    record = self._turns[existing_turn_id]
                    return {"turn_id": record.turn_id, "status": "duplicate", "original_status": record.status}

            session = self._runtime_bridge.get_existing_session(session_key)
            if SessionRuntimeBridge.session_is_running(session) and session is not None:
                await session.queue_follow_up_batch([user_message], persist_transcript=True)
                return {"turn_id": None, "status": "queued", "receipt": QUEUED_RECEIPT_TEXT}

            turn_id = uuid.uuid4().hex
            record = TurnRecord(
                turn_id=turn_id,
                session_key=session_key,
                bridge_id=entry.bridge_id,
                external_key=entry.external_key,
                status="running",
                started_at=datetime.now().isoformat(),
                idempotency_key=idem,
            )
            self._turns[turn_id] = record
            if idem:
                self._idempotency[(session_key, idem)] = turn_id
                while len(self._idempotency) > IDEMPOTENCY_MEMORY_LIMIT:
                    self._idempotency.popitem(last=False)

            task = asyncio.create_task(self._execute_turn(record, user_message))
            if callable(self._register_task):
                # None-key contract: see module docstring.
                self._register_task(None, task)
            else:
                task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            return {"turn_id": turn_id, "status": "started"}

    # -- controls -----------------------------------------------------------

    async def pause_turn(self, turn_id: str | None) -> int:
        record = self.get_turn(turn_id)
        if record is None:
            raise KeyError(str(turn_id or ""))
        return await self._runtime_bridge.pause(record.session_key, manual=True)

    async def cancel_session(self, session_key: str) -> int:
        return await self._runtime_bridge.cancel(str(session_key or "").strip(), reason="external_api_cancel")

    # -- execution ----------------------------------------------------------

    async def _execute_turn(self, record: TurnRecord, user_message: str | UserInputMessage) -> None:
        hub = get_session_event_hub(record.session_key)
        session = self._runtime_bridge.get_existing_session(record.session_key)
        relay = make_session_event_relay(record.session_key, turn_id=record.turn_id, session=session)
        hub.publish("turn.started", turn_id=record.turn_id)
        try:
            await self._runtime_bridge.prompt(
                user_message,
                session_key=record.session_key,
                channel=EXTERNAL_OUTBOUND_CHANNEL,
                chat_id=record.external_key,
                runtime_channel=EXTERNAL_OUTBOUND_CHANNEL,
                runtime_chat_id=record.external_key,
                runtime_memory_channel=EXTERNAL_OUTBOUND_CHANNEL,
                runtime_memory_chat_id=record.external_key,
                listeners=[relay],
                register_task=self._register_task,
            )
            await self._drain_queued_follow_ups(record, relay)
            record.status = "completed"
            hub.publish("turn.completed", turn_id=record.turn_id)
        except asyncio.CancelledError:
            # Paused/cancelled mid-turn: emit the terminal event before
            # re-raising so consumers are never left without a closeout.
            record.status = "cancelled"
            hub.publish("turn.completed", turn_id=record.turn_id, cancelled=True)
            raise
        except Exception as exc:
            record.status = "failed"
            hub.publish(
                "turn.failed",
                turn_id=record.turn_id,
                error=str(exc).strip() or TURN_FAILED_FRIENDLY_TEXT,
                detail=str(exc),
            )

    async def _drain_queued_follow_ups(self, record: TurnRecord, relay: Any) -> None:
        """Drain-loop continuation: follow-ups queued while the turn ran get
        chained after the prompt returns; the whole chain keeps a single
        terminal event (mirrors the legacy transport drain contract)."""
        while True:
            session = self._runtime_bridge.get_existing_session(record.session_key)
            drained = session.drain_queued_follow_up_messages() if session is not None else []
            if not drained:
                return
            archive = getattr(session, "archive_follow_up_chain_transition", None)
            if callable(archive):
                follow_up_turn_ids = {
                    str((getattr(item, "metadata", None) or {}).get("_transcript_turn_id") or "").strip()
                    for item in drained
                }
                follow_up_turn_ids.discard("")
                await archive(pending_follow_up_turn_ids=follow_up_turn_ids)
            await self._runtime_bridge.prompt_batch(
                drained,
                session_key=record.session_key,
                channel=EXTERNAL_OUTBOUND_CHANNEL,
                chat_id=record.external_key,
                runtime_channel=EXTERNAL_OUTBOUND_CHANNEL,
                runtime_chat_id=record.external_key,
                runtime_memory_channel=EXTERNAL_OUTBOUND_CHANNEL,
                runtime_memory_chat_id=record.external_key,
                listeners=[relay],
                register_task=self._register_task,
            )


_SERVICE: ExternalTurnService | None = None


def set_external_turn_service(service: ExternalTurnService | None) -> None:
    """Install a service instance (production wiring or test fake)."""
    global _SERVICE
    _SERVICE = service


def get_external_turn_service() -> ExternalTurnService:
    """Return the process-wide service, building it from the live runtime on
    first use. Raises RuntimeError when the runtime is unavailable."""
    global _SERVICE
    if _SERVICE is None:
        from g3ku.shells.web import get_agent, get_runtime_manager

        agent = get_agent()
        if agent is None:
            raise RuntimeError("runtime_unavailable")
        runtime_bridge = SessionRuntimeBridge(get_runtime_manager(agent))
        registrar = getattr(agent, "_register_active_task", None)
        _SERVICE = ExternalTurnService(
            runtime_bridge=runtime_bridge,
            register_task=registrar if callable(registrar) else None,
        )
    return _SERVICE
