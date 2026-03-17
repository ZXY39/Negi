from __future__ import annotations

from typing import Any

DEFAULT_WEB_MEMORY_SCOPE = {"channel": "web", "chat_id": "shared"}


def session_key_scope(session_key: str | None, *, default_channel: str = "unknown", default_chat_id: str = "unknown") -> dict[str, str]:
    raw = str(session_key or "").strip()
    if ":" in raw:
        channel, chat_id = raw.split(":", 1)
        return {
            "channel": channel or default_channel,
            "chat_id": chat_id or default_chat_id,
        }
    if raw:
        return {
            "channel": default_channel,
            "chat_id": raw,
        }
    return {
        "channel": default_channel,
        "chat_id": default_chat_id,
    }


def normalize_memory_scope(
    value: Any,
    *,
    fallback_session_key: str | None = None,
    fallback_channel: str | None = None,
    fallback_chat_id: str | None = None,
) -> dict[str, str]:
    if isinstance(value, dict):
        channel = str(value.get("channel") or value.get("channel_id") or "").strip()
        chat_id = str(value.get("chat_id") or value.get("chatId") or "").strip()
        if channel and chat_id:
            return {"channel": channel, "chat_id": chat_id}

    if fallback_channel and fallback_chat_id:
        return {
            "channel": str(fallback_channel).strip() or "unknown",
            "chat_id": str(fallback_chat_id).strip() or "unknown",
        }

    if fallback_session_key:
        return session_key_scope(fallback_session_key)

    return {"channel": "unknown", "chat_id": "unknown"}


def metadata_memory_scope(
    metadata: Any,
    *,
    session_key: str | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
) -> dict[str, str]:
    payload = metadata if isinstance(metadata, dict) else {}
    return normalize_memory_scope(
        payload.get("memory_scope"),
        fallback_session_key=session_key,
        fallback_channel=channel,
        fallback_chat_id=chat_id,
    )

