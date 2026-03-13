from __future__ import annotations

from typing import Protocol

from g3ku.config.schema import Config
from g3ku.providers.provider_factory import build_provider_from_model_key
from g3ku.providers.base import LLMResponse
from g3ku.providers.fallback import is_retryable_model_error, response_requires_retry


class ChatBackend(Protocol):
    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        model_refs: list[str],
        max_tokens: int = 1200,
        temperature: float = 0.2,
        reasoning_effort: str | None = None,
    ) -> LLMResponse: ...


class ConfigChatBackend:
    def __init__(self, config: Config):
        self._config = config

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        model_refs: list[str],
        max_tokens: int = 1200,
        temperature: float = 0.2,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        refs = [str(item or '').strip() for item in list(model_refs or []) if str(item or '').strip()]
        if not refs:
            raise ValueError('model_refs must not be empty')
        last_error: Exception | None = None
        last_response: LLMResponse | None = None
        for index, ref in enumerate(refs):
            target = build_provider_from_model_key(self._config, ref)
            try:
                response = await target.provider.chat(
                    messages=messages,
                    tools=tools,
                    model=target.model_id,
                    max_tokens=max(1, min(int(max_tokens), int(target.max_tokens_limit))) if target.max_tokens_limit else max(1, int(max_tokens)),
                    temperature=float(target.default_temperature) if target.default_temperature is not None else float(temperature),
                    reasoning_effort=target.default_reasoning_effort or reasoning_effort,
                    tool_choice='auto',
                    parallel_tool_calls=False,
                )
            except Exception as exc:
                last_error = exc
                if index < len(refs) - 1 and is_retryable_model_error(exc, retry_on=target.retry_on):
                    continue
                raise
            last_response = response
            if index < len(refs) - 1 and response_requires_retry(response, retry_on=target.retry_on):
                continue
            return response
        if last_error is not None:
            raise last_error
        if last_response is None:
            raise RuntimeError('chat backend returned no response')
        return last_response
