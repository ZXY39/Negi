from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, TypeVar

import json_repair

from g3ku.providers.base import ToolCallRequest, normalize_usage_payload

T = TypeVar("T")

DEFAULT_STREAMING_FIRST_CHUNK_TIMEOUT_SECONDS = 60.0
DEFAULT_STREAMING_IDLE_CHUNK_TIMEOUT_SECONDS = 60.0
DEFAULT_NON_STREAMING_FIRST_RESPONSE_TIMEOUT_SECONDS = 120.0


class StreamingChunkTimeoutError(TimeoutError):
    """Raised when a streaming provider stops yielding chunks in time."""


class StreamingFallbackRequested(RuntimeError):
    """Raised when a provider should transparently retry in non-streaming mode."""


@dataclass(slots=True)
class StreamingDiagnostics:
    provider_label: str
    started_at: float
    first_chunk_received_at: float | None = None
    first_text_delta_received_at: float | None = None
    last_chunk_kind: str = ""
    chunk_count: int = 0

    @classmethod
    def start(cls, provider_label: str) -> "StreamingDiagnostics":
        return cls(provider_label=provider_label, started_at=time.perf_counter())

    def note_chunk(self, kind: str, *, is_text: bool = False) -> None:
        now = time.perf_counter()
        self.chunk_count += 1
        self.last_chunk_kind = str(kind or "chunk")
        if self.first_chunk_received_at is None:
            self.first_chunk_received_at = now
        if is_text and self.first_text_delta_received_at is None:
            self.first_text_delta_received_at = now

    def render_summary(self, *, outcome: str, extra_fields: dict[str, object] | None = None) -> str:
        now = time.perf_counter()
        elapsed_ms = lambda ts: "" if ts is None else f"{max(0.0, (ts - self.started_at) * 1000.0):.1f}"
        parts = [
            f"outcome={outcome}",
            f"first_chunk_received_ms={elapsed_ms(self.first_chunk_received_at)}",
            f"first_text_delta_received_ms={elapsed_ms(self.first_text_delta_received_at)}",
            f"last_chunk_kind={self.last_chunk_kind or '<none>'}",
            f"chunk_count={self.chunk_count}",
        ]
        if outcome == "completed":
            parts.append(f"stream_completed_ms={max(0.0, (now - self.started_at) * 1000.0):.1f}")
        elif outcome == "failed":
            parts.append(f"stream_failed_ms={max(0.0, (now - self.started_at) * 1000.0):.1f}")
        for key, value in dict(extra_fields or {}).items():
            parts.append(f"{key}={value}")
        return f"{self.provider_label} stream diagnostics: " + " ".join(parts)


