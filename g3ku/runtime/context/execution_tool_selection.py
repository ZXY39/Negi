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


def build_execution_tool_selection(
    *,
    prompt: str,
    goal: str,
    core_requirement: str,
    visible_tool_families: list[Any],
    visible_tool_names: list[str],
    schema_size_by_executor: dict[str, int],
    always_callable_tool_names: list[str],
    max_schema_chars: int,
) -> ExecutionToolSelectionResult:
    normalized_visible_names = [
        str(name or "").strip()
        for name in list(visible_tool_names or [])
        if str(name or "").strip()
    ]
    hydrated: list[str] = []
    seen: set[str] = set()
    budget = max(0, int(max_schema_chars or 0))
    used = 0

    for tool_name in list(always_callable_tool_names or []):
        normalized = str(tool_name or "").strip()
        if not normalized or normalized in seen or normalized not in normalized_visible_names:
            continue
        size = max(0, int(schema_size_by_executor.get(normalized, 0) or 0))
        hydrated.append(normalized)
        seen.add(normalized)
        used += size

    lightweight_tool_ids: list[str] = []
    optional_hydration_enabled = budget > 0
    for family in list(visible_tool_families or []):
        tool_id = str(_field(family, "tool_id") or "").strip()
        if tool_id and tool_id not in lightweight_tool_ids:
            lightweight_tool_ids.append(tool_id)
        if not optional_hydration_enabled:
            continue
        for action in list(_field(family, "actions") or []):
            for executor_name in list(_field(action, "executor_names") or []):
                executor = str(executor_name or "").strip()
                if not executor or executor in seen or executor not in normalized_visible_names:
                    continue
                size = max(0, int(schema_size_by_executor.get(executor, 0) or 0))
                if used + size > budget:
                    continue
                hydrated.append(executor)
                seen.add(executor)
                used += size

    return ExecutionToolSelectionResult(
        lightweight_tool_ids=lightweight_tool_ids,
        hydrated_tool_names=hydrated,
        schema_chars=used,
        trace={
            "prompt": str(prompt or ""),
            "goal": str(goal or ""),
            "core_requirement": str(core_requirement or ""),
            "budget": budget,
            "optional_hydration_enabled": optional_hydration_enabled,
        },
    )
