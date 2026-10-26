"""Pull consumers must be able to read what the outbound ledger actually holds.

Live trigger (2026-10-26, session `ext:qoder:5bd6b0b1c2835485`): a root node escalated through
`send_message(to="frontdoor")`, the front door consumed the notice in a **heartbeat-origin** turn
and wrote a 1442-char report, and `negi_get_reply` still answered `no_final_written /
unconsumed_replies: 0`. Heartbeat turns never emit `reply.final` — their body leaves through the
proactive push lane and lands in the ledger as `outbound.created` — while every pull read filtered
on `reply.final`. Worse, the SSE stream acked that frame on its way out even though the client
dropped it, so asking "is there a reply?" marked the only stored copy as delivered.
"""

from __future__ import annotations

from typing import Any

import pytest

# 从 `main` 起导入：以 `g3ku.runtime.*` 为入口会先半初始化 frontdoor.tool_contract，
# 再被 main.runtime.react_loop 回灌成循环导入。
import main  # noqa: F401
from g3ku.mcp_gateway.client import G3kuMcpClient
from g3ku.runtime.api import external_v1
from g3ku.runtime.external_outbox import PROACTIVE_EVENT, REPLY_EVENT, is_pull_consumable

SESSION = 'ext:qoder:5bd6b0b1c2835485'


def _record(outbox_id: str, event: str, text: str, ts: str = '2026-10-26T02:17:05.934016') -> dict[str, Any]:
    return {
        'id': outbox_id,
        'ts': ts,
        'event': event,
        'session_key': SESSION,
        'external_key': 'mcp:span-probe-1',
        'text': text,
    }


class TestPullConsumable:
    def test_turn_reply_is_consumable(self):
        assert is_pull_consumable(_record('obx-1', REPLY_EVENT, 'answer')) is True

    def test_proactive_push_is_consumable(self):
        # 心跳回合的正文只有这一类记录，把它排除掉等于让账本里的存货不可读。
        assert is_pull_consumable(_record('obx-2', PROACTIVE_EVENT, 'report')) is True

    def test_empty_text_is_not_consumable(self):
        assert is_pull_consumable(_record('obx-3', PROACTIVE_EVENT, '   ')) is False

    def test_other_event_kinds_are_not_consumable(self):
        assert is_pull_consumable(_record('obx-4', 'turn.completed', 'noise')) is False


class TestWakeSummary:
    def test_both_kinds_are_surfaced_and_tagged(self):
        payload = external_v1.summarize_external_session_wake(
            cursor='',
            task_rows=[],
            pending_records=[
                _record('obx-1', REPLY_EVENT, 'answer'),
                _record('obx-2', PROACTIVE_EVENT, 'report'),
            ],
        )
        assert [item['outbox_id'] for item in payload['replies']] == ['obx-1', 'obx-2']
        assert [item['event'] for item in payload['replies']] == [REPLY_EVENT, PROACTIVE_EVENT]
        assert payload['replies_pending'] == 2

    def test_cursor_still_bounds_the_window(self):
        # 水位线是 `_wake_wall_second` 自己那串形状（ISO、到秒、带 T）。
        payload = external_v1.summarize_external_session_wake(
            cursor='2026-10-26T02:20:00',
            task_rows=[],
            pending_records=[_record('obx-2', PROACTIVE_EVENT, 'report')],
        )
        assert payload['replies'] == []
        assert payload['replies_pending'] == 0

    def test_cursor_boundary_is_inclusive(self):
        payload = external_v1.summarize_external_session_wake(
            cursor='2026-10-26T02:17:05',
            task_rows=[],
            pending_records=[_record('obx-2', PROACTIVE_EVENT, 'report')],
        )
        assert [item['outbox_id'] for item in payload['replies']] == ['obx-2']


class TestStreamOutAck:
    """Reading must not destroy the only stored copy of a body the client cannot take."""

    def test_reply_frame_acks(self, monkeypatch):
        acked: list[str] = []
        monkeypatch.setattr(
            external_v1, 'ack_outbound_message', lambda outbox_id, **kwargs: acked.append(outbox_id)
        )
        external_v1._ack_streamed_outbox_event(
            {'type': REPLY_EVENT, 'outbox_id': 'obx-1'}, SESSION
        )
        assert acked == ['obx-1']

    def test_proactive_frame_does_not_ack(self, monkeypatch):
        acked: list[str] = []
        monkeypatch.setattr(
            external_v1, 'ack_outbound_message', lambda outbox_id, **kwargs: acked.append(outbox_id)
        )
        external_v1._ack_streamed_outbox_event(
            {'type': PROACTIVE_EVENT, 'outbox_id': 'obx-2'}, SESSION
        )
        assert acked == []

    def test_frame_without_outbox_id_is_ignored(self, monkeypatch):
        acked: list[str] = []
        monkeypatch.setattr(
            external_v1, 'ack_outbound_message', lambda outbox_id, **kwargs: acked.append(outbox_id)
        )
        external_v1._ack_streamed_outbox_event({'type': REPLY_EVENT}, SESSION)
        assert acked == []


def _client(acks: list[str]) -> G3kuMcpClient:
    client = G3kuMcpClient.__new__(G3kuMcpClient)

    async def _ack_reply(session_id: str, outbox_id: str) -> None:
        acks.append(f'{session_id}|{outbox_id}')

    client.ack_reply = _ack_reply  # type: ignore[attr-defined]
    return client


async def _stream(events: list[dict[str, Any]]):
    for item in events:
        yield item


class TestGatewayUntargetedRead:
    @pytest.mark.asyncio
    async def test_proactive_body_is_returned_and_acked(self):
        acks: list[str] = []
        outcome = await _client(acks).wait_for_reply(
            _stream([{'seq': 3, 'type': PROACTIVE_EVENT, 'text': 'report', 'outbox_id': 'obx-2'}]),
            turn_id=None,
            after_seq=0,
            timeout=1.0,
            session_id=SESSION,
        )
        assert outcome['kind'] == 'reply'
        assert outcome['text'] == 'report'
        assert outcome['source'] == 'proactive'
        assert acks == [f'{SESSION}|obx-2']

    @pytest.mark.asyncio
    async def test_targeted_read_is_not_hijacked_by_a_push(self):
        # 点名等某一回合的答案时，一条无关的主动推送不能顶掉它。
        acks: list[str] = []
        outcome = await _client(acks).wait_for_reply(
            _stream([{'seq': 3, 'type': PROACTIVE_EVENT, 'text': 'report', 'outbox_id': 'obx-2'}]),
            turn_id='turn-9',
            after_seq=0,
            timeout=0.5,
            session_id=SESSION,
        )
        assert outcome['kind'] != 'reply'
        assert acks == []

    @pytest.mark.asyncio
    async def test_blank_push_is_skipped(self):
        acks: list[str] = []
        outcome = await _client(acks).wait_for_reply(
            _stream([
                {'seq': 3, 'type': PROACTIVE_EVENT, 'text': '', 'outbox_id': 'obx-2'},
                {'seq': 4, 'type': REPLY_EVENT, 'text': 'answer', 'outbox_id': 'obx-1'},
            ]),
            turn_id=None,
            after_seq=0,
            timeout=1.0,
            session_id=SESSION,
        )
        assert outcome['kind'] == 'reply'
        assert outcome['text'] == 'answer'
        assert outcome.get('source') is None
