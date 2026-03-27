"""Shared timezone helpers for cron scheduling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from importlib import resources
from importlib.util import find_spec
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FIXED_OFFSET_ALIASES: dict[str, tzinfo] = {
    "UTC": timezone.utc,
    "Etc/UTC": timezone.utc,
    "GMT": timezone.utc,
    "Etc/GMT": timezone.utc,
    # Safe aliases for common no-DST Asia timezones used by the product prompts.
    "Asia/Shanghai": timezone(timedelta(hours=8), "Asia/Shanghai"),
    "Asia/Tokyo": timezone(timedelta(hours=9), "Asia/Tokyo"),
}

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_ETC_GMT_RE = re.compile(r"^Etc/GMT([+-])(\d{1,2})$", re.IGNORECASE)


@lru_cache(maxsize=1)
def _tzdata_available() -> bool:
    return bool(find_spec("tzdata"))


def _format_timezone_error(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        return "timezone name is required"
    hint = (
        " IANA tz database is unavailable in this Python environment; install the `tzdata` package"
        if not _tzdata_available()
        else ""
    )
    return f"unknown timezone '{normalized}'.{hint}".rstrip(".") if hint else f"unknown timezone '{normalized}'"


def _parse_fixed_offset_timezone(name: str) -> tzinfo | None:
    normalized = str(name or "").strip()
    if not normalized:
        return None
    alias = _FIXED_OFFSET_ALIASES.get(normalized)
    if alias is not None:
        return alias

    match = _OFFSET_RE.match(normalized)
    if match:
        sign, hours_text, minutes_text = match.groups()
        hours = int(hours_text)
        minutes = int(minutes_text or "0")
        offset = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            offset = -offset
        return timezone(offset, normalized)

    # IANA "Etc/GMT-8" means UTC+08:00; the sign is inverted by convention.
    match = _ETC_GMT_RE.match(normalized)
    if match:
        sign, hours_text = match.groups()
        hours = int(hours_text)
        offset = timedelta(hours=hours if sign == "-" else -hours)
        return timezone(offset, normalized)

    return None


def _load_tzdata_timezone(name: str) -> tzinfo | None:
    normalized = str(name or "").strip()
    if not normalized or not _tzdata_available():
        return None
    try:
        resource = resources.files("tzdata.zoneinfo")
    except Exception:
        return None
    for segment in normalized.split("/"):
        resource = resource.joinpath(segment)
    try:
        if not resource.is_file():
            return None
    except Exception:
        return None
    try:
        with resource.open("rb") as handle:
            return ZoneInfo.from_file(handle, key=normalized)
    except Exception:
        return None


def local_timezone() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    return tz if tz is not None else timezone.utc


def resolve_timezone(name: str | None) -> tzinfo:
    normalized = str(name or "").strip()
    if not normalized:
        return local_timezone()
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        tzdata_timezone = _load_tzdata_timezone(normalized)
        if tzdata_timezone is not None:
            return tzdata_timezone
        fixed_offset = _parse_fixed_offset_timezone(normalized)
        if fixed_offset is not None:
            return fixed_offset
        raise ValueError(_format_timezone_error(normalized)) from None


def validate_timezone_name(name: str | None) -> None:
    normalized = str(name or "").strip()
    if not normalized:
        return
    resolve_timezone(normalized)
