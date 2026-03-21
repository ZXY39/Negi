from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from g3ku.config.schema import Base, MemoryToolsConfig
from g3ku.resources.manifest import load_manifest
from g3ku.resources.models import ToolResourceDescriptor

T = TypeVar("T", bound=BaseModel)


class ExecToolSettings(Base):
    timeout: int = 60
    path_append: str = ""
    restrict_to_workspace: bool = False
    enable_safety_guard: bool = False


class FilesystemToolSettings(Base):
    restrict_to_workspace: bool = False
    edit_validation_enabled: bool = True
    edit_validation_timeout_seconds: int = 20
    edit_validation_rollback_on_failure: bool = True
    edit_validation_default_commands: list[str] = Field(default_factory=list)
    edit_validation_commands_by_ext: dict[str, list[str]] = Field(
        default_factory=lambda: {'.py': ['python -m py_compile {path}']}
    )
    write_validation_enabled: bool = True
    write_validation_timeout_seconds: int = 20
    write_validation_rollback_on_failure: bool = True
    write_validation_default_commands: list[str] = Field(default_factory=list)
    write_validation_commands_by_ext: dict[str, list[str]] = Field(
        default_factory=lambda: {'.py': ['python -m py_compile {path}']}
    )


class ContentToolSettings(Base):
    restrict_to_workspace: bool = False


class MemorySearchToolSettings(Base):
    default_limit: int = 8


class MemoryRuntimeSettings(MemoryToolsConfig):
    pass


class SkillInstallerToolSettings(Base):
    download_timeout: int = 30
    git_timeout: int = 120
    auto_prefer: str = "git"


class AgentBrowserToolSettings(Base):
    command_prefix: list[str] = Field(default_factory=lambda: ['agent-browser'])
    default_timeout_seconds: int = 300
    auto_session: bool = True
    default_session_name: str = 'g3ku-agent-browser'
    profile_root: str = '.g3ku/tool-data/agent_browser/profiles'
    retry_after_session_cleanup: bool = True
    cleanup_on_timeout: bool = True


def raw_tool_settings_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload = (metadata or {}).get("settings") if isinstance(metadata, dict) else None
    return dict(payload or {}) if isinstance(payload, dict) else {}


def raw_tool_settings_from_descriptor(descriptor: ToolResourceDescriptor | None) -> dict[str, Any]:
    return raw_tool_settings_from_metadata(getattr(descriptor, "metadata", None))


def raw_tool_settings_from_runtime(runtime: Any) -> dict[str, Any]:
    payload = getattr(runtime, "tool_settings", None)
    return dict(payload or {}) if isinstance(payload, dict) else {}


def raw_tool_secrets_from_config(app_config: Any, tool_name: str) -> dict[str, Any]:
    secrets = getattr(app_config, "tool_secrets", None)
    if isinstance(secrets, dict):
        payload = secrets.get(str(tool_name or "").strip()) or {}
        return dict(payload or {}) if isinstance(payload, dict) else {}
    return {}


def raw_tool_secrets_from_runtime(runtime: Any) -> dict[str, Any]:
    payload = getattr(runtime, "tool_secrets", None)
    return dict(payload or {}) if isinstance(payload, dict) else {}


def validate_tool_settings(model_cls: type[T], payload: dict[str, Any] | None, *, tool_name: str) -> T:
    try:
        return model_cls.model_validate(dict(payload or {}))
    except Exception as exc:
        raise ValueError(f"invalid settings for tool '{tool_name}': {exc}") from exc


def runtime_tool_settings(runtime: Any, model_cls: type[T], *, tool_name: str | None = None) -> T:
    resolved_name = str(tool_name or getattr(getattr(runtime, "resource_descriptor", None), "name", "") or "tool").strip()
    return validate_tool_settings(model_cls, raw_tool_settings_from_runtime(runtime), tool_name=resolved_name)


def load_tool_settings_from_manifest(workspace: Path, tool_name: str, model_cls: type[T]) -> T:
    manifest_path = Path(workspace) / "tools" / str(tool_name or "").strip() / "resource.yaml"
    data = load_manifest(manifest_path)
    return validate_tool_settings(model_cls, raw_tool_settings_from_metadata(data), tool_name=tool_name)
