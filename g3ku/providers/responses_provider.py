"""Generic /v1/responses provider (Codex protocol) using API Key."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
from loguru import logger

from g3ku.providers.base import RETRYABLE_STATUS_CODES, LLMProvider, LLMResponse
from g3ku.providers.fallback import normalize_forced_function_tool_choice
from g3ku.providers.responses_protocol_helpers import (
    _convert_messages,
    _convert_tools,
    _consume_sse,
    _friendly_error,
    _prompt_cache_key,
)
from g3ku.providers.streaming_timeouts import (
    StreamingChunkTimeoutError,
    StreamingDiagnostics,
    notice_payload_silence,
    resolve_streaming_timeout_seconds,
)


_TLS_VERIFY_DISABLED_VALUES = frozenset({"off", "false", "0", "no"})

_tls_verify_warning_logged = False


def _tls_verify_enabled() -> bool:
    """Whether TLS certificate verification is enabled for provider requests.

    Defaults to True. Set ``G3KU_PROVIDER_TLS_VERIFY`` to off/false/0/no
    (case-insensitive, surrounding whitespace ignored) to disable; when
    disabled a security warning is logged at most once per process.
    """
    global _tls_verify_warning_logged
    value = os.environ.get("G3KU_PROVIDER_TLS_VERIFY", "").strip().lower()
    if value not in _TLS_VERIFY_DISABLED_VALUES:
        return True
    if not _tls_verify_warning_logged:
        _tls_verify_warning_logged = True
        logger.warning(
            "TLS certificate verification is DISABLED for the Responses provider "
            "(G3KU_PROVIDER_TLS_VERIFY override); API keys and conversation content "
            "can be intercepted by a network attacker"
        )
    return False


class _SSEDiagnosticsResponseProxy:
    """Wrap a streamed SSE response and apply per-line timeouts plus diagnostics."""

    def __init__(
        self,
        response: httpx.Response,
        *,
        first_line_timeout_seconds: float,
        idle_line_timeout_seconds: float,
        on_upstream_wait: Any = None,
    ) -> None:
        self._response = response
        self.status_code = response.status_code
        self._diagnostics = StreamingDiagnostics.start("responses")
        self._on_upstream_wait = on_upstream_wait
        self._first_event_received_at: float | None = None
        self._first_data_received_at: float | None = None
        self._last_event_name = ""
        self._event_line_count = 0
        self._data_line_count = 0
        self._first_line_timeout_seconds = first_line_timeout_seconds
        self._idle_line_timeout_seconds = idle_line_timeout_seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def note_terminal_event(self) -> None:
        """`response.completed` 到达时由流消费者调用：这一条车道没有 finish_reason 分片，
        终止事件就是唯一的完成凭据，缺它即"上游没说完"。
        """
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
            await notice_payload_silence(self._diagnostics, self._on_upstream_wait)
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

    # 单一来源：链侧 `is_retryable_model_error` 读同一份，两处各自维护就会分裂成
    # "provider 说可重试、链说不算"（实测过一次链耗尽审计带 retryable:true）。
    RETRYABLE_STATUS_CODES = RETRYABLE_STATUS_CODES

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

    @property
    def supports_upstream_wait_notice(self) -> bool:
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
        on_upstream_wait: Any = None,
    ) -> LLMResponse:
        model = model or self.default_model
        # The /responses API rejects the Chat-Completions nested selector
        # {"type":"function","function":{"name":X}}; rewrite it to the flat form
        # this protocol requires before building the request body.
        tool_choice = normalize_forced_function_tool_choice(tool_choice, protocol="responses")
        system_prompt, input_items = _convert_messages(messages, model=model)
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
            # The system prompt rides in `input`: /responses endpoints that proxy to a
            # Chat Completions backend reject the `instructions` field outright.
            input_items.insert(0, {
                "role": "user",
                "content": [{"type": "input_text", "text": f"[SYSTEM]\n{system_prompt}\n[END SYSTEM]"}],
            })

        # text.verbosity is an OpenAI-only extension: /responses endpoints that proxy
        # to a Chat Completions backend reject the whole request over it.
        body: dict[str, Any] = {
            "model": model,
            "store": False,
            "stream": True,
            "input": input_items,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": str(prompt_cache_key or _prompt_cache_key(messages)),
        }
        if max_tokens is not None:
            body["max_output_tokens"] = max(1, int(max_tokens))
        if temperature is not None:
            body["temperature"] = float(temperature)
        if reasoning_effort and str(reasoning_effort).strip().lower() != "none":
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
            async with httpx.AsyncClient(timeout=client_timeout, verify=_tls_verify_enabled()) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        text = await response.aread()
                        detail = _friendly_error(response.status_code, text.decode("utf-8", "ignore"))
                        if response.status_code in self.RETRYABLE_STATUS_CODES:
                            raise _RetryableResponsesError(detail, error_status=response.status_code)
                        error = RuntimeError(detail)
                        # 不可重试也把状态带上：链侧先看结构化状态，不再猜文本。
                        error.error_status = response.status_code
                        raise error
                    diagnostics = _SSEDiagnosticsResponseProxy(
                        response,
                        first_line_timeout_seconds=stream_timeout_seconds,
                        idle_line_timeout_seconds=stream_timeout_seconds,
                        on_upstream_wait=on_upstream_wait,
                    )
                    consume_kwargs: dict[str, Any] = {}
                    if on_text_delta is not None:
                        consume_kwargs["on_text_delta"] = on_text_delta
                    content, tool_calls, finish_reason, usage, reasoning_items = await _consume_sse(
                        diagnostics,
                        **consume_kwargs,
                    )
                    logger.debug(diagnostics.render_summary(outcome="completed"))
                    # 与 chat 车道同判：没收到终止事件、这一跳又没有工具调用 ⇒ 传输故障，
                    # 如实标 error 让模型链前进到下一位。error_text 避开 network / 429 关键字。
                    stream_aborted = not diagnostics._diagnostics.finish_reason_seen and not tool_calls
                    return LLMResponse(
                        content=content,
                        tool_calls=tool_calls,
                        finish_reason="error" if stream_aborted else finish_reason,
                        error_text=(
                            "stream closed before response.completed after "
                            f"{int(diagnostics._diagnostics.chunk_count)} chunks"
                            if stream_aborted
                            else None
                        ),
                        error_kind="StreamIncomplete" if stream_aborted else None,
                        usage=usage,
                        reasoning_items=reasoning_items,
                        provider_request_meta=provider_request_meta,
                        provider_request_body=provider_request_body,
                        visible_text_streamed=diagnostics._diagnostics.first_text_delta_received_at is not None,
                        stream_incomplete=not diagnostics._diagnostics.finish_reason_seen,
                        first_token_ms=diagnostics._diagnostics.first_token_ms(),
                    )
        except Exception as e:
            partial_content = str(getattr(e, "partial_content", "") or "").strip()
            error_text = self._format_error(e, url)
            diagnostics_summary = ""
            diagnostics = locals().get("diagnostics")
            if isinstance(diagnostics, _SSEDiagnosticsResponseProxy):
                diagnostics_summary = diagnostics.render_summary(outcome="failed")
            # 完整错误体外置到日志：节点错误与心跳只带 _format_error 的有界文本。
            full_error_body = str(getattr(e, "error_body", "") or "").strip()
            if full_error_body and full_error_body not in error_text:
                logger.warning("Responses stream failure body: {}", full_error_body[:4000])
            if partial_content:
                if diagnostics_summary:
                    logger.warning(diagnostics_summary)
                logger.warning("Responses API stream failed after partial content; returning partial content for structured recovery")
                return LLMResponse(
                    content=partial_content,
                    finish_reason="error",
                    error_text=error_text,
                    error_status=_exc_error_status(e),
                    error_code=_exc_error_code(e),
                    provider_request_meta=provider_request_meta,
                    provider_request_body=provider_request_body,
                    visible_text_streamed=True,
                )
            if diagnostics_summary:
                logger.warning(diagnostics_summary)
            logger.error("Error calling Responses API: {}", error_text)
            if isinstance(e, _RetryableResponsesError):
                raise
            # 重新包成普通 RuntimeError 时把结构化状态一起搬过去：链层的判据先看状态，
            # 拿不到状态才退到关键词——不搬就等于逼它去猜供应商散文。
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


def _exc_error_status(exc: Exception) -> int | None:
    """从异常上取结构化 HTTP 状态（provider 侧的 error_status/status_code）。"""
    for attr in ("error_status", "status_code", "status"):
        raw_value = getattr(exc, attr, None)
        try:
            value = int(raw_value) if raw_value is not None else None
        except (TypeError, ValueError):
            continue
        if value and value > 0:
            return value
    return None


def _exc_error_code(exc: Exception) -> str | None:
    code = getattr(exc, "error_code", None)
    return str(code).strip() if code else None


class _RetryableResponsesError(RuntimeError):
    """Transient upstream failure that outer model-chain fallback may handle."""

    def __init__(self, message: str, *, error_status: int | None = None) -> None:
        super().__init__(message)
        self.error_status = error_status

