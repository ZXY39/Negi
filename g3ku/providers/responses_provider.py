"""Generic /v1/responses provider (Codex protocol) using API Key."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from loguru import logger

from g3ku.providers.base import LLMProvider, LLMResponse
from g3ku.providers.openai_codex_provider import (
    _convert_messages,
    _convert_tools,
    _consume_sse,
    _friendly_error,
    _prompt_cache_key,
)


class _SSEDiagnosticsResponseProxy:
    """Response proxy that captures coarse SSE timing without recording content."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self.status_code = response.status_code
        self._started_at = time.perf_counter()
        self._headers_received_at = self._started_at
        self._first_event_received_at: float | None = None
        self._first_data_received_at: float | None = None
        self._first_text_delta_received_at: float | None = None
        self._last_event_name = ""
        self._data_line_count = 0
        self._event_line_count = 0
        self._first_line_timeout_seconds: float | None = None
        self._idle_line_timeout_seconds: float | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    async def aiter_lines(self):
        iterator = self._response.aiter_lines().__aiter__()
        line_index = 0
        while True:
            timeout_seconds = self._first_line_timeout_seconds if line_index == 0 else self._idle_line_timeout_seconds
            try:
                if timeout_seconds is None:
                    line = await iterator.__anext__()
                else:
                    line = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_seconds)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                if line_index == 0:
                    raise _ResponsesStreamTimeoutError(
                        f"Responses stream timeout waiting for first chunk after {timeout_seconds:.3f}s"
                    ) from exc
                raise _ResponsesStreamTimeoutError(
                    f"Responses stream idle timeout after {timeout_seconds:.3f}s without a new chunk"
                ) from exc
            now = time.perf_counter()
            if line.startswith("event:"):
                event_name = str(line.split(":", 1)[1].strip())
                self._event_line_count += 1
                self._last_event_name = event_name
                if self._first_event_received_at is None:
                    self._first_event_received_at = now
                if event_name == "response.output_text.delta" and self._first_text_delta_received_at is None:
                    self._first_text_delta_received_at = now
            elif line.startswith("data:"):
                self._data_line_count += 1
                if self._first_data_received_at is None:
                    self._first_data_received_at = now
                if self._last_event_name == "response.output_text.delta" and self._first_text_delta_received_at is None:
                    self._first_text_delta_received_at = now
            line_index += 1
            yield line

    def render_summary(self, *, outcome: str) -> str:
        now = time.perf_counter()
        elapsed_ms = lambda ts: "" if ts is None else f"{max(0.0, (ts - self._started_at) * 1000.0):.1f}"
        parts = [
            f"outcome={outcome}",
            f"status_code={self.status_code}",
            f"headers_received_ms={elapsed_ms(self._headers_received_at)}",
            f"first_event_received_ms={elapsed_ms(self._first_event_received_at)}",
            f"first_data_received_ms={elapsed_ms(self._first_data_received_at)}",
            f"first_text_delta_received_ms={elapsed_ms(self._first_text_delta_received_at)}",
            f"last_event={self._last_event_name or '<none>'}",
            f"event_line_count={self._event_line_count}",
            f"data_line_count={self._data_line_count}",
        ]
        if outcome == "completed":
            parts.append(f"stream_completed_ms={max(0.0, (now - self._started_at) * 1000.0):.1f}")
        elif outcome == "failed":
            parts.append(f"stream_failed_ms={max(0.0, (now - self._started_at) * 1000.0):.1f}")
        return "responses sse diagnostics: " + " ".join(parts)


