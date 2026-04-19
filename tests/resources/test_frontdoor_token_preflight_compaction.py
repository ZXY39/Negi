from __future__ import annotations

import g3ku.runtime.frontdoor.token_preflight_compaction as token_preflight_compaction_module
from g3ku.runtime.frontdoor._ceo_runtime_ops import (
    _FrontdoorTokenPreflightPolicy,
    _build_frontdoor_token_preflight_policy,
    _estimate_frontdoor_provider_request_tokens,
    _should_run_frontdoor_token_preflight,
)
from g3ku.runtime.frontdoor.token_preflight_compaction import (
    FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS,
    build_runtime_observed_input_truth,
    compact_frontdoor_history_zone,
)


def test_frontdoor_token_preflight_compaction_public_boundary_stays_focused() -> None:
    assert hasattr(token_preflight_compaction_module, "compact_frontdoor_history_zone")
    assert hasattr(token_preflight_compaction_module, "build_runtime_observed_input_truth")
    assert hasattr(token_preflight_compaction_module, "FrontdoorTokenPreflightResult")
    assert hasattr(token_preflight_compaction_module, "build_frontdoor_token_preflight_policy") is False
    assert hasattr(token_preflight_compaction_module, "estimate_frontdoor_provider_request_tokens") is False
    assert hasattr(token_preflight_compaction_module, "should_run_frontdoor_token_preflight") is False


def test_frontdoor_legacy_token_preflight_uses_fixed_max_context_and_ratio() -> None:
    policy = _build_frontdoor_token_preflight_policy(
        max_context_tokens=200_000,
        trigger_ratio=0.10,
    )

    assert isinstance(policy, _FrontdoorTokenPreflightPolicy)
    assert policy.max_context_tokens == 200_000
    assert policy.trigger_tokens == 20_000
    assert FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS == 5000
    assert _should_run_frontdoor_token_preflight(final_request_tokens=19_999, policy=policy) is False
    assert _should_run_frontdoor_token_preflight(final_request_tokens=20_000, policy=policy) is True

    truth = build_runtime_observed_input_truth(
        usage={"input_tokens": 12, "cache_hit_tokens": 3},
        provider_model="demo:model",
        actual_request_hash="req",
        source="provider_usage",
    )
    assert truth.effective_input_tokens == 15


def test_frontdoor_compaction_keeps_active_stage_and_latest_three_completed_stages() -> None:
    stages = [
        {"stage_id": f"stage-{index}", "stage_index": index, "status": "completed", "stage_goal": f"goal-{index}"}
        for index in range(1, 6)
    ] + [
        {"stage_id": "stage-6", "stage_index": 6, "status": "active", "stage_goal": "goal-6"}
    ]

    result = compact_frontdoor_history_zone(
        raw_history_messages=[{"role": "user", "content": f"user-{index}"} for index in range(6)],
        frontdoor_stage_state={"active_stage_id": "stage-6", "transition_required": False, "stages": stages},
        max_compacted_tokens=5000,
    )

    assert result.retained_completed_stage_ids == ["stage-3", "stage-4", "stage-5"]
    assert result.active_stage_id == "stage-6"
    assert result.compacted_block_tokens <= 5000


def test_frontdoor_legacy_token_preflight_estimates_large_provider_payload_without_summary_truncation() -> None:
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
