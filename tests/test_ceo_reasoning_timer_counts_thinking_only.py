"""思考表的终点是最后一个思维块，不是该跳的返回或正文首帧。

实测时间线（三跳回合，工具执行已由 `9fe2d3e10` 排除在该跳返回之后）：读数 1.200s 里真思考
只有 0.438s——多出来的两笔是「思维之后模型继续写工具调用参数」（写文件时那是一整个文件
正文，几十秒级）与「该跳根本没思考时的排队/预填充窗口」。操作员读起来就是"思考时间把工具
那段时间也算进去了"。

口径（操作者裁定）：表从该跳请求起点走到**最后一个思维块到达**为止；没有思维块的跳贡献 0。
正文首帧与该跳返回仍各收口一次（收的是段落与内容），但读数不越过末块时刻。
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

# 思维之后模型还在吐工具调用参数 / 排队后才有首帧：这两段都不算思考。
_POST_THOUGHT_STREAM = 0.30
_QUEUE_BEFORE_THOUGHT = 0.05
_CHUNK_STEP = 0.03


class _LaneStub:
    def __init__(self) -> None:
        self.turn = "turn-1"
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
        pass

    def _current_turn_id(self, *_args: object) -> str:
        return self.turn

    def _internal_prompt_source(self, *_args: object, **_kwargs: object) -> None:
        return None


for _name in _LANE_METHODS:
    setattr(_LaneStub, _name, getattr(RuntimeAgentSession, _name))


async def _stream_hop(stub: _LaneStub, chunks: int) -> None:
    stub._reset_assistant_reasoning_state()
    stub._begin_assistant_reasoning_segment()
    await asyncio.sleep(_QUEUE_BEFORE_THOUGHT)
    for index in range(chunks):
        stub._reasoning_last_emit_monotonic = 0.0
        await stub._handle_assistant_reasoning_delta(f"想{index}" * 12)
        await asyncio.sleep(_CHUNK_STEP)


def test_tool_argument_stream_after_the_last_thought_is_not_thinking_time() -> None:
    async def run() -> None:
        stub = _LaneStub()
        await _stream_hop(stub, chunks=6)
        thinking_only = stub._reasoning_seconds()
        assert thinking_only > 0.0

        # 该跳想完之后开始吐工具调用参数，参数流完这跳才返回；收口点在生产链路是返回处。
        await asyncio.sleep(_POST_THOUGHT_STREAM)
        stub._reasoning_close_segment()
        summary = stub._assistant_reasoning_turn_summary()

        assert float(summary["seconds"]) == round(thinking_only, 1), (summary, thinking_only)
        assert float(summary["seconds"]) < thinking_only + _POST_THOUGHT_STREAM / 2

    asyncio.run(run())


def test_hop_without_any_thought_contributes_no_seconds() -> None:
    async def run() -> None:
        stub = _LaneStub()
        await _stream_hop(stub, chunks=0)

        await asyncio.sleep(_POST_THOUGHT_STREAM)
        stub._reasoning_close_segment()

        assert stub._reasoning_seconds() == 0.0
        assert stub._assistant_reasoning_turn_summary() == {}

    asyncio.run(run())


def test_multi_hop_turn_sums_per_hop_thought_windows() -> None:
    async def run() -> None:
        stub = _LaneStub()

        await _stream_hop(stub, chunks=4)
        first = stub._reasoning_seconds()
        stub._reasoning_close_segment()

        # 两次思维之间真实工具执行的那段（生产里发生在该跳返回之后、下一跳起点之前）。
        await asyncio.sleep(_POST_THOUGHT_STREAM)

        await _stream_hop(stub, chunks=4)
        second = stub._reasoning_seconds()
        summary = stub._assistant_reasoning_turn_summary()

        assert first > 0.0 and second > 0.0
        assert abs(float(summary["seconds"]) - (first + second)) < 0.15, (summary, first, second)
        assert float(summary["seconds"]) < first + second + _POST_THOUGHT_STREAM / 2

    asyncio.run(run())
