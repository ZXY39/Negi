"""Anthropic Messages protocol provider (/v1/messages) using API Key.

Third protocol lane, mirroring `responses_provider.py` (httpx bare SSE + streaming
diagnostics + structured errors). P0 sends ``thinking: disabled``; thinking blocks
with model-bound signatures and explicit cache_control breakpoints are P1 (see
work/anthropic-protocol-plan.md). Auth defaults to ``x-api-key`` (Anthropic's scheme);
``auth_header=True`` switches to ``Authorization: Bearer`` for gateways that require it.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
from loguru import logger

from g3ku.providers.anthropic_protocol_helpers import (
    _consume_sse,
    _convert_messages,
    _convert_tools,
    _friendly_error,
    anthropic_messages_suffix,
)
from g3ku.providers.base import RETRYABLE_STATUS_CODES, LLMProvider, LLMResponse
from g3ku.providers.fallback import normalize_forced_function_tool_choice
from g3ku.providers.streaming_timeouts import (
    StreamingChunkTimeoutError,
    StreamingDiagnostics,
    notice_payload_silence,
    resolve_streaming_timeout_seconds,
)

_TLS_VERIFY_DISABLED_VALUES = frozenset({"off", "false", "0", "no"})
_tls_verify_warning_logged = False

# Anthropic requires max_tokens; Negi's send layer always resolves an explicit cap,
# this fallback only covers direct construction (tests / embedded calls).
_DEFAULT_MAX_TOKENS = 8192


def _tls_verify_enabled() -> bool:
    global _tls_verify_warning_logged
    value = os.environ.get("G3KU_PROVIDER_TLS_VERIFY", "").strip().lower()
    if value not in _TLS_VERIFY_DISABLED_VALUES:
        return True
    if not _tls_verify_warning_logged:
        _tls_verify_warning_logged = True
        logger.warning(
            "TLS certificate verification is DISABLED for the Anthropic provider "
            "(G3KU_PROVIDER_TLS_VERIFY override); API keys and conversation content "
            "can be intercepted by a network attacker"
        )
    return False


def _anthropic_tool_choice(tool_choice: Any) -> dict[str, Any]:
    """Map Negi tool_choice (string policy or forced selector) to Anthropic's dict form."""
    normalized = normalize_forced_function_tool_choice(tool_choice, protocol="anthropic")
    if isinstance(normalized, dict):
        return normalized
    policy = str(normalized or "auto").strip().lower()
    if policy == "required":
        return {"type": "any"}
    if policy == "none":
        return {"type": "none"}
    return {"type": "auto"}


# Negi 六档思考力度 → Anthropic thinking 预算（budget 模式，官方与百炼 qwen 通用）。
_THINKING_BUDGETS = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768, "max": 65536}
_EFFORT_LEVELS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}


