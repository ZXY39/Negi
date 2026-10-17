"""思考计时只算思考发生的那段：工具执行与正文流式期间必须停表。

实盘症状：会话气泡上那行「思考 Ns」在工具跑的时候一直涨。车道只在**下一次 provider
attempt 起点**（`_begin_assistant_reasoning_segment`）或**回合收尾**
（`_assistant_reasoning_turn_summary`）才收口，工具执行整段时间都落在没停的表上；而每一次
状态快照都重算 `_reasoning_running_totals()`，前端据此重画，读数就随快照一帧帧变大。

合同要保住的另一半：同一回合内多跳仍按整回合累计（`web-and-admin.md`
「Live Reasoning Row Contract」），停表不等于把已经想过的时间抹掉。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext
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
    "_reset_assistant_reasoning_state",
    "_handle_assistant_text_delta",
    "_flush_assistant_text_delta",
    "_begin_assistant_text_segment",
)

_TEXT_LANE_ATTRS = {
    "_assistant_stream_flush_task": None,
    "_assistant_stream_pending_text": "",
    "_assistant_stream_seq": 0,
    "_assistant_stream_last_emit_monotonic": 0.0,
    "_assistant_segment_open": False,
}

_REASONING_LANE_ATTRS = {
    "_reasoning_text": "",
    "_reasoning_pending": "",
    "_reasoning_seq": 0,
    "_reasoning_attempt": 0,
    "_reasoning_started_monotonic": 0.0,
    "_reasoning_last_emit_monotonic": 0.0,
    "_reasoning_flush_task": None,
    "_reasoning_turn_key": "",
    "_reasoning_turn_seconds": 0.0,
    "_reasoning_chars_total": 0,
    "_reasoning_est_tokens_total": 0,
}


class _ReasoningTimerStub:
    """思维链 + 正文两条实时车道挂在最小宿主上：不建会话、不读配置、不落盘。"""

    def __init__(self) -> None:
        self.turn = "turn-1"
        self.frames: list[dict[str, object]] = []
        self._state = SimpleNamespace(latest_message="")
        for name, value in {**_TEXT_LANE_ATTRS, **_REASONING_LANE_ATTRS}.items():
            setattr(self, name, value)

    async def _emit(self, kind: str, **payload: object) -> None:
        self.frames.append({"kind": kind, **payload})

    def _current_turn_id(self, *_args: object) -> str:
        return self.turn

    def _internal_prompt_source(self, *_args: object, **_kwargs: object) -> None:
        return None

    def _sync_persisted_inflight_turn(self) -> None:
        return None

    def _schedule_assistant_stream_flush(self) -> None:
        return None

    def _cancel_assistant_reasoning_flush_task(self) -> None:
        return None


for _name in _LANE_METHODS:
    setattr(_ReasoningTimerStub, _name, getattr(RuntimeAgentSession, _name))


async def _open_hop(stub: _ReasoningTimerStub, *, thought_for_seconds: float) -> None:
    """一跳的开始：开思考段、想了一会儿（有思考分片才会有那一行），然后等正文。"""

    stub._begin_assistant_text_segment()
    stub._begin_assistant_reasoning_segment()
    stub._reasoning_started_monotonic = (
        asyncio.get_running_loop().time() - thought_for_seconds
    )
    await stub._handle_assistant_reasoning_delta("思" * 40)


def _thinking_line_seconds(stub: _ReasoningTimerStub) -> float:
    # 界面上那一行读的就是这份快照标量。
    return float(stub._assistant_reasoning_snapshot()["seconds"])


def test_visible_text_stops_the_reasoning_stopwatch() -> None:
    async def run() -> None:
        stub = _ReasoningTimerStub()
        await _open_hop(stub, thought_for_seconds=42.0)
        assert _thinking_line_seconds(stub) == pytest.approx(42.0, abs=0.6)

        await stub._handle_assistant_text_delta("正文第一帧")

        # 停表而不是清零：已经想过的 42 秒留在回合累计里，往后不再增长。
        assert stub._reasoning_started_monotonic == 0.0
        assert _thinking_line_seconds(stub) == pytest.approx(42.0, abs=0.6)

    asyncio.run(run())


def test_tool_execution_window_does_not_add_to_thinking_seconds() -> None:
    async def run() -> None:
        stub = _ReasoningTimerStub()
        await _open_hop(stub, thought_for_seconds=10.0)
        await stub._handle_assistant_text_delta("正文第一帧")
        frozen = _thinking_line_seconds(stub)

        # 工具跑了 30 秒：这段时间里没有任何思考分片，只有状态快照在重画那一行。
        stub._reasoning_started_monotonic -= 30.0
        assert _thinking_line_seconds(stub) == pytest.approx(frozen, abs=0.6)

        # 下一跳真的又想了一会儿：新的一段接上，回合累计继续涨。
        stub._begin_assistant_reasoning_segment()
        stub._reasoning_started_monotonic = asyncio.get_running_loop().time() - 5.0
        assert _thinking_line_seconds(stub) == pytest.approx(frozen + 5.0, abs=0.6)

    asyncio.run(run())


def test_turn_summary_keeps_only_thinking_time_across_hops() -> None:
    async def run() -> None:
        stub = _ReasoningTimerStub()
        await _open_hop(stub, thought_for_seconds=8.0)
        await stub._handle_assistant_text_delta("第一段正文")
        stub._reasoning_started_monotonic -= 30.0  # 工具窗口
        await _open_hop(stub, thought_for_seconds=4.0)
        await stub._handle_assistant_text_delta("第二段正文")

        summary = stub._assistant_reasoning_turn_summary()
        assert float(summary["seconds"]) == pytest.approx(12.0, abs=1.2), summary

    asyncio.run(run())


def test_frontdoor_closes_reasoning_segment_when_the_model_call_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """前门这一跳的模型流结束就收口，工具在 `_graph_call_model` 之后才跑。"""

    async def run() -> None:
        runner = CeoFrontDoorRunner(loop=SimpleNamespace())
        monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **kwargs: [])
        monkeypatch.setattr(
            runner,
            "_selected_tool_schemas",
            lambda tool_names: [{"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}}],
        )
        monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
        monkeypatch.setattr(
            runner,
            "_resolve_frontdoor_send_model_context_window",
            lambda **_: {"model_key": "ceo", "provider_model": "openai:test", "context_window_tokens": 32000},
            raising=False,
        )
        monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1200, raising=False)

        events: list[str] = []

        async def _call_model_with_tools(**kwargs):
            kwargs["on_reasoning_segment"]()
            events.append("model_call_returned")
            return {"content": "reply", "finish_reason": "stop"}

        monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
        session = SimpleNamespace(
            state=SimpleNamespace(session_key="web:test"),
            _active_turn_id="turn-1",
            _frontdoor_actual_request_history=[],
            _current_turn_id=lambda prompt=None: "turn-1",
            _begin_assistant_reasoning_segment=lambda: events.append("segment_begin"),
            _handle_assistant_reasoning_delta=None,
            _handle_assistant_text_delta=None,
            _begin_assistant_text_segment=lambda: events.append("text_segment_begin"),
            _reasoning_close_segment=lambda: events.append("segment_close"),
        )

        await runner._graph_call_model(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "stable_messages": [{"role": "user", "content": "hello"}],
                "dynamic_appendix_messages": [],
                "user_input": {"content": "hello", "metadata": {}},
                "tool_names": ["submit_next_stage"],
                "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
                "model_refs": ["openai:test"],
                "parallel_enabled": False,
                "iteration": 0,
                "max_iterations": 4,
                "session_key": "web:test",
            },
            runtime=SimpleNamespace(
                context=CeoRuntimeContext(
                    loop=None,
                    session=session,
                    session_key="web:test",
                    on_progress=None,
                )
            ),
        )

        assert events.index("segment_close") > events.index("model_call_returned"), events
        assert events.count("segment_close") == 1, events

    asyncio.run(run())
