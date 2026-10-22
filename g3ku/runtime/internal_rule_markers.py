from __future__ import annotations

from typing import Any

DEFAULT_INTERNAL_RULE_MARKERS: tuple[str, ...] = (
    "This is a background heartbeat.",
    "# Heartbeat Rules",
)


def internal_rule_message_indexes(
    messages: list[dict[str, Any]] | None,
    *,
    after_index: int = 0,
    markers: tuple[str, ...] = DEFAULT_INTERNAL_RULE_MARKERS,
) -> list[int]:
    """Indexes of user/system messages carrying a repeated internal-rule marker.

    The caller decides the cache-neutral lower bound; this helper only applies the
    same marker/role predicate the stage compactor used.
    """
    normalized_markers = tuple(str(item or "") for item in list(markers or ()) if str(item or ""))
    if not normalized_markers:
        return []
    indexes: list[int] = []
    for index, message in enumerate(list(messages or [])):
        if index < max(0, int(after_index or 0)):
            continue
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in ("user", "system"):
            continue
        content = str(message.get("content") or "")
        if any(marker in content for marker in normalized_markers):
            indexes.append(index)
    return indexes


__all__ = [
    "DEFAULT_INTERNAL_RULE_MARKERS",
    "internal_rule_message_indexes",
]
