"""Deprecated idle-boundary settings (kept for config compatibility).

The runtime no longer treats wall-clock idleness as a reprint boundary: pinned heads
reprint only at compaction, and changes between reprints are delivered as one-time
`[RUNTIME_NOTICE]` messages. `idle_boundary_exceeded` remains importable for older
callers but has no runtime call site.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from g3ku.config.live_runtime import get_runtime_config


def context_refresh_settings(config: Any | None = None) -> tuple[bool, int]:
    """Return ``(enabled, idle_minutes)``; an unreadable config means never refresh.

    Failing closed keeps the pinning behaviour, which is the pre-existing state, rather
    than silently turning every hop into a reprint.
    """
    if config is None:
        try:
            config = get_runtime_config(force=False)[0]
        except Exception:
            return False, 0
    section = getattr(config, "context_refresh", None)
    if section is None:
        return False, 0
    try:
        idle_minutes = int(getattr(section, "idle_minutes", 0) or 0)
    except (TypeError, ValueError):
        idle_minutes = 0
    return bool(getattr(section, "enabled", False)), max(0, idle_minutes)


def idle_boundary_exceeded(last_active_at: str, *, config: Any | None = None) -> bool:
    """Legacy idle-window probe; kept for config/API compatibility.

    No runtime lane calls this anymore. Heads reprint only at compaction and the
    between-reprint delta is carried by one-time notices.
    """
    stamp = str(last_active_at or "").strip()
    if not stamp:
        return False
    enabled, idle_minutes = context_refresh_settings(config)
    if not enabled or idle_minutes <= 0:
        return False
    try:
        previous = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (datetime.now().astimezone() - previous).total_seconds() >= float(idle_minutes) * 60.0
