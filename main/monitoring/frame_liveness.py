"""Runtime-frame liveness judgement shared by the tool output and the web tree.

A frame row is rewritten on every step a node takes, so the age of its
``updated_at`` is the only evidence that distinguishes "executing right now"
from "a frame left behind when the turn stopped being rewritten".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# 阈值取停滞提醒首档(20 分钟)的一半,给正常长工具执行留足余量。
STALE_FRAME_MINUTES = 10.0


def parse_iso_utc(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def frame_is_stale(updated_at: Any, *, now: datetime | None = None) -> bool:
    """Unparseable timestamps count as fresh: the age is unknown, not proven old."""
    started = parse_iso_utc(updated_at)
    if started is None:
        return False
    reference = now or datetime.now(timezone.utc)
    return (reference - started).total_seconds() / 60.0 >= STALE_FRAME_MINUTES
