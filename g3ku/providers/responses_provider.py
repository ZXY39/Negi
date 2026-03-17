"""Generic /v1/responses provider (Codex protocol) using API Key."""

from __future__ import annotations

import asyncio
import json
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


class ResponsesProvider(LLMProvider):
    """Call any /v1/responses endpoint with an API Key."""

    NETWORK_RETRY_LIMIT = 10
    NETWORK_RETRY_DELAY_SECONDS = 1.0
    NETWORK_FALLBACK_MESSAGE = "Network is unstable right now. Please retry shortly."
    RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

    def __init__(self, api_key: str, api_base: str, default_model: str = "gpt-5.3-codex"):
        super().__init__(api_key=api_key, api_base=api_base)
        self.default_model = default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        model = model or self.default_model
        system_prompt, input_items = _convert_messages(messages)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "responses=experimental",
            "originator": "g3ku",
            "User-Agent": "g3ku (python)",
            "accept": "text/event-stream",
            "content-type": "application/json",
        }

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
            "prompt_cache_key": _prompt_cache_key(messages),
        }

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
            last_network_exc: Exception | None = None
            for attempt in range(1, self.NETWORK_RETRY_LIMIT + 1):
                try:
                    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
                        async with client.stream("POST", url, headers=headers, json=body) as response:
                            if response.status_code != 200:
                                text = await response.aread()
                                detail = _friendly_error(response.status_code, text.decode("utf-8", "ignore"))
                                if response.status_code in self.RETRYABLE_STATUS_CODES:
                                    raise _RetryableResponsesError(detail)
                                raise RuntimeError(detail)
                            content, tool_calls, finish_reason, usage = await _consume_sse(response)
                            return LLMResponse(
                                content=content,
                                tool_calls=tool_calls,
                                finish_reason=finish_reason,
                                usage=usage,
                            )
                except Exception as exc:
                    if not self._is_retryable_network_error(exc):
                        raise
                    last_network_exc = exc
                    if attempt >= self.NETWORK_RETRY_LIMIT:
                        break
                    logger.warning(
                        "Responses API transient error (attempt {}/{}): {}",
                        attempt,
                        self.NETWORK_RETRY_LIMIT,
                        self._format_error(exc, url),
                    )
                    await asyncio.sleep(self.NETWORK_RETRY_DELAY_SECONDS)
            if last_network_exc is not None:
                logger.error(
                    "Responses API network retry exhausted after {} attempts: {}",
                    self.NETWORK_RETRY_LIMIT,
                    self._format_error(last_network_exc, url),
                )
                return LLMResponse(
                    content=self.NETWORK_FALLBACK_MESSAGE,
                    finish_reason="error",
                )
        except Exception as e:
            error_text = self._format_error(e, url)
            logger.error("Error calling Responses API: {}", error_text)
            return LLMResponse(
                content=f"Error calling Responses API: {error_text}",
                finish_reason="error",
            )

    def get_default_model(self) -> str:
        return self.default_model

    @staticmethod
    def _is_retryable_network_error(exc: Exception) -> bool:
        if isinstance(exc, _RetryableResponsesError):
            return True
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return True
        text = str(exc).strip().lower()
        if not text:
            return False
        return "codex response failed" in text

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
    """Transient upstream failure that should be retried quietly."""

