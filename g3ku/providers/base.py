"""Base LLM provider interface."""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from g3ku.prompt_trace import render_send_data_trace

_TRACE_TRUE_VALUES = {"1", "true", "yes", "on", "debug", "log"}


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMModelAttempt:
    """One concrete model call attempt, including normalized token usage."""

    model_key: str
    provider_id: str
    provider_model: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    error_text: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    attempts: list[LLMModelAttempt] = field(default_factory=list)
    reasoning_content: str | None = None
    thinking_blocks: list[dict] | None = None
    request_message_count: int | None = None
    request_message_chars: int | None = None

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base

    @property
    def manages_request_timeout_internally(self) -> bool:
        """Whether the provider enforces its own streaming timeout semantics."""
        return False

    @staticmethod
    def _trace_enabled() -> bool:
        raw = str(os.getenv("G3KU_PROMPT_TRACE", "")).strip().lower()
        return raw in _TRACE_TRUE_VALUES

    @staticmethod
    def _trace_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    def _trace_request_payload(
        self,
        *,
        provider: str,
        endpoint: str | None = None,
        body: dict[str, Any],
    ) -> None:
        if not self._trace_enabled():
            return
        del provider, endpoint
        logger.info(render_send_data_trace(self._trace_json(body)))

    @staticmethod
    def _sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace empty text content that causes provider 400 errors.

        Empty content can appear when MCP tools return nothing. Most providers
        reject empty-string content or empty text blocks in list content.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")

            if isinstance(content, str) and not content:
                clean = dict(msg)
                clean["content"] = None if (msg.get("role") == "assistant" and msg.get("tool_calls")) else "(empty)"
                result.append(clean)
                continue

            if isinstance(content, list):
                filtered = [
                    item for item in content
                    if not (
                        isinstance(item, dict)
                        and item.get("type") in ("text", "input_text", "output_text")
                        and not item.get("text")
                    )
                ]
                if len(filtered) != len(content):
                    clean = dict(msg)
                    if filtered:
                        clean["content"] = filtered
                    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                        clean["content"] = None
                    else:
                        clean["content"] = "(empty)"
                    result.append(clean)
                    continue

            if isinstance(content, dict):
                clean = dict(msg)
                clean["content"] = [content]
                result.append(clean)
                continue

            result.append(msg)
        return result

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        prompt_cache_key: str | None = None,
        request_timeout_seconds: float | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            tool_choice: Optional tool-choice policy (e.g. "auto", "required", or explicit tool).
            parallel_tool_calls: Optional provider hint to enable/disable parallel tool execution.
            prompt_cache_key: Optional stable prompt-cache key for providers that support it.
            request_timeout_seconds: Optional per-request network timeout. When provided,
                providers should pass it down to their transport/SDK instead of waiting on
                library defaults.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        raise NotImplementedError

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        raise NotImplementedError


def _usage_lookup(value: Any, *path: str) -> tuple[bool, Any]:
    current = value
    for key in path:
        if isinstance(current, dict):
            if key not in current:
                return False, None
            current = current.get(key)
            continue
        if not hasattr(current, key):
            return False, None
        current = getattr(current, key)
    return True, current


def _coerce_usage_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return max(0, int(float(text)))
    except Exception:
        return 0


def normalize_usage_payload(raw_usage: Any) -> dict[str, int]:
    """Normalize provider-specific usage payloads into a stable token shape."""

    payload: dict[str, int] = {}
    fields = (
        ("input_tokens", ("input_tokens",), ("prompt_tokens",)),
        ("output_tokens", ("output_tokens",), ("completion_tokens",)),
        (
            "cache_hit_tokens",
            ("cache_hit_tokens",),
            ("cache_read_tokens",),
            ("cached_tokens",),
            ("prompt_tokens_details", "cached_tokens"),
            ("input_tokens_details", "cached_tokens"),
        ),
    )
    for target, *candidates in fields:
        for path in candidates:
            found, value = _usage_lookup(raw_usage, *path)
            if not found:
                continue
            payload[target] = _coerce_usage_int(value)
            break
    return payload
