"""Skill loader backed by the unified root-level resource system."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from g3ku.resources import ResourceManager, get_shared_resource_manager


class SkillsLoader:
    """Load skills from the shared workspace resource manager."""

    def __init__(self, workspace: Path, resource_manager: ResourceManager | None = None, app_config=None):
        self.workspace = Path(workspace)
        self.resource_manager = resource_manager or get_shared_resource_manager(self.workspace, app_config=app_config)

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        skills = []
        for descriptor in self.resource_manager.list_skills():
            if filter_unavailable and not descriptor.available:
                continue
            skills.append(
                {
                    "name": descriptor.name,
                    "path": str(descriptor.main_path),
                    "source": str(descriptor.root),
                }
            )
        return skills

    def load_skill(self, name: str) -> str | None:
        try:
            return self.resource_manager.load_skill_body(name)
        except FileNotFoundError:
            return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                parts.append(f"### Skill: {name}\n\n{content.strip()}")
        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        all_skills = self.resource_manager.list_skills()
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for descriptor in all_skills:
            lines.append(f"  <skill available=\"{str(descriptor.available).lower()}\">")
            lines.append(f"    <name>{escape_xml(descriptor.name)}</name>")
            lines.append(f"    <description>{escape_xml(descriptor.description or descriptor.name)}</description>")
            lines.append(f"    <location>{descriptor.main_path}</location>")
            if not descriptor.available:
                missing = []
                for tool_name in descriptor.requires_tools:
                    if self.resource_manager.get_tool_descriptor(tool_name) is None:
                        missing.append(f"tool:{tool_name}")
                for bin_name in descriptor.requires_bins:
                    if shutil.which(bin_name) is None:
                        missing.append(f"CLI:{bin_name}")
                for env_name in descriptor.requires_env:
                    if not os.environ.get(env_name):
                        missing.append(f"ENV:{env_name}")
                if missing:
                    lines.append(f"    <requires>{escape_xml(', '.join(missing))}</requires>")
            lines.append("  </skill>")
        lines.append("</skills>")
        return "\n".join(lines)

    def build_capability_summary(self) -> str:
        return ""

    def get_always_skills(self) -> list[str]:
        return [descriptor.name for descriptor in self.resource_manager.list_skills() if descriptor.always and descriptor.available]

    def get_skill_metadata(self, name: str) -> dict | None:
        descriptor = self.resource_manager.get_skill(name)
        if descriptor is None:
            return None
        metadata = dict(descriptor.metadata)
        metadata.setdefault("name", descriptor.name)
        metadata.setdefault("description", descriptor.description)
        metadata.setdefault("always", descriptor.always)
        metadata.setdefault("requires_tools", descriptor.requires_tools)
        metadata.setdefault("requires_bins", descriptor.requires_bins)
        metadata.setdefault("requires_env", descriptor.requires_env)
        return metadata
