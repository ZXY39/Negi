"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

from g3ku.capabilities.registry import CapabilityRegistry

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """Loader for agent skills with capability-pack support and legacy fallback."""

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.registry = CapabilityRegistry(workspace)

    def _refresh_registry(self) -> None:
        self.registry.refresh()

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        self._refresh_registry()
        skills = []
        for descriptor in self.registry.list_skills():
            if filter_unavailable and not descriptor.available:
                continue
            skills.append(
                {
                    "name": descriptor.name,
                    "path": str(descriptor.main_path),
                    "source": descriptor.capability_name,
                }
            )
        return skills

    def load_skill(self, name: str) -> str | None:
        self._refresh_registry()
        descriptor = self.registry.resolve_skill(name)
        if descriptor and descriptor.main_path.exists():
            return descriptor.main_path.read_text(encoding="utf-8")
        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")
        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        self._refresh_registry()
        all_skills = self.registry.list_skills()
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
                    if self.registry.resolve_tool(tool_name) is None:
                        missing.append(f"tool:{tool_name}")
                for bin_name in descriptor.requires_bins:
                    if not shutil.which(bin_name):
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
        self._refresh_registry()
        capabilities = self.registry.list_capabilities()
        if not capabilities:
            return ""

        def escape_xml(value: str) -> str:
            return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<capabilities>"]
        for descriptor in capabilities:
            lines.append(
                "  <capability "
                f"enabled=\"{str(descriptor.enabled).lower()}\" "
                f"available=\"{str(descriptor.available).lower()}\" "
                f"source=\"{escape_xml(descriptor.source.type)}\">"
            )
            lines.append(f"    <name>{escape_xml(descriptor.name)}</name>")
            tool_states = []
            for tool_name in descriptor.exported_tools:
                tool = self.registry.resolve_tool(tool_name)
                if tool is None:
                    tool_states.append(f"{tool_name}[missing]")
                    continue
                tool_states.append(
                    f"{tool.name}[enabled={str(tool.enabled).lower()},available={str(tool.available).lower()}]"
                )
            skill_states = []
            for skill_name in descriptor.exported_skills:
                skill = self.registry.resolve_skill(skill_name)
                if skill is None:
                    skill_states.append(f"{skill_name}[missing]")
                    continue
                skill_states.append(
                    f"{skill.name}[enabled={str(skill.enabled).lower()},available={str(skill.available).lower()}]"
                )
            lines.append(
                f"    <tools>{escape_xml(', '.join(tool_states) or '-')}</tools>"
            )
            lines.append(
                f"    <skills>{escape_xml(', '.join(skill_states) or '-')}</skills>"
            )
            issues = list(dict.fromkeys([*descriptor.errors, *descriptor.warnings]))
            if issues:
                lines.append(f"    <issues>{escape_xml('; '.join(issues))}</issues>")
            lines.append("  </capability>")
        lines.append("</capabilities>")
        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        for tool_name in requires.get("tools", []):
            if self.registry.resolve_tool(tool_name) is None:
                missing.append(f"tool: {tool_name}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return str(meta["description"])
        descriptor = self.registry.resolve_skill(name)
        if descriptor and descriptor.description:
            return descriptor.description
        return name

    def _strip_frontmatter(self, content: str) -> str:
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_g3ku_metadata(self, raw: str) -> dict:
        try:
            data = json.loads(raw)
            return data.get("g3ku", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        for tool_name in requires.get("tools", []):
            descriptor = self.registry.resolve_tool(tool_name)
            if descriptor is None or not descriptor.available:
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        meta = self.get_skill_metadata(name) or {}
        if "metadata" in meta:
            return self._parse_g3ku_metadata(meta.get("metadata", ""))
        requires = {
            "tools": meta.get("requires_tools", []),
            "bins": meta.get("requires_bins", []),
            "env": meta.get("requires_env", []),
        }
        return {"requires": requires, "always": meta.get("always", False)}

    def get_always_skills(self) -> list[str]:
        self._refresh_registry()
        result = []
        for descriptor in self.registry.list_skills():
            if descriptor.always and descriptor.available:
                result.append(descriptor.name)
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        self._refresh_registry()
        descriptor = self.registry.resolve_skill(name)
        if descriptor is None:
            return None
        if not descriptor.legacy:
            metadata = dict(descriptor.metadata)
            metadata.setdefault("name", descriptor.name)
            metadata.setdefault("description", descriptor.description)
            metadata.setdefault("always", descriptor.always)
            metadata.setdefault("requires_tools", descriptor.requires_tools)
            metadata.setdefault("requires_bins", descriptor.requires_bins)
            metadata.setdefault("requires_env", descriptor.requires_env)
            return metadata

        content = descriptor.main_path.read_text(encoding="utf-8")
        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                metadata.setdefault("description", descriptor.description)
                metadata.setdefault("always", descriptor.always)
                metadata.setdefault("requires_bins", descriptor.requires_bins)
                metadata.setdefault("requires_env", descriptor.requires_env)
                return metadata
        return {"description": descriptor.description, "always": descriptor.always}

