from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from g3ku.runtime.message_token_estimation import estimate_message_tokens
from main.runtime.send_token_preflight import (
    RUNTIME_SEND_TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO,
    RUNTIME_SEND_TOKEN_COMPRESSION_TRIGGER_RATIO,
    RuntimeHybridSendTokenEstimate,
    RuntimeObservedInputTruth,
    RuntimeSendTokenPreflightSnapshot,
    RuntimeSendTokenPreflightThresholds,
    build_runtime_hybrid_send_token_estimate,
    build_runtime_observed_input_truth,
    build_runtime_send_token_preflight_snapshot,
    compute_runtime_send_token_preflight_thresholds,
    estimate_runtime_provider_request_preview_tokens,
    should_trigger_runtime_token_compression,
)

FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS = 5000


@dataclass(frozen=True, slots=True)
class FrontdoorTokenPreflightResult:
    request_messages: list[dict[str, Any]]
    final_request_tokens: int
    history_shrink_reason: str
    diagnostics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FrontdoorCompactedHistory:
    active_stage_id: str
    retained_completed_stage_ids: list[str]
    compacted_block: dict[str, Any]
    compacted_block_tokens: int


def compact_frontdoor_history_zone(
    *,
    raw_history_messages: list[dict[str, Any]],
    frontdoor_stage_state: dict[str, Any],
    max_compacted_tokens: int = FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS,
) -> FrontdoorCompactedHistory:
    stages = list(frontdoor_stage_state.get("stages") or [])
    active_stage_id = str(frontdoor_stage_state.get("active_stage_id") or "").strip()
    completed = [
        stage
        for stage in stages
        if str(stage.get("status") or "").strip().lower() == "completed"
    ]
    retained_completed = completed[-3:]
    retained_completed_stage_ids = [
        str(stage.get("stage_id") or "").strip()
        for stage in retained_completed
        if str(stage.get("stage_id") or "").strip()
    ]
    compacted_payload = {
        "kind": "frontdoor_token_compaction",
        "older_completed_stage_ids": [
            str(stage.get("stage_id") or "").strip()
            for stage in completed[:-3]
            if str(stage.get("stage_id") or "").strip()
        ],
        "history_message_count": len(raw_history_messages),
    }
    compacted_block = {
        "role": "assistant",
        "content": "[G3KU_TOKEN_COMPACT_V1]\n" + json.dumps(compacted_payload, ensure_ascii=False, sort_keys=True),
    }
    compacted_block_tokens = min(
        max_compacted_tokens,
        estimate_message_tokens([compacted_block]),
    )
    return FrontdoorCompactedHistory(
        active_stage_id=active_stage_id,
        retained_completed_stage_ids=retained_completed_stage_ids,
        compacted_block=compacted_block,
        compacted_block_tokens=compacted_block_tokens,
    )


__all__ = [
    "FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS",
    "RUNTIME_SEND_TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO",
    "RUNTIME_SEND_TOKEN_COMPRESSION_TRIGGER_RATIO",
    "FrontdoorCompactedHistory",
    "FrontdoorTokenPreflightResult",
    "RuntimeHybridSendTokenEstimate",
    "RuntimeObservedInputTruth",
    "RuntimeSendTokenPreflightSnapshot",
    "RuntimeSendTokenPreflightThresholds",
    "build_runtime_hybrid_send_token_estimate",
    "build_runtime_observed_input_truth",
    "build_runtime_send_token_preflight_snapshot",
    "compact_frontdoor_history_zone",
    "estimate_runtime_provider_request_preview_tokens",
    "compute_runtime_send_token_preflight_thresholds",
    "should_trigger_runtime_token_compression",
]
