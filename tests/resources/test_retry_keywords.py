from __future__ import annotations

import g3ku.providers.fallback as fallback_module
from g3ku.providers.base import LLMResponse
from g3ku.utils.retry_keywords import (
    DEFAULT_RETRY_ON_KEYWORDS,
    expand_retry_keywords,
    split_retry_keywords,
)

# NOTE: fallback symbols are resolved through the module attribute at call time
# (instead of top-level name imports) because other test modules reload
# g3ku.providers.fallback via importlib.reload, which replaces class objects.


def test_schema_normalizes_comma_separated_retry_on() -> None:
    from g3ku.config.schema import ModelFallbackTarget

    target = ModelFallbackTarget.model_validate({"model_key": "demo", "retry_on": "Network, 429 , 502"})
    assert target.retry_on == ["network", "429", "502"]

    # 显式置空被尊重（关闭关键字重试），不再强制回填默认。
    emptied = ModelFallbackTarget.model_validate({"model_key": "demo", "retry_on": []})
    assert emptied.retry_on == []

    # 仅在省略字段时才用默认关键字（default_factory）。
    omitted = ModelFallbackTarget.model_validate({"model_key": "demo"})
    assert omitted.retry_on == list(DEFAULT_RETRY_ON_KEYWORDS)


def test_split_retry_keywords_accepts_comma_separated_string() -> None:
    assert split_retry_keywords("network, 429") == ["network", "429"]


def test_split_retry_keywords_flattens_inner_commas_and_normalizes() -> None:
    assert split_retry_keywords(["Timeout, 502", "TIMEOUT", ""]) == ["timeout", "502"]


def test_split_retry_keywords_handles_newlines_and_none() -> None:
    assert split_retry_keywords("network\n429,502") == ["network", "429", "502"]
    assert split_retry_keywords(None) == []
    assert split_retry_keywords(123) == []


def test_expand_retry_keywords_presets_and_literals() -> None:
    tokens = expand_retry_keywords(["network", "429", "datainspectionfailed"])
    assert "timed out" in tokens
    assert "rate limit" in tokens
    assert "datainspectionfailed" in tokens


def test_default_keywords_are_network_and_429() -> None:
    assert DEFAULT_RETRY_ON_KEYWORDS == ["network", "429"]


def test_custom_keyword_hits_provider_error_text() -> None:
    error = RuntimeError("Error code: 400 - InternalError.Algo.DataInspectionFailed: inappropriate content")
    assert fallback_module.is_retryable_model_error(error, retry_on=["datainspectionfailed"]) is True
    assert fallback_module.is_retryable_model_error(error, retry_on="datainspectionfailed, timeout") is True
    assert fallback_module.is_retryable_model_error(error, retry_on=["nomatch"]) is False


def test_default_keywords_do_not_retry_server_errors_anymore() -> None:
    # The 5xx preset was removed; server errors need explicit keywords now.
    assert fallback_module.is_retryable_model_error(RuntimeError("HTTP 502: upstream request failed")) is False
    assert fallback_module.is_retryable_model_error(RuntimeError("HTTP 502: upstream request failed"), retry_on=["502"]) is True
    assert fallback_module.is_retryable_model_error(RuntimeError("HTTP 502: upstream request failed"), retry_on=["5xx"]) is False


def test_network_and_429_presets_still_expand() -> None:
    assert fallback_module.is_retryable_model_error(RuntimeError("ReadTimeout after 60s"), retry_on=["network"]) is True
    assert fallback_module.is_retryable_model_error(RuntimeError("connection reset by peer"), retry_on=["network"]) is True
    assert fallback_module.is_retryable_model_error(RuntimeError("rate limit exceeded"), retry_on=["429"]) is True
    assert fallback_module.is_retryable_model_error(RuntimeError("Error code: 429"), retry_on=["429"]) is True


def test_internal_runtime_error_is_never_retryable() -> None:
    assert fallback_module.is_retryable_model_error(RuntimeError("sqlite timeout while writing"), retry_on=["timeout"]) is False
    assert fallback_module.is_retryable_model_error("database cursor closed: timeout", retry_on=["timeout"]) is False


def test_exhausted_error_message_preserves_original_case() -> None:
    error = RuntimeError("Error: <400> InternalError.Algo.DataInspectionFailed: Input rejected")
    exhausted = fallback_module.exhausted_model_chain_error(error, retry_on=["datainspectionfailed"])

    assert isinstance(exhausted, fallback_module.ModelProviderExhaustedError)
    assert "InternalError.Algo.DataInspectionFailed" in str(exhausted)
    assert "internalerror.algo.datainspectionfailed" in exhausted.raw_message
    assert exhausted.retryable is True
    assert fallback_module.should_retry_model_chain_error(exhausted, retry_on=["datainspectionfailed"]) is True


def test_exhausted_error_without_raw_message_falls_back_to_public_text() -> None:
    exhausted = fallback_module.exhausted_model_chain_error(None)
    assert str(exhausted) == fallback_module.PUBLIC_PROVIDER_FAILURE_MESSAGE
    assert exhausted.raw_message == ""
    assert exhausted.retryable is False


def test_sanitize_keeps_existing_error_text_unwrapped() -> None:
    response = LLMResponse(
        content="",
        finish_reason="error",
        error_text="HTTP 400: bad request",
    )
    sanitized = fallback_module.sanitize_terminal_model_error(response)
    assert sanitized.error_text == "HTTP 400: bad request"


def test_sanitize_only_backfills_empty_error_text() -> None:
    response = LLMResponse(content="", finish_reason="error", error_text="")
    sanitized = fallback_module.sanitize_terminal_model_error(response)
    assert sanitized.error_text == fallback_module.PUBLIC_PROVIDER_FAILURE_MESSAGE
