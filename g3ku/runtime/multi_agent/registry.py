from __future__ import annotations

from typing import Any

from g3ku.capabilities.loader import CapabilityLoader
from g3ku.capabilities.registry import CapabilityRegistry
from g3ku.runtime.multi_agent.state import CompiledAgentRole


class MultiAgentRoleRegistry:
    """Compile and index agent roles exported by capability packs."""

    def __init__(self, capability_registry: CapabilityRegistry, capability_loader: CapabilityLoader, *, loop: Any):
        self.capability_registry = capability_registry
        self.capability_loader = capability_loader
        self.loop = loop
        self._roles: list[CompiledAgentRole] = []
        self.refresh()

    def refresh(self) -> None:
        roles: list[CompiledAgentRole] = []
        for descriptor in self.capability_registry.enabled_agents():
            roles.append(self.capability_loader.build_agent_role(descriptor, loop=self.loop))
        self._roles = sorted(roles, key=lambda item: (item.capability_name, item.name))

    def list_roles(self) -> list[CompiledAgentRole]:
        return list(self._roles)

    def get_role(self, name: str) -> CompiledAgentRole | None:
        return next((item for item in self._roles if item.name == name), None)

    def roles_for_pattern(self, pattern: str) -> list[CompiledAgentRole]:
        normalized = str(pattern or "").strip()
        return [item for item in self._roles if normalized in set(item.pattern_affinity or [])]

    def get_intent_gate(self) -> CompiledAgentRole | None:
        role = self.get_role("intent-gate")
        if role is not None:
            return role
        return next((item for item in self._roles if item.role_type == "router"), None)

