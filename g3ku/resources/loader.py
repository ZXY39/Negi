from __future__ import annotations

import hashlib
import importlib.util
import inspect
import keyword
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.repair_required import RepairRequiredTool
from g3ku.resources.embedded_mcp import EmbeddedMCPTool
from g3ku.resources.models import ToolResourceDescriptor
from g3ku.resources.tool_settings import (
    raw_tool_secrets_from_config,
    raw_tool_settings_from_descriptor,
)


class ManifestBackedTool(Tool):
    def __init__(self, descriptor: ToolResourceDescriptor, handler: Any):
        self._descriptor = descriptor
        self._handler = handler

    @property
    def name(self) -> str:
        return self._descriptor.name

    @property
    def description(self) -> str:
        return self._descriptor.description

    @property
    def model_description(self) -> str:
        if isinstance(self._handler, Tool):
            runtime_description = str(getattr(self._handler, "model_description", "") or "").strip()
            if runtime_description:
                return runtime_description
        configured = str((self._descriptor.metadata or {}).get("model_description") or "").strip()
        return configured or self.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._descriptor.parameters or {"type": "object", "properties": {}, "required": []}

    @property
    def model_parameters(self) -> dict[str, Any]:
        if isinstance(self._handler, Tool):
            runtime_parameters = getattr(self._handler, "model_parameters", None)
            if isinstance(runtime_parameters, dict):
                return _normalize_manifest_schema(runtime_parameters)
        configured = (self._descriptor.metadata or {}).get("model_parameters")
        if isinstance(configured, dict):
            return _normalize_manifest_schema(configured)
        return self.parameters

    def set_context(self, *args: Any, **kwargs: Any) -> Any:
        if hasattr(self._handler, "set_context"):
            return self._handler.set_context(*args, **kwargs)
        return None

    async def execute(self, **kwargs: Any) -> Any:
        if isinstance(self._handler, Tool):
            return await self._handler.execute(**kwargs)
        if hasattr(self._handler, "execute"):
            result = self._handler.execute(**kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        if callable(self._handler):
            result = self._handler(**kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        raise TypeError(f"Unsupported tool handler for {self.name}: {type(self._handler).__name__}")


class ResourceLoader:
    def __init__(self, workspace: Path, app_config: Any = None):
        self.workspace = Path(workspace)
        self.app_config = app_config

    def build_runtime_context(self, descriptor: ToolResourceDescriptor, services: dict[str, Any] | None = None) -> Any:
        services = dict(services or {})
        loop = services.get("loop")
        app_config = services.get("app_config", self.app_config)
        return SimpleNamespace(
            workspace=self.workspace,
            loop=loop,
            app_config=app_config,
            resource_manifest=dict(descriptor.metadata or {}),
            resource_root=descriptor.root,
            main_root=descriptor.main_root,
            toolskills_root=descriptor.toolskills_root,
            tool_settings=raw_tool_settings_from_descriptor(descriptor),
            tool_secrets=raw_tool_secrets_from_config(app_config, descriptor.name),
            services=SimpleNamespace(**services),
            resource_descriptor=descriptor,
        )

    def load_tool(self, descriptor: ToolResourceDescriptor, *, services: dict[str, Any] | None = None) -> Tool | None:
        if not bool(getattr(descriptor, "callable", True)):
            return None
        if str(descriptor.protocol or "mcp").strip().lower() != "mcp":
            raise RuntimeError(f"unsupported tool protocol for {descriptor.name}: {descriptor.protocol}")
        if str(descriptor.mcp_transport or "embedded").strip().lower() != "embedded":
            raise RuntimeError(
                f"unsupported MCP transport for {descriptor.name}: {descriptor.mcp_transport}"
            )
        entrypoint = descriptor.entrypoint_path
        if entrypoint is None or not entrypoint.exists():
            return None
        module_name = self._module_name(descriptor)
        spec = importlib.util.spec_from_file_location(module_name, entrypoint)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"failed to load module spec for {entrypoint}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        runtime_context = self.build_runtime_context(descriptor, services=services)
        if hasattr(module, "build"):
            built = module.build(runtime_context)
            if inspect.isawaitable(built):
                raise TypeError(f"tool build() must be synchronous: {descriptor.name}")
            if built is None:
                return None
            if self._requires_manifest_backed_tool(descriptor):
                return ManifestBackedTool(descriptor, built)
            return EmbeddedMCPTool(descriptor, built)
        if hasattr(module, "execute"):
            return EmbeddedMCPTool(descriptor, getattr(module, "execute"))
        raise RuntimeError(f"tool module missing build()/execute(): {entrypoint}")

    def load_repair_required_tool(self, descriptor: ToolResourceDescriptor, *, reason: str = '') -> Tool | None:
        if not bool(getattr(descriptor, 'callable', True)):
            return None
        return RepairRequiredTool(descriptor, reason=reason)

    def _module_name(self, descriptor: ToolResourceDescriptor) -> str:
        digest = descriptor.entrypoint_hash or hashlib.sha256(str(descriptor.entrypoint_path).encode("utf-8")).hexdigest()
        safe_name = descriptor.name.replace("-", "_")
        return f"g3ku_runtime_tools_{safe_name}_{digest[:12]}_{descriptor.generation}"

    @staticmethod
    def _requires_manifest_backed_tool(descriptor: ToolResourceDescriptor) -> bool:
        properties = dict((descriptor.parameters or {}).get('properties') or {})
        for name in properties:
            text = str(name or '').strip()
            if not text:
                return True
            if keyword.iskeyword(text):
                return True
            if not text.isidentifier():
                return True
        return False


def _normalize_manifest_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}
    return {
        **schema,
        "type": "object",
        "properties": dict(schema.get("properties") or {}),
        "required": list(schema.get("required") or []),
    }
