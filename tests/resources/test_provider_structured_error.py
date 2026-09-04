from __future__ import annotations

from openai import APIError as OpenAISDKAPIError

from g3ku.providers.base import LLMResponse
from g3ku.providers.fallback import ModelProviderResponseError
from g3ku.providers.openai_chat_provider import _structured_error_fields


class _FakeSDKError(OpenAISDKAPIError):
    """最小 OpenAI SDK 错误替身：isinstance(OpenAISDKAPIError) 成立，带 code/status_code/body。"""

    def __init__(self, *, code, status_code, body=None, message="fake provider error"):
        Exception.__init__(self, message)
        self.code = code
        self.status_code = status_code
        self.body = body or {}


def test_structured_error_fields_extracts_code_status_kind() -> None:
    exc = _FakeSDKError(
        code="insufficient_quota",
        status_code=429,
        body={"error": {"type": "invalid_request_error", "code": "insufficient_quota"}},
    )
    fields = _structured_error_fields(exc)
    assert fields["error_code"] == "insufficient_quota"
    assert fields["error_status"] == 429
    assert fields["error_kind"] == "_FakeSDKError"  # 异常类名（生产里是 RateLimitError 等）


def test_structured_error_fields_walks_cause_chain() -> None:
    inner = _FakeSDKError(code="8", status_code=429)
    outer = RuntimeError("wrapper")
    outer.__cause__ = inner
    fields = _structured_error_fields(outer)
    assert fields["error_code"] == "8"
    assert fields["error_status"] == 429


def test_structured_error_fields_empty_for_plain_error() -> None:
    assert _structured_error_fields(ValueError("nope")) == {
        "error_code": None,
        "error_status": None,
        "error_kind": None,
    }


def test_model_provider_response_error_satisfies_classifier_contract() -> None:
    exc = ModelProviderResponseError(
        message="RateLimitError: Error code: 429 - insufficient_quota",
        code="insufficient_quota",
        status=429,
        kind="RateLimitError",
    )
    # session_agent 分类器要求 .code/.message/.recoverable 同时存在才会取真实 code
    assert all(hasattr(exc, key) for key in ("code", "message", "recoverable"))
    assert exc.code == "insufficient_quota"
    assert exc.status == 429
    assert exc.kind == "RateLimitError"
    assert exc.recoverable is True
    assert isinstance(exc, RuntimeError)  # 兼容既有 except RuntimeError
    assert "429" in str(exc)  # 完整原文保留


def test_model_provider_response_error_defaults_code_when_missing() -> None:
    exc = ModelProviderResponseError(message="boom")
    assert exc.code == "model_provider_error"
    assert exc.status is None
    assert exc.recoverable is True


def test_llm_response_carries_structured_error_fields() -> None:
    resp = LLMResponse(
        content="err",
        error_text="err",
        finish_reason="error",
        error_code="insufficient_quota",
        error_status=429,
        error_kind="RateLimitError",
    )
    assert resp.error_code == "insufficient_quota"
    assert resp.error_status == 429
    assert resp.error_kind == "RateLimitError"
    # 默认值：未填充时为 None（其他 provider 不设这些字段也不报错）
    plain = LLMResponse(content="ok")
    assert plain.error_code is None and plain.error_status is None and plain.error_kind is None
