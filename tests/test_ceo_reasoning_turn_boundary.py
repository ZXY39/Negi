"""思维链实时行的累计口径按回合划界：换一轮就从零重新统计。

回归的实盘症状：会话气泡上那一行「~N tok」只涨不降，第二轮的第一帧读到 500 字符而本轮
只想过 100 字符。`_reasoning_touch_turn()` 在回合边界只复位了耗时，`_reasoning_chars_total`
与 `_reasoning_est_tokens_total` 从没清过，所以那一行报的是整条会话的思考总量；回合收尾
写进转录行的 `thinking` 读的是同一份累加器，历史气泡跟着一起虚高。

合同要保住的另一半：同一回合内的多跳（心跳、工具轮、换链降级的各次 attempt）仍按整回合
累计，那是 `web-and-admin.md`「Live Reasoning Row Contract」写明的口径。
"""

from __future__ import annotations

import asyncio

from g3ku.runtime.reasoning_clock import ReasoningSecondsClock
from g3ku.runtime.session_agent import RuntimeAgentSession

_LANE_METHODS = (
    "_handle_assistant_reasoning_delta",
    "_flush_assistant_reasoning",
    "_begin_assistant_reasoning_segment",
    "_reasoning_seconds",
    "_reasoning_estimate",
    "_reasoning_running_totals",
    "_reasoning_total_seconds",
    "_reasoning_touch_turn",
    "_reasoning_close_segment",
    "_assistant_reasoning_snapshot",
    "_assistant_reasoning_turn_summary",
    "_schedule_assistant_reasoning_flush",
    "_delayed_assistant_reasoning_flush",
    "_reset_assistant_reasoning_state",
    "_cancel_assistant_reasoning_flush_task",
)


class _ReasoningLaneStub:
    """真实思维链车道方法挂在一个最小宿主上：不建会话、不读配置、不落盘。"""

    def __init__(self) -> None:
        self.turn = "turn-1"
        self.frames: list[dict[str, object]] = []
        self._reasoning_text = ""
        self._reasoning_pending = ""
        self._reasoning_seq = 0
        self._reasoning_attempt = 0
        self._reasoning_clock = ReasoningSecondsClock()
        self._reasoning_last_emit_monotonic = 0.0
        self._reasoning_flush_task = None
        self._reasoning_turn_key = ""
        self._reasoning_chars_total = 0
        self._reasoning_est_tokens_total = 0

    async def _emit(self, kind: str, **payload: object) -> None:
        if kind == "assistant_reasoning_delta":
            self.frames.append(payload)

    def _current_turn_id(self, *_args: object) -> str:
        return self.turn

    def _internal_prompt_source(self, *_args: object, **_kwargs: object) -> None:
        return None


for _name in _LANE_METHODS:
    setattr(_ReasoningLaneStub, _name, getattr(RuntimeAgentSession, _name))


def _open_turn(stub: _ReasoningLaneStub, turn: str) -> None:
    """一轮的开始：真实链路里由 `_reset_assistant_stream_state()` 清道，再开首个 attempt。"""
    stub.turn = turn
    stub._reset_assistant_reasoning_state()
    stub._begin_assistant_reasoning_segment()


async def _stream_reasoning(stub: _ReasoningLaneStub, text: str) -> dict[str, object]:
    # 每帧都强制立即 flush（把上次发射时刻清零即命中那条闸门），读数取最后一帧。
    stub._reasoning_last_emit_monotonic = 0.0
    await stub._handle_assistant_reasoning_delta(text)
    return stub.frames[-1]


def test_reasoning_totals_restart_at_turn_boundary() -> None:
    async def run() -> None:
        stub = _ReasoningLaneStub()

        _open_turn(stub, "turn-1")
        first = await _stream_reasoning(stub, "思" * 400)
        assert first["chars"] == 400
        first_est = int(first["est_tokens"] or 0)
        assert first_est > 0
        # 回合收尾：真实链路由 `_persist_turn` 取这份读数写转录行，同时把这一段并进回合累计。
        assert stub._assistant_reasoning_turn_summary()["chars"] == 400

        _open_turn(stub, "turn-2")
        second = await _stream_reasoning(stub, "考" * 100)
        assert second["chars"] == 100, second
        assert int(second["est_tokens"] or 0) < first_est, second

        summary = stub._assistant_reasoning_turn_summary()
        assert summary["chars"] == 100, summary
        assert int(summary["est_tokens"] or 0) < first_est, summary

    asyncio.run(run())


def test_reasoning_totals_stay_cumulative_within_one_turn() -> None:
    async def run() -> None:
        stub = _ReasoningLaneStub()

        _open_turn(stub, "turn-1")
        await _stream_reasoning(stub, "思" * 400)

        # 同回合的第二跳（工具轮、换链降级都会重开 attempt）：文本整段重来，读数继续累。
        stub._begin_assistant_reasoning_segment()
        frame = await _stream_reasoning(stub, "考" * 50)
        assert frame["chars"] == 450, frame

        summary = stub._assistant_reasoning_turn_summary()
        assert summary["chars"] == 450, summary

    asyncio.run(run())


def test_turn_boundary_ignores_previous_turn_open_segment() -> None:
    """换轮时上一轮那段还挂着表：收口进它自己的回合，不算进新一轮的开头。"""

    async def run() -> None:
        stub = _ReasoningLaneStub()

        _open_turn(stub, "turn-1")
        await _stream_reasoning(stub, "思" * 400)

        # 未经 `_reset_assistant_reasoning_state()` 直接换 turn_id：车道必须自己划清边界。
        stub.turn = "turn-2"
        stub._begin_assistant_reasoning_segment()
        frame = await _stream_reasoning(stub, "考" * 100)
        assert frame["chars"] == 100, frame

    asyncio.run(run())
