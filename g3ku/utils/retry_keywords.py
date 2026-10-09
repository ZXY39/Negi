"""Shared retry keyword parsing for model error classification.

retry_on entries are free-form keywords matched as lowercase substrings of the
provider error text. Two preset aliases are kept for backward compatibility:
``network`` and ``429`` expand to curated token lists. Any other entry is used
as a literal keyword. Entries may be supplied as a list or as a
space/comma/newline-separated string; keywords are single whitespace-free
tokens (the model config page collects them space-separated).
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_RETRY_ON_KEYWORDS = ["network", "429"]

RETRYABLE_ERROR_PRESETS: dict[str, tuple[str, ...]] = {
    "network": (
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
    ),
    "429": (
        "429",
        "rate limit",
        "too many requests",
        "quota",
    ),
}

_RETRY_KEYWORD_SPLIT_RE = re.compile(r"[\s,]+")

# 限流维度词表。429 内部是两个物种：「这一分钟的窗口到顶了」与「额度打光了」，
# 前者等一个窗口就自己松开、后者等多久都不会自己恢复，所以退避节拍必须按维度取档。
THROTTLE_DIMENSION_RPM = "rpm"
THROTTLE_DIMENSION_TPM = "tpm"
THROTTLE_DIMENSION_RPS = "rps"
THROTTLE_DIMENSION_TOKEN = "token"
THROTTLE_DIMENSION_UNKNOWN = "unknown"

# 分钟级窗口：rpm/tpm 都是按分钟重置。rps 下一拍就通，不值得等；token 一类是
# entitlement/quota 到顶，等待只是把链耗尽推迟到更晚。
MINUTE_WINDOW_THROTTLE_DIMENSIONS = frozenset({THROTTLE_DIMENSION_RPM, THROTTLE_DIMENSION_TPM})


def classify_throttle_dimension(error_text: str) -> str:
    """尽力把 429 归到限流维度，归不出来记 unknown。

    文本不是可靠的分类依据（网关会把 429 标成 `invalid_request_error`），所以这条判据
    只用在"最坏只是多等一轮"的地方：配额桶观测加权、以及退避节拍取档。它绝不参与
    「能不能 fallback」的判定。
    """
    lowered = str(error_text or "").lower()
    if "rpm" in lowered:
        return THROTTLE_DIMENSION_RPM
    if "tpm" in lowered:
        return THROTTLE_DIMENSION_TPM
    if "rps" in lowered:
        return THROTTLE_DIMENSION_RPS
    if "token" in lowered:
        return THROTTLE_DIMENSION_TOKEN
    return THROTTLE_DIMENSION_UNKNOWN


def is_minute_window_throttle(error_text: str) -> bool:
    """这条限流是不是「等到窗口翻面就会自己松」的那一类（rpm / tpm）。"""
    return classify_throttle_dimension(error_text) in MINUTE_WINDOW_THROTTLE_DIMENSIONS


def split_retry_keywords(value: Any) -> list[str]:
    """Normalize retry_on input into a flat, lowercased, de-duplicated keyword list.

    Accepts ``None``, strings (space/comma/newline separated), and list/tuple
    inputs whose string entries may themselves contain separators. Unsupported
    types and empty fragments are dropped. Keywords are single tokens; phrases
    containing whitespace are split into separate keywords.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item or "") for item in value]
    else:
        return []
    clean: list[str] = []
    seen: set[str] = set()
    for item in items:
        for fragment in _RETRY_KEYWORD_SPLIT_RE.split(str(item or "")):
            keyword = fragment.strip().lower()
            if not keyword or keyword in seen:
                continue
            seen.add(keyword)
            clean.append(keyword)
    return clean


def expand_retry_keywords(keywords: list[str] | None) -> list[str]:
    """Expand preset aliases into their token lists; other keywords stay literal."""
    tokens: list[str] = []
    for keyword in list(keywords or []):
        normalized = str(keyword or "").strip().lower()
        if not normalized:
            continue
        preset = RETRYABLE_ERROR_PRESETS.get(normalized)
        if preset is not None:
            tokens.extend(preset)
        else:
            tokens.append(normalized)
    return tokens
