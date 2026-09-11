"""外部事件 turn_id 命名空间统一回归测试。

修复缺陷（B-F1）：relay 发布 reply.final / reply.delta 时优先采用会话内部
事件携带的 16 位 transcript turn id，而等待方（OpenAI 兼容网关/外部桥）按
32 位 record turn_id 匹配——两套命名空间恒不相等，等待方永远收不到
终稿，表现为「有回复但拿不到回复」。对外事件现一律携带 record turn_id，
与 turn.started/completed/failed 同一命名空间；usage 记录按会话内部
transcript id 建键，查找时先按 payload id 回退 record id。
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from g3ku.runtime.external_events import (
    _drain_queue_for_outcome,
    get_session_event_hub,
    make_session_event_relay,
    reset_session_event_hubs,
    wait_for_external_reply,
)

_RECORD_TURN_ID = "record-" + "a" * 25  # 32 位 record 命名空间
_TRANSCRIPT_TURN_ID = "transcript16char"  # 16 位会话内部命名空间


def _event(event_type: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, payload=payload)


def test_reply_final_publishes_record_turn_id_and_keeps_usage() -> None:
    reset_session_event_hubs()
    session = SimpleNamespace(_frontdoor_turn_usage={_TRANSCRIPT_TURN_ID: {"input_tokens": 3, "output_tokens": 5}})
    relay = make_session_event_relay("ext:t1", turn_id=_RECORD_TURN_ID, session=session)

    asyncio.run(relay(_event("message_end", {"text": "hello", "turn_id": _TRANSCRIPT_TURN_ID})))

    hub = get_session_event_hub("ext:t1")
    finals = [event for event in hub.replay(0) if event.get("type") == "reply.final"]
    assert len(finals) == 1
    assert finals[0]["turn_id"] == _RECORD_TURN_ID, "终稿必须携带 record turn_id"
    assert finals[0]["usage"] == {"input_tokens": 3, "output_tokens": 5}, "usage 按 transcript id 建键仍可解析"


def test_reply_delta_publishes_record_turn_id() -> None:
    reset_session_event_hubs()
    relay = make_session_event_relay("ext:t2", turn_id=_RECORD_TURN_ID, session=None)

    asyncio.run(relay(_event("assistant_stream_delta", {"text": "片段", "turn_id": _TRANSCRIPT_TURN_ID})))

    hub = get_session_event_hub("ext:t2")
    deltas = [event for event in hub.replay(0) if event.get("type") == "reply.delta"]
    assert len(deltas) == 1
    assert deltas[0]["turn_id"] == _RECORD_TURN_ID


async def test_waiter_receives_final_relayed_under_transcript_id() -> None:
    """P0 回归：等待方按 record turn_id 等待；会话内部事件携带 16 位
    transcript id，修复前等待方永远匹配不到终稿。"""
    reset_session_event_hubs()
    record_turn_id = uuid.uuid4().hex

    async def _produce() -> None:
        await asyncio.sleep(0.05)
        relay = make_session_event_relay("ext:t3", turn_id=record_turn_id, session=None)
        await relay(_event("message_end", {"text": "最终回复", "turn_id": "t16-internal"}))

    producer = asyncio.create_task(_produce())
    outcome = await wait_for_external_reply(
        "ext:t3",
        after_seq=0,
        timeout=5.0,
        want_turn_id=record_turn_id,
    )
    await producer

    assert outcome.kind == "reply", f"必须拿到终稿而不是 {outcome.kind}"
    assert outcome.text == "最终回复"
    assert outcome.turn_id == record_turn_id


async def test_stream_delta_then_final_matched_by_record_turn_id() -> None:
    reset_session_event_hubs()
    record_turn_id = uuid.uuid4().hex
    hub = get_session_event_hub("ext:t4")

    async def _produce() -> None:
        await asyncio.sleep(0.05)
        relay = make_session_event_relay("ext:t4", turn_id=record_turn_id, session=None)
        await relay(_event("assistant_stream_delta", {"text": "流式片段", "turn_id": "t16-internal"}))
        await relay(_event("message_end", {"text": "完整回复", "turn_id": "t16-internal"}))

    producer = asyncio.create_task(_produce())
    outcome = await wait_for_external_reply(
        "ext:t4",
        after_seq=hub.last_seq,
        timeout=5.0,
        want_turn_id=record_turn_id,
    )
    await producer

    assert outcome.kind == "reply"
    assert outcome.text == "完整回复"


def test_drain_queue_returns_pending_outcome_and_none_when_empty() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    want = "wanted-turn"

    def _consider(event: dict) -> str | None:
        if event.get("type") == "reply.final" and event.get("turn_id") == want:
            return "HIT"
        return None

    assert _drain_queue_for_outcome(queue, _consider) is None

    queue.put_nowait({"type": "progress", "turn_id": want})
    queue.put_nowait({"type": "reply.final", "turn_id": "other"})
    queue.put_nowait({"type": "reply.final", "turn_id": want})
    assert _drain_queue_for_outcome(queue, _consider) == "HIT"
    assert queue.empty()
