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
    origin_ref: str = ""
    # 对文件路径目标，size_bytes 是**磁盘上的真实字节数**；对文本产物（artifact）是文本
    # 编码后的字节数。两者都以「目标的真实体积」为准，而不是「展示出来的文本体积」。
    size_bytes: int = 0
    line_count: int = 0
    char_count: int = 0
    # True 表示 text/char_count/line_count 描述的是展示占位串（"[二进制文件：X]" / "[图片文件：X]"），
    # 而不是目标文件的内容——二进制目标的展示文本，绝不能当作文件体积或内容证据。
    content_display_replaced: bool = False
    head_preview: str = ""
    tail_preview: str = ""
    requested_ref: str = ""
    resolved_ref: str = ""
    wrapper_ref: str = ""
    wrapper_depth: int = 0
    invocation_text: str = ""
    canonical_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "source_kind": self.source_kind,
            "display_name": self.display_name,
            "mime_type": self.mime_type,
            "origin_ref": self.origin_ref,
            "size_bytes": int(self.size_bytes or 0),
            "line_count": int(self.line_count or 0),
            "char_count": int(self.char_count or 0),
            "content_display_replaced": bool(self.content_display_replaced),
            "head_preview": self.head_preview,
            "tail_preview": self.tail_preview,
            "requested_ref": self.requested_ref,
            "resolved_ref": self.resolved_ref,
            "wrapper_ref": self.wrapper_ref,
            "wrapper_depth": int(self.wrapper_depth or 0),
            "invocation_text": self.invocation_text,
            "canonical_summary": self.canonical_summary,
        }


@dataclass(slots=True)
class ContentEnvelope:
    type: str = "content_ref"
    summary: str = ""
    ref: str = ""
    resolved_ref: str = ""
    wrapper_ref: str = ""
    handle: ContentHandle | None = None
    next_actions: list[str] = field(default_factory=lambda: ["content_search", "content_open"])

    def to_dict(
        self,
        *,
        include_handle: bool = True,
        summary_override: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "type": self.type,
            "summary": self.summary if summary_override is None else str(summary_override or ""),
            "ref": self.ref,
            "resolved_ref": self.resolved_ref,
            "wrapper_ref": self.wrapper_ref,
            "next_actions": list(self.next_actions or []),
        }
        if include_handle:
            payload["handle"] = self.handle.to_dict() if self.handle is not None else None
        return payload

    def to_model_dict(self, *, summary_override: str | None = None) -> dict[str, Any]:
        return self.to_dict(include_handle=False, summary_override=summary_override)


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
    is_silent_reply: bool = False
