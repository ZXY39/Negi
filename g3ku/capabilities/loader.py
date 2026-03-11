from __future__ import annotations

from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.capabilities.models import AgentRoleDescriptor, SkillDescriptor, ToolDescriptor
from g3ku.capabilities.registry import CapabilityRegistry
from g3ku.capabilities.utils import import_from_file, import_string


class CapabilityLoader:
    """Instantiate tools and load skill/agent bodies from capability descriptors."""

    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def build_tools(self, *, loop: Any) -> list[Tool]:
        tools: list[Tool] = []
        for descriptor in self.registry.enabled_tools():
            tool = self._build_tool(descriptor, loop=loop)
            if tool is not None:
                tools.append(tool)
        return tools

    def _build_tool(self, descriptor: ToolDescriptor, *, loop: Any) -> Tool | None:
        factory = self._resolve_factory(descriptor)
        try:
            built = factory(loop=loop, descriptor=descriptor)
        except TypeError:
            try:
                built = factory(loop)
            except TypeError:
                built = factory()
        if built is None:
            return None
        if not isinstance(built, Tool):
            raise TypeError(f"Capability factory for {descriptor.name} did not return Tool: {descriptor.entrypoint}")
        return built

    def load_skill_body(self, skill: SkillDescriptor) -> str:
        return skill.main_path.read_text(encoding="utf-8")

    def load_agent_prompt(self, role: AgentRoleDescriptor) -> str:
        return role.prompt_path.read_text(encoding="utf-8")

    def build_agent_role(self, role: AgentRoleDescriptor, *, loop: Any):
        from g3ku.runtime.multi_agent.state import CompiledAgentRole

        return CompiledAgentRole(
            name=role.name,
            label=role.label,
            description=role.description,
            role_type=role.role_type,
            capability_name=role.capability_name,
            capability_version=role.capability_version,
            manifest_path=role.manifest_path,
            root=role.root,
            prompt_path=role.prompt_path,
            prompt=self.load_agent_prompt(role),
            pattern_affinity=list(role.pattern_affinity),
            delegate_roles=list(role.delegate_roles),
            reference_paths=list(role.reference_paths),
            tools_allow=list(role.tools_allow),
            skills_allow=list(role.skills_allow),
            context_policy=dict(role.context_policy),
            output_policy=dict(role.output_policy),
            execution=dict(role.execution),
            result_schema_path=role.result_schema_path,
            descriptor=role,
            runtime_loop=loop,
        )

    def _resolve_factory(self, descriptor: ToolDescriptor):
        try:
            return import_string(descriptor.entrypoint)
        except Exception:
            impl_file = descriptor.root / "implementation.py"
            _, _, attr = str(descriptor.entrypoint or "").partition(":")
            if not impl_file.exists() or not attr:
                raise
            return import_from_file(impl_file, attr)
