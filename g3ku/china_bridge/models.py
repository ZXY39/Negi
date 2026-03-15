from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ChinaAttachment:
    kind: str = "unknown"
    url: str | None = None
    path: str | None = None
    mime_type: str | None = None
    file_name: str | None = None
    size_bytes: int | None = None


@dataclass(slots=True)
class ChinaInboundEnvelope:
    event_id: str
    channel: str
    account_id: str
    peer_kind: str
    peer_id: str
    peer_display_name: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    text: str = ""
    attachments: list[ChinaAttachment] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChinaBridgeState:
    enabled: bool = False
    running: bool = False
    connected: bool = False
    built: bool = False
    pid: int | None = None
    public_port: int | None = None
    control_port: int | None = None
    last_error: str = ""
    last_event_at: str = ""
    channels: dict[str, Any] = field(default_factory=dict)
