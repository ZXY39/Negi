"""Shared fallback provider utilities for managed model chains."""

from __future__ import annotations

import asyncio
import inspect
import random
from typing import Any

from loguru import logger

from g3ku.config.schema import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_REASONING_EFFORT,
    Config,
    normalize_reasoning_effort,
)
from g3ku.prompt_trace import render_model_chain_trace
from g3ku.providers.base import LLMProvider, LLMResponse
from g3ku.utils.api_keys import APIKeyConfigurationError, iter_api_key_retry_slots
from g3ku.utils.retry_keywords import (
    DEFAULT_RETRY_ON_KEYWORDS,
    expand_retry_keywords,
    split_retry_keywords,
)

PUBLIC_PROVIDER_FAILURE_MESSAGE = "Model provider call failed after exhausting the configured fallback chain."
# Shared per-single-request provider response timeout for CEO and task-runtime
# model chains: one provider request (single attempt) may take at most 10
# minutes. This replaces the previous accumulated
# "attempt timeout x attempts x chain rounds" total-budget semantics; retryable
# chain retries are now unbounded in count and paced by backoff instead.
DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS = 600.0
# Retryable model-chain failures retry with capped exponential backoff plus
# jitter so concurrent nodes do not stampede the same rate window. The retry is
# bounded by a cumulative backoff budget (below), not by count.
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_BACKOFF_CAP_SECONDS = 60.0
RETRY_BACKOFF_JITTER_RATIO = 0.25
# 整链可重试退避的累计上限：一个 turn 在"退避等待"上最多累计花这么多秒，超过即中止整链
# 重试并按耗尽处理。**只计退避等待，不含请求本身耗时**（请求耗时另由
# DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS 单次约束）。默认 20 分钟，可调。
MAX_RETRYABLE_CHAIN_BACKOFF_SECONDS = 1200.0
_INTERNAL_RUNTIME_ERROR_TOKENS = (
    "sqlite",
    "database",
    "cursor",
    "aiosqlite",
    "programmingerror",
    "no active connection",
    "cannot operate on a closed database",
)


class ModelProviderExhaustedError(RuntimeError):
    def __init__(
        self,
        *,
        raw_message: str = "",
        retryable: bool = False,
        message: str = "",
        config_revision_changed: bool = False,
    ) -> None:
        # Surface the original provider error untouched; fall back to the
        # public message only when no raw error text is available.
        super().__init__(str(message or "").strip() or PUBLIC_PROVIDER_FAILURE_MESSAGE)
        self.raw_message = str(raw_message or "")
        self.retryable = bool(retryable)
        # Set when the chain retry loop aborted because the runtime config
        # revision changed mid-retry, so callers should rebuild/restart with
        # the refreshed model chain instead of counting a normal retry attempt.
        self.config_revision_changed = bool(config_revision_changed)


class ModelProviderResponseError(RuntimeError):
    """provider 返回 finish_reason="error" 的终态响应时抛出，携带结构化错误信号。

    继承 RuntimeError 以兼容既有 `except RuntimeError`。带 `.code` / `.message` /
    `.recoverable` 三个属性，使 session_agent 的错误分类器（`all(hasattr(exc, k) for k
    in ("code","message","recoverable"))`）能取到真实 provider code（如
    `insufficient_quota`），而不是退化成 `legacy_session_error`。`.status` / `.kind`
    供需要 HTTP 状态或异常类别的下游使用。完整错误原文保留在 message/raw_message。
    """

    def __init__(
        self,
        *,
        message: str = "",
        code: str = "",
        status: int | None = None,
        kind: str = "",
        recoverable: bool = True,
        raw_message: str = "",
    ) -> None:
        resolved_message = str(message or "").strip() or str(raw_message or "").strip() or PUBLIC_PROVIDER_FAILURE_MESSAGE
        super().__init__(resolved_message)
        self.message = resolved_message
        self.code = str(code or "").strip() or "model_provider_error"
        self.status = status
        self.kind = str(kind or "").strip()
        self.recoverable = bool(recoverable)
        self.raw_message = str(raw_message or "").strip() or resolved_message


