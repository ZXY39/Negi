"""Shared API key parsing and rotation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator, TypeAlias

MULTI_API_KEY_HELP_TEXT = (
    "Supports comma-separated or newline-separated values, for example "
    "`key1,key2`. Multiple keys are used in order, and retry_count counts one "
    "full pass across all configured keys."
)
MULTI_API_KEY_PLACEHOLDER = "sk-... or sk-1,sk-2"

_API_KEY_SPLIT_RE = re.compile(r"[\r\n,]+")
_AUTH_HTTP_STATUS_CODES = frozenset({401, 403})
SingleAPIKeyMaxConcurrency: TypeAlias = int | list[int] | None


class APIKeyConfigurationError(RuntimeError):
    """Raised when API key rotation or concurrency settings are invalid."""


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


def normalize_single_api_key_max_concurrency(value: object) -> SingleAPIKeyMaxConcurrency:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if any(separator in stripped for separator in (",", "\n", "\r")):
            parts = [item.strip() for item in _API_KEY_SPLIT_RE.split(stripped)]
            limits: list[int] = []
            for item in parts:
                if not item:
                    raise ValueError("single_api_key_max_concurrency list entries must not be empty")
                parsed = int(item)
                if parsed < 0:
                    raise ValueError("single_api_key_max_concurrency list entries must be >= 0")
                limits.append(parsed)
            if not limits:
                return None
            return limits
        parsed = int(stripped)
        if parsed < 1:
            raise ValueError("single_api_key_max_concurrency must be >= 1")
        return parsed
    if isinstance(value, (list, tuple)):
        limits: list[int] = []
        for item in value:
            if item in (None, ""):
                raise ValueError("single_api_key_max_concurrency list entries must not be empty")
            parsed = int(item)
            if parsed < 0:
                raise ValueError("single_api_key_max_concurrency list entries must be >= 0")
            limits.append(parsed)
        if not limits:
            return None
        return limits
    parsed = int(value)
    if parsed < 1:
        raise ValueError("single_api_key_max_concurrency must be >= 1")
    return parsed


@dataclass(frozen=True, slots=True)
class APIKeyConcurrencyLayout:
    configured_value: SingleAPIKeyMaxConcurrency
    raw_key_count: int
    key_indexes: tuple[int, ...]
    key_limits: tuple[int | None, ...]
    disabled_key_indexes: tuple[int, ...]

    @property
    def key_count(self) -> int:
        return len(self.key_indexes)


def resolve_api_key_concurrency_layout(
    api_key_value: str | Iterable[str] | None,
    configured_value: object = None,
    *,
    include_empty_slot: bool = False,
    reject_all_zero: bool = False,
) -> APIKeyConcurrencyLayout:
    if isinstance(api_key_value, str) or api_key_value is None:
        api_keys = parse_api_keys(api_key_value)
    else:
        api_keys = [str(item or "").strip() for item in api_key_value if str(item or "").strip()]
    raw_key_count = len(api_keys)
    normalized_value = normalize_single_api_key_max_concurrency(configured_value)
    slot_count = raw_key_count if raw_key_count > 0 else (1 if include_empty_slot else 0)

    if isinstance(normalized_value, list):
        if slot_count == 0:
            raise ValueError("single_api_key_max_concurrency requires at least one API key")
        if len(normalized_value) != slot_count:
            raise ValueError(
                f"single_api_key_max_concurrency expects {slot_count} entries, got {len(normalized_value)}"
            )
        disabled = tuple(index for index, item in enumerate(normalized_value) if int(item) == 0)
        key_indexes = tuple(index for index, item in enumerate(normalized_value) if int(item) > 0)
        key_limits = tuple(int(item) for item in normalized_value if int(item) > 0)
    else:
        key_indexes = tuple(range(slot_count))
        key_limits = tuple(None if normalized_value is None else int(normalized_value) for _ in key_indexes)
        disabled = tuple()

    if reject_all_zero and slot_count > 0 and not key_indexes:
        raise ValueError("single_api_key_max_concurrency must leave at least one API key enabled")

    return APIKeyConcurrencyLayout(
        configured_value=normalized_value,
        raw_key_count=raw_key_count,
        key_indexes=key_indexes,
        key_limits=key_limits,
        disabled_key_indexes=disabled,
    )


@dataclass(frozen=True, slots=True)
class APIKeyRetrySlot:
    round_index: int
    round_count: int
    key_index: int
    key_position: int
    key_count: int
    attempt_number: int

    @property
    def is_last_key(self) -> bool:
        return self.key_position >= self.key_count - 1

    @property
    def is_last_round(self) -> bool:
        return self.round_index >= self.round_count - 1


def iter_api_key_retry_slots(
    *,
    api_key_count: int,
    retry_count: int,
    key_indexes: Iterable[int] | None = None,
) -> Iterator[APIKeyRetrySlot]:
    if key_indexes is not None:
        key_order = [max(0, int(index or 0)) for index in key_indexes]
        if not key_order:
            return
        key_count = len(key_order)
    else:
        key_count = max(1, int(api_key_count or 0))
        key_order = list(range(key_count))
    round_count = max(1, int(retry_count or 0) + 1)
    attempt_number = 0
    for round_index in range(round_count):
        for key_position, key_index in enumerate(key_order):
            attempt_number += 1
            yield APIKeyRetrySlot(
                round_index=round_index,
                round_count=round_count,
                key_index=key_index,
                key_position=key_position,
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
