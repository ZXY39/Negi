"""OpenAI Codex Responses Provider."""

import asyncio
import json
import time
from typing import Any

import httpx
from loguru import logger
from oauth_cli_kit import get_token as get_codex_token

from g3ku.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from g3ku.providers.responses_protocol_helpers import (  # noqa: F401 (compat re-exports)
    CodexStreamError,
    _consume_sse,
    _convert_messages,
    _convert_tools,
    _friendly_error,
    _iter_sse,
    _prompt_cache_key,
    _strip_model_prefix,
)
from g3ku.providers.streaming_timeouts import (
    StreamingChunkTimeoutError,
    StreamingDiagnostics,
    resolve_streaming_timeout_seconds,
)

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "g3ku"


class OpenAICodexProvider(LLMProvider):
    """Use Codex OAuth to call the Responses API."""

    def __init__(self, default_model: str = "openai-codex/gpt-5.1-codex"):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model

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
        on_text_delta: Any = None,
    ) -> LLMResponse:
        model = model or self.default_model
        system_prompt, input_items = _convert_messages(messages)

        token = await asyncio.to_thread(get_codex_token)
        headers = _build_headers(token.account_id, token.access)

        body: dict[str, Any] = {
            "model": _strip_model_prefix(model),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
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

        url = DEFAULT_CODEX_URL
        provider_request_meta, provider_request_body = self._capture_request_payload(
            provider="openai_codex",
            endpoint=url,
            body=body,
        )

        try:
            stream_timeout_seconds = resolve_streaming_timeout_seconds(request_timeout_seconds)
            try:
                request_codex_kwargs = {
                    "verify": True,
                    "timeout": stream_timeout_seconds,
                }
                if on_text_delta is not None:
                    request_codex_kwargs["on_text_delta"] = on_text_delta
                content, tool_calls, finish_reason, usage = await _request_codex(
                    url,
                    headers,
                    body,
                    **request_codex_kwargs,
                )
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                logger.warning("SSL certificate verification failed for Codex API; retrying with verify=False")
                retry_request_codex_kwargs = {
                    "verify": False,
                    "timeout": stream_timeout_seconds,
                }
                if on_text_delta is not None:
                    retry_request_codex_kwargs["on_text_delta"] = on_text_delta
                content, tool_calls, finish_reason, usage = await _request_codex(
                    url,
                    headers,
                    body,
                    **retry_request_codex_kwargs,
                )
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                provider_request_meta=provider_request_meta,
                provider_request_body=provider_request_body,
                visible_text_streamed=bool(content),
            )
        except Exception as e:
            partial_content = str(getattr(e, "partial_content", "") or "").strip()
            if partial_content:
                logger.warning("Codex stream failed after partial content; returning partial content for JSON recovery")
                return LLMResponse(
                    content=partial_content,
                    finish_reason="error",
                    provider_request_meta=provider_request_meta,
                    provider_request_body=provider_request_body,
                    visible_text_streamed=True,
                )
            return LLMResponse(
                content=f"Error calling Codex: {str(e)}",
                finish_reason="error",
                provider_request_meta=provider_request_meta,
                provider_request_body=provider_request_body,
            )

    def get_default_model(self) -> str:
        return self.default_model


def _build_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "g3ku (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
    timeout: float | None,
    on_text_delta: Any = None,
) -> tuple[str, list[ToolCallRequest], str, dict[str, int]]:
    client_timeout = float(timeout) if timeout is not None else 60.0
    async with httpx.AsyncClient(timeout=client_timeout, verify=verify) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(_friendly_error(response.status_code, text.decode("utf-8", "ignore")))
            diagnostics = _CodexSSEDiagnosticsProxy(
                response,
                first_line_timeout_seconds=client_timeout,
                idle_line_timeout_seconds=client_timeout,
            )
            try:
                consume_kwargs: dict[str, Any] = {}
                if on_text_delta is not None:
                    consume_kwargs["on_text_delta"] = on_text_delta
                content, tool_calls, finish_reason, usage = await _consume_sse(
                    diagnostics,
                    **consume_kwargs,
                )
            except Exception:
                logger.warning(diagnostics.render_summary(outcome="failed"))
                raise
            logger.debug(diagnostics.render_summary(outcome="completed"))
            return content, tool_calls, finish_reason, usage


class _CodexSSEDiagnosticsProxy:
    def __init__(
        self,
        response: httpx.Response,
        *,
        first_line_timeout_seconds: float,
        idle_line_timeout_seconds: float,
    ) -> None:
        self._response = response
        self._diagnostics = StreamingDiagnostics.start("openai_codex")
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
                        f"Codex stream timeout waiting for first chunk after {timeout_seconds:.3f}s"
                    ) from exc
                raise StreamingChunkTimeoutError(
                    f"Codex stream idle timeout after {timeout_seconds:.3f}s without a new chunk"
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
                "first_event_received_ms": elapsed_ms(self._first_event_received_at),
                "first_data_received_ms": elapsed_ms(self._first_data_received_at),
                "last_event": self._last_event_name or "<none>",
                "event_line_count": self._event_line_count,
                "data_line_count": self._data_line_count,
            },
        )
