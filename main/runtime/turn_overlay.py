from __future__ import annotations

from typing import Any

TURN_ONLY_SYSTEM_NOTE_PREFIX = "System note for this turn only:"


def build_turn_only_system_note_message(overlay_text: str | None) -> dict[str, Any] | None:
    text = str(overlay_text or '').strip()
    if not text:
        return None
    return {"role": "user", "content": f"{TURN_ONLY_SYSTEM_NOTE_PREFIX}\n{text}"}


def is_turn_only_system_note_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    if str((message or {}).get("role") or "").strip().lower() != "user":
        return False
    return str((message or {}).get("content") or "").strip().startswith(TURN_ONLY_SYSTEM_NOTE_PREFIX)


def strip_turn_only_system_note_messages(messages: Any) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in list(messages or [])
        if isinstance(item, dict) and not is_turn_only_system_note_message(item)
    ]


__all__ = [
    "TURN_ONLY_SYSTEM_NOTE_PREFIX",
    "build_turn_only_system_note_message",
    "is_turn_only_system_note_message",
    "strip_turn_only_system_note_messages",
]
