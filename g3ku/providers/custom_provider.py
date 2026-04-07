"""Direct OpenAI-compatible provider 鈥?bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

import json_repair
from openai import AsyncOpenAI

from g3ku.providers.base import LLMProvider, LLMResponse, ToolCallRequest, normalize_usage_payload


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
        if request_timeout_seconds is not None:
            kwargs["timeout"] = float(request_timeout_seconds)
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
            return self._parse(await self._client.chat.completions.create(**kwargs))
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


