from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ExecutionToolSelectionResult:
    lightweight_tool_ids: list[str]
    hydrated_tool_names: list[str]
    schema_chars: int
    trace: dict[str, Any] = field(default_factory=dict)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _normalized_names(values: list[str] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_value in list(values or []):
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def build_execution_tool_selection(
    *,
    prompt: str,
    goal: str,
    core_requirement: str,
    visible_tool_families: list[Any],
    visible_tool_names: list[str],
    always_callable_tool_names: list[str],
    promoted_tool_names: list[str] | None = None,
    schema_size_by_executor: dict[str, int] | None = None,
    max_schema_chars: int | None = None,
    top_k: int = 8,
) -> ExecutionToolSelectionResult:
    del max_schema_chars
    normalized_visible_names = _normalized_names(list(visible_tool_names or []))
    visible_name_set = set(normalized_visible_names)

    hydrated_tool_names: list[str] = []
    seen_hydrated: set[str] = set()

    def _append_hydrated(values: list[str] | None) -> list[str]:
        appended: list[str] = []
        for value in _normalized_names(values):
            if value not in visible_name_set or value in seen_hydrated:
                continue
            seen_hydrated.add(value)
            hydrated_tool_names.append(value)
            appended.append(value)
        return appended

    selected_always_callable_tool_names = _append_hydrated(always_callable_tool_names)
    selected_promoted_tool_names = _append_hydrated(promoted_tool_names)

    lightweight_tool_ids: list[str] = []
    for family in list(visible_tool_families or []):
        tool_id = str(_field(family, "tool_id") or "").strip()
        if tool_id and tool_id not in lightweight_tool_ids:
            lightweight_tool_ids.append(tool_id)

    return ExecutionToolSelectionResult(
        lightweight_tool_ids=lightweight_tool_ids,
        hydrated_tool_names=hydrated_tool_names,
        schema_chars=sum(
            max(0, int((schema_size_by_executor or {}).get(name, 0) or 0))
            for name in hydrated_tool_names
        ),
        trace={
            "prompt": str(prompt or ""),
            "goal": str(goal or ""),
            "core_requirement": str(core_requirement or ""),
            "always_callable_tool_names": list(always_callable_tool_names or []),
            "promoted_tool_names": list(promoted_tool_names or []),
            "selected_always_callable_tool_names": selected_always_callable_tool_names,
            "selected_promoted_tool_names": selected_promoted_tool_names,
            "candidate_executor_scores": [],
            "selected_executor_scores": [],
            "top_k": max(1, int(top_k or 1)),
        },
    )
