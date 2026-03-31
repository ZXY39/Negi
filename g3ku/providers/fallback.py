"""Shared fallback provider utilities for managed model chains."""

from __future__ import annotations

from typing import Any

from loguru import logger

from g3ku.config.schema import Config
from g3ku.providers.base import LLMProvider, LLMResponse
from g3ku.utils.api_keys import iter_api_key_retry_slots

PUBLIC_PROVIDER_FAILURE_MESSAGE = "Model provider call failed after exhausting the configured fallback chain."
RETRYABLE_MODEL_CHAIN_MAX_ROUNDS = 10
_INTERNAL_RUNTIME_ERROR_TOKENS = (
    "sqlite",
    "database",
    "cursor",
    "checkpointer",
    "aiosqlite",
    "programmingerror",
    "no active connection",
    "cannot operate on a closed database",
)
_AUTH_ERROR_TOKENS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "authentication failed",
    "auth failed",
    "invalid api key",
    "incorrect api key",
    "bad api key",
)


class ModelProviderExhaustedError(RuntimeError):
    def __init__(self, *, raw_message: str = "", retryable: bool = False) -> None:
        super().__init__(PUBLIC_PROVIDER_FAILURE_MESSAGE)
        self.raw_message = str(raw_message or "")
        self.retryable = bool(retryable)


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


def is_internal_runtime_model_error(error: Exception | str) -> bool:
    text = exception_chain_text(error) if isinstance(error, Exception) else str(error or "").lower()
    return any(token in text for token in _INTERNAL_RUNTIME_ERROR_TOKENS)


def is_retryable_model_error(error: Exception | str, retry_on: list[str] | None = None) -> bool:
    retry_on = [str(item or "").strip().lower() for item in (retry_on or ["network", "429", "5xx"]) if str(item or "").strip()]
    if not retry_on:
        return False

    text = exception_chain_text(error) if isinstance(error, Exception) else str(error or "").lower()
    if is_internal_runtime_model_error(text):
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


def is_auth_model_error(error: Exception | str) -> bool:
    text = exception_chain_text(error) if isinstance(error, Exception) else str(error or "").lower()
    if is_internal_runtime_model_error(text):
        return False
    return any(token in text for token in _AUTH_ERROR_TOKENS)


def should_rotate_api_key_error(error: Exception | str, retry_on: list[str] | None = None) -> bool:
    return is_auth_model_error(error) or is_retryable_model_error(error, retry_on=retry_on)


def should_fallback_model_error(error: Exception | str) -> bool:
    return not is_internal_runtime_model_error(error)


def response_requires_retry(response: LLMResponse, retry_on: list[str] | None = None) -> bool:
    if str(response.finish_reason or "").lower() != "error":
        return False
    error_source = str(response.error_text or response.content or "")
    return is_retryable_model_error(error_source, retry_on=retry_on)


def response_requires_api_key_rotation(response: LLMResponse, retry_on: list[str] | None = None) -> bool:
    if str(response.finish_reason or "").lower() != "error":
        return False
    error_source = str(response.error_text or response.content or "")
    return should_rotate_api_key_error(error_source, retry_on=retry_on)


def response_requires_fallback(response: LLMResponse) -> bool:
    if str(response.finish_reason or "").lower() != "error":
        return False
    error_source = str(response.error_text or response.content or "")
    return should_fallback_model_error(error_source)


def sanitize_terminal_model_error(response: LLMResponse) -> LLMResponse:
    if response_requires_fallback(response):
        response.error_text = PUBLIC_PROVIDER_FAILURE_MESSAGE
    return response


def exhausted_model_chain_error(
    error: Exception | str | None = None,
    *,
    retry_on: list[str] | None = None,
) -> ModelProviderExhaustedError:
    if isinstance(error, Exception):
        raw_message = exception_chain_text(error)
    else:
        raw_message = str(error or "")
    return ModelProviderExhaustedError(
        raw_message=raw_message,
        retryable=is_retryable_model_error(raw_message, retry_on=retry_on),
    )


