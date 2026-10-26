"""Runtime settings for sticker egress.

Read through the live config so a value changed in the platform applies without
a restart, and fall back to the module default when config is unavailable (a
standalone script or a test that never booted the app).
"""

from __future__ import annotations

from g3ku.stickers.marker import MAX_STICKERS_PER_REPLY


def max_per_reply() -> int:
    """Per-reply sticker budget; 0 disables sending but still strips markers."""
    try:
        from g3ku.config.live_runtime import get_runtime_config

        config, _revision, _changed = get_runtime_config()
        raw = getattr(getattr(config, "stickers", None), "max_per_reply", MAX_STICKERS_PER_REPLY)
        value = int(raw)
    except Exception:
        return MAX_STICKERS_PER_REPLY
    return max(0, min(4, value))


def semantic_search_enabled() -> bool:
    """Whether the precise-pick tool is registered (`stickers.semantic_search`)."""
    try:
        from g3ku.config.live_runtime import get_runtime_config

        config, _revision, _changed = get_runtime_config()
        return bool(getattr(getattr(config, "stickers", None), "semantic_search", False))
    except Exception:
        return False


__all__ = ["max_per_reply", "semantic_search_enabled"]