class ResponsesProvider(LLMProvider):
    """Call any /v1/responses endpoint with an API Key."""

    RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

    def __init__(
        self,
        api_key: str,
        api_base: str,
        default_model: str = "gpt-5.3-codex",
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key=api_key, api_base=api_base)
        self.default_model = default_model
        self.extra_headers = dict(extra_headers or {})

    @property
    def manages_request_timeout_internally(self) -> bool:
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
    ) -> LLMResponse:
        model = model or self.default_model
        system_prompt, input_items = _convert_messages(messages)
        api_key = str(self.api_key or "").strip()
        if not api_key:
            raise ValueError(
                "Missing API key for Responses provider; refusing to send an empty Authorization header."
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "responses=experimental",
            "originator": "g3ku",
            "User-Agent": "g3ku (python)",
            "accept": "text/event-stream",
            "content-type": "application/json",
        }
        if self.extra_headers:
            headers.update(self.extra_headers)

        if system_prompt:
            # Prepend system prompt as a user message to ensure visibility
            # This is a workaround for providers that ignore the 'instructions' field
            input_items.insert(0, {
                "role": "user",
                "content": [{"type": "input_text", "text": f"[SYSTEM]\n{system_prompt}\n[END SYSTEM]"}],
            })

        body: dict[str, Any] = {
            "model": model,
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "high" if reasoning_effort == "high" else "medium"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": str(prompt_cache_key or _prompt_cache_key(messages)),
        }
        if max_tokens is not None:
            body["max_output_tokens"] = max(1, int(max_tokens))
        if temperature is not None:
            body["temperature"] = float(temperature)
        if reasoning_effort:
            body["reasoning"] = {"effort": str(reasoning_effort).strip()}

        if tools:
            body["tools"] = _convert_tools(tools)
            body["tool_choice"] = tool_choice if tool_choice is not None else "auto"
            body["parallel_tool_calls"] = (
                bool(parallel_tool_calls) if parallel_tool_calls is not None else True
            )

        url = self.api_base
        if not url.endswith("/responses"):
            url = url.rstrip("/") + "/responses"
        self._trace_request_payload(
            provider="responses",
            endpoint=url,
            body=body,
        )

        try:
            client_timeout = float(request_timeout_seconds) if request_timeout_seconds is not None else 60.0
            async with httpx.AsyncClient(timeout=client_timeout, verify=False) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        text = await response.aread()
                        detail = _friendly_error(response.status_code, text.decode("utf-8", "ignore"))
                        if response.status_code in self.RETRYABLE_STATUS_CODES:
                            raise _RetryableResponsesError(detail)
                        raise RuntimeError(detail)
                    diagnostics = _SSEDiagnosticsResponseProxy(response)
                    stream_timeout_seconds = float(request_timeout_seconds) if request_timeout_seconds is not None else 60.0
                    diagnostics._first_line_timeout_seconds = stream_timeout_seconds
                    diagnostics._idle_line_timeout_seconds = stream_timeout_seconds
                    content, tool_calls, finish_reason, usage = await _consume_sse(diagnostics)
                    logger.debug(diagnostics.render_summary(outcome="completed"))
                    return LLMResponse(
                        content=content,
                        tool_calls=tool_calls,
                        finish_reason=finish_reason,
                        usage=usage,
                    )
        except Exception as e:
            partial_content = str(getattr(e, "partial_content", "") or "").strip()
            error_text = self._format_error(e, url)
            diagnostics_summary = ""
            diagnostics = locals().get("diagnostics")
            if isinstance(diagnostics, _SSEDiagnosticsResponseProxy):
                diagnostics_summary = diagnostics.render_summary(outcome="failed")
            if partial_content:
                if diagnostics_summary:
                    logger.warning(diagnostics_summary)
                logger.warning("Responses API stream failed after partial content; returning partial content for structured recovery")
                return LLMResponse(
                    content=partial_content,
                    finish_reason="error",
                    error_text=error_text,
                )
            if diagnostics_summary:
                logger.warning(diagnostics_summary)
            logger.error("Error calling Responses API: {}", error_text)
            if isinstance(e, _RetryableResponsesError):
                raise
            raise RuntimeError(error_text) from e

    def get_default_model(self) -> str:
        return self.default_model

    @staticmethod
    def _format_error(exc: Exception, url: str) -> str:
        """Return a user-friendly, non-empty error string."""
        message = str(exc).strip()
        if not message and getattr(exc, "args", None):
            parts = [str(arg).strip() for arg in exc.args if str(arg).strip()]
            message = "; ".join(parts)
        if not message:
            message = exc.__class__.__name__

        if isinstance(exc, httpx.TimeoutException):
            return f"Request timeout to {url} ({message})"
        if isinstance(exc, httpx.NetworkError):
            return f"Network error when connecting to {url} ({message})"
        return message


class _RetryableResponsesError(RuntimeError):
    """Transient upstream failure that outer model-chain fallback may handle."""


class _ResponsesStreamTimeoutError(TimeoutError):
    """Raised when a streamed responses request stops producing chunks in time."""

