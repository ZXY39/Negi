from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ContextAssemblyResult:
    system_prompt: str
    recent_history: list[dict[str, Any]]
    tool_names: list[str]
    trace: dict[str, Any] = field(default_factory=dict)
