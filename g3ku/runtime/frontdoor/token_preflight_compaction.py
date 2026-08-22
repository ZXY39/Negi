from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True, slots=True)
class FrontdoorTokenPreflightResult:
    request_messages: list[dict[str, Any]]
    final_request_tokens: int
    history_shrink_reason: str
    diagnostics: dict[str, Any]


__all__ = [
    "RUNTIME_SEND_TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO",
    "RUNTIME_SEND_TOKEN_COMPRESSION_TRIGGER_RATIO",
    "FrontdoorTokenPreflightResult",
    "RuntimeHybridSendTokenEstimate",
    "RuntimeObservedInputTruth",
    "RuntimeSendTokenPreflightSnapshot",
    "RuntimeSendTokenPreflightThresholds",
    "build_runtime_hybrid_send_token_estimate",
    "build_runtime_observed_input_truth",
    "build_runtime_send_token_preflight_snapshot",
    "estimate_runtime_provider_request_preview_tokens",
    "compute_runtime_send_token_preflight_thresholds",
    "should_trigger_runtime_token_compression",
]
