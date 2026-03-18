from __future__ import annotations

from typing import Protocol

from g3ku.config.schema import Config
from g3ku.providers.provider_factory import build_provider_from_model_key
from g3ku.providers.base import LLMModelAttempt, LLMResponse, normalize_usage_payload
from g3ku.providers.fallback import (
    is_retryable_model_error,
    normalized_retry_count,
    response_requires_retry,
)


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
        parallel_tool_calls: bool | None = None,
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
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        refs = [str(item or '').strip() for item in list(model_refs or []) if str(item or '').strip()]
        if not refs:
            raise ValueError('model_refs must not be empty')
        last_error: Exception | None = None
        last_response: LLMResponse | None = None
        attempts: list[LLMModelAttempt] = []
        for index, ref in enumerate(refs):
            target = build_provider_from_model_key(self._config, ref)
            retry_count = normalized_retry_count(getattr(target, "retry_count", 0))
            move_to_next_model = False
            for retry_index in range(retry_count + 1):
                try:
                    response = await target.provider.chat(
                        messages=messages,
                        tools=tools,
                        model=target.model_id,
                        max_tokens=max(1, min(int(max_tokens), int(target.max_tokens_limit))) if target.max_tokens_limit else max(1, int(max_tokens)),
                        temperature=float(target.default_temperature) if target.default_temperature is not None else float(temperature),
                        reasoning_effort=target.default_reasoning_effort or reasoning_effort,
                        tool_choice='auto',
                        parallel_tool_calls=parallel_tool_calls,
                    )
                except Exception as exc:
                    last_error = exc
                    retryable = is_retryable_model_error(exc, retry_on=target.retry_on)
                    if retryable and retry_index < retry_count:
                        continue
                    if retryable and index < len(refs) - 1:
                        move_to_next_model = True
                        break
                    raise
                response.usage = normalize_usage_payload(response.usage)
                response_attempts = list(response.attempts or [])
                if not response_attempts:
                    response_attempts = [
                        LLMModelAttempt(
                            model_key=target.provider_ref,
                            provider_id=target.provider_id,
                            provider_model=target.model_id,
                            usage=dict(response.usage or {}),
                            finish_reason=str(response.finish_reason or 'stop'),
                        )
                    ]
                attempts.extend(response_attempts)
                response.attempts = list(attempts)
                last_response = response
                if response_requires_retry(response, retry_on=target.retry_on):
                    if retry_index < retry_count:
                        continue
                    if index < len(refs) - 1:
                        move_to_next_model = True
                        break
                return response
            if move_to_next_model:
                continue
        if last_error is not None:
            raise last_error
        if last_response is None:
            raise RuntimeError('chat backend returned no response')
        last_response.attempts = list(attempts)
        return last_response
