from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def extract_call_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "|" in raw:
        return raw.split("|", 1)[0].strip()
    return raw


@dataclass(slots=True)
class ToolCallHistoryAnalysis:
    declared_call_ids: list[str] = field(default_factory=list)
    completed_call_ids: list[str] = field(default_factory=list)
    orphan_tool_result_ids: list[str] = field(default_factory=list)
    dangling_assistant_call_ids: list[str] = field(default_factory=list)

    @property
    def has_orphan_tool_results(self) -> bool:
        return bool(self.orphan_tool_result_ids)

    @property
    def has_dangling_assistant_calls(self) -> bool:
        return bool(self.dangling_assistant_call_ids)


def analyze_tool_call_history(messages: list[dict[str, Any]]) -> ToolCallHistoryAnalysis:
    declared_seen: set[str] = set()
    completed_seen: set[str] = set()
    declared_call_ids: list[str] = []
    completed_call_ids: list[str] = []

    for msg in list(messages or []):
        role = str((msg or {}).get("role") or "").strip().lower()
        if role == "assistant":
            for tool_call in list((msg or {}).get("tool_calls") or []):
                call_id = extract_call_id((tool_call or {}).get("id"))
                if call_id and call_id not in declared_seen:
                    declared_seen.add(call_id)
                    declared_call_ids.append(call_id)
            continue
        if role == "tool":
            call_id = extract_call_id((msg or {}).get("tool_call_id"))
            if call_id and call_id not in completed_seen:
                completed_seen.add(call_id)
                completed_call_ids.append(call_id)

    declared_set = set(declared_call_ids)
    completed_set = set(completed_call_ids)
    orphan_tool_result_ids = [call_id for call_id in completed_call_ids if call_id not in declared_set]
    dangling_assistant_call_ids = [call_id for call_id in declared_call_ids if call_id not in completed_set]

    return ToolCallHistoryAnalysis(
        declared_call_ids=declared_call_ids,
        completed_call_ids=completed_call_ids,
        orphan_tool_result_ids=orphan_tool_result_ids,
        dangling_assistant_call_ids=dangling_assistant_call_ids,
    )
