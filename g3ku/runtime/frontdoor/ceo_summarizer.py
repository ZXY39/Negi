from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .history_compaction import (
    FRONTDOOR_HISTORY_SUMMARY_MARKER,
    compact_frontdoor_history,
    effective_message_count,
    frontdoor_summary_state,
    partition_frontdoor_history,
)

SummaryInvoker = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class CeoSummaryResult:
    messages: list[dict[str, Any]]
    summary_text: str
    summary_payload: dict[str, Any]
    summary_version: int
    summary_model_key: str


class _ModelSummaryValidationError(ValueError):
    pass


def _normalize_string_list_field(payload: dict[str, Any], field_name: str) -> list[str]:
    raw_value = payload.get(field_name)
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise _ModelSummaryValidationError(f"{field_name} must be a list of strings")
    normalized: list[str] = []
    for item in raw_value:
        if not isinstance(item, str):
            raise _ModelSummaryValidationError(f"{field_name} must be a list of strings")
        text = item.strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_summary_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise _ModelSummaryValidationError("model summary payload must be a dict")

    raw_narrative = raw_payload.get("narrative")
    if raw_narrative is None:
        narrative = ""
    elif isinstance(raw_narrative, str):
        narrative = raw_narrative.strip()
    else:
        raise _ModelSummaryValidationError("narrative must be a string")

    return {
        "stable_preferences": _normalize_string_list_field(raw_payload, "stable_preferences"),
        "stable_facts": _normalize_string_list_field(raw_payload, "stable_facts"),
        "open_loops": _normalize_string_list_field(raw_payload, "open_loops"),
        "recent_actions": _normalize_string_list_field(raw_payload, "recent_actions"),
        "narrative": narrative,
    }


def _heuristic_fallback_result(
    *,
    normalized_messages: list[dict[str, Any]],
    previous_summary_payload: dict[str, Any],
    keep_count: int,
    trigger_message_count: int,
) -> CeoSummaryResult:
    compacted = compact_frontdoor_history(
        normalized_messages,
        recent_message_count=keep_count,
        summary_trigger_message_count=max(1, int(trigger_message_count)),
    )
    state = frontdoor_summary_state(compacted)
    return CeoSummaryResult(
        messages=compacted,
        summary_text=str(state.get("summary_text") or ""),
        summary_payload={"fallback": "heuristic"},
        summary_version=int(state.get("summary_version") or 1),
        summary_model_key="",
    )


def _render_summary_text(payload: dict[str, Any]) -> str:
    lines = [
        f"## CEO Durable Summary {FRONTDOOR_HISTORY_SUMMARY_MARKER}",
        "",
        "### Stable Preferences",
        *[f"- {item}" for item in list(payload.get("stable_preferences") or [])],
        "### Stable Facts",
        *[f"- {item}" for item in list(payload.get("stable_facts") or [])],
        "### Open Loops",
        *[f"- {item}" for item in list(payload.get("open_loops") or [])],
        "### Recent Actions",
        *[f"- {item}" for item in list(payload.get("recent_actions") or [])],
        "### Narrative",
        str(payload.get("narrative") or "").strip(),
    ]
    return "\n".join(line for line in lines if str(line).strip()).strip()


async def summarize_frontdoor_history(
    *,
    messages: list[dict[str, Any]],
    previous_summary_text: str,
    previous_summary_payload: dict[str, Any],
    keep_message_count: int,
    trigger_message_count: int,
    model_key: str | None,
    model_invoke: SummaryInvoker,
) -> CeoSummaryResult:
    normalized_messages = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
    if len(normalized_messages) <= max(1, int(trigger_message_count)):
        state = frontdoor_summary_state(normalized_messages)
        return CeoSummaryResult(
            messages=normalized_messages,
            summary_text=str(state.get("summary_text") or previous_summary_text or ""),
            summary_payload=dict(previous_summary_payload or {}),
            summary_version=int(state.get("summary_version") or 0),
            summary_model_key=str(state.get("summary_model_key") or ""),
        )

    keep_count = max(1, int(keep_message_count))
    preserved_prefix, compactable_messages, tail = partition_frontdoor_history(
        normalized_messages,
        recent_message_count=keep_count,
        summary_trigger_message_count=max(1, int(trigger_message_count)),
    )
    if not compactable_messages or not tail:
        state = frontdoor_summary_state(normalized_messages)
        return CeoSummaryResult(
            messages=normalized_messages,
            summary_text=str(state.get("summary_text") or previous_summary_text or ""),
            summary_payload=dict(previous_summary_payload or {}),
            summary_version=int(state.get("summary_version") or 0),
            summary_model_key=str(state.get("summary_model_key") or ""),
        )
    prompt = {
        "previous_summary_text": str(previous_summary_text or ""),
        "previous_summary_payload": dict(previous_summary_payload or {}),
        "messages": compactable_messages,
    }
    try:
        raw_payload = await model_invoke(prompt)
    except Exception:
        return _heuristic_fallback_result(
            normalized_messages=normalized_messages,
            previous_summary_payload=previous_summary_payload,
            keep_count=keep_count,
            trigger_message_count=trigger_message_count,
        )
    try:
        payload = _normalize_summary_payload(raw_payload)
        summary_text = _render_summary_text(payload)
    except _ModelSummaryValidationError:
        return _heuristic_fallback_result(
            normalized_messages=normalized_messages,
            previous_summary_payload=previous_summary_payload,
            keep_count=keep_count,
            trigger_message_count=trigger_message_count,
        )
    summary_message = {
        "role": "assistant",
        "content": summary_text,
        "metadata": {
            "frontdoor_history_summary": True,
            "summary_version": 2,
            "summary_model_key": str(model_key or "").strip(),
            "compacted_message_count": effective_message_count(compactable_messages),
        },
    }
    return CeoSummaryResult(
        messages=[*preserved_prefix, summary_message, *tail],
        summary_text=summary_text,
        summary_payload=payload,
        summary_version=2,
        summary_model_key=str(model_key or "").strip(),
    )
