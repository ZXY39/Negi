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
from g3ku.providers.streaming_timeouts import (
    StreamingChunkTimeoutError,
    StreamingDiagnostics,
    resolve_streaming_timeout_seconds,
)


class _SSEDiagnosticsResponseProxy:
    """Wrap a streamed SSE response and apply per-line timeouts plus diagnostics."""

    def __init__(
        self,
        response: httpx.Response,
        *,
        first_line_timeout_seconds: float,
        idle_line_timeout_seconds: float,
    ) -> None:
        self._response = response
        self.status_code = response.status_code
        self._diagnostics = StreamingDiagnostics.start("responses")
        self._first_event_received_at: float | None = None
        self._first_data_received_at: float | None = None
        self._last_event_name = ""
        self._event_line_count = 0
        self._data_line_count = 0
        self._first_line_timeout_seconds = first_line_timeout_seconds
        self._idle_line_timeout_seconds = idle_line_timeout_seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

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
                        f"Responses stream timeout waiting for first chunk after {timeout_seconds:.3f}s"
                    ) from exc
                raise StreamingChunkTimeoutError(
                    f"Responses stream idle timeout after {timeout_seconds:.3f}s without a new chunk"
                ) from exc
            line_index += 1
            if not line:
                yield line
                continue
            now = time.perf_counter()
            if line.startswith("event:"):
                self._last_event_name = str(line.split(":", 1)[1].strip() or self._last_event_name)
                self._event_line_count += 1
                if self._first_event_received_at is None:
                    self._first_event_received_at = now
                self._diagnostics.note_chunk(f"event:{self._last_event_name}")
            elif line.startswith("data:"):
                self._data_line_count += 1
                if self._first_data_received_at is None:
                    self._first_data_received_at = now
                is_text = self._last_event_name == "response.output_text.delta"
                self._diagnostics.note_chunk(f"data:{self._last_event_name or 'unknown'}", is_text=is_text)
            else:
                self._diagnostics.note_chunk("line")
            yield line

    def render_summary(self, *, outcome: str) -> str:
        started_at = self._diagnostics.started_at
        elapsed_ms = lambda ts: "" if ts is None else f"{max(0.0, (ts - started_at) * 1000.0):.1f}"
        return self._diagnostics.render_summary(
            outcome=outcome,
            extra_fields={
                "status_code": self.status_code,
                "first_event_received_ms": elapsed_ms(self._first_event_received_at),
                "first_data_received_ms": elapsed_ms(self._first_data_received_at),
                "last_event": self._last_event_name or "<none>",
                "event_line_count": self._event_line_count,
                "data_line_count": self._data_line_count,
            },
        )


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

    @property
    def supports_streaming(self) -> bool:
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
        provider_request_meta, provider_request_body = self._capture_request_payload(
            provider="responses",
            endpoint=url,
            body=body,
        )

        try:
            stream_timeout_seconds = resolve_streaming_timeout_seconds(request_timeout_seconds)
            client_timeout = stream_timeout_seconds
            async with httpx.AsyncClient(timeout=client_timeout, verify=False) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        text = await response.aread()
                        detail = _friendly_error(response.status_code, text.decode("utf-8", "ignore"))
                        if response.status_code in self.RETRYABLE_STATUS_CODES:
                            raise _RetryableResponsesError(detail)
                        raise RuntimeError(detail)
                    diagnostics = _SSEDiagnosticsResponseProxy(
                        response,
                        first_line_timeout_seconds=stream_timeout_seconds,
                        idle_line_timeout_seconds=stream_timeout_seconds,
                    )
                    content, tool_calls, finish_reason, usage = await _consume_sse(diagnostics)
                    logger.debug(diagnostics.render_summary(outcome="completed"))
                    return LLMResponse(
                        content=content,
                        tool_calls=tool_calls,
                        finish_reason=finish_reason,
                        usage=usage,
                        provider_request_meta=provider_request_meta,
                        provider_request_body=provider_request_body,
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
                    provider_request_meta=provider_request_meta,
                    provider_request_body=provider_request_body,
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

