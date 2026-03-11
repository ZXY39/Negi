from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


ContentPayload: TypeAlias = str | list[dict[str, Any]]


@dataclass(slots=True)
class UserInputMessage:
    role: Literal["user"] = "user"
    content: ContentPayload = ""
    attachments: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str | None = None


@dataclass(slots=True)
class AssistantMessage:
    role: Literal["assistant"] = "assistant"
    content: ContentPayload = ""
    thinking: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str | None = None


@dataclass(slots=True)
class ToolResultMessage:
    role: Literal["tool_result"] = "tool_result"
    tool_name: str = ""
    tool_call_id: str = ""
    content: ContentPayload = ""
    is_error: bool = False
    timestamp: str | None = None


@dataclass(slots=True)
class SystemNoteMessage:
    role: Literal["system_note"] = "system_note"
    content: str = ""
    timestamp: str | None = None


@dataclass(slots=True)
class ControlMessage:
    role: Literal["control"] = "control"
    action: str = ""
    content: str = ""
    timestamp: str | None = None


@dataclass(slots=True)
class AttachmentReferenceMessage:
    role: Literal["attachment"] = "attachment"
    placeholder: str = ""
    name: str = ""
    mime: str = ""
    timestamp: str | None = None


AgentMessage = (
    UserInputMessage
    | AssistantMessage
    | ToolResultMessage
    | SystemNoteMessage
    | ControlMessage
    | AttachmentReferenceMessage
)
