from __future__ import annotations

from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.semantic_context_summary import (
    HERMES_MIN_CONTEXT_FLOOR,
    build_global_summary_thresholds,
    default_semantic_context_state,
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
