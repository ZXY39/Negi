"""State-machine contract tests for ExternalTurnService.

Owned contract: docs/architecture/external-agent-api.md §4 (回合契约) —
- started path: idle submission → ``{status: "started", turn_id}`` (32-hex),
  hub sees ``turn.started``, exactly one terminal event after prompt returns.
- queued path: running submission → ``{status: "queued", receipt}``, message
  goes into ``queue_follow_up_batch``, idempotency slot occupied by the
  ``QUEUED_IDEMPOTENCY_SENTINEL`` so retries do not re-queue.
- duplicate path: same (session_key, idempotency_key) under running record /
  completed record / evicted record / queued sentinel — reports ``duplicate``
  with the correct ``original_status``.
- terminal invariant: prompt raising → exactly one ``turn.failed``; prompt
  cancelled (``asyncio.CancelledError``) → exactly one
  ``turn.completed``(cancelled=True) and the exception keeps propagating.
- record eviction: terminal records past ``TURN_RECORD_MEMORY_LIMIT`` are
  evicted oldest-first; running records are never evicted; idempotency rows
  survive record eviction.
- drain chain: follow-ups queued while the turn ran are chained via
  ``prompt_batch`` after prompt returns, with a single terminal event.

All ``prompt``/``prompt_batch`` interaction is faked; no production code
beyond the module under test is exercised.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.api import external_turns
from g3ku.runtime.api.external_turns import (
    QUEUED_RECEIPT_TEXT,
    QUEUED_IDEMPOTENCY_SENTINEL,
    ExternalTurnService,
)
from g3ku.runtime.external_events import get_session_event_hub, reset_session_event_hubs
from g3ku.runtime.external_sessions import EXTERNAL_OUTBOUND_CHANNEL, ExternalSessionEntry
from g3ku.runtime.session_agent import TURN_FAILED_FRIENDLY_TEXT

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class _FakeSession:
    """Mirrors the RuntimeAgentSession surface the turn executor touches:
    running-state fields (read by the static session_is_running), the
    follow-up queue/drain/archive trio, and transcript turn-id metadata."""

    def __init__(self, *, running: bool = False):
        self.state = SimpleNamespace(
            is_running=running,
            status="running" if running else "idle",
        )
        self.queued: list = []
        self.queue_persist_flags: list[bool] = []
        self.drain_calls = 0
        self.archive_calls = 0
        self.archive_payloads: list = []

    async def queue_follow_up_batch(self, messages, *, persist_transcript=True):
        self.queue_persist_flags.append(persist_transcript)
        self.queued.extend(messages)
        return list(messages)

    def drain_queued_follow_up_messages(self):
        self.drain_calls += 1
        drained = list(self.queued)
        self.queued.clear()
        return drained

    async def archive_follow_up_chain_transition(self, *, pending_follow_up_turn_ids=None):
        self.archive_calls += 1
        self.archive_payloads.append(set(pending_follow_up_turn_ids or ()))
        return None


class _FakeBridge:
    """Plug-replaceable SessionRuntimeBridge fake: prompt/prompt_batch
    behavior is scripted per test (fail_with / cancel / block / injectors)."""

    def __init__(
        self,
        session=None,
        *,
        fail_with: Exception | None = None,
        cancel: bool = False,
        on_prompt=None,
        on_prompt_batch=None,
        hold: str | None = None,
    ):
        self._session = session
        self.fail_with = fail_with
        self.cancel = cancel
        self.on_prompt = on_prompt
        self.on_prompt_batch = on_prompt_batch
        # Only the prompt whose message equals `hold` blocks on the gate event;
        # other prompts pass through so a held (running) turn and concurrently
        # submitted turns can coexist on one bridge/service.
        self._hold_message = hold
        self._hold_event: asyncio.Event | None = None
        self.prompts: list = []
        self.batches: list = []
        self.prompt_kwargs: list[dict] = []
        self.batch_kwargs: list[dict] = []

    def get_existing_session(self, session_key):
        return self._session

    async def block(self) -> asyncio.Event:
        if self._hold_event is None:
            self._hold_event = asyncio.Event()
        return self._hold_event

    async def release(self) -> None:
        if self._hold_event is not None:
            self._hold_event.set()

    async def prompt(self, message, **kwargs):
        self.prompts.append(message)
        self.prompt_kwargs.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        if self._hold_message is not None and message == self._hold_message:
            await (await self.block()).wait()
        if self.on_prompt is not None:
            await self.on_prompt()
        if self.cancel:
            raise asyncio.CancelledError()
        return SimpleNamespace(output="ok")

    async def prompt_batch(self, messages, **kwargs):
        self.batches.append(list(messages))
        self.batch_kwargs.append(kwargs)
        if self.on_prompt_batch is not None:
            await self.on_prompt_batch()
        return SimpleNamespace(output="ok")


def _entry(session_key: str) -> ExternalSessionEntry:
    return ExternalSessionEntry(
        bridge_id="test-bridge",
        external_key="qq:dm:1",
        session_key=session_key,
        created_at="2026-09-11T00:00:00",
    )


def _make_service(bridge: _FakeBridge) -> ExternalTurnService:
    return ExternalTurnService(runtime_bridge=bridge, register_task=None)


@pytest.fixture(autouse=True)
def _clean_hubs():
    reset_session_event_hubs()
    yield
    reset_session_event_hubs()


async def _wait_terminal(
    session_key: str,
    *,
    turn_id: str | None = None,
    timeout: float = 2.0,
) -> list[dict]:
    """Poll the session hub until the given turn's terminal event is published.

    ``turn_id`` must identify the turn just submitted: without it, an earlier
    turn's stale terminal event satisfies the wait immediately, and the new
    turn's task may not have run at all yet (the check then races the event
    loop). Every submission in a multi-turn test passes its own turn_id.
    """
    hub = get_session_event_hub(session_key)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        events = hub.replay(0)
        if any(
            e["type"] in {"turn.completed", "turn.failed"}
            and (turn_id is None or e.get("turn_id") == turn_id)
            for e in events
        ):
            return events
        await asyncio.sleep(0.01)
    raise AssertionError(f"no terminal turn event within {timeout}s (events={hub.replay(0)})")


# -- a) started path --------------------------------------------------------


@pytest.mark.asyncio
async def test_started_idle_turn_full_event_sequence():
    session_key = "ext:test-bridge:turns-a1"
    session = _FakeSession(running=False)
    bridge = _FakeBridge(session)
    service = _make_service(bridge)

    result = await service.submit(entry=_entry(session_key), user_message="你好", idempotency_key="k-a1")

    assert result["status"] == "started"
    turn_id = str(result["turn_id"] or "")
    assert _HEX32.match(turn_id)
    assert bridge.prompts == []  # prompt runs on the background turn task

    events = await _wait_terminal(session_key, turn_id=turn_id)
    types = [e["type"] for e in events]
    assert types == ["turn.started", "turn.completed"], types
    assert types.count("turn.completed") == 1
    assert types.count("turn.failed") == 0
    started = events[0]
    completed = events[-1]
    assert started["turn_id"] == turn_id
    assert completed["turn_id"] == turn_id
    assert "cancelled" not in completed

    record = service.get_turn(turn_id)
    assert record is not None and record.status == "completed"
    assert record.session_key == session_key
    assert record.idempotency_key == "k-a1"
    assert service.inflight_turn_id_for(session_key) is None

    # Bridge handoff contract: ext channel throughout, relay attached.
    kwargs = bridge.prompt_kwargs[0]
    assert kwargs["channel"] == EXTERNAL_OUTBOUND_CHANNEL
    assert kwargs["runtime_chat_id"] == "qq:dm:1"
    assert kwargs["runtime_memory_channel"] == EXTERNAL_OUTBOUND_CHANNEL
    assert len(kwargs["listeners"]) == 1
    assert bridge.prompts == ["你好"]


@pytest.mark.asyncio
async def test_no_idempotency_key_means_no_dedup():
    session_key = "ext:test-bridge:turns-a2"
    session = _FakeSession(running=False)
    bridge = _FakeBridge(session)
    service = _make_service(bridge)

    first = await service.submit(entry=_entry(session_key), user_message="m1")
    await _wait_terminal(session_key, turn_id=first["turn_id"])
    second = await service.submit(entry=_entry(session_key), user_message="m2")
    assert first["status"] == second["status"] == "started"
    assert first["turn_id"] != second["turn_id"]
    await _wait_terminal(session_key, turn_id=second["turn_id"])

    events = get_session_event_hub(session_key).replay(0)
    assert [e["type"] for e in events] == ["turn.started", "turn.completed", "turn.started", "turn.completed"]
    assert bridge.prompts == ["m1", "m2"]


# -- b) queued path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_running_session_queues_follow_up_with_receipt_and_no_hub_events():
    session_key = "ext:test-bridge:turns-b1"
    session = _FakeSession(running=True)
    bridge = _FakeBridge(session)
    service = _make_service(bridge)

    result = await service.submit(entry=_entry(session_key), user_message="在地铁上呢", idempotency_key="k-b1")

    assert result == {"turn_id": None, "status": "queued", "receipt": QUEUED_RECEIPT_TEXT}
    assert session.queued == ["在地铁上呢"]
    assert session.queue_persist_flags == [True]
    assert bridge.prompts == [] and bridge.batches == []
    # No turn machinery runs on the queued path: hub untouched.
    assert get_session_event_hub(session_key).replay(0) == []


@pytest.mark.asyncio
async def test_queued_idempotency_sentinel_blocks_requeue():
    session_key = "ext:test-bridge:turns-b2"
    session = _FakeSession(running=True)
    bridge = _FakeBridge(session)
    service = _make_service(bridge)

    first = await service.submit(entry=_entry(session_key), user_message="m1", idempotency_key="k-b2")
    assert first["status"] == "queued"
    # Sentinel occupies the slot: entry exists with the empty-turn_id marker.
    assert service._idempotency[(session_key, "k-b2")] == QUEUED_IDEMPOTENCY_SENTINEL

    # A retry with the same key must not re-queue (guard against duplicate
    # user-visible replies for one channel message).
    retry = await service.submit(entry=_entry(session_key), user_message="m1", idempotency_key="k-b2")
    assert retry == {"turn_id": None, "status": "duplicate", "original_status": "queued"}
    assert session.queued == ["m1"]
    assert session.queue_persist_flags == [True]


# -- c) duplicate path --------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_against_running_record():
    session_key = "ext:test-bridge:turns-c1"
    session = _FakeSession(running=False)
    bridge = _FakeBridge(session, hold="借我五百")
    service = _make_service(bridge)
    await bridge.block()

    started = await service.submit(entry=_entry(session_key), user_message="借我五百", idempotency_key="k-c1")
    turn_id = started["turn_id"]

    dup = await service.submit(entry=_entry(session_key), user_message="借我五百", idempotency_key="k-c1")
    assert dup == {"turn_id": turn_id, "status": "duplicate", "original_status": "running"}

    await bridge.release()
    await _wait_terminal(session_key, turn_id=started["turn_id"])
    assert bridge.prompts == ["借我五百"]  # dedup prevented a second prompt
    record = service.get_turn(turn_id)
    assert record is not None and record.status == "completed"


@pytest.mark.asyncio
async def test_duplicate_against_completed_record():
    session_key = "ext:test-bridge:turns-c2"
    session = _FakeSession(running=False)
    bridge = _FakeBridge(session)
    service = _make_service(bridge)

    started = await service.submit(entry=_entry(session_key), user_message="m1", idempotency_key="k-c2")
    await _wait_terminal(session_key, turn_id=started["turn_id"])

    dup = await service.submit(entry=_entry(session_key), user_message="m1", idempotency_key="k-c2")
    assert dup == {"turn_id": started["turn_id"], "status": "duplicate", "original_status": "completed"}
    assert service.get_turn(started["turn_id"]).status == "completed"
    assert bridge.prompts == ["m1"]  # no new turn


@pytest.mark.asyncio
async def test_duplicate_after_record_eviction_reports_completed(monkeypatch):
    monkeypatch.setattr(external_turns, "TURN_RECORD_MEMORY_LIMIT", 3)
    session_key = "ext:test-bridge:turns-c3"
    session = _FakeSession(running=False)
    bridge = _FakeBridge(session)
    service = _make_service(bridge)

    submitted = []
    for index in range(4):
        result = await service.submit(
            entry=_entry(session_key), user_message=f"m{index}", idempotency_key=f"k-{index}"
        )
        submitted.append(result["turn_id"])
        await _wait_terminal(session_key, turn_id=result["turn_id"])

    # Oldest terminal record evicted; idempotency row survives.
    assert service.get_turn(submitted[0]) is None
    assert (session_key, "k-0") in service._idempotency

    dup = await service.submit(entry=_entry(session_key), user_message="m0", idempotency_key="k-0")
    assert dup == {"turn_id": None, "status": "duplicate", "original_status": "completed"}
    # No new record was created by the duplicate.
    assert len(service._turns) == 3


# -- d) terminal invariants ----------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_exception_emits_single_turn_failed_with_friendly_fallback():
    session_key = "ext:test-bridge:turns-d1"
    bridge = _FakeBridge(_FakeSession(running=False), fail_with=ValueError(""))
    registered: list[tuple] = []
    service = ExternalTurnService(runtime_bridge=bridge, register_task=lambda key, task: registered.append((key, task)))

    started = await service.submit(entry=_entry(session_key), user_message="m1")
    events = await _wait_terminal(session_key, turn_id=started["turn_id"])

    failed = [e for e in events if e["type"] == "turn.failed"]
    assert len(failed) == 1
    assert not [e for e in events if e["type"] == "turn.completed"]
    assert failed[0]["turn_id"] == started["turn_id"]
    assert failed[0]["error"] == TURN_FAILED_FRIENDLY_TEXT  # empty str → friendly fallback
    assert failed[0]["detail"] == ""
    assert service.get_turn(started["turn_id"]).status == "failed"
    assert service.inflight_turn_id_for(session_key) is None

    # None-key registration contract: the turn task is registered, not the
    # real session key (gather-on-self deadlock guard).
    assert registered == [(None, registered[0][1])]
    assert registered[0][1].result() is None  # generic exceptions are contained in the turn event


@pytest.mark.asyncio
async def test_cancel_publishes_terminal_then_propagates():
    session_key = "ext:test-bridge:turns-d2"
    bridge = _FakeBridge(_FakeSession(running=False), cancel=True)
    registered: list[tuple] = []
    service = ExternalTurnService(runtime_bridge=bridge, register_task=lambda key, task: registered.append((key, task)))

    started = await service.submit(entry=_entry(session_key), user_message="m1")
    (registered_key, task) = registered[0]
    assert registered_key is None

    with pytest.raises(asyncio.CancelledError):
        await task  # CancelledError escapes the turn task and continues propagating

    events = get_session_event_hub(session_key).replay(0)
    assert [e["type"] for e in events] == ["turn.started", "turn.completed"]
    completed = events[-1]
    assert completed["turn_id"] == started["turn_id"]
    assert completed.get("cancelled") is True
    assert service.get_turn(started["turn_id"]).status == "cancelled"


# -- e) record eviction --------------------------------------------------------


@pytest.mark.asyncio
async def test_eviction_keeps_running_never_evicts_and_bounds_table(monkeypatch):
    monkeypatch.setattr(external_turns, "TURN_RECORD_MEMORY_LIMIT", 3)
    running_key = "ext:test-bridge:turns-e-running"
    # One service owns one table: eviction only fires when more turns land on
    # IT, so the held running turn and the completing turns must share the
    # same bridge/service. `hold` gates only the running turn's prompt.
    bridge = _FakeBridge(_FakeSession(running=False), hold="hold")
    await bridge.block()
    service = _make_service(bridge)

    running_started = await service.submit(
        entry=_entry(running_key), user_message="hold", idempotency_key="e-hold"
    )
    running_id = running_started["turn_id"]

    completed_ids: list[str] = []
    for index in range(4):
        key = f"ext:test-bridge:turns-e-{index}"
        started = await service.submit(
            entry=_entry(key), user_message=f"m{index}", idempotency_key=f"e-{index}"
        )
        completed_ids.append(started["turn_id"])
        await _wait_terminal(key, turn_id=started["turn_id"])
        # Running record must survive every eviction pass.
        assert service.get_turn(running_id) is not None
        assert service.get_turn(running_id).status == "running"
        assert service.inflight_turn_id_for(running_key) == running_id

    # Bounded at the limit, the running record counting toward it: a
    # run-of-4 with limit 3 keeps running + last 2 terminals (eviction skips
    # running records and pops oldest terminal records until the table fits).
    assert len(service._turns) == 3
    assert service.get_turn(completed_ids[0]) is None
    assert service.get_turn(completed_ids[1]) is None
    assert service.get_turn(completed_ids[2]) is not None
    assert service.get_turn(completed_ids[3]) is not None
    assert service.get_turn(running_id) is not None

    await bridge.release()
    await _wait_terminal(running_key, turn_id=running_id)
    assert service.get_turn(running_id).status == "completed"


@pytest.mark.asyncio
async def test_idempotency_table_is_bounded_lru(monkeypatch):
    monkeypatch.setattr(external_turns, "IDEMPOTENCY_MEMORY_LIMIT", 2)
    session_key = "ext:test-bridge:turns-e2"
    session = _FakeSession(running=False)
    bridge = _FakeBridge(session)
    service = _make_service(bridge)

    for index in range(3):
        result = await service.submit(entry=_entry(session_key), user_message=f"m{index}", idempotency_key=f"k-{index}")
        assert result["status"] == "started"
        await _wait_terminal(session_key, turn_id=result["turn_id"])

    assert (session_key, "k-0") not in service._idempotency  # LRU-evicted
    assert (session_key, "k-1") in service._idempotency
    assert (session_key, "k-2") in service._idempotency

    # Evicted idempotency row → the key is treated as new: a turn starts again.
    result = await service.submit(entry=_entry(session_key), user_message="m0-again", idempotency_key="k-0")
    assert result["status"] == "started"
    await _wait_terminal(session_key, turn_id=result["turn_id"])
    assert bridge.prompts == ["m0", "m1", "m2", "m0-again"]


# -- f) drain chain -----------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_follow_ups_chain_with_single_terminal_event():
    session_key = "ext:test-bridge:turns-f1"
    session = _FakeSession(running=False)

    async def inject_first(_message=None):
        await session.queue_follow_up_batch(
            [UserInputMessage(content="跟上的问题", metadata={"_transcript_turn_id": "fu-1"})],
            persist_transcript=True,
        )

    # inject_second fires exactly once: the drain loop re-runs prompt_batch
    # while anything is drained, so an unbounded injector would keep the chain
    # populated forever and the turn would never reach its terminal event.
    injected_second = {"done": False}

    async def inject_second(_messages=None):
        if injected_second["done"]:
            return
        injected_second["done"] = True
        await session.queue_follow_up_batch(
            [UserInputMessage(content="补一句", metadata={"_transcript_turn_id": "fu-2"})],
            persist_transcript=True,
        )

    bridge = _FakeBridge(session, on_prompt=inject_first, on_prompt_batch=inject_second)
    service = _make_service(bridge)

    started = await service.submit(entry=_entry(session_key), user_message="先问个问题")
    events = await _wait_terminal(session_key, turn_id=started["turn_id"])

    types = [e["type"] for e in events]
    assert types.count("turn.started") == 1
    assert types.count("turn.completed") == 1
    assert types.count("turn.failed") == 0
    assert events[-1]["turn_id"] == started["turn_id"]
    assert service.get_turn(started["turn_id"]).status == "completed"

    # Chain: first drain → prompt_batch(1); second round drained the follow-up
    # queued during the first batch; final drain emptied the queue.
    assert len(bridge.batches) == 2
    assert [m.content for m in bridge.batches[0]] == ["跟上的问题"]
    assert [m.content for m in bridge.batches[1]] == ["补一句"]
    assert session.drain_calls == 3
    assert session.archive_calls == 2
    # Transcript turn ids of the chained follow-ups are handed to the archive step.
    assert session.archive_payloads == [{"fu-1"}, {"fu-2"}]
    for kwargs in bridge.batch_kwargs:
        assert kwargs["channel"] == EXTERNAL_OUTBOUND_CHANNEL
        assert kwargs["runtime_memory_chat_id"] == "qq:dm:1"
        assert len(kwargs["listeners"]) == 1