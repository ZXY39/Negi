from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from g3ku.providers.base import normalize_usage_payload
from g3ku.runtime.context.summarizer import estimate_tokens

# CEO-aligned send-preflight constants shared by frontdoor and node/runtime callers.
#
# Contract:
# trigger_tokens = int(context_window_tokens * 0.80)
# effective_trigger_tokens = int(trigger_tokens * 0.95)
RUNTIME_SEND_TOKEN_COMPRESSION_TRIGGER_RATIO = 0.80
RUNTIME_SEND_TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO = 0.95


@dataclass(frozen=True, slots=True)
class RuntimeSendTokenPreflightThresholds:
    context_window_tokens: int
    trigger_tokens: int
    effective_trigger_tokens: int


@dataclass(frozen=True, slots=True)
class RuntimeSendTokenPreflightSnapshot:
    context_window_tokens: int
    estimated_total_tokens: int
    trigger_tokens: int
    effective_trigger_tokens: int
    would_exceed_context_window: bool
    would_trigger_token_compression: bool
    ratio: float


@dataclass(frozen=True, slots=True)
class RuntimeObservedInputTruth:
    effective_input_tokens: int
    input_tokens: int
    cache_hit_tokens: int
    provider_model: str
    actual_request_hash: str
    source: str


@dataclass(frozen=True, slots=True)
class RuntimeHybridSendTokenEstimate:
    final_estimate_tokens: int
    preview_estimate_tokens: int
    usage_based_estimate_tokens: int
    delta_estimate_tokens: int
    estimate_source: str
    comparable_to_previous_request: bool


def estimate_runtime_provider_request_preview_tokens(
    *,
    provider_request_body: dict[str, Any] | None,
    request_messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
) -> int:
    payload = dict(provider_request_body or {})
    if not payload:
        payload = {
            "messages": list(request_messages or []),
            "tools": list(tool_schemas or []),
        }
    return estimate_tokens(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def compute_runtime_send_token_preflight_thresholds(
    *,
    context_window_tokens: int,
) -> RuntimeSendTokenPreflightThresholds:
    normalized_context_window = max(0, int(context_window_tokens or 0))
    trigger_tokens = (
        int(normalized_context_window * RUNTIME_SEND_TOKEN_COMPRESSION_TRIGGER_RATIO)
        if normalized_context_window > 0
        else 0
    )
    effective_trigger_tokens = (
        int(trigger_tokens * RUNTIME_SEND_TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO)
        if trigger_tokens > 0
        else 0
    )
    return RuntimeSendTokenPreflightThresholds(
        context_window_tokens=normalized_context_window,
        trigger_tokens=trigger_tokens,
        effective_trigger_tokens=effective_trigger_tokens,
    )


def should_trigger_runtime_token_compression(
    *,
    estimated_total_tokens: int,
    thresholds: RuntimeSendTokenPreflightThresholds,
) -> bool:
    if int(thresholds.effective_trigger_tokens or 0) <= 0:
        return False
    return int(estimated_total_tokens or 0) >= int(thresholds.effective_trigger_tokens or 0)


def build_runtime_send_token_preflight_snapshot(
    *,
    context_window_tokens: int,
    estimated_total_tokens: int,
) -> RuntimeSendTokenPreflightSnapshot:
    thresholds = compute_runtime_send_token_preflight_thresholds(
        context_window_tokens=int(context_window_tokens or 0),
    )
    would_exceed_context_window = (
        thresholds.context_window_tokens > 0
        and int(estimated_total_tokens or 0) > int(thresholds.context_window_tokens or 0)
    )
    would_trigger_token_compression = should_trigger_runtime_token_compression(
        estimated_total_tokens=int(estimated_total_tokens or 0),
        thresholds=thresholds,
    )
    ratio = (
        float(int(estimated_total_tokens or 0)) / float(thresholds.context_window_tokens)
        if thresholds.context_window_tokens > 0
        else 0.0
    )
    return RuntimeSendTokenPreflightSnapshot(
        context_window_tokens=thresholds.context_window_tokens,
        estimated_total_tokens=int(estimated_total_tokens or 0),
        trigger_tokens=thresholds.trigger_tokens,
        effective_trigger_tokens=thresholds.effective_trigger_tokens,
        would_exceed_context_window=bool(would_exceed_context_window),
        would_trigger_token_compression=bool(would_trigger_token_compression),
        ratio=float(ratio),
    )


def build_runtime_observed_input_truth(
    *,
    usage: Any,
    provider_model: str,
    actual_request_hash: str,
    source: str,
) -> RuntimeObservedInputTruth:
    normalized = normalize_usage_payload(usage or {})
    input_tokens = max(0, int(normalized.get("input_tokens") or 0))
    cache_hit_tokens = max(0, int(normalized.get("cache_hit_tokens") or 0))
    return RuntimeObservedInputTruth(
        effective_input_tokens=max(0, input_tokens + cache_hit_tokens),
        input_tokens=input_tokens,
        cache_hit_tokens=cache_hit_tokens,
        provider_model=str(provider_model or "").strip(),
        actual_request_hash=str(actual_request_hash or "").strip(),
        source=str(source or "").strip() or "provider_usage",
    )


def build_runtime_hybrid_send_token_estimate(
    *,
    preview_estimate_tokens: int,
    previous_effective_input_tokens: int,
    delta_estimate_tokens: int,
    comparable_to_previous_request: bool,
) -> RuntimeHybridSendTokenEstimate:
    normalized_preview_estimate_tokens = max(0, int(preview_estimate_tokens or 0))
    normalized_previous_effective_input_tokens = max(
        0,
        int(previous_effective_input_tokens or 0),
    )
    normalized_delta_estimate_tokens = max(0, int(delta_estimate_tokens or 0))
    usage_based_estimate_tokens = (
        normalized_previous_effective_input_tokens + normalized_delta_estimate_tokens
        if comparable_to_previous_request and normalized_previous_effective_input_tokens > 0
        else 0
    )
    final_estimate_tokens = max(
        normalized_preview_estimate_tokens,
        usage_based_estimate_tokens,
    )
    estimate_source = (
        "usage_plus_delta"
        if usage_based_estimate_tokens >= normalized_preview_estimate_tokens
        and usage_based_estimate_tokens > 0
        else "preview_estimate"
    )
    return RuntimeHybridSendTokenEstimate(
        final_estimate_tokens=final_estimate_tokens,
        preview_estimate_tokens=normalized_preview_estimate_tokens,
        usage_based_estimate_tokens=usage_based_estimate_tokens,
        delta_estimate_tokens=normalized_delta_estimate_tokens,
        estimate_source=estimate_source,
        comparable_to_previous_request=bool(comparable_to_previous_request),
    )


__all__ = [
    "RUNTIME_SEND_TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO",
    "RUNTIME_SEND_TOKEN_COMPRESSION_TRIGGER_RATIO",
    "RuntimeHybridSendTokenEstimate",
    "RuntimeObservedInputTruth",
    "RuntimeSendTokenPreflightSnapshot",
    "RuntimeSendTokenPreflightThresholds",
    "build_runtime_hybrid_send_token_estimate",
    "build_runtime_observed_input_truth",
    "build_runtime_send_token_preflight_snapshot",
    "compute_runtime_send_token_preflight_thresholds",
    "estimate_runtime_provider_request_preview_tokens",
    "should_trigger_runtime_token_compression",
]
