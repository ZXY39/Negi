from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool


class ResourceKind(str, Enum):
    SKILL = "skill"
    TOOL = "tool"


@dataclass(slots=True)
class ResourceBusyState:
    refs: int = 0
    pending_delete: bool = False
    busy: bool = False


@dataclass(slots=True)
class BaseResourceDescriptor:
    kind: ResourceKind
    name: str
    description: str
    root: Path
    manifest_path: Path
    fingerprint: str
    enabled: bool = True
    available: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    exposure: dict[str, bool] = field(default_factory=dict)
    generation: int = 0


@dataclass(slots=True)
class SkillResourceDescriptor(BaseResourceDescriptor):
    main_path: Path | None = None
    references_root: Path | None = None
    scripts_root: Path | None = None
    assets_root: Path | None = None
    trigger_keywords: list[str] = field(default_factory=list)
    always: bool = False
    requires_tools: list[str] = field(default_factory=list)
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolResourceDescriptor(BaseResourceDescriptor):
    main_root: Path | None = None
    entrypoint_path: Path | None = None
    entrypoint_hash: str = ""
    protocol: str = "mcp"
    mcp_transport: str = "embedded"
    toolskills_root: Path | None = None
    toolskills_main_path: Path | None = None
    toolskills_references_root: Path | None = None
    toolskills_scripts_root: Path | None = None
    toolskills_assets_root: Path | None = None
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}, "required": []})
    permissions: dict[str, Any] = field(default_factory=dict)
    requires_tools: list[str] = field(default_factory=list)
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    config_namespace: str = ""
    toolskill_enabled: bool = True


@dataclass(slots=True)
class ResourceSnapshot:
    generation: int
    tools: dict[str, ToolResourceDescriptor] = field(default_factory=dict)
    skills: dict[str, SkillResourceDescriptor] = field(default_factory=dict)
    tool_instances: dict[str, Tool] = field(default_factory=dict)
