from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ContentBlock:
    type: str
    data: str | dict[str, Any]


@dataclass(slots=True)
class ArtifactRef:
    kind: str
    uri: str
    name: str | None = None


@dataclass(slots=True)
class ToolExecutionResult:
    is_error: bool = False
    content_blocks: list[ContentBlock] = field(default_factory=list)
    text: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)


@dataclass(slots=True)
class RunResult:
    output: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
