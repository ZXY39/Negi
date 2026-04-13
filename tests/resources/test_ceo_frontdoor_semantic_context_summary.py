from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.semantic_context_summary import (
    HERMES_MIN_CONTEXT_FLOOR,
    build_global_summary_thresholds,
    default_semantic_context_state,
    summarize_global_context_model_first,
)


def test_build_global_summary_thresholds_aligns_with_hermes_defaults() -> None:
    thresholds = build_global_summary_thresholds(
        context_window_tokens=200_000,
        compressed_zone_tokens=30_000,
    )

    assert HERMES_MIN_CONTEXT_FLOOR == 64_000
    assert thresholds["trigger_tokens"] == 100_000
    assert thresholds["pressure_warn_tokens"] == 170_000
    assert thresholds["force_refresh_tokens"] == 190_000
    assert thresholds["max_output_tokens"] == 10_000
    assert thresholds["target_tokens"] == 6_000


def test_build_global_summary_thresholds_respects_floor_and_ceiling() -> None:
    thresholds = build_global_summary_thresholds(
        context_window_tokens=32_000,
        compressed_zone_tokens=2_000,
    )

    assert thresholds["trigger_tokens"] == 64_000
    assert thresholds["max_output_tokens"] == 1_600
    assert thresholds["target_tokens"] == 2_000


def test_default_semantic_context_state_is_empty() -> None:
    assert default_semantic_context_state() == {
        "summary_text": "",
        "coverage_history_source": "",
        "coverage_message_index": -1,
        "coverage_stage_index": 0,
        "needs_refresh": False,
        "failure_cooldown_until": "",
        "updated_at": "",
    }


def test_initial_persistent_state_tracks_semantic_context_state() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["semantic_context_state"] == default_semantic_context_state()


def test_memory_assembly_config_exposes_global_summary_defaults() -> None:
    config = MemoryAssemblyConfig()

    assert config.frontdoor_global_summary_trigger_ratio == 0.50
    assert config.frontdoor_global_summary_target_ratio == 0.20
    assert config.frontdoor_global_summary_min_output_tokens == 2000
    assert config.frontdoor_global_summary_max_output_ratio == 0.05
    assert config.frontdoor_global_summary_max_output_tokens_ceiling == 12000
    assert config.frontdoor_global_summary_pressure_warn_ratio == 0.85
    assert config.frontdoor_global_summary_force_refresh_ratio == 0.95
    assert config.frontdoor_global_summary_min_delta_tokens == 2000
    assert config.frontdoor_global_summary_failure_cooldown_seconds == 600


@pytest.mark.asyncio
async def test_summarize_global_context_model_first_returns_structured_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeModel:
        async def ainvoke(self, messages):
            _ = messages
            return SimpleNamespace(content="## Goals\nContinue the current task")

    monkeypatch.setattr(
        "g3ku.config.live_runtime.get_runtime_config",
        lambda force=False: (SimpleNamespace(), 1, False),
    )
    monkeypatch.setattr(
        "g3ku.providers.chatmodels.build_chat_model",
        lambda config, **kwargs: _FakeModel(),
    )

    result = await summarize_global_context_model_first(
        [{"role": "assistant", "content": "Older context"}],
        max_output_tokens=128,
    )

    assert isinstance(result, dict)
    assert result["summary_text"] == "## Goals\nContinue the current task"
    assert result["used_fallback"] is False
    assert result["failed"] is False
    assert result["error_text"] == ""


@pytest.mark.asyncio
async def test_summarize_global_context_model_first_returns_structured_fallback_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "g3ku.config.live_runtime.get_runtime_config",
        lambda force=False: (SimpleNamespace(), 1, False),
    )
    monkeypatch.setattr(
        "g3ku.providers.chatmodels.build_chat_model",
        lambda config, **kwargs: (_ for _ in ()).throw(RuntimeError("summary model unavailable")),
    )

    result = await summarize_global_context_model_first(
        [{"role": "assistant", "content": "Older context that still matters"}],
        max_output_tokens=64,
    )

    assert isinstance(result, dict)
    assert result["used_fallback"] is True
    assert result["failed"] is True
    assert "summary model unavailable" in result["error_text"]
    assert str(result["summary_text"] or "").strip()