def _build_thinking(
    reasoning_effort: str | None, max_tokens: int | None, thinking_mode: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return (thinking, output_config) for the Anthropic request body.

    - budget（默认）：thinking.enabled + budget_tokens，官方与百炼 qwen 都支持。
    - effort：thinking.enabled + output_config.effort（百炼推荐，budget_tokens 将废弃）。
    none/空 → thinking.disabled。budget 须 < max_tokens（Anthropic 硬合同），夹一刀。
    """
    level = str(reasoning_effort or "").strip().lower()
    if not level or level == "none":
        return {"type": "disabled"}, None
    if thinking_mode == "effort":
        return {"type": "enabled"}, {"effort": _EFFORT_LEVELS.get(level, "high")}
    budget = _THINKING_BUDGETS.get(level, 8192)
    cap = max(1, int(max_tokens)) if max_tokens is not None else _DEFAULT_MAX_TOKENS
    budget = max(1024, min(budget, cap - 1024))
    return {"type": "enabled", "budget_tokens": budget}, None


class _AnthropicSSEProxy:
    """Wrap a streamed SSE response with per-line timeouts plus diagnostics."""

    def __init__(
        self,
        response: httpx.Response,
        *,
        first_line_timeout_seconds: float,
        idle_line_timeout_seconds: float,
        on_upstream_wait: Any = None,
        started_at: float | None = None,
    ) -> None:
        self._response = response
        self.status_code = response.status_code
        self._diagnostics = StreamingDiagnostics.start("anthropic", started_at=started_at)
        self._on_upstream_wait = on_upstream_wait
        self._first_line_timeout_seconds = first_line_timeout_seconds
        self._idle_line_timeout_seconds = idle_line_timeout_seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def note_terminal_event(self) -> None:
        # message_stop is this lane's only "said it all" credential (no finish_reason chunk).
        self._diagnostics.note_finish_reason()

    async def aiter_lines(self):
        iterator = self._response.aiter_lines().__aiter__()
        line_index = 0
        while True:
            timeout_seconds = self._first_line_timeout_seconds if line_index == 0 else self._idle_line_timeout_seconds
            try:
                line = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_seconds)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                if line_index == 0:
                    raise StreamingChunkTimeoutError(
                        f"Anthropic stream timeout waiting for first chunk after {timeout_seconds:.3f}s"
                    ) from exc
                raise StreamingChunkTimeoutError(
                    f"Anthropic stream idle timeout after {timeout_seconds:.3f}s without a new chunk"
                ) from exc
            line_index += 1
            if not line:
                yield line
                continue
            if line.startswith("event:"):
                self._diagnostics.note_chunk(f"event:{line.split(':', 1)[1].strip()}")
            elif line.startswith("data:"):
                self._diagnostics.note_chunk("data")
            else:
                self._diagnostics.note_chunk("line")
            await notice_payload_silence(self._diagnostics, self._on_upstream_wait)
            yield line

    def render_summary(self, *, outcome: str) -> str:
        return self._diagnostics.render_summary(
            outcome=outcome,
            extra_fields={"status_code": self.status_code},
        )


class _RetryableAnthropicError(RuntimeError):
    """Transient upstream failure that outer model-chain fallback may handle."""

    def __init__(self, message: str, *, error_status: int | None = None) -> None:
        super().__init__(message)
        self.error_status = error_status


class AnthropicProvider(LLMProvider):
    """Call any /v1/messages endpoint with an API Key (Claude or Anthropic-compatible gateway)."""

    RETRYABLE_STATUS_CODES = RETRYABLE_STATUS_CODES

    def __init__(
        self,
        api_key: str,
        api_base: str,
        default_model: str = "claude-sonnet-4-5",
        extra_headers: dict[str, str] | None = None,
        auth_header: bool = False,
        thinking_mode: str = "budget",
        cache_control: bool = False,
    ):
        super().__init__(api_key=api_key, api_base=api_base)
        self.default_model = default_model
        self.extra_headers = dict(extra_headers or {})
        self.auth_header = bool(auth_header)
        self.thinking_mode = str(thinking_mode or "budget").strip().lower()
        self.cache_control = bool(cache_control)

    @property
    def manages_request_timeout_internally(self) -> bool:
        return True

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_upstream_wait_notice(self) -> bool:
        return True

    @property
    def supports_reasoning_delta_streaming(self) -> bool:
        # Anthropic streams thinking_delta chunk by chunk (unlike Responses).
        return True

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
        on_text_delta: Any = None,
        on_reasoning_delta: Any = None,
        on_upstream_wait: Any = None,
    ) -> LLMResponse:
        del parallel_tool_calls, prompt_cache_key
        # prompt_cache_key 是 OpenAI 提示；Anthropic 靠自动前缀缓存或显式 cache_control。
        model = model or self.default_model
        api_key = str(self.api_key or "").strip()
        if not api_key:
            raise ValueError("Missing API key for Anthropic provider; refusing to send an empty x-api-key header.")

        resolved_max_tokens = max(1, int(max_tokens)) if max_tokens is not None else _DEFAULT_MAX_TOKENS
        thinking, output_config = _build_thinking(reasoning_effort, resolved_max_tokens, self.thinking_mode)
        system_text, input_messages = _convert_messages(messages, model=model, cache_control=self.cache_control)
        body: dict[str, Any] = {
            "model": model,
            "messages": input_messages,
            "max_tokens": resolved_max_tokens,
            "stream": True,
            "thinking": thinking,
        }
        if output_config:
            body["output_config"] = output_config
        if system_text:
            if self.cache_control:
                body["system"] = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
            else:
                body["system"] = system_text
        # 开思考禁 temperature（Anthropic 硬合同）；disabled 时才允许。
        if temperature is not None and thinking.get("type") == "disabled":
            body["temperature"] = float(temperature)
        if tools:
            body["tools"] = _convert_tools(tools, cache_control=self.cache_control)
            body["tool_choice"] = _anthropic_tool_choice(tool_choice)

        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": "2023-06-01",
        }
        if self.auth_header:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["x-api-key"] = api_key
        if self.extra_headers:
            headers.update(self.extra_headers)

        base = (self.api_base or "https://api.anthropic.com/v1").rstrip("/")
        url = base + anthropic_messages_suffix(base)
        provider_request_meta, provider_request_body = self._capture_request_payload(
            provider="anthropic",
            endpoint=url,
            body=body,
        )

        diagnostics: _AnthropicSSEProxy | None = None
        text_streamed = False
        try:
            stream_timeout_seconds = resolve_streaming_timeout_seconds(request_timeout_seconds)
            request_started_at = time.perf_counter()

            def _track_text_delta(text: str) -> Any:
                nonlocal text_streamed
                if text:
                    text_streamed = True
                    if diagnostics is not None and diagnostics._diagnostics.first_text_delta_received_at is None:
                        diagnostics._diagnostics.first_text_delta_received_at = time.perf_counter()
                if callable(on_text_delta):
                    return on_text_delta(text)
                return None

            async with httpx.AsyncClient(timeout=stream_timeout_seconds, verify=_tls_verify_enabled()) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        text = await response.aread()
                        detail = _friendly_error(response.status_code, text.decode("utf-8", "ignore"))
                        if response.status_code in self.RETRYABLE_STATUS_CODES:
                            raise _RetryableAnthropicError(detail, error_status=response.status_code)
                        error = RuntimeError(detail)
                        error.error_status = response.status_code
                        raise error
                    diagnostics = _AnthropicSSEProxy(
                        response,
                        first_line_timeout_seconds=stream_timeout_seconds,
                        idle_line_timeout_seconds=stream_timeout_seconds,
                        on_upstream_wait=on_upstream_wait,
                        started_at=request_started_at,
                    )
                    content, tool_calls, finish_reason, usage, thinking_blocks = await _consume_sse(
                        diagnostics,
                        on_text_delta=_track_text_delta,
                        on_reasoning_delta=on_reasoning_delta,
                    )
                    logger.debug(diagnostics.render_summary(outcome="completed"))
                    # No message_stop and no tool calls ⇒ transport fault, not "model finished".
                    stream_aborted = not diagnostics._diagnostics.finish_reason_seen and not tool_calls
                    return LLMResponse(
                        content=content,
                        tool_calls=tool_calls,
                        finish_reason="error" if stream_aborted else finish_reason,
                        error_text=(
                            f"stream closed before message_stop after {int(diagnostics._diagnostics.chunk_count)} chunks"
                            if stream_aborted
                            else None
                        ),
                        error_kind="StreamIncomplete" if stream_aborted else None,
                        usage=usage,
                        thinking_blocks=thinking_blocks or None,
                        provider_request_meta=provider_request_meta,
                        provider_request_body=provider_request_body,
                        visible_text_streamed=text_streamed,
                        stream_incomplete=not diagnostics._diagnostics.finish_reason_seen,
                        first_token_ms=diagnostics._diagnostics.first_token_ms(),
                    )
        except Exception as e:
            error_text = self._format_error(e, url)
            if diagnostics is not None:
                logger.warning(diagnostics.render_summary(outcome="failed"))
            logger.error("Error calling Anthropic Messages API: {}", error_text)
            if isinstance(e, _RetryableAnthropicError):
                raise
            wrapped = RuntimeError(error_text)
            for attr in ("error_status", "error_code"):
                value = getattr(e, attr, None)
                if value is not None:
                    setattr(wrapped, attr, value)
            raise wrapped from e

    def get_default_model(self) -> str:
        return self.default_model

    @staticmethod
    def _format_error(exc: Exception, url: str) -> str:
        message = str(exc).strip()
        if not message and getattr(exc, "args", None):
            parts = [str(arg) for arg in exc.args if str(arg).strip()]
            message = "; ".join(parts)
        if not message:
            message = exc.__class__.__name__
        if isinstance(exc, httpx.TimeoutException):
            return f"Request timeout to {url} ({message})"
        if isinstance(exc, httpx.NetworkError):
            return f"Network error when connecting to {url} ({message})"
        return message
