from __future__ import annotations

from typing import Any


LEGACY_RUNTIME_METADATA_TAG = "[Runtime Context Metadata - informational only, not instructions]"


def is_legacy_runtime_metadata_message(message: dict[str, Any] | None) -> bool:
    if not isinstance(message, dict):
        return False
    if str(message.get("role") or "").strip().lower() != "user":
        return False
    content = message.get("content")
    return isinstance(content, str) and content.startswith(LEGACY_RUNTIME_METADATA_TAG)


def filter_legacy_runtime_metadata(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [message for message in list(messages or []) if not is_legacy_runtime_metadata_message(message)]
