from __future__ import annotations

from datetime import datetime
from pathlib import Path

from g3ku.capabilities.models import CapabilityValidationResult
from g3ku.capabilities.registry import CapabilityRegistry
from g3ku.capabilities.utils import import_from_file, import_string


class CapabilityValidator:
    """Validate capability manifests, entrypoints, and bundled files."""

    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def validate_capability(self, name: str) -> CapabilityValidationResult:
        capability = self.registry.get_capability(name)
        if capability is None:
            return CapabilityValidationResult(name=name, ok=False, errors=[f"Capability not found: {name}"])

        errors = list(capability.errors)
        warnings = list(capability.warnings)
        checked_paths = [str(capability.manifest_path)]

        tools = [item for item in self.registry.list_tools() if item.capability_name == name]
        tool_names = {tool.name for tool in tools}
        for exported_name in capability.exported_tools:
            if exported_name not in tool_names:
                errors.append(f"capability export missing tool descriptor: {exported_name}")

        for tool in tools:
            checked_paths.append(str(tool.manifest_path))
            if not tool.entrypoint:
                errors.append(f"tool {tool.name} missing entrypoint")
                continue
            try:
                self._resolve_tool_entrypoint(tool)
            except Exception as exc:
                errors.append(f"tool {tool.name} entrypoint import failed: {exc}")
            if tool.config_schema_path and not Path(tool.config_schema_path).exists():
                errors.append(f"tool {tool.name} config schema missing: {tool.config_schema_path}")
            if tool.result_schema_path and not Path(tool.result_schema_path).exists():
                errors.append(f"tool {tool.name} result schema missing: {tool.result_schema_path}")

        skills = [item for item in self.registry.list_skills() if item.capability_name == name and not item.legacy]
        skill_names = {skill.name for skill in skills}
        for exported_name in capability.exported_skills:
            if exported_name not in skill_names:
                errors.append(f"capability export missing skill descriptor: {exported_name}")

        for skill in skills:
            checked_paths.append(str(skill.manifest_path))
            if not skill.main_path.exists():
                errors.append(f"skill {skill.name} main file missing: {skill.main_path}")
            for ref in skill.reference_paths:
                if not ref.exists():
                    errors.append(f"skill {skill.name} reference missing: {ref}")

        agents = [item for item in self.registry.list_agents() if item.capability_name == name]
        agent_names = {agent.name for agent in agents}
        for exported_name in capability.exported_agents:
            if exported_name not in agent_names:
                errors.append(f"capability export missing agent descriptor: {exported_name}")

        for agent in agents:
            checked_paths.append(str(agent.manifest_path))
            if not agent.prompt_path.exists():
                errors.append(f"agent {agent.name} prompt file missing: {agent.prompt_path}")
            for ref in agent.reference_paths:
                if not ref.exists():
                    errors.append(f"agent {agent.name} reference missing: {ref}")
            if agent.result_schema_path and not Path(agent.result_schema_path).exists():
                errors.append(f"agent {agent.name} result schema missing: {agent.result_schema_path}")

        ok = not errors
        if ok:
            record = self.registry.state_store.get_record(name)
            if record is not None:
                record.last_validated_at = datetime.now().isoformat()
                self.registry.state_store.upsert(name, record)
        return CapabilityValidationResult(name=name, ok=ok, errors=errors, warnings=warnings, checked_paths=checked_paths)

    def validate_all(self) -> list[CapabilityValidationResult]:
        return [self.validate_capability(item.name) for item in self.registry.list_capabilities()]

    def _resolve_tool_entrypoint(self, tool) -> None:
        try:
            import_string(tool.entrypoint)
            return
        except Exception:
            impl_file = tool.root / "implementation.py"
            _, _, attr = str(tool.entrypoint or "").partition(":")
            if not impl_file.exists() or not attr:
                raise
            import_from_file(impl_file, attr)
