from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CapabilitySource:
    type: str = "builtin"
    uri: str | None = None
    ref: str | None = None


@dataclass(slots=True)
class CapabilityDescriptor:
    name: str
    display_name: str
    version: str
    root: Path
    manifest_path: Path
    kind: list[str] = field(default_factory=list)
    compat: dict[str, str] = field(default_factory=dict)
    requires: dict[str, list[str]] = field(default_factory=dict)
    exported_tools: list[str] = field(default_factory=list)
    exported_skills: list[str] = field(default_factory=list)
    exported_agents: list[str] = field(default_factory=list)
    source: CapabilitySource = field(default_factory=CapabilitySource)
    enabled: bool = True
    available: bool = True
    defaults: dict[str, Any] = field(default_factory=dict)
    healthcheck: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolDescriptor:
    name: str
    label: str
    description: str
    entrypoint: str
    capability_name: str
    capability_version: str
    manifest_path: Path
    root: Path
    config_schema_path: Path | None = None
    result_schema_path: Path | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    streaming: bool = False
    admin_only: bool = False
    enabled: bool = True
    available: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SkillDescriptor:
    name: str
    description: str
    capability_name: str
    capability_version: str
    manifest_path: Path
    root: Path
    main_path: Path
    reference_paths: list[Path] = field(default_factory=list)
    trigger_keywords: list[str] = field(default_factory=list)
    always: bool = False
    requires_tools: list[str] = field(default_factory=list)
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    enabled: bool = True
    available: bool = True
    legacy: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentRoleDescriptor:
    name: str
    label: str
    description: str
    role_type: str
    capability_name: str
    capability_version: str
    manifest_path: Path
    root: Path
    prompt_path: Path
    pattern_affinity: list[str] = field(default_factory=list)
    delegate_roles: list[str] = field(default_factory=list)
    reference_paths: list[Path] = field(default_factory=list)
    tools_allow: list[str] = field(default_factory=list)
    skills_allow: list[str] = field(default_factory=list)
    context_policy: dict[str, Any] = field(default_factory=dict)
    output_policy: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    result_schema_path: Path | None = None
    enabled: bool = True
    available: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CapabilityValidationResult:
    name: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CapabilityRecord:
    enabled: bool = True
    version: str | None = None
    install_hash: str | None = None
    last_validated_at: str | None = None
    compatibility: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    installed_path: str | None = None


@dataclass(slots=True)
class CapabilityInstallResult:
    ok: bool
    name: str
    action: str
    message: str
    path: str | None = None
    version: str | None = None
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CapabilityIndexSource:
    type: str
    uri: str
    ref: str | None = None


@dataclass(slots=True)
class CapabilityIndexCandidate:
    name: str
    version: str
    display_name: str
    source: CapabilityIndexSource
    compat: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    index_name: str | None = None
    index_path: str | None = None
