from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.cancellation import ToolCancellationToken
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext


@pytest.mark.asyncio
async def test_graph_call_model_raises_when_request_already_exceeds_context_window_before_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 32000,
        },
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_estimate_frontdoor_send_total_tokens",
        lambda **_: 33001,
        raising=False,
    )

    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "hello"},
        ],
        "model_refs": ["ceo_primary"],
        "parallel_enabled": False,
        "prompt_cache_key": "cache-key",
        "iteration": 0,
        "max_iterations": 3,
        "session_key": "web:shared",
        "tool_names": [],
        "provider_tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "hydrated_tool_names": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "turn_overlay_text": "",
        "repair_overlay_text": None,
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},
    }
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=None, session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared")), session_key="web:shared", on_progress=None)
    )

    with pytest.raises(RuntimeError):
        await runner._graph_call_model(state, runtime=runtime)


@pytest.mark.asyncio
async def test_graph_call_model_runs_llm_token_compression_before_main_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    captured_calls: list[list[dict[str, object]]] = []

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 32000,
        },
        raising=False,
    )

    def _estimate(**kwargs):
        rendered = "\n".join(str(item.get("content") or "") for item in list(kwargs.get("request_messages") or []))
        return 26000 if "[G3KU_TOKEN_COMPACT_V2]" not in rendered else 18000

    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", _estimate, raising=False)

    async def _call_model_with_tools(**kwargs):
        messages = list(kwargs.get("messages") or [])
        captured_calls.append(messages)
        if len(captured_calls) == 1:
            return {"content": "[压缩后的较早历史摘要]"}
        return {"content": "主请求回复"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(
        runner,
        "_model_response_view",
        lambda message: SimpleNamespace(
            content=message.get("content", ""),
            tool_calls=[],
            provider_request_meta={},
            provider_request_body={},
        ),
    )
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _frontdoor_stage_state={"active_stage_id": "", "transition_required": False, "stages": []},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={},
        _semantic_context_state={},
        _frontdoor_hydrated_tool_names=[],
        _emit_state_snapshot=lambda: None,
    )
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=None, session=session, session_key="web:shared", on_progress=None)
    )
    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "older-1"},
            {"role": "assistant", "content": "older-2"},
            {"role": "user", "content": "older-3"},
            {"role": "assistant", "content": "older-4"},
            {"role": "user", "content": "recent-1"},
            {"role": "assistant", "content": "recent-2"},
            {"role": "user", "content": "recent-3"},
            {"role": "assistant", "content": "recent-4"},
        ],
        "model_refs": ["ceo_primary"],
        "parallel_enabled": False,
        "prompt_cache_key": "cache-key",
        "iteration": 0,
        "max_iterations": 3,
        "session_key": "web:shared",
        "tool_names": [],
        "provider_tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "hydrated_tool_names": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "turn_overlay_text": "",
        "repair_overlay_text": None,
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},
    }

    result = await runner._graph_call_model(state, runtime=runtime)

    assert len(captured_calls) == 2
    assert "[G3KU_TOKEN_COMPACT_V2]" in "\n".join(str(item.get("content") or "") for item in captured_calls[1])
    assert result["frontdoor_history_shrink_reason"] == "token_compression"


@pytest.mark.asyncio
async def test_graph_call_model_discards_late_compression_result_after_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    captured_calls: list[list[dict[str, object]]] = []

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 32000,
        },
        raising=False,
    )

    def _estimate(**kwargs):
        rendered = "\n".join(str(item.get("content") or "") for item in list(kwargs.get("request_messages") or []))
        return 26000 if "[G3KU_TOKEN_COMPACT_V2]" not in rendered else 18000

    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", _estimate, raising=False)

    cancel_token = ToolCancellationToken(session_key="web:shared")

    class _CompressionAwareSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(session_key="web:shared")
            self._frontdoor_stage_state = {"active_stage_id": "", "transition_required": False, "stages": []}
            self._frontdoor_canonical_context = {"active_stage_id": "", "transition_required": False, "stages": []}
            self._compression_state = {}
            self._semantic_context_state = {}
            self._frontdoor_hydrated_tool_names = []
            self._active_cancel_token = cancel_token
            self._active_frontdoor_compression_generation = None
            self._cancelled_frontdoor_compression_generations: set[int] = set()

        def _emit_state_snapshot(self):
            return None

        def _begin_frontdoor_compression_generation(self) -> int:
            next_generation = int(self._active_frontdoor_compression_generation or 0) + 1
            self._active_frontdoor_compression_generation = next_generation
            return next_generation

        def _finish_frontdoor_compression_generation(self, generation_id: int) -> None:
            if self._active_frontdoor_compression_generation == generation_id:
                self._active_frontdoor_compression_generation = None
            self._cancelled_frontdoor_compression_generations.discard(generation_id)

        def _cancel_active_frontdoor_compression_generation(self) -> None:
            generation_id = self._active_frontdoor_compression_generation
            if generation_id is not None:
                self._cancelled_frontdoor_compression_generations.add(generation_id)

        def _is_frontdoor_compression_generation_cancelled(self, generation_id: int) -> bool:
            return generation_id in self._cancelled_frontdoor_compression_generations

    session = _CompressionAwareSession()

    async def _call_model_with_tools(**kwargs):
        messages = list(kwargs.get("messages") or [])
        captured_calls.append(messages)
        session._cancel_active_frontdoor_compression_generation()
        cancel_token.cancel(reason="user_pause")
        return {"content": "[late-compression-result]"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(
        runner,
        "_model_response_view",
        lambda message: SimpleNamespace(
            content=message.get("content", ""),
            tool_calls=[],
            provider_request_meta={},
            provider_request_body={},
        ),
    )
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=None, session=session, session_key="web:shared", on_progress=None)
    )
    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "older-1"},
            {"role": "assistant", "content": "older-2"},
            {"role": "user", "content": "older-3"},
            {"role": "assistant", "content": "older-4"},
            {"role": "user", "content": "recent-1"},
            {"role": "assistant", "content": "recent-2"},
            {"role": "user", "content": "recent-3"},
            {"role": "assistant", "content": "recent-4"},
        ],
        "model_refs": ["ceo_primary"],
        "parallel_enabled": False,
        "prompt_cache_key": "cache-key",
        "iteration": 0,
        "max_iterations": 3,
        "session_key": "web:shared",
        "tool_names": [],
        "provider_tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "hydrated_tool_names": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "turn_overlay_text": "",
        "repair_overlay_text": None,
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},
    }

    with pytest.raises(asyncio.CancelledError):
        await runner._graph_call_model(state, runtime=runtime)

    assert len(captured_calls) == 1
