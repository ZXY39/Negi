from __future__ import annotations

import g3ku.runtime.frontdoor.token_preflight_compaction as token_preflight_compaction_module
from g3ku.runtime.frontdoor._ceo_runtime_ops import (
    _estimate_frontdoor_provider_request_tokens,
)
from g3ku.runtime.frontdoor.token_preflight_compaction import (
    RuntimeSendTokenPreflightThresholds,
    build_runtime_observed_input_truth,
    compute_runtime_send_token_preflight_thresholds,
    should_trigger_runtime_token_compression,
)


def test_frontdoor_token_preflight_compaction_public_boundary_stays_focused() -> None:
    assert hasattr(token_preflight_compaction_module, "FrontdoorTokenPreflightResult")
    assert hasattr(token_preflight_compaction_module, "build_runtime_observed_input_truth")
    assert hasattr(token_preflight_compaction_module, "compute_runtime_send_token_preflight_thresholds")
    assert hasattr(token_preflight_compaction_module, "should_trigger_runtime_token_compression")
    assert hasattr(token_preflight_compaction_module, "build_runtime_send_token_preflight_snapshot")
    assert hasattr(token_preflight_compaction_module, "build_frontdoor_token_preflight_policy") is False
    assert hasattr(token_preflight_compaction_module, "estimate_frontdoor_provider_request_tokens") is False
    assert hasattr(token_preflight_compaction_module, "should_run_frontdoor_token_preflight") is False
    assert hasattr(token_preflight_compaction_module, "compact_frontdoor_history_zone") is False
    assert hasattr(token_preflight_compaction_module, "FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS") is False


def test_frontdoor_token_preflight_thresholds_use_fixed_context_window_ratios() -> None:
    assert token_preflight_compaction_module.RUNTIME_SEND_TOKEN_COMPRESSION_TRIGGER_RATIO == 0.80
    assert token_preflight_compaction_module.RUNTIME_SEND_TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO == 0.95

    thresholds = compute_runtime_send_token_preflight_thresholds(
        context_window_tokens=200_000,
    )

    assert isinstance(thresholds, RuntimeSendTokenPreflightThresholds)
    assert thresholds.context_window_tokens == 200_000
    assert thresholds.trigger_tokens == 160_000
    assert thresholds.effective_trigger_tokens == 152_000
    assert should_trigger_runtime_token_compression(
        estimated_total_tokens=151_999,
        thresholds=thresholds,
    ) is False
    assert should_trigger_runtime_token_compression(
        estimated_total_tokens=152_000,
        thresholds=thresholds,
    ) is True

    truth = build_runtime_observed_input_truth(
        usage={"input_tokens": 12, "cache_hit_tokens": 3},
        provider_model="demo:model",
        actual_request_hash="req",
        source="provider_usage",
    )
    assert truth.effective_input_tokens == 15


def test_frontdoor_token_preflight_estimates_large_provider_payload_without_summary_truncation() -> None:
    huge_text = "A" * 120_000
    estimated = _estimate_frontdoor_provider_request_tokens(
        provider_request_body={
            "model": "gpt-5.2",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": huge_text,
                        }
                    ],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "exec",
                    "description": "Run a command",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "parallel_tool_calls": True,
        },
        request_messages=[],
        tool_schemas=[],
    )

    assert estimated >= 20_000
