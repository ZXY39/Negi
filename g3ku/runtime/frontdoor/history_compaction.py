from __future__ import annotations

from typing import Any

_SUMMARY_SNIPPET_LIMIT = 120
_SUMMARY_MESSAGE_LIMIT = 6
_SUMMARY_HEAD_LIMIT = 3


def _truncate(text: str, *, limit: int) -> str:
    value = " ".join(str(text or "").split()).strip()
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3].rstrip()}..."


def _message_text(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = " ".join(item.split()).strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text", item.get("content", ""))
            if isinstance(text, str):
                normalized = " ".join(text.split()).strip()
                if normalized:
                    parts.append(normalized)
        return "\n".join(parts).strip()
    return " ".join(str(content or "").split()).strip()


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        role = message.get("role", "")
    else:
        role = getattr(message, "type", "") or getattr(message, "role", "")
    normalized = str(role or "").strip().lower()
    if normalized == "human":
        return "user"
    if normalized == "ai":
        return "assistant"
    return normalized


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def _message_has_tool_calls(message: Any) -> bool:
    if isinstance(message, dict):
        return bool(message.get("tool_calls"))
    return bool(getattr(message, "tool_calls", None))


def build_compact_boundary_message(*, summarized_count: int) -> dict[str, Any]:
    return {
        "role": "system",
        "content": f"COMPACT BOUNDARY: summarized {max(0, int(summarized_count))} earlier conversation messages.",
    }


def build_history_summary_message(*, messages: list[Any]) -> dict[str, Any]:
    snippets: list[str] = []
    for item in list(messages or []):
        role = _message_role(item)
        content = _truncate(_message_text(_message_content(item)), limit=_SUMMARY_SNIPPET_LIMIT)
        if role and content:
            snippets.append(f"{role}: {content}")
    if len(snippets) > _SUMMARY_MESSAGE_LIMIT:
        head = snippets[:_SUMMARY_HEAD_LIMIT]
        tail_limit = max(_SUMMARY_MESSAGE_LIMIT - len(head), 0)
        tail = snippets[-tail_limit:] if tail_limit else []
        snippets = [*head, *tail]
    summary = " | ".join(snippets) if snippets else "Earlier conversation was compacted."
    return {
        "role": "assistant",
        "content": f"Conversation summary: {summary}",
    }


def _tail_start_index(messages: list[Any], *, keep_message_count: int) -> int:
    start = max(0, len(messages) - max(1, int(keep_message_count)))
    while start > 0:
        current = messages[start]
        if _message_role(current) == "tool":
            start -= 1
            continue
        previous = messages[start - 1]
        if _message_role(previous) == "assistant" and _message_has_tool_calls(previous):
            start -= 1
            continue
        break
    return start


def compact_history_messages(
    *,
    messages: list[Any],
    trigger_message_count: int,
    keep_message_count: int,
) -> list[Any]:
    items = list(messages or [])
    if not items:
        return []

    trigger = max(1, int(trigger_message_count))
    keep = max(1, int(keep_message_count))
    if len(items) <= trigger:
        return items

    system_end = 0
    while system_end < len(items) and _message_role(items[system_end]) == "system":
        system_end += 1

    leading_system = items[:system_end]
    remainder = items[system_end:]
    if len(remainder) <= keep:
        return items

    tail_start = _tail_start_index(remainder, keep_message_count=keep)
    tail = remainder[tail_start:]
    prefix = remainder[:tail_start]
    if not prefix:
        return items

    return [
        *leading_system,
        build_compact_boundary_message(summarized_count=len(prefix)),
        build_history_summary_message(messages=prefix),
        *tail,
    ]