class ModelAttemptTimeoutError(TimeoutError):
    def __init__(
        self,
        *,
        timeout_seconds: float,
        model_ref: str,
        provider_id: str,
        provider_model: str,
        key_index: int | None = None,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.model_ref = str(model_ref or "").strip()
        self.provider_id = str(provider_id or "").strip()
        self.provider_model = str(provider_model or "").strip()
        self.key_index = None if key_index is None else max(0, int(key_index))
        details: list[str] = []
        if self.model_ref:
            details.append(f"model_ref={self.model_ref}")
        if self.provider_id:
            details.append(f"provider_id={self.provider_id}")
        if self.provider_model:
            details.append(f"provider_model={self.provider_model}")
        if self.key_index is not None:
            details.append(f"key_index={self.key_index}")
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"model attempt timeout after {self.timeout_seconds:.3f}s{suffix}")


def normalize_request_timeout_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    if normalized <= 0:
        return None
    return normalized


async def wait_for_model_attempt(
    awaitable,
    *,
    timeout_seconds: float | None,
    model_ref: str,
    provider_id: str,
    provider_model: str,
    key_index: int | None = None,
):
    normalized_timeout = normalize_request_timeout_seconds(timeout_seconds)
    if normalized_timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=normalized_timeout)
    except asyncio.TimeoutError as exc:
        raise ModelAttemptTimeoutError(
            timeout_seconds=normalized_timeout,
            model_ref=model_ref,
            provider_id=provider_id,
            provider_model=provider_model,
            key_index=key_index,
        ) from exc


def _exception_chain_parts(exc: Exception) -> list[str]:
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
    return parts


def exception_chain_text(exc: Exception) -> str:
    return " | ".join(_exception_chain_parts(exc)).lower()


def exception_chain_display_text(exc: Exception) -> str:
    """Case-preserving variant of :func:`exception_chain_text` for user display."""
    return " | ".join(_exception_chain_parts(exc))


def is_internal_runtime_model_error(error: Exception | str) -> bool:
    text = exception_chain_text(error) if isinstance(error, Exception) else str(error or "").lower()
    return any(token in text for token in _INTERNAL_RUNTIME_ERROR_TOKENS)


def is_retryable_model_error(error: Exception | str, retry_on: list[str] | None = None) -> bool:
    # retry_on=None（未设置）用默认关键字；retry_on=[]（显式置空）→ 无关键字 → 不可重试。
    keywords = split_retry_keywords(DEFAULT_RETRY_ON_KEYWORDS if retry_on is None else retry_on)
    if not keywords:
        return False

    text = exception_chain_text(error) if isinstance(error, Exception) else str(error or "").lower()
    if is_internal_runtime_model_error(text):
        return False

    return any(token in text for token in expand_retry_keywords(keywords))


# 请求体/参数形状错误的 HTTP 状态：换一把 key 修不了畸形 payload，不轮换、快速失败。
_REQUEST_SHAPE_STATUS_CODES = frozenset({400, 422})
# 结构化 status 取不到时（如纯文本 RuntimeError）的窄文本信号——只认标准的请求形状措辞，
# 不做模糊数字匹配（避免误伤 "400 tokens" 之类）。
_REQUEST_SHAPE_TEXT_TOKENS = ("bad request", "badrequesterror", "invalid_request_error", "invalid request")


