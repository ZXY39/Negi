from __future__ import annotations

import re
from typing import Any


CRON_STOP_CONDITION_CANCEL_SUFFIX = "或用户要求取消"
CRON_STOP_CONDITION_LEGACY_FALLBACK = "用户要求取消"

_CANCEL_SUFFIX_PATTERN = re.compile(
    r"(?:\s*(?:\+|,|，|、|/|;|；|-)?\s*[【\[]?(?:或)?用户(?:要求)?取消[】\]]?\s*)+$"
)


def cron_schedule_requires_stop_condition(schedule: Any) -> bool:
    kind = str(getattr(schedule, "kind", schedule or "") or "").strip().lower()
    return kind in {"every", "cron"}


def normalize_cron_stop_condition(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("stop_condition is required for recurring cron jobs")

    normalized = " ".join(raw.split())
    normalized = normalized.replace("【或用户要求取消】", CRON_STOP_CONDITION_CANCEL_SUFFIX)
    normalized = normalized.replace("【用户要求取消】", "用户要求取消")
    base = _CANCEL_SUFFIX_PATTERN.sub("", normalized).strip(" \t\r\n,，.。;；+、")
    if not base:
        raise ValueError(
            "stop_condition must include a specific exit condition before '或用户要求取消'"
        )
    return f"{base}{CRON_STOP_CONDITION_CANCEL_SUFFIX}"


def effective_cron_stop_condition(value: str | None) -> tuple[str, bool]:
    raw = str(value or "").strip()
    if not raw:
        return CRON_STOP_CONDITION_LEGACY_FALLBACK, False
    try:
        return normalize_cron_stop_condition(raw), True
    except ValueError:
        return CRON_STOP_CONDITION_LEGACY_FALLBACK, False


__all__ = [
    "CRON_STOP_CONDITION_CANCEL_SUFFIX",
    "CRON_STOP_CONDITION_LEGACY_FALLBACK",
    "cron_schedule_requires_stop_condition",
    "effective_cron_stop_condition",
    "normalize_cron_stop_condition",
]
