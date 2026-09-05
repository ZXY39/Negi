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
# Retryable failures retry with capped exponential backoff plus jitter so
# concurrent nodes do not stampede the same rate window. The retry is bounded
# by the per-model round budget (below), not by a cumulative time cap.
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_BACKOFF_CAP_SECONDS = 60.0
RETRY_BACKOFF_JITTER_RATIO = 0.25
# 可重试错误（retry_on 命中）的每模型退避重试轮数预算：模型绑定 retry_count 为
# 0/未设置时使用该默认值。一轮 = 完整轮过该模型所有 key；轮间走封顶指数退避加
# 抖动。模型预算耗尽才前进到链上下一个模型，全链模型预算耗尽即报错停止——次数
# 预算是唯一权威上限（请求耗时另由 DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS
# 单次约束），不存在无限重试循环。
DEFAULT_RETRYABLE_MODEL_ROUNDS = 10
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

    只信任结构化 HTTP 状态（LLMResponse.error_status / SDK 异常 status_code）：
    status 可得且为 400/422 → 形状错误；status 可得但为其他值（429 限流、401 坏
    key、503 等）→ 不是形状错误；status 不可得 → 一律不按形状错误处理，交给正常
    轮换/退避/降级判定。
    文本关键字不作为判定依据：bad request / invalid_request_error 等是
    OpenAI 兼容网关的通用错误 type 字段，不是 400 专属标识（如 sensenova 网关把
    429 限流标成 invalid_request_error），文本匹配会把可重试错误误判成形状错误。
    """
    return _error_status_code(error) in _REQUEST_SHAPE_STATUS_CODES


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
        model_index = 0
        while model_index < len(chain):
            model_key = chain[model_index]
            try:
                base_target = build_provider_from_model_key(self._config, model_key)
            except Exception as exc:
                last_error = exc
                if should_fallback_model_error(exc) and model_index < len(chain) - 1:
                    logger.warning("Model target init failed for {}: {}", model_key, exc)
                    model_index += 1
                    continue
                if should_fallback_model_error(exc):
                    profile = self._config.get_model_runtime_profile(model_key)
                    raise exhausted_model_chain_error(
                        exc,
                        retry_on=list(profile.retry_on) if profile is not None else None,
                    ) from exc
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

            # 本模型的可重试轮数预算：绑定 retry_count 即配置页「重试次数」，
            # 0/未设置用默认 DEFAULT_RETRYABLE_MODEL_ROUNDS。一轮 = 完整轮过该模型所有 key。
            budget_rounds = normalized_retry_count(getattr(base_target, "retry_count", 0)) or DEFAULT_RETRYABLE_MODEL_ROUNDS
            model_retry_on = list(getattr(base_target, "retry_on", []) or [])
            rounds_used = 0
            model_last_error: Exception | None = None
            model_last_response: LLMResponse | None = None
            model_last_failure_reason: Any = None
            advance_to_next_model = False
            while True:  # 本模型的轮循环
                round_retryable_failed = False
                # 单轮 = 每个 key 各试一次（retry_count=0 → 单趟 key 遍历）。
                for slot in iter_api_key_retry_slots(api_key_count=getattr(base_target, "api_key_count", 0), retry_count=0, key_indexes=api_key_indexes):
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
                        last_error = model_last_error = exc
                        model_last_response = None
                        if is_request_shape_error(exc):
                            # 请求形状错误（结构化 400/422）：换 key 修不了畸形
                            # payload，直接切下一模型，不消耗重试轮。
                            advance_to_next_model = True
                            model_last_failure_reason = exception_chain_display_text(exc)
                            break
                        if not should_fallback_model_error(exc):
                            raise  # 内部运行时错误 / API key 配置错误直接上抛
                        if is_retryable_model_error(exc, retry_on=target.retry_on):
                            round_retryable_failed = True
                            model_last_failure_reason = exception_chain_display_text(exc)
                            continue  # 轮内继续轮过该模型下一个 key
                        logger.warning(
                            "Model key rotation for {} (key {}/{}): {}",
                            model_key,
                            slot.key_position + 1,
                            slot.key_count,
                            exc,
                        )
                        model_last_failure_reason = exception_chain_display_text(exc)
                        continue  # 非可重试：每个 key 各试一次，轮完切下一模型
                    retryable_response = response_requires_retry(response, retry_on=target.retry_on)
                    fallback_response = response_requires_fallback(response)
                    if not fallback_response:
                        return response  # 成功终态（或内部错误响应按原样返回）
                    last_response = model_last_response = response
                    model_last_error = None
                    model_last_failure_reason = str(response.error_text or response.content or response.finish_reason or "")
                    if is_request_shape_error(response):
                        advance_to_next_model = True
                        break
                    if retryable_response:
                        round_retryable_failed = True
                    continue  # 轮内继续轮过该模型下一个 key
                if advance_to_next_model:
                    break
                rounds_used += 1
                if round_retryable_failed and rounds_used < budget_rounds:
                    if current_runtime_config_revision() != start_revision:
                        raise retryable_chain_config_changed_error() from model_last_error
                    delay_seconds = model_retry_backoff_seconds(rounds_used)
                    reason_text = str(model_last_failure_reason or "")
                    logger.warning(
                        "Retryable model failure for {} (round {}/{}); retrying in {:.1f}s: {}",
                        model_key,
                        rounds_used,
                        budget_rounds,
                        delay_seconds,
                        reason_text,
                    )
                    # 可重试退避重试发彩色 RETRY trace 保持可观测性。
                    _log_model_chain_retry(model_ref=model_key, reason=reason_text)
                    await asyncio.sleep(delay_seconds)
                    continue  # 同模型重跑一轮
                break  # 预算耗尽或本轮无可重试失败 → 本模型耗尽

            # 本模型耗尽：非链尾前进到下一模型，链尾落终态。
            if model_index < len(chain) - 1:
                logger.warning(
                    "Model fallback triggered for {}: {}",
                    model_key,
                    model_last_failure_reason if model_last_failure_reason is not None else (model_last_error or ""),
                )
                model_index += 1
                continue
            if model_last_error is not None:
                if should_fallback_model_error(model_last_error):
                    raise exhausted_model_chain_error(model_last_error, retry_on=model_retry_on) from model_last_error
                raise model_last_error
            if model_last_response is not None:
                return sanitize_terminal_model_error(model_last_response)
            return LLMResponse(content="Error: no model candidate available", finish_reason="error")

        if last_response is not None:
            return sanitize_terminal_model_error(last_response)
        if last_error is not None:
            if should_fallback_model_error(last_error):
                raise exhausted_model_chain_error(last_error) from last_error
            raise last_error
        return LLMResponse(content="Error: no model candidate available", finish_reason="error")

    def get_default_model(self) -> str:
        return self._default_model_ref