def _error_status_code(error: Any) -> int | None:
    """从 LLMResponse / 结构化异常 / SDK 异常里提取 HTTP 状态码，取不到返回 None。"""
    for attr in ("error_status", "status_code", "status"):
        raw = getattr(error, attr, None)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def is_request_shape_error(error: Exception | str | Any) -> bool:
    """是否为请求体/参数形状错误（400/422 类）：这类错误换 key 无用，应快速失败不轮换。

    优先用结构化 HTTP 状态（3.1 起 LLMResponse.error_status / SDK status_code 可得）；
    退化到窄文本信号（标准 bad-request 措辞），不做模糊数字匹配。
    """
    if _error_status_code(error) in _REQUEST_SHAPE_STATUS_CODES:
        return True
    text = exception_chain_text(error) if isinstance(error, Exception) else str(error or "")
    text = text.lower()
    return any(token in text for token in _REQUEST_SHAPE_TEXT_TOKENS)


def should_rotate_api_key_error(error: Exception | str, retry_on: list[str] | None = None) -> bool:
    """换 key 判据：内部运行时错误不换；请求体形状错误（400/422）不换；retryOn 命中
    （判定可重试）不换、走重试路径；其余（未命中且非请求形状错误）才换 key（换完再切模型）。

    去掉旧的 auth 关键字判据——它易误判，且坏 key（401/403）在默认 retryOn 不含这些码时
    本就走"未命中 → 换 key"路径，无需单独的 auth 分支。请求形状错误（400/bad request）
    单独豁免：换一把 key 修不了畸形 payload，只会在每把 key 上重发同一坏请求（保留旧的
    bad-request 快速失败智慧）。轮换是最便宜的自愈，位于切模型与整链重试之前。注意：把
    401 之类配进 retryOn 会让坏 key 只重试不换 key（配置脚枪，详见 config-and-models.md）。
    """
    text = exception_chain_text(error) if isinstance(error, Exception) else str(error or "")
    if is_internal_runtime_model_error(text):
        return False
    if is_request_shape_error(error):
        return False
    return not is_retryable_model_error(error, retry_on=retry_on)


def should_fallback_model_error(error: Exception | str) -> bool:
    if isinstance(error, APIKeyConfigurationError):
        return False
    return not is_internal_runtime_model_error(error)


def response_requires_retry(response: LLMResponse, retry_on: list[str] | None = None) -> bool:
    if str(response.finish_reason or "").lower() != "error":
        return False
    error_source = str(response.error_text or response.content or "")
    return is_retryable_model_error(error_source, retry_on=retry_on)


def response_requires_api_key_rotation(response: LLMResponse, retry_on: list[str] | None = None) -> bool:
    if str(response.finish_reason or "").lower() != "error":
        return False
    # 用 response 的结构化 error_status 判请求形状错误（下面的 error_source 字符串拿不到 status）。
    if is_request_shape_error(response):
        return False
    error_source = str(response.error_text or response.content or "")
    return should_rotate_api_key_error(error_source, retry_on=retry_on)


def response_requires_fallback(response: LLMResponse) -> bool:
    if str(response.finish_reason or "").lower() != "error":
        return False
    error_source = str(response.error_text or response.content or "")
    return should_fallback_model_error(error_source)


def _is_bare_error_prefix(text: str) -> bool:
    normalized = str(text or "").strip()
    return normalized.lower() in {"error", "error:", "none"}


def sanitize_terminal_model_error(response: LLMResponse) -> LLMResponse:
    # Keep the provider's original error text so failures surface unwrapped;
    # only backfill the public message when there is no error detail at all.
    error_detail = str(response.error_text or response.content or "").strip()
    if _is_bare_error_prefix(error_detail):
        error_detail = ""

    if response_requires_fallback(response) and not error_detail:
        public = PUBLIC_PROVIDER_FAILURE_MESSAGE
        # Preserve the caller-visible detail so a node pause / session error that
        # surfaces through an error response carries complete, non-empty text.
        if response.error_text is not None and str(response.error_text or "").strip():
            response.error_text = f"{str(response.error_text or '').strip()} - {public}"
        else:
            response.error_text = public
    return response


