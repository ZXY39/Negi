"""Shared API key parsing and rotation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator

MULTI_API_KEY_HELP_TEXT = (
    "Supports comma-separated or newline-separated values, for example "
    "`key1,key2`. Multiple keys are used in order, and retry_count counts one "
    "full pass across all configured keys."
)
MULTI_API_KEY_PLACEHOLDER = "sk-... or sk-1,sk-2"

_API_KEY_SPLIT_RE = re.compile(r"[\r\n,]+")
_AUTH_HTTP_STATUS_CODES = frozenset({401, 403})


def parse_api_keys(value: str | None) -> list[str]:
    raw = str(value or "")
    return [item.strip() for item in _API_KEY_SPLIT_RE.split(raw) if item and item.strip()]


def has_api_keys(value: str | None) -> bool:
    return bool(parse_api_keys(value))


def first_api_key(value: str | None) -> str:
    keys = parse_api_keys(value)
    return keys[0] if keys else ""


def api_key_count(value: str | None) -> int:
    return len(parse_api_keys(value))


def is_auth_http_status(status_code: int | None) -> bool:
    return int(status_code or 0) in _AUTH_HTTP_STATUS_CODES


def is_retryable_http_status(status_code: int | None) -> bool:
    code = int(status_code or 0)
    return code == 429 or 500 <= code < 600


def should_switch_api_key_for_http_status(status_code: int | None) -> bool:
    return is_auth_http_status(status_code) or is_retryable_http_status(status_code)


@dataclass(frozen=True, slots=True)
class APIKeyRetrySlot:
    round_index: int
    round_count: int
    key_index: int
    key_count: int
    attempt_number: int

    @property
    def is_last_key(self) -> bool:
        return self.key_index >= self.key_count - 1

    @property
    def is_last_round(self) -> bool:
        return self.round_index >= self.round_count - 1


def iter_api_key_retry_slots(*, api_key_count: int, retry_count: int) -> Iterator[APIKeyRetrySlot]:
    key_count = max(1, int(api_key_count or 0))
    round_count = max(1, int(retry_count or 0) + 1)
    attempt_number = 0
    for round_index in range(round_count):
        for key_index in range(key_count):
            attempt_number += 1
            yield APIKeyRetrySlot(
                round_index=round_index,
                round_count=round_count,
                key_index=key_index,
                key_count=key_count,
                attempt_number=attempt_number,
            )


def iter_api_key_values(
    value: str | Iterable[str] | None,
    *,
    retry_count: int = 0,
    include_empty: bool = False,
) -> Iterator[tuple[APIKeyRetrySlot, str]]:
    if isinstance(value, str) or value is None:
        keys = parse_api_keys(value)
    else:
        keys = [str(item or "").strip() for item in value if str(item or "").strip()]

    if not keys and include_empty:
        keys = [""]
    if not keys:
        return

    for slot in iter_api_key_retry_slots(api_key_count=len(keys), retry_count=retry_count):
        yield slot, keys[slot.key_index]
