"""provider 错误的可重试判定：结构化状态优先，关键词只作兜底。

起因是实盘一条链：链首那把键收到 SSE 里的 `{"code": "rate_limit_exceeded", "message":
"Request rate increased too quickly …"}`，因为关键词表里是带空格的短语 `rate limit`、
上游给的是下划线的 code，判定成了"不可重试"，那把键一次退避都没拿到就被让位，
链在 16/26 次尝试上就报了"耗尽"，而审计里同一条错误的 `retryable` 又是 true。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.providers.base import LLMResponse  # noqa: E402
from g3ku.providers.fallback import (  # noqa: E402
    is_retryable_model_error,
    response_requires_retry,
)
from g3ku.providers.responses_protocol_helpers import (  # noqa: E402
    CodexStreamError,
    _codex_failure_summary,
    _friendly_error,
)
from g3ku.providers.responses_provider import ResponsesProvider  # noqa: E402

RATE_LIMIT_MESSAGE = (
    "Request rate increased too quickly. To ensure system stability, please adjust your "
    "client logic to scale requests more smoothly over time."
)
CHAIN_TEXT_WITHOUT_STATUS = (
    f"runtimeerror: codex response failed: {RATE_LIMIT_MESSAGE} | rate_limit_exceeded"
)


def test_codex_rate_limit_event_yields_structured_429() -> None:
    event = {"error": {"code": "rate_limit_exceeded", "message": RATE_LIMIT_MESSAGE}}

    summary, full_body, error_status, error_code = _codex_failure_summary(event)

    assert error_status == 429
    assert error_code == "rate_limit_exceeded"
    assert "rate_limit_exceeded" in summary


def test_error_status_makes_it_retryable_even_without_keyword_hit() -> None:
    error = CodexStreamError("upstream said something unclassified", error_status=429)

    assert is_retryable_model_error(error, retry_on=["network", "429"]) is True


def test_underscored_provider_code_matches_spaced_keyword() -> None:
    # 兜底路径也要成立：只有文本可用（异常被重新包装、状态丢了）时不能再漏判。
    assert is_retryable_model_error(CHAIN_TEXT_WITHOUT_STATUS, retry_on=["network", "429"]) is True


def test_response_error_status_is_read_before_text() -> None:
    response = LLMResponse(
        content="",
        finish_reason="error",
        error_text="some provider wording with no keywords",
        error_status=429,
    )

    assert response_requires_retry(response, retry_on=["network", "429"]) is True


def test_http_429_error_text_keeps_status_and_drops_invented_branding() -> None:
    raw = json.dumps({"error": {"message": RATE_LIMIT_MESSAGE, "code": "rate_limit_exceeded"}})

    detail = _friendly_error(429, raw)

    assert "429" in detail
    assert "ChatGPT" not in detail and "Codex" not in detail


def test_non_retryable_statuses_and_empty_keyword_list_stay_false() -> None:
    unauthorized = RuntimeError("invalid api key provided")
    unauthorized.error_status = 401

    assert is_retryable_model_error(unauthorized, retry_on=["network", "429"]) is False
    assert is_retryable_model_error(CHAIN_TEXT_WITHOUT_STATUS, retry_on=[]) is False
    shape_error = LLMResponse(content="", finish_reason="error", error_text="bad json", error_status=400)
    assert response_requires_retry(shape_error, retry_on=["network", "429"]) is False


def test_provider_and_chain_share_one_retryable_status_list() -> None:
    from g3ku.providers import fallback
    from g3ku.providers.base import RETRYABLE_STATUS_CODES

    assert ResponsesProvider.RETRYABLE_STATUS_CODES == RETRYABLE_STATUS_CODES
    assert fallback.RETRYABLE_STATUS_CODES == RETRYABLE_STATUS_CODES
    assert 429 in RETRYABLE_STATUS_CODES
