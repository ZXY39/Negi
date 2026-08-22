"""Shared retry keyword parsing for model error classification.

retry_on entries are free-form keywords matched as lowercase substrings of the
provider error text. Two preset aliases are kept for backward compatibility:
``network`` and ``429`` expand to curated token lists. Any other entry is used
as a literal keyword. Entries may be supplied as a list or as a
comma/newline-separated string.
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

_RETRY_KEYWORD_SPLIT_RE = re.compile(r"[\r\n,]+")


def split_retry_keywords(value: Any) -> list[str]:
    """Normalize retry_on input into a flat, lowercased, de-duplicated keyword list.

    Accepts ``None``, strings (comma/newline separated), and list/tuple inputs
    whose string entries may themselves contain commas. Unsupported types and
    empty fragments are dropped.
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
