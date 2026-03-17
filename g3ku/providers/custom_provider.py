"""Direct OpenAI-compatible provider 鈥?bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

import json_repair
from openai import AsyncOpenAI

from g3ku.providers.base import LLMProvider, LLMResponse, ToolCallRequest, normalize_usage_payload


class CustomProvider(LLMProvider):

    def __init__(self, api_key: str = "no-key", api_base: str = "http://localhost:8000/v1", default_model: str = "default"):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self._client = AsyncOpenAI(api_key=api_key, base_url=api_base)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                   reasoning_effort: str | None = None,
                   tool_choice: str | dict[str, Any] | None = None,
                   parallel_tool_calls: bool | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
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


