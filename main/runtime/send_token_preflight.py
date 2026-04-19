from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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
    would_trigger_token_compression = (
        not would_exceed_context_window
        and should_trigger_runtime_token_compression(
            estimated_total_tokens=int(estimated_total_tokens or 0),
            thresholds=thresholds,
        )
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


__all__ = [
    "RUNTIME_SEND_TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO",
    "RUNTIME_SEND_TOKEN_COMPRESSION_TRIGGER_RATIO",
    "RuntimeSendTokenPreflightSnapshot",
    "RuntimeSendTokenPreflightThresholds",
    "build_runtime_send_token_preflight_snapshot",
    "compute_runtime_send_token_preflight_thresholds",
    "estimate_runtime_provider_request_preview_tokens",
    "should_trigger_runtime_token_compression",
]
