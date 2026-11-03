"""发送层短压（window_shortfall）落压缩区分线合同用例。

预检车道在摘要落地时记下 `(turn_id, boundary_turn_seq)` 并 announce；发送层在链前进
到更窄成员时另压一次，过去只写诊断、不落线，转录里没有任何压缩痕迹。这里固定三条
行为：回调自己记 turn/boundary 并 announce；同一回合同一落点只 announce 一次；同一
回合出现新落点时再 announce 一条。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext


def _graph_state(model_refs: list[str]) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "older-1"},
            {"role": "assistant", "content": "older-2"},
            {"role": "user", "content": "recent-1"},
            {"role": "assistant", "content": "recent-2"},
        ],
        "model_refs": list(model_refs),
        "parallel_enabled": False,
        "prompt_cache_key": "cache-key",
        "iteration": 0,
        "max_iterations": 3,
        "session_key": "web:shared",
        "tool_names": [],
        "provider_tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "turn_overlay_text": "",
        "repair_overlay_text": None,
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},
    }


class _RecordingSession(SimpleNamespace):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.announces: list[tuple[str, int]] = []

    async def announce_context_compression_boundary(
        self,
        *,
        turn_id: str,
        boundary_turn_seq: int,
    ) -> None:
        self.announces.append((str(turn_id), int(boundary_turn_seq)))


def _session(*, turn_id: str = "turn-x", turn_count: int = 3) -> _RecordingSession:
    return _RecordingSession(
        state=SimpleNamespace(session_key="web:shared"),
        _compression_state={},
        _semantic_context_state={},
        _emit_state_snapshot=lambda: None,
        _active_turn_id=turn_id,
        _frontdoor_turns=[{"seq": index} for index in range(1, turn_count + 1)],
    )


def _runtime(session: _RecordingSession) -> SimpleNamespace:
    return SimpleNamespace(
        context=CeoRuntimeContext(
            loop=None,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )


def _build_graph_runner(monkeypatch: pytest.MonkeyPatch) -> CreateAgentCeoFrontDoorRunner:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [], raising=False)
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "head",
            "provider_model": "openai:glm-5.2-2",
            "context_window_tokens": 240_000,
        },
        raising=False,
    )
    # 体量压在触发阈值以下：本用例只走发送层回调，不让预检车道先压一次。
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1_000, raising=False)
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _m: {"ok": True}, raising=False)
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {}, raising=False)
    return runner


async def _capture_shortfall_callback(
    monkeypatch: pytest.MonkeyPatch,
    session: _RecordingSession,
):
    runner = _build_graph_runner(monkeypatch)
    captured: dict = {}

    async def _call_model_with_tools(**kwargs):
        captured.update(kwargs)
        return {"content": "主请求回复", "finish_reason": "stop", "tool_calls": []}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    await runner._graph_call_model(_graph_state(["head"]), runtime=_runtime(session))
    callback = captured.get("on_window_shortfall_compressed")
    assert callback is not None
    return callback


def _payload() -> dict:
    return {
        "messages": [{"role": "assistant", "content": "压缩后的摘要"}],
        "estimated_tokens": 30_260,
        "required_before": 242_769,
        "target_window_tokens": 240_000,
        "model_ref": "glm-5.2-2",
        "compression_mode": "llm",
        "chunk_count": 1,
        "chunk_failed_count": 0,
    }


@pytest.mark.asyncio
async def test_shortfall_callback_stamps_turn_and_announces_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(turn_id="turn-x", turn_count=3)
    callback = await _capture_shortfall_callback(monkeypatch, session)

    notified = callback(_payload())
    assert inspect.isawaitable(notified)
    await notified

    assert session._frontdoor_compressed_turn_id == "turn-x"
    assert session._frontdoor_compression_boundary_turn_seq == 3
    assert session.announces == [("turn-x", 3)]
    assert session._frontdoor_token_compression_applied_turn is True


@pytest.mark.asyncio
async def test_shortfall_callback_same_turn_same_boundary_announces_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(turn_id="turn-x", turn_count=3)
    callback = await _capture_shortfall_callback(monkeypatch, session)

    await callback(_payload())
    await callback(_payload())

    assert session.announces == [("turn-x", 3)]


@pytest.mark.asyncio
async def test_shortfall_callback_later_boundary_announces_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(turn_id="turn-x", turn_count=3)
    callback = await _capture_shortfall_callback(monkeypatch, session)

    await callback(_payload())
    session._frontdoor_turns = [*session._frontdoor_turns, {"seq": 4}]
    await callback(_payload())

    assert session.announces == [("turn-x", 3), ("turn-x", 4)]
    assert session._frontdoor_compression_boundary_turn_seq == 4
