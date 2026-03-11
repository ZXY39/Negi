from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentEvent:
    type: str
    timestamp: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
