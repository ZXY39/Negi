"""Shared reply-silencing sentinel for the G3KU runtime.

A model may end any turn (user, heartbeat, cron) with the literal
``[G3KU_SILENT]`` to request that no user-visible reply be delivered or
persisted. The runtime recognizes the exact token and swallows the finalized
reply while still completing the turn lifecycle and publishing any stages /
tool activity that already happened during the turn.

Recognition is exact-match, single-line, after stripping surrounding
whitespace; substring matches are never treated as the sentinel.
"""

from __future__ import annotations

SILENT_REPLY_TOKEN = "[G3KU_SILENT]"

# 静默回合在 Web 前端会话页里可见的占位文案（外部渠道如 QQ 仍不投递）。
SILENT_REPLY_VISIBLE_TEXT = "信息已静默"


def is_silent_reply_token(text: object) -> bool:
    """True when ``text`` is exactly the silent-reply sentinel."""
    return str(text or "").strip() == SILENT_REPLY_TOKEN