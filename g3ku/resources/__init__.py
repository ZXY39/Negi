from __future__ import annotations

__all__ = [
    "ManifestBackedTool",
    "ResourceBusyState",
    "ResourceKind",
    "ResourceManager",
    "ResourceSnapshot",
    "SkillResourceDescriptor",
    "ToolResourceDescriptor",
    "get_shared_resource_manager",
]


def __getattr__(name: str):
    if name == "ManifestBackedTool":
        from g3ku.resources.loader import ManifestBackedTool

        return ManifestBackedTool
    if name in {"ResourceManager", "get_shared_resource_manager"}:
        from g3ku.resources.manager import ResourceManager, get_shared_resource_manager

        return ResourceManager if name == "ResourceManager" else get_shared_resource_manager
    if name in {"ResourceBusyState", "ResourceKind", "ResourceSnapshot", "SkillResourceDescriptor", "ToolResourceDescriptor"}:
        from g3ku.resources.models import (
            ResourceBusyState,
            ResourceKind,
            ResourceSnapshot,
            SkillResourceDescriptor,
            ToolResourceDescriptor,
        )

        mapping = {
            "ResourceBusyState": ResourceBusyState,
            "ResourceKind": ResourceKind,
            "ResourceSnapshot": ResourceSnapshot,
            "SkillResourceDescriptor": SkillResourceDescriptor,
            "ToolResourceDescriptor": ToolResourceDescriptor,
        }
        return mapping[name]
    raise AttributeError(f"module 'g3ku.resources' has no attribute {name!r}")
