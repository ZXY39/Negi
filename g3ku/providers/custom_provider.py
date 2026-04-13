"""Direct OpenAI-compatible provider 鈥?bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

import json_repair
from openai import AsyncOpenAI

from g3ku.providers.base import LLMProvider, LLMResponse, ToolCallRequest, normalize_usage_payload
from g3ku.providers.streaming_timeouts import (
    StreamingDiagnostics,
    consume_openai_like_chat_stream,
    resolve_non_streaming_timeout_seconds,
    resolve_streaming_timeout_seconds,
    should_fallback_to_non_streaming_from_error,
)


class CustomProvider(LLMProvider):

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str = "http://localhost:8000/v1",
        default_model: str = "default",
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = dict(extra_headers or {})
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            default_headers=self.extra_headers or None,
        )

    @property
    def manages_request_timeout_internally(self) -> bool:
        return True

    @property
    def supports_streaming(self) -> bool:
        return True

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int | None = None, temperature: float | None = None,
                   reasoning_effort: str | None = None,
                   tool_choice: str | dict[str, Any] | None = None,
                   parallel_tool_calls: bool | None = None,
                   prompt_cache_key: str | None = None,
                   request_timeout_seconds: float | None = None) -> LLMResponse:
        del prompt_cache_key
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max(1, int(max_tokens))
        if temperature is not None:
            kwargs["temperature"] = float(temperature)
        if self.extra_headers:
            kwargs["extra_headers"] = dict(self.extra_headers)
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice if tool_choice is not None else "auto")
            if parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = bool(parallel_tool_calls)
        endpoint = f"{self.api_base.rstrip('/')}/chat/completions" if self.api_base else None
        self._trace_request_payload(
            provider="custom",
            endpoint=endpoint,
            body=dict(kwargs),
        )
        try:
            stream_timeout_seconds = resolve_streaming_timeout_seconds(request_timeout_seconds)
            stream_kwargs = {
                **kwargs,
                "stream": True,
                "stream_options": {"include_usage": True},
                "timeout": float(stream_timeout_seconds),
            }
            try:
                stream = await self._client.chat.completions.create(**stream_kwargs)
                diagnostics = StreamingDiagnostics.start("custom")
                content, tool_calls, finish_reason, usage, reasoning_content = await consume_openai_like_chat_stream(
                    stream,
                    diagnostics=diagnostics,
                    first_chunk_timeout_seconds=stream_timeout_seconds,
                    idle_chunk_timeout_seconds=stream_timeout_seconds,
                )
                from loguru import logger
                logger.debug(diagnostics.render_summary(outcome="completed"))
                return LLMResponse(
                    content=content,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    usage=usage,
                    reasoning_content=reasoning_content,
                )
            except Exception as stream_exc:
                if not should_fallback_to_non_streaming_from_error(stream_exc):
                    raise
            non_stream_timeout_seconds = resolve_non_streaming_timeout_seconds(request_timeout_seconds)
            return self._parse(await self._client.chat.completions.create(**{**kwargs, "timeout": float(non_stream_timeout_seconds)}))
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    def _parse(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCallRequest(id=tc.id, name=tc.function.name,
                            arguments=json_repair.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        return LLMResponse(
            content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason or "stop",
            usage=normalize_usage_payload(getattr(response, "usage", None)),
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    def get_default_model(self) -> str:
        return self.default_model


