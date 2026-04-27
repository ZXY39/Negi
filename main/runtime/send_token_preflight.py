from __future__ import annotations

import base64
import json
import math
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
_DEFAULT_IMAGE_ESTIMATION_METHOD = "openai_vision_heuristic"
_OPENAI_DEFAULT_IMAGE_LOW_TOKENS = 70
_OPENAI_DEFAULT_IMAGE_HIGH_BASE_TOKENS = 70
_OPENAI_DEFAULT_IMAGE_HIGH_TILE_TOKENS = 140
_OPENAI_DEFAULT_IMAGE_TILE_SIZE = 512
_OPENAI_DEFAULT_IMAGE_MAX_SIDE = 2048
_OPENAI_DEFAULT_IMAGE_TARGET_SHORT_SIDE = 768


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
    breakdown = estimate_runtime_provider_request_token_breakdown(
        provider_request_body=provider_request_body,
        request_messages=request_messages,
        tool_schemas=tool_schemas,
    )
    return int(breakdown.get("estimated_total_tokens") or 0)


def _data_url_media_bytes(url: str) -> tuple[str, bytes]:
    text = str(url or "").strip()
    if not text.startswith("data:") or "," not in text:
        return "", b""
    header, _, raw_data = text.partition(",")
    media = header[5:]
    is_base64 = False
    if ";base64" in media:
        media = media.replace(";base64", "")
        is_base64 = True
    mime_type = str(media or "").strip().lower()
    if not is_base64:
        return mime_type, raw_data.encode("utf-8", errors="ignore")
    try:
        return mime_type, base64.b64decode(raw_data, validate=False)
    except Exception:
        return mime_type, b""


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xFF\xD8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        while marker == 0xFF and index < len(data):
            marker = data[index]
            index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 1 >= len(data):
            break
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2:
            break
        if marker in {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        } and index + 7 < len(data):
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            if width > 0 and height > 0:
                return width, height
            return None
        index += segment_length
    return None


def _image_dimensions_from_bytes(data: bytes, *, mime_type: str = "") -> tuple[int, int] | None:
    normalized_mime = str(mime_type or "").strip().lower()
    if normalized_mime == "image/png" and len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return (width, height) if width > 0 and height > 0 else None
    if normalized_mime == "image/gif" and len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return (width, height) if width > 0 and height > 0 else None
    if normalized_mime in {"image/jpeg", "image/jpg"} or data[:2] == b"\xFF\xD8":
        return _jpeg_dimensions(data)
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return (width, height) if width > 0 and height > 0 else None
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return (width, height) if width > 0 and height > 0 else None
    return _jpeg_dimensions(data)


def _image_dimensions_from_data_url(url: str) -> tuple[int, int] | None:
    mime_type, data = _data_url_media_bytes(url)
    if not data:
        return None
    return _image_dimensions_from_bytes(data, mime_type=mime_type)


def _normalize_openai_default_image_dimensions(width: int, height: int) -> tuple[int, int]:
    normalized_width = max(1, int(width or 1))
    normalized_height = max(1, int(height or 1))
    largest_side = max(normalized_width, normalized_height)
    if largest_side > _OPENAI_DEFAULT_IMAGE_MAX_SIDE:
        scale = float(_OPENAI_DEFAULT_IMAGE_MAX_SIDE) / float(largest_side)
        normalized_width = max(1, int(math.ceil(normalized_width * scale)))
        normalized_height = max(1, int(math.ceil(normalized_height * scale)))
    smallest_side = min(normalized_width, normalized_height)
    if smallest_side > _OPENAI_DEFAULT_IMAGE_TARGET_SHORT_SIDE:
        scale = float(_OPENAI_DEFAULT_IMAGE_TARGET_SHORT_SIDE) / float(smallest_side)
        normalized_width = max(1, int(math.ceil(normalized_width * scale)))
        normalized_height = max(1, int(math.ceil(normalized_height * scale)))
    return normalized_width, normalized_height


def _estimate_openai_default_image_tokens(
    *,
    width: int | None,
    height: int | None,
    detail: str | None,
) -> int:
    normalized_detail = str(detail or "auto").strip().lower()
    if normalized_detail == "low":
        return _OPENAI_DEFAULT_IMAGE_LOW_TOKENS
    resolved_width = max(1, int(width or _OPENAI_DEFAULT_IMAGE_TILE_SIZE))
    resolved_height = max(1, int(height or _OPENAI_DEFAULT_IMAGE_TILE_SIZE))
    final_width, final_height = _normalize_openai_default_image_dimensions(resolved_width, resolved_height)
    tiles_wide = max(1, int(math.ceil(float(final_width) / float(_OPENAI_DEFAULT_IMAGE_TILE_SIZE))))
    tiles_high = max(1, int(math.ceil(float(final_height) / float(_OPENAI_DEFAULT_IMAGE_TILE_SIZE))))
    return _OPENAI_DEFAULT_IMAGE_HIGH_BASE_TOKENS + (
        tiles_wide * tiles_high * _OPENAI_DEFAULT_IMAGE_HIGH_TILE_TOKENS
    )