def exhausted_model_chain_error(
    error: Exception | str | None = None,
    *,
    retry_on: list[str] | None = None,
) -> ModelProviderExhaustedError:
    if isinstance(error, Exception):
        raw_message = exception_chain_text(error)
        display_message = exception_chain_display_text(error)
    else:
        raw_message = str(error or "")
        display_message = raw_message
    return ModelProviderExhaustedError(
        raw_message=raw_message,
        message=display_message,
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


def model_retry_backoff_seconds(attempt_number: int) -> float:
    """Capped exponential backoff with jitter for retryable model-chain rounds.

    Retryable chain failures retry indefinitely; pacing comes from this delay.
    Jitter keeps concurrently retrying nodes from waking up in lockstep and
    hammering the same still-exhausted rate window.
    """
    exponent = max(0, int(attempt_number or 1) - 1)
    delay = min(RETRY_BACKOFF_CAP_SECONDS, RETRY_BACKOFF_BASE_SECONDS * (2.0 ** exponent))
    jitter = delay * RETRY_BACKOFF_JITTER_RATIO
    return max(0.1, delay + random.uniform(-jitter, jitter))


def current_runtime_config_revision() -> int:
    """Best-effort snapshot of the live runtime config revision.

    Used by model-chain retry loops to detect route/binding changes made while
    they retry, so they can abort and let callers rebuild with the fresh chain.
    Returns 0 when the revision is unavailable.
    """
    try:
        from g3ku.config.live_runtime import peek_runtime_revision

        return int(peek_runtime_revision() or 0)
    except Exception:
        return 0


def retryable_chain_config_changed_error(reason: str = "") -> ModelProviderExhaustedError:
    """Retryable exhaustion raised when the runtime config revision changed mid-retry."""
    message = str(reason or "").strip() or "runtime config revision changed during model chain retry"
    return ModelProviderExhaustedError(
        raw_message=message,
        message=message,
        retryable=True,
        config_revision_changed=True,
    )


def _build_provider_target_compat(build_provider_from_model_key, config: Config, model_key: str, *, api_key_index: int | None = None):
    if api_key_index is None:
        return build_provider_from_model_key(config, model_key)
    try:
        parameters = inspect.signature(build_provider_from_model_key).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "api_key_index" in parameters:
        return build_provider_from_model_key(config, model_key, api_key_index=api_key_index)
    return build_provider_from_model_key(config, model_key)


def _log_model_chain_retry(*, model_ref: str, reason: Any) -> None:
    logger.warning(
        render_model_chain_trace(
            title="RETRY",
            severity="retry",
            lines=[
                f"model_ref: {str(model_ref or '').strip()}",
                f"reason: {str(reason or '').strip()}",
            ],
        )
    )


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
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        prompt_cache_key: str | None = None,
        request_timeout_seconds: float | None = DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
    ) -> LLMResponse:
        from g3ku.providers.provider_factory import build_provider_from_model_key

        requested = str(model or "").strip()
        if requested and requested not in self._model_chain:
            chain = [requested]
        else:
            chain = list(self._model_chain or ([requested] if requested else []) or ([self._default_model_ref] if self._default_model_ref else []))

        last_error: Exception | None = None
        last_response: LLMResponse | None = None
        start_revision = current_runtime_config_revision()
        chain_round_index = 0
        retryable_backoff_count = 0
        # 整链可重试退避的累计秒数（只计 sleep 等待，不含请求耗时），用于 20 分钟上限。
        cumulative_backoff_seconds = 0.0
        while True:
            chain_round_index += 1
            round_last_error: Exception | None = None
            retry_full_chain = False
            retry_full_chain_reason = ""
            for model_key in chain:
                try:
                    base_target = build_provider_from_model_key(self._config, model_key)
                except Exception as exc:
                    last_error = round_last_error = exc
                    if len(chain) > 1:
                        logger.warning("Model target init failed for {}: {}", model_key, exc)
                        continue
                    if should_fallback_model_error(exc):
                        profile = self._config.get_model_runtime_profile(model_key)
                        exhausted = exhausted_model_chain_error(
                            exc,
                            retry_on=list(profile.retry_on) if profile is not None else None,
                        )
                        if should_retry_model_chain_error(exhausted):
                            retry_full_chain = True
                            retry_full_chain_reason = exhausted.raw_message or str(exhausted)
                            break
                        raise exhausted from exc
                    raise
                configured_api_key_indexes = getattr(base_target, "api_key_indexes", None)
                if configured_api_key_indexes is None:
                    api_key_indexes = list(range(max(1, int(getattr(base_target, "api_key_count", 0) or 0))))
                else:
                    api_key_indexes = [int(item) for item in configured_api_key_indexes]
                if int(getattr(base_target, "api_key_count", 0) or 0) > 0 and not api_key_indexes:
                    raise APIKeyConfigurationError(f"All configured API keys are disabled for model {model_key}")

                target_parameters = dict(getattr(base_target, "model_parameters", {}) or {})
                if target_parameters.get("max_tokens") is None and getattr(base_target, "max_tokens_limit", None) is not None:
                    target_parameters["max_tokens"] = getattr(base_target, "max_tokens_limit", None)
                if target_parameters.get("temperature") is None and getattr(base_target, "default_temperature", None) is not None:
                    target_parameters["temperature"] = getattr(base_target, "default_temperature", None)
                if not str(target_parameters.get("reasoning_effort") or "").strip() and getattr(base_target, "default_reasoning_effort", None) is not None:
                    target_parameters["reasoning_effort"] = getattr(base_target, "default_reasoning_effort", None)
                # Per-model parameters from the llm-config record win over the
                # engine-global defaults; when neither is configured the global
                # output default applies so requests always carry an explicit cap.
                effective_max_tokens = (
                    max(1, int(target_parameters["max_tokens"]))
                    if target_parameters.get("max_tokens") is not None
                    else max(1, int(max_tokens))
                    if max_tokens is not None
                    else max(1, int(DEFAULT_MAX_OUTPUT_TOKENS))
                )
                effective_temperature = (
                    float(temperature)
                    if temperature is not None
                    else float(target_parameters["temperature"])
                    if target_parameters.get("temperature") is not None
                    else None
                )
                configured_reasoning = str(target_parameters.get("reasoning_effort") or "").strip()
                effective_reasoning = (
                    normalize_reasoning_effort(configured_reasoning)
                    if configured_reasoning
                    else normalize_reasoning_effort(reasoning_effort)
                    if reasoning_effort is not None and str(reasoning_effort).strip()
                    else normalize_reasoning_effort(DEFAULT_REASONING_EFFORT)
                )
                if str(effective_reasoning or "").strip().lower() == "none":
                    effective_reasoning = None
                retry_count = normalized_retry_count(getattr(base_target, "retry_count", 0))
                move_to_next_model = False

                for slot in iter_api_key_retry_slots(api_key_count=getattr(base_target, "api_key_count", 0), retry_count=retry_count, key_indexes=api_key_indexes):
                    target = base_target
                    selected_key_index = int(slot.key_index)
                    try:
                        target = base_target if slot.attempt_number == 1 else _build_provider_target_compat(
                            build_provider_from_model_key,
                            self._config,
                            model_key,
                            api_key_index=selected_key_index,
                        )
                        provider_kwargs: dict[str, Any] = {
                            "messages": messages,
                            "tools": tools,
                            "model": target.model_id,
                            "tool_choice": tool_choice,
                            "parallel_tool_calls": parallel_tool_calls,
                            "prompt_cache_key": prompt_cache_key,
                            "request_timeout_seconds": request_timeout_seconds,
                        }
                        if effective_max_tokens is not None:
                            provider_kwargs["max_tokens"] = effective_max_tokens
                        if effective_temperature is not None:
                            provider_kwargs["temperature"] = effective_temperature
                        if effective_reasoning:
                            provider_kwargs["reasoning_effort"] = effective_reasoning
                        outer_attempt_timeout_seconds = None if bool(getattr(target.provider, "manages_request_timeout_internally", False)) else request_timeout_seconds
                        response = await wait_for_model_attempt(
                            target.provider.chat(
                                **provider_kwargs,
                            ),
                            timeout_seconds=outer_attempt_timeout_seconds,
                            model_ref=str(getattr(target, "provider_ref", model_key) or model_key),
                            provider_id=str(getattr(target, "provider_id", "") or ""),
                            provider_model=str(getattr(target, "model_id", "") or ""),
                            key_index=selected_key_index,
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
                                slot.key_position + 1,
                                slot.key_count,
                                exc,
                            )
                            _log_model_chain_retry(model_ref=model_key, reason=exc)
                            continue
                        if rotate_key and not slot.is_last_round:
                            logger.warning(
                                "Model retry triggered for {} (round {}/{}, key {}/{}): {}",
                                model_key,
                                slot.round_index + 1,
                                slot.round_count,
                                slot.key_position + 1,
                                slot.key_count,
                                exc,
                            )
                            _log_model_chain_retry(model_ref=model_key, reason=exc)
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
                            if should_retry_model_chain_error(exhausted):
                                retry_full_chain = True
                                retry_full_chain_reason = exhausted.raw_message or str(exhausted)
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
                                slot.key_position + 1,
                                slot.key_count,
                                response.content or response.finish_reason,
                            )
                            _log_model_chain_retry(
                                model_ref=model_key,
                                reason=response.error_text or response.content or response.finish_reason,
                            )
                            continue
                        if not slot.is_last_round:
                            logger.warning(
                                "Model retry triggered for {} (round {}/{}, key {}/{}): {}",
                                model_key,
                                slot.round_index + 1,
                                slot.round_count,
                                slot.key_position + 1,
                                slot.key_count,
                                response.content or response.finish_reason,
                            )
                            _log_model_chain_retry(
                                model_ref=model_key,
                                reason=response.error_text or response.content or response.finish_reason,
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
                        if retryable_response:
                            retry_full_chain = True
                            retry_full_chain_reason = response.error_text or response.content or response.finish_reason
                            break
                        return last_response
                    return response

                if retry_full_chain:
                    break
                if move_to_next_model:
                    continue

            if not retry_full_chain and round_last_error is not None and should_retry_model_chain_error(round_last_error):
                retry_full_chain = True
                retry_full_chain_reason = getattr(round_last_error, "raw_message", "") or str(round_last_error)
            if retry_full_chain:
                if current_runtime_config_revision() != start_revision:
                    raise retryable_chain_config_changed_error() from round_last_error
                retryable_backoff_count += 1
                delay_seconds = model_retry_backoff_seconds(retryable_backoff_count)
                # 整链退避累计上限：超过则停止重试，跳出 while 落到终态处理（返回 last_response
                # 或 raise last_error），把"无限整链重试"降级为有界。只计退避等待，不含请求耗时。
                if cumulative_backoff_seconds + delay_seconds > MAX_RETRYABLE_CHAIN_BACKOFF_SECONDS:
                    logger.warning(
                        "Retryable model-chain backoff budget exhausted ({:.0f}s + {:.0f}s > {:.0f}s cap); "
                        "stopping chain retry: {}",
                        cumulative_backoff_seconds,
                        delay_seconds,
                        MAX_RETRYABLE_CHAIN_BACKOFF_SECONDS,
                        retry_full_chain_reason,
                    )
                    break
                cumulative_backoff_seconds += delay_seconds
                logger.warning(
                    "Retryable model-chain failure (round {}); retrying full chain in {:.1f}s: {}",
                    chain_round_index,
                    delay_seconds,
                    retry_full_chain_reason,
                )
                # 整链重试也是重试事件，与 key/轮次重试一样发彩色 RETRY trace 保持可观测性
                # （可重试错误不再走 key 轮换分支，其重试可观测性由这里承担）。
                _log_model_chain_retry(model_ref=",".join(chain), reason=retry_full_chain_reason)
                await asyncio.sleep(delay_seconds)
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
