from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from g3ku.runtime.context.summarizer import estimate_tokens
from g3ku.runtime.message_token_estimation import estimate_message_tokens

FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS = 5000


@dataclass(frozen=True, slots=True)
class FrontdoorTokenPreflightPolicy:
    max_context_tokens: int
    trigger_ratio: float
    trigger_tokens: int


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


def build_frontdoor_token_preflight_policy(*, max_context_tokens: int, trigger_ratio: float) -> FrontdoorTokenPreflightPolicy:
    normalized_max = max(1, int(max_context_tokens or 0))
    normalized_ratio = max(0.0, float(trigger_ratio or 0.0))
    return FrontdoorTokenPreflightPolicy(
        max_context_tokens=normalized_max,
        trigger_ratio=normalized_ratio,
        trigger_tokens=int(normalized_max * normalized_ratio),
    )


def estimate_frontdoor_provider_request_tokens(
    *,
    provider_request_body: dict[str, Any] | None,
    request_messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
) -> int:
    payload = dict(provider_request_body or {})
    if not payload:
        payload = {
            "input": list(request_messages),
            "tools": list(tool_schemas or []),
        }
    return estimate_tokens(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def should_run_frontdoor_token_preflight(
    *,
    final_request_tokens: int,
    policy: FrontdoorTokenPreflightPolicy,
) -> bool:
    return int(final_request_tokens or 0) >= int(policy.trigger_tokens)


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
    "FrontdoorCompactedHistory",
    "FrontdoorTokenPreflightPolicy",
    "FrontdoorTokenPreflightResult",
    "build_frontdoor_token_preflight_policy",
    "compact_frontdoor_history_zone",
    "estimate_frontdoor_provider_request_tokens",
    "should_run_frontdoor_token_preflight",
]