def _image_block_payload(url: str, *, detail: str | None) -> dict[str, Any]:
    dimensions = _image_dimensions_from_data_url(url)
    width, height = dimensions if dimensions else (None, None)
    return {
        "width": int(width or 0),
        "height": int(height or 0),
        "detail": str(detail or "auto").strip().lower() or "auto",
        "estimated_tokens": _estimate_openai_default_image_tokens(
            width=width,
            height=height,
            detail=detail,
        ),
    }


def _extract_image_blocks(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_extract_image_blocks(item))
        return found
    if not isinstance(value, dict):
        return found
    item_type = str(value.get("type") or "").strip().lower()
    if item_type in {"image_url", "input_image"}:
        image_value = value.get("image_url")
        if isinstance(image_value, dict):
            image_url = image_value.get("url")
            detail = image_value.get("detail", value.get("detail"))
        else:
            image_url = image_value or value.get("url")
            detail = value.get("detail")
        if isinstance(image_url, str) and image_url:
            found.append(_image_block_payload(image_url, detail=detail))
        return found
    for item in value.values():
        found.extend(_extract_image_blocks(item))
    return found


def _payload_without_inline_images(value: Any) -> Any:
    if isinstance(value, list):
        return [_payload_without_inline_images(item) for item in value]
    if not isinstance(value, dict):
        return value
    item_type = str(value.get("type") or "").strip().lower()
    if item_type in {"image_url", "input_image"}:
        image_value = value.get("image_url")
        detail = (
            image_value.get("detail", value.get("detail"))
            if isinstance(image_value, dict)
            else value.get("detail")
        )
        return {
            "type": item_type,
            "image_estimation": "inline_image_omitted",
            **({"detail": str(detail or "").strip()} if str(detail or "").strip() else {}),
        }
    return {
        str(key): _payload_without_inline_images(item)
        for key, item in value.items()
    }


def estimate_runtime_provider_request_token_breakdown(
    *,
    provider_request_body: dict[str, Any] | None,
    request_messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(provider_request_body or {})
    if not payload:
        payload = {
            "messages": list(request_messages or []),
            "tools": list(tool_schemas or []),
        }
    image_blocks = _extract_image_blocks(payload)
    estimated_image_tokens = sum(int(item.get("estimated_tokens") or 0) for item in image_blocks)
    tools_payload = [
        dict(item)
        for item in list(payload.get("tools") or tool_schemas or [])
        if isinstance(item, dict)
    ]
    estimated_tool_schema_tokens = (
        estimate_tokens(json.dumps(tools_payload, ensure_ascii=False, separators=(",", ":"), default=str))
        if tools_payload
        else 0
    )
    text_payload = dict(_payload_without_inline_images(payload) or {})
    text_payload.pop("tools", None)
    estimated_text_tokens = estimate_tokens(
        json.dumps(text_payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    return {
        "estimated_total_tokens": int(estimated_text_tokens + estimated_tool_schema_tokens + estimated_image_tokens),
        "estimated_text_tokens": int(estimated_text_tokens),
        "estimated_tool_schema_tokens": int(estimated_tool_schema_tokens),
        "estimated_image_tokens": int(estimated_image_tokens),
        "image_count": len(image_blocks),
        "image_estimation_method": _DEFAULT_IMAGE_ESTIMATION_METHOD if image_blocks else "",
    }


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


def build_runtime_estimated_input_truth(
    *,
    estimated_input_tokens: int,
    provider_model: str,
    actual_request_hash: str,
    source: str,
) -> RuntimeObservedInputTruth:
    input_tokens = max(0, int(estimated_input_tokens or 0))
    return RuntimeObservedInputTruth(
        effective_input_tokens=input_tokens,
        input_tokens=input_tokens,
        cache_hit_tokens=0,
        provider_model=str(provider_model or "").strip(),
        actual_request_hash=str(actual_request_hash or "").strip(),
        source=str(source or "").strip() or "preflight_estimate",
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
    "estimate_runtime_provider_request_token_breakdown",
    "build_runtime_estimated_input_truth",
    "build_runtime_hybrid_send_token_estimate",
    "build_runtime_observed_input_truth",
    "build_runtime_send_token_preflight_snapshot",
    "compute_runtime_send_token_preflight_thresholds",
    "estimate_runtime_provider_request_preview_tokens",
    "should_trigger_runtime_token_compression",
]