def should_retry_model_chain_error(error: Exception | str, retry_on: list[str] | None = None) -> bool:
    if isinstance(error, ModelProviderExhaustedError):
        if error.retryable:
            return True
        return is_retryable_model_error(error.raw_message or str(error), retry_on=retry_on)
    return is_retryable_model_error(error, retry_on=retry_on)


def normalized_retry_count(value: int | None) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
        prompt_cache_key: str | None = None,
    ) -> LLMResponse:
        from g3ku.providers.provider_factory import build_provider_from_model_key

        requested = str(model or "").strip()
        if requested and requested not in self._model_chain:
            chain = [requested]
        else:
            chain = list(self._model_chain or ([requested] if requested else []) or ([self._default_model_ref] if self._default_model_ref else []))

        last_error: Exception | None = None
        last_response: LLMResponse | None = None
        for chain_round_index in range(RETRYABLE_MODEL_CHAIN_MAX_ROUNDS):
            round_last_error: Exception | None = None
            retry_full_chain = False
            for model_key in chain:
                try:
                    base_target = build_provider_from_model_key(self._config, model_key)
                except Exception as exc:
                    last_error = round_last_error = exc
                    if len(chain) > 1:
                        logger.warning("Model target init failed for {}: {}", model_key, exc)
                        continue
                    if should_fallback_model_error(exc):
                        exhausted = exhausted_model_chain_error(exc)
                        if should_retry_model_chain_error(exhausted) and chain_round_index < RETRYABLE_MODEL_CHAIN_MAX_ROUNDS - 1:
                            logger.warning(
                                "Retryable model-chain failure exhausted round {}/{}; retrying full chain: {}",
                                chain_round_index + 1,
                                RETRYABLE_MODEL_CHAIN_MAX_ROUNDS,
                                exhausted.raw_message or exhausted,
                            )
                            retry_full_chain = True
                            break
                        raise exhausted from exc
                    raise

                effective_max_tokens = int(max_tokens)
                if base_target.max_tokens_limit is not None:
                    effective_max_tokens = max(1, min(effective_max_tokens, int(base_target.max_tokens_limit)))

                effective_temperature = float(base_target.default_temperature if base_target.default_temperature is not None else temperature)
                effective_reasoning = str(base_target.default_reasoning_effort) if base_target.default_reasoning_effort is not None else reasoning_effort
                retry_count = normalized_retry_count(getattr(base_target, "retry_count", 0))
                move_to_next_model = False

                for slot in iter_api_key_retry_slots(api_key_count=getattr(base_target, "api_key_count", 0), retry_count=retry_count):
                    target = base_target
                    try:
                        target = base_target if slot.attempt_number == 1 else build_provider_from_model_key(
                            self._config,
                            model_key,
                            api_key_index=slot.key_index,
                        )
                        response = await target.provider.chat(
                            messages=messages,
                            tools=tools,
                            model=target.model_id,
                            max_tokens=effective_max_tokens,
                            temperature=effective_temperature,
                            reasoning_effort=effective_reasoning,
                            tool_choice=tool_choice,
                            parallel_tool_calls=parallel_tool_calls,
                            prompt_cache_key=prompt_cache_key,
                        )
                    except Exception as exc:
                        last_error = round_last_error = exc
                        rotate_key = should_rotate_api_key_error(exc, retry_on=target.retry_on)
                        if rotate_key and not slot.is_last_key:
                            logger.warning(
                                "Model key rotation triggered for {} (round {}/{}, key {}/{}): {}",
                                model_key,
                                slot.round_index + 1,
                                slot.round_count,
                                slot.key_index + 1,
                                slot.key_count,
                                exc,
                            )
                            continue
                        if rotate_key and not slot.is_last_round:
                            logger.warning(
                                "Model retry triggered for {} (round {}/{}, key {}/{}): {}",
                                model_key,
                                slot.round_index + 1,
                                slot.round_count,
                                slot.key_index + 1,
                                slot.key_count,
                                exc,
                            )
                            continue
                        if should_fallback_model_error(exc) and model_key != chain[-1]:
                            logger.warning(
                                "Model fallback triggered for {} after {} retry rounds: {}",
                                model_key,
                                retry_count,
                                exc,
                            )
                            move_to_next_model = True
                            break
                        if should_fallback_model_error(exc):
                            exhausted = exhausted_model_chain_error(exc, retry_on=target.retry_on)
                            if should_retry_model_chain_error(exhausted) and chain_round_index < RETRYABLE_MODEL_CHAIN_MAX_ROUNDS - 1:
                                logger.warning(
                                    "Retryable model-chain failure exhausted round {}/{}; retrying full chain: {}",
                                    chain_round_index + 1,
                                    RETRYABLE_MODEL_CHAIN_MAX_ROUNDS,
                                    exhausted.raw_message or exhausted,
                                )
                                retry_full_chain = True
                                break
                            raise exhausted from exc
                        raise

                    rotate_key_response = response_requires_api_key_rotation(response, retry_on=target.retry_on)
                    retryable_response = response_requires_retry(response, retry_on=target.retry_on)
                    fallback_response = response_requires_fallback(response)
                    if rotate_key_response:
                        last_response = response
                        if not slot.is_last_key:
                            logger.warning(
                                "Model key rotation triggered for {} (round {}/{}, key {}/{}): {}",
                                model_key,
                                slot.round_index + 1,
                                slot.round_count,
                                slot.key_index + 1,
                                slot.key_count,
                                response.content or response.finish_reason,
                            )
                            continue
                        if not slot.is_last_round:
                            logger.warning(
                                "Model retry triggered for {} (round {}/{}, key {}/{}): {}",
                                model_key,
                                slot.round_index + 1,
                                slot.round_count,
                                slot.key_index + 1,
                                slot.key_count,
                                response.content or response.finish_reason,
                            )
                            continue
                    if fallback_response and model_key != chain[-1]:
                        logger.warning(
                            "Model fallback triggered for {} after {} retry rounds: {}",
                            model_key,
                            retry_count,
                            response.content or response.finish_reason,
                        )
                        move_to_next_model = True
                        break
                    if fallback_response:
                        last_response = sanitize_terminal_model_error(response)
                        if retryable_response and chain_round_index < RETRYABLE_MODEL_CHAIN_MAX_ROUNDS - 1:
                            logger.warning(
                                "Retryable model-chain response exhausted round {}/{}; retrying full chain: {}",
                                chain_round_index + 1,
                                RETRYABLE_MODEL_CHAIN_MAX_ROUNDS,
                                response.error_text or response.content or response.finish_reason,
                            )
                            retry_full_chain = True
                            break
                        return last_response
                    return response

                if retry_full_chain:
                    break
                if move_to_next_model:
                    continue

            if retry_full_chain:
                continue
            if round_last_error is not None and should_retry_model_chain_error(round_last_error) and chain_round_index < RETRYABLE_MODEL_CHAIN_MAX_ROUNDS - 1:
                logger.warning(
                    "Retryable model-chain round {}/{} ended without success; retrying full chain: {}",
                    chain_round_index + 1,
                    RETRYABLE_MODEL_CHAIN_MAX_ROUNDS,
                    getattr(round_last_error, "raw_message", str(round_last_error)) or round_last_error,
                )
                continue
            break

        if last_response is not None:
            return sanitize_terminal_model_error(last_response)
        if last_error is not None:
            if should_fallback_model_error(last_error):
                raise exhausted_model_chain_error(last_error) from last_error
            raise last_error
        return LLMResponse(content="Error: no model candidate available", finish_reason="error")

    def get_default_model(self) -> str:
        return self._default_model_ref
