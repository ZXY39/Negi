"""Shared fallback provider utilities for managed model chains."""

from __future__ import annotations

from typing import Any

from loguru import logger

from g3ku.config.schema import Config
from g3ku.providers.base import LLMProvider, LLMResponse


def exception_chain_text(exc: Exception) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop(0)
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        parts.append(f"{type(current).__name__}: {current}")
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if cause is not None:
            stack.append(cause)
        if context is not None:
            stack.append(context)
    return " | ".join(parts).lower()


def is_retryable_model_error(error: Exception | str, retry_on: list[str] | None = None) -> bool:
    retry_on = [str(item or "").strip().lower() for item in (retry_on or ["network", "429", "5xx"]) if str(item or "").strip()]
    if not retry_on:
        return False

    text = exception_chain_text(error) if isinstance(error, Exception) else str(error or "").lower()
    if any(token in text for token in [
        "sqlite",
        "database",
        "cursor",
        "checkpointer",
        "aiosqlite",
        "programmingerror",
        "no active connection",
        "cannot operate on a closed database",
    ]):
        return False

    token_map = {
        "network": [
            "timeout",
            "timed out",
            "network error",
            "network is unstable",
            "connecterror",
            "connect error",
            "all connection attempts failed",
            "connection reset",
            "connection refused",
            "remoteprotocolerror",
            "readerror",
            "sslerror",
        ],
        "429": [
            "429",
            "rate limit",
            "too many requests",
            "quota",
        ],
        "5xx": [
            "500",
            "502",
            "503",
            "504",
            "5xx",
            "server error",
            "temporar",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
        ],
    }
    for key in retry_on:
        if any(token in text for token in token_map.get(key, [])):
            return True
    return False


def response_requires_retry(response: LLMResponse, retry_on: list[str] | None = None) -> bool:
    if str(response.finish_reason or "").lower() != "error":
        return False
    return is_retryable_model_error(str(response.content or ""), retry_on=retry_on)


class FallbackProvider(LLMProvider):
    """LLMProvider wrapper that retries through an ordered model chain."""

    def __init__(self, *, config: Config, model_chain: list[str], default_model_ref: str):
        super().__init__(api_key=None, api_base=None)
        self._config = config
        self._model_chain = [str(item or "").strip() for item in model_chain if str(item or "").strip()]
        self._default_model_ref = str(default_model_ref or "").strip()

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
        from g3ku.providers.provider_factory import build_provider_from_model_key

        requested = str(model or "").strip()
        if requested and requested not in self._model_chain:
            chain = [requested]
        else:
            chain = list(self._model_chain or ([requested] if requested else []) or ([self._default_model_ref] if self._default_model_ref else []))

        last_error: Exception | None = None
        last_response: LLMResponse | None = None
        for model_key in chain:
            try:
                target = build_provider_from_model_key(self._config, model_key)
            except Exception as exc:
                last_error = exc
                if len(chain) > 1:
                    logger.warning("Model target init failed for {}: {}", model_key, exc)
                    continue
                raise

            effective_max_tokens = int(max_tokens)
            if target.max_tokens_limit is not None:
                effective_max_tokens = max(1, min(effective_max_tokens, int(target.max_tokens_limit)))

            effective_temperature = float(target.default_temperature if target.default_temperature is not None else temperature)
            effective_reasoning = str(target.default_reasoning_effort) if target.default_reasoning_effort is not None else reasoning_effort

            try:
                response = await target.provider.chat(
                    messages=messages,
                    tools=tools,
                    model=target.model_id,
                    max_tokens=effective_max_tokens,
                    temperature=effective_temperature,
                    reasoning_effort=effective_reasoning,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                )
            except Exception as exc:
                last_error = exc
                if not is_retryable_model_error(exc, retry_on=target.retry_on) or model_key == chain[-1]:
                    raise
                logger.warning("Model fallback triggered for {}: {}", model_key, exc)
                continue

            if response_requires_retry(response, retry_on=target.retry_on) and model_key != chain[-1]:
                last_response = response
                logger.warning("Model fallback triggered for {}: {}", model_key, response.content or response.finish_reason)
                continue
            return response

        if last_response is not None:
            return last_response
        if last_error is not None:
            raise last_error
        return LLMResponse(content="Error: no model candidate available", finish_reason="error")

    def get_default_model(self) -> str:
        return self._default_model_ref
