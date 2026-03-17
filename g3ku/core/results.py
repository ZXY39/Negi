from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ContentHandle:
    ref: str
    artifact_id: str = ""
    uri: str = ""
    source_kind: str = "text"
    display_name: str = ""
    mime_type: str = "text/plain"
    size_bytes: int = 0
    line_count: int = 0
    char_count: int = 0
    head_preview: str = ""
    tail_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "source_kind": self.source_kind,
            "display_name": self.display_name,
            "mime_type": self.mime_type,
            "size_bytes": int(self.size_bytes or 0),
            "line_count": int(self.line_count or 0),
            "char_count": int(self.char_count or 0),
            "head_preview": self.head_preview,
            "tail_preview": self.tail_preview,
        }


@dataclass(slots=True)
class ContentEnvelope:
    type: str = "content_ref"
    summary: str = ""
    ref: str = ""
    handle: ContentHandle | None = None
    next_actions: list[str] = field(default_factory=lambda: ["content.search", "content.open"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "summary": self.summary,
            "ref": self.ref,
            "handle": self.handle.to_dict() if self.handle is not None else None,
            "next_actions": list(self.next_actions or []),
        }


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