def resolve_streaming_timeout_seconds(request_timeout_seconds: float | None) -> float:
    if request_timeout_seconds is None:
        return DEFAULT_STREAMING_FIRST_CHUNK_TIMEOUT_SECONDS
    try:
        value = float(request_timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_STREAMING_FIRST_CHUNK_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_STREAMING_FIRST_CHUNK_TIMEOUT_SECONDS


def resolve_non_streaming_timeout_seconds(request_timeout_seconds: float | None) -> float:
    if request_timeout_seconds is None:
        return DEFAULT_NON_STREAMING_FIRST_RESPONSE_TIMEOUT_SECONDS
    try:
        value = float(request_timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_NON_STREAMING_FIRST_RESPONSE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_NON_STREAMING_FIRST_RESPONSE_TIMEOUT_SECONDS


async def iterate_with_chunk_timeouts(
    stream: Any,
    *,
    first_chunk_timeout_seconds: float,
    idle_chunk_timeout_seconds: float,
) -> AsyncIterator[T]:
    if not hasattr(stream, "__aiter__"):
        raise StreamingFallbackRequested("streaming response is not async iterable")
    iterator = stream.__aiter__()
    chunk_index = 0
    while True:
        timeout_seconds = first_chunk_timeout_seconds if chunk_index == 0 else idle_chunk_timeout_seconds
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_seconds)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError as exc:
            if chunk_index == 0:
                raise StreamingChunkTimeoutError(
                    f"stream timeout waiting for first chunk after {timeout_seconds:.3f}s"
                ) from exc
            raise StreamingChunkTimeoutError(
                f"stream idle timeout after {timeout_seconds:.3f}s without a new chunk"
            ) from exc
        chunk_index += 1
        yield item


def should_fallback_to_non_streaming_from_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    fallback_tokens = (
        "streaming response is not async iterable",
        "stream unsupported",
        "streaming unsupported",
        "does not support stream",
        "does not support streaming",
        "stream not supported",
        "unsupported value: 'stream'",
        "unsupported parameter: stream",
        "response body is not streamable",
    )
    return any(token in text for token in fallback_tokens) or isinstance(exc, StreamingFallbackRequested)


def _maybe_get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _content_delta_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            text = _maybe_get(item, "text", "")
            if text:
                parts.append(str(text))
        return "".join(parts)
    return ""


async def consume_openai_like_chat_stream(
    stream: Any,
    *,
    diagnostics: StreamingDiagnostics,
    first_chunk_timeout_seconds: float,
    idle_chunk_timeout_seconds: float,
) -> tuple[str | None, list[ToolCallRequest], str, dict[str, int], str | None]:
    content_parts: list[str] = []
    tool_call_buffers: dict[int, dict[str, str]] = {}
    finish_reason = "stop"
    usage: dict[str, int] = {}
    reasoning_content_parts: list[str] = []

    async for chunk in iterate_with_chunk_timeouts(
        stream,
        first_chunk_timeout_seconds=first_chunk_timeout_seconds,
        idle_chunk_timeout_seconds=idle_chunk_timeout_seconds,
    ):
        choices = list(_maybe_get(chunk, "choices", []) or [])
        chunk_usage = normalize_usage_payload(_maybe_get(chunk, "usage", None))
        if chunk_usage:
            usage = chunk_usage
        if not choices:
            diagnostics.note_chunk("non_choice_chunk")
            continue
        choice = choices[0]
        delta = _maybe_get(choice, "delta", None)
        text_delta = _content_delta_text(_maybe_get(delta, "content", None))
        reasoning_delta = _content_delta_text(_maybe_get(delta, "reasoning_content", None))
        tool_call_deltas = list(_maybe_get(delta, "tool_calls", []) or [])
        if text_delta:
            content_parts.append(text_delta)
            diagnostics.note_chunk("text_delta", is_text=True)
        elif reasoning_delta:
            reasoning_content_parts.append(reasoning_delta)
            diagnostics.note_chunk("reasoning_delta")
        elif tool_call_deltas:
            diagnostics.note_chunk("tool_call_delta")
        else:
            diagnostics.note_chunk("chunk")

        for tool_delta in tool_call_deltas:
            index = int(_maybe_get(tool_delta, "index", 0) or 0)
            buffer = tool_call_buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
            tool_id = str(_maybe_get(tool_delta, "id", "") or "")
            if tool_id:
                buffer["id"] = tool_id
            function_payload = _maybe_get(tool_delta, "function", None) or {}
            name = str(_maybe_get(function_payload, "name", "") or "")
            if name:
                buffer["name"] = name
            arguments_delta = str(_maybe_get(function_payload, "arguments", "") or "")
            if arguments_delta:
                buffer["arguments"] += arguments_delta

        finish_reason = str(_maybe_get(choice, "finish_reason", "") or finish_reason or "stop")

    tool_calls: list[ToolCallRequest] = []
    for index in sorted(tool_call_buffers):
        buffer = tool_call_buffers[index]
        raw_arguments = str(buffer.get("arguments", "") or "").strip() or "{}"
        try:
            arguments = json_repair.loads(raw_arguments)
        except Exception:
            try:
                arguments = json.loads(raw_arguments)
            except Exception:
                arguments = {"raw": raw_arguments}
        tool_calls.append(
            ToolCallRequest(
                id=str(buffer.get("id") or f"tool_call_{index}"),
                name=str(buffer.get("name") or f"tool_{index}"),
                arguments=arguments,
            )
        )

    reasoning_content = "".join(reasoning_content_parts).strip() or None
    return "".join(content_parts) or None, tool_calls, finish_reason or "stop", usage, reasoning_content
