from __future__ import annotations

from typing import Any

FRONTDOOR_HISTORY_SUMMARY_MARKER = "[frontdoor-history-summary]"
_SUMMARY_HEADER = f"## Frontdoor History Summary {FRONTDOOR_HISTORY_SUMMARY_MARKER}"
_SUMMARY_METADATA_KEY = "frontdoor_history_summary"
_SUMMARY_VERSION = 1
_MAX_SUMMARY_LINE_LENGTH = 240


def _message_role(message: dict[str, Any]) -> str:
    return str(message.get("role") or "").strip().lower()


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text", item.get("content", ""))
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(value or "").strip()


def is_frontdoor_history_summary_message(message: dict[str, Any] | None) -> bool:
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata")
    if isinstance(metadata, dict) and bool(metadata.get(_SUMMARY_METADATA_KEY)):
        return True
    return FRONTDOOR_HISTORY_SUMMARY_MARKER in _message_text(message.get("content"))


def _summary_prefix_length(messages: list[dict[str, Any]]) -> int:
    index = 0
    while index < len(messages) and _message_role(messages[index]) == "system":
        index += 1
    while index < len(messages):
        message = messages[index]
        if _message_role(message) != "assistant":
            break
        if "## Retrieved Context" not in _message_text(message.get("content")):
            break
        index += 1
    return index


def _tail_start_index(messages: list[dict[str, Any]], *, recent_message_count: int) -> int:
    if not messages:
        return 0
    # Once history is eligible for compaction, force at least one message into the
    # compacted prefix unless that would isolate the current user turn.
    keep_count = min(max(1, int(recent_message_count)), max(1, len(messages) - 1))
    if _message_role(messages[-1]) == "user" and len(messages) >= 2:
        keep_count = max(2, keep_count)
    split_index = max(0, len(messages) - keep_count)
    while split_index > 0 and _message_role(messages[split_index]) == "tool":
        split_index -= 1
    return split_index


def _render_summary_line(message: dict[str, Any]) -> str:
    role = _message_role(message) or "message"
    content = " ".join(_message_text(message.get("content")).split())
    if not content and role == "assistant" and message.get("tool_calls"):
        tool_names = [
            str(item.get("function", {}).get("name") or item.get("name") or "").strip()
            for item in list(message.get("tool_calls") or [])
            if isinstance(item, dict)
        ]
        content = f"tool calls: {', '.join(name for name in tool_names if name) or 'assistant tool call'}"
    if len(content) > _MAX_SUMMARY_LINE_LENGTH:
        content = f"{content[: _MAX_SUMMARY_LINE_LENGTH - 3].rstrip()}..."
    if not content:
        content = "(no text)"
    return f"- {role}: {content}"


def _summary_effective_message_count(message: dict[str, Any]) -> int:
    if not is_frontdoor_history_summary_message(message):
        return 1
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("compacted_message_count")
        if isinstance(value, int | float):
            return max(1, int(value))
        text = str(value or "").strip()
        if text.isdigit():
            return max(1, int(text))
    return 1


def effective_message_count(messages: list[dict[str, Any]] | None) -> int:
    total = 0
    for message in list(messages or []):
        if isinstance(message, dict):
            total += _summary_effective_message_count(message)
    return total


def _build_summary_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
    rendered: list[str] = []
    total_compacted_count = 0
    for message in messages:
        total_compacted_count += _summary_effective_message_count(message)
        if is_frontdoor_history_summary_message(message):
            rendered.append("### Preserved Prior Summary")
            rendered.extend(_message_text(message.get("content")).splitlines())
            continue
        rendered.append(_render_summary_line(message))
    content = "\n".join(
        [
            _SUMMARY_HEADER,
            "",
            f"Compacted {total_compacted_count} earlier frontdoor messages. Keep using this block as durable prior context.",
            *rendered,
        ]
    ).strip()
    return {
        "role": "assistant",
        "content": content,
        "metadata": {
            _SUMMARY_METADATA_KEY: True,
            "compacted_message_count": total_compacted_count,
            "summary_version": _SUMMARY_VERSION,
        },
    }


def frontdoor_summary_state(messages: list[dict[str, Any]] | None) -> dict[str, Any]:
    for message in list(messages or []):
        if not is_frontdoor_history_summary_message(message):
            continue
        metadata = message.get("metadata") if isinstance(message, dict) else None
        summary_version = _SUMMARY_VERSION
        summary_model_key = ""
        if isinstance(metadata, dict):
            raw_version = metadata.get("summary_version")
            raw_model_key = metadata.get("summary_model_key")
            if isinstance(raw_model_key, str):
                summary_model_key = raw_model_key.strip()
            if isinstance(raw_version, int | float):
                summary_version = max(1, int(raw_version))
            else:
                text_version = str(raw_version or "").strip()
                if text_version.isdigit():
                    summary_version = max(1, int(text_version))
        return {
            "summary_text": _message_text(message.get("content")),
            "summary_version": summary_version,
            "summary_model_key": summary_model_key,
        }
    return {
        "summary_text": "",
        "summary_version": 0,
        "summary_model_key": "",
    }


def compact_frontdoor_history(
    messages: list[dict[str, Any]] | None,
    *,
    recent_message_count: int,
    summary_trigger_message_count: int,
) -> list[dict[str, Any]]:
    normalized = [dict(message) for message in list(messages or []) if isinstance(message, dict)]
    if not normalized:
        return []

    prefix, older_messages, recent_tail = partition_frontdoor_history(
        normalized,
        recent_message_count=recent_message_count,
        summary_trigger_message_count=summary_trigger_message_count,
    )
    if not older_messages or not recent_tail:
        return normalized

    return [
        *prefix,
        _build_summary_message(older_messages),
        *recent_tail,
    ]


def partition_frontdoor_history(
    messages: list[dict[str, Any]] | None,
    *,
    recent_message_count: int,
    summary_trigger_message_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    normalized = [dict(message) for message in list(messages or []) if isinstance(message, dict)]
    if not normalized:
        return [], [], []

    prefix_length = _summary_prefix_length(normalized)
    prefix = normalized[:prefix_length]
    history = normalized[prefix_length:]
    trigger_count = max(1, int(summary_trigger_message_count))
    if len(history) <= trigger_count:
        return prefix, [], history

    tail_start = _tail_start_index(history, recent_message_count=recent_message_count)
    if tail_start <= 0:
        return prefix, [], history

    older_messages = history[:tail_start]
    recent_tail = history[tail_start:]
    return prefix, older_messages, recent_tail


__all__ = [
    "FRONTDOOR_HISTORY_SUMMARY_MARKER",
    "compact_frontdoor_history",
    "effective_message_count",
    "frontdoor_summary_state",
    "is_frontdoor_history_summary_message",
    "partition_frontdoor_history",
]
