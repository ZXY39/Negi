from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .history_compaction import compact_frontdoor_history, frontdoor_summary_state

SummaryInvoker = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class CeoSummaryResult:
    messages: list[dict[str, Any]]
    summary_text: str
    summary_payload: dict[str, Any]
    summary_version: int
    summary_model_key: str


def _render_summary_text(payload: dict[str, Any]) -> str:
    lines = [
        "## CEO Durable Summary",
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
    prefix = normalized_messages[:-keep_count]
    tail = normalized_messages[-keep_count:]
    prompt = {
        "previous_summary_text": str(previous_summary_text or ""),
        "previous_summary_payload": dict(previous_summary_payload or {}),
        "messages": prefix,
    }
    try:
        raw_payload = await model_invoke(prompt)
        payload = dict(raw_payload or {})
    except Exception:
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
    summary_text = _render_summary_text(payload)
    summary_message = {
        "role": "assistant",
        "content": summary_text,
        "metadata": {
            "frontdoor_history_summary": True,
            "summary_version": 2,
            "summary_model_key": str(model_key or "").strip(),
            "compacted_message_count": len(prefix),
        },
    }
    return CeoSummaryResult(
        messages=[summary_message, *tail],
        summary_text=summary_text,
        summary_payload=payload,
        summary_version=2,
        summary_model_key=str(model_key or "").strip(),
    )
