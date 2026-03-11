"""Capability administration tools."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.capabilities.installer import CapabilityInstaller
from g3ku.capabilities.registry import CapabilityRegistry
from g3ku.capabilities.validator import CapabilityValidator


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _runtime_session_key(kwargs: dict[str, Any]) -> str:
    runtime = kwargs.get("__g3ku_runtime") if isinstance(kwargs, dict) else None
    if isinstance(runtime, dict):
        return str(runtime.get("session_key") or "unknown")
    return "unknown"


class _CapabilityAdminTool(Tool):
    def __init__(self, registry: CapabilityRegistry, installer: CapabilityInstaller, validator: CapabilityValidator):
        self._registry = registry
        self._installer = installer
        self._validator = validator


class CapabilityListTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_list"

    @property
    def description(self) -> str:
        return "List installed and builtin capability packs, including enabled and availability state."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return _dump(self._installer.list())


class CapabilitySourcesTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_sources"

    @property
    def description(self) -> str:
        return "List configured capability source types and source-policy status."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return _dump(self._installer.list_sources())


class CapabilitySearchTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_search"

    @property
    def description(self) -> str:
        return "Search configured capability indexes for installable capability packages."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"query": {"type": "string", "description": "Optional capability name filter."}}}

    async def execute(self, query: str | None = None, **kwargs: Any) -> str:
        _ = kwargs
        return _dump([asdict(item) for item in self._installer.search(query)])


class CapabilityValidateTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_validate"

    @property
    def description(self) -> str:
        return "Validate one capability pack or all packs before enable/update."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"name": {"type": "string", "description": "Capability name. Omit to validate all."}}}

    async def execute(self, name: str | None = None, **kwargs: Any) -> str:
        _ = kwargs
        if name:
            result = self._validator.validate_capability(name)
            return _dump({"name": result.name, "ok": result.ok, "errors": result.errors, "warnings": result.warnings})
        results = self._validator.validate_all()
        return _dump([{"name": item.name, "ok": item.ok, "errors": item.errors, "warnings": item.warnings} for item in results])


class CapabilityEnableTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_enable"

    @property
    def description(self) -> str:
        return "Enable a capability pack."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"name": {"type": "string"}, "dry_run": {"type": "boolean"}}, "required": ["name"]}

    async def execute(self, name: str, dry_run: bool = False, **kwargs: Any) -> str:
        _ = kwargs
        result = self._installer.enable(name, dry_run=dry_run)
        return _dump(asdict(result))


class CapabilityDisableTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_disable"

    @property
    def description(self) -> str:
        return "Disable a capability pack without deleting it."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"name": {"type": "string"}, "dry_run": {"type": "boolean"}}, "required": ["name"]}

    async def execute(self, name: str, dry_run: bool = False, **kwargs: Any) -> str:
        _ = kwargs
        result = self._installer.disable(name, dry_run=dry_run)
        return _dump(asdict(result))


class CapabilityInitTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_init"

    @property
    def description(self) -> str:
        return "Scaffold a new workspace capability pack with tool/skill manifests."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "capability_type": {"type": "string", "enum": ["tool", "skill", "hybrid"]},
                "dry_run": {"type": "boolean"},
            },
            "required": ["name"],
        }

    async def execute(self, name: str, capability_type: str = "hybrid", dry_run: bool = False, **kwargs: Any) -> str:
        _ = kwargs
        result = self._installer.init_capability(name=name, capability_type=capability_type, dry_run=dry_run)
        return _dump(asdict(result))


class CapabilityInstallTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_install"

    @property
    def description(self) -> str:
        return "Install a capability pack from local path, git, or registry."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Local path, git repository URL, or registry package name."},
                "source_type": {"type": "string", "enum": ["local", "git", "registry"]},
                "ref": {"type": "string", "description": "Optional git ref."},
                "version": {"type": "string", "description": "Optional registry version."},
                "index_name": {"type": "string", "description": "Optional registry index name filter."},
                "enable": {"type": "boolean", "description": "Enable after install."},
                "dry_run": {"type": "boolean"},
            },
            "required": ["source", "source_type"],
        }

    async def execute(self, source: str, source_type: str, ref: str | None = None, version: str | None = None, index_name: str | None = None, enable: bool = True, dry_run: bool = False, **kwargs: Any) -> str:
        _ = kwargs
        if source_type == "git":
            result = self._installer.install_from_git(source, ref=ref, enable=enable, dry_run=dry_run)
        elif source_type == "registry":
            result = self._installer.install_from_registry(source, version=version, index_name=index_name, enable=enable, dry_run=dry_run)
        else:
            result = self._installer.install_from_path(source, enable=enable, dry_run=dry_run)
        return _dump(asdict(result))


class CapabilityUpdateTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_update"

    @property
    def description(self) -> str:
        return "Update an installed capability from its recorded source."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"name": {"type": "string"}, "dry_run": {"type": "boolean"}}, "required": ["name"]}

    async def execute(self, name: str, dry_run: bool = False, **kwargs: Any) -> str:
        _ = kwargs
        result = self._installer.update(name, dry_run=dry_run)
        return _dump(asdict(result))


class CapabilityRemoveTool(_CapabilityAdminTool):
    @property
    def name(self) -> str:
        return "capability_remove"

    @property
    def description(self) -> str:
        return "Remove a workspace-installed capability or clear builtin overrides."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"name": {"type": "string"}, "dry_run": {"type": "boolean"}}, "required": ["name"]}

    async def execute(self, name: str, dry_run: bool = False, **kwargs: Any) -> str:
        _ = kwargs
        result = self._installer.remove(name, dry_run=dry_run)
        return _dump(asdict(result))

