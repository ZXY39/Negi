from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from g3ku.capabilities.models import (
    AgentRoleDescriptor,
    CapabilityDescriptor,
    CapabilitySource,
    SkillDescriptor,
    ToolDescriptor,
)
from g3ku.capabilities.state_store import CapabilityStateStore
from g3ku.capabilities.utils import check_compat, check_requires

BUILTIN_CAPABILITIES_DIR = Path(__file__).parent / "builtin"
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class CapabilityRegistry:
    """Discover and index capability packs, tools, skills, and agent roles."""

    def __init__(
        self,
        workspace: Path,
        *,
        builtin_dir: Path | None = None,
        workspace_dir: Path | None = None,
        state_path: Path | None = None,
        admin_enabled: bool = False,
    ):
        self.workspace = Path(workspace)
        self.builtin_dir = builtin_dir or BUILTIN_CAPABILITIES_DIR
        self.workspace_dir = workspace_dir or (self.workspace / "capabilities")
        self.state_store = CapabilityStateStore(state_path or (self.workspace / ".g3ku" / "capabilities.lock.json"))
        self.admin_enabled = bool(admin_enabled)
        self._capabilities: list[CapabilityDescriptor] = []
        self._tools: list[ToolDescriptor] = []
        self._skills: list[SkillDescriptor] = []
        self._agents: list[AgentRoleDescriptor] = []
        self.refresh()

    def refresh(self) -> None:
        capabilities: list[CapabilityDescriptor] = []
        tools: list[ToolDescriptor] = []
        skills: list[SkillDescriptor] = []
        agents: list[AgentRoleDescriptor] = []

        for root, source_type in ((self.builtin_dir, "builtin"), (self.workspace_dir, "workspace")):
            if not root.exists():
                continue
            for capability_dir in sorted(path for path in root.iterdir() if path.is_dir()):
                capability = self._read_capability(capability_dir, source_type)
                if capability is None:
                    continue
                capabilities.append(capability)
                tools.extend(self._read_tools(capability))
                skills.extend(self._read_skills(capability))

        skills.extend(self._read_legacy_skills(skills))

        self._capabilities = sorted(capabilities, key=lambda item: item.name)
        self._tools = sorted(tools, key=lambda item: (item.capability_name, item.name))
        tool_map = {item.name: item for item in self._tools}

        for skill in skills:
            if skill.requires_tools:
                missing = [
                    name
                    for name in skill.requires_tools
                    if name not in tool_map or not tool_map[name].enabled or not tool_map[name].available
                ]
                if missing:
                    skill.available = False
                    skill.warnings.append("missing required tools: " + ", ".join(missing))
        self._skills = sorted(skills, key=lambda item: (item.capability_name, item.name))

        skill_map = {item.name: item for item in self._skills}
        for agent in agents:
            missing_tools = [
                name
                for name in agent.tools_allow
                if name not in {'*', '__all__'} and (name not in tool_map or not tool_map[name].enabled or not tool_map[name].available)
            ]
            if missing_tools:
                agent.available = False
                agent.warnings.append("missing allowed tools: " + ", ".join(missing_tools))
            missing_skills = [name for name in agent.skills_allow if name not in skill_map]
            if missing_skills:
                agent.warnings.append("missing allowed skills: " + ", ".join(missing_skills))
        self._agents = sorted(agents, key=lambda item: (item.capability_name, item.name))

    def list_capabilities(self) -> list[CapabilityDescriptor]:
        return list(self._capabilities)

    def get_capability(self, name: str) -> CapabilityDescriptor | None:
        return next((item for item in self._capabilities if item.name == name), None)

    def list_tools(self) -> list[ToolDescriptor]:
        return list(self._tools)

    def list_skills(self) -> list[SkillDescriptor]:
        return list(self._skills)

    def list_agents(self) -> list[AgentRoleDescriptor]:
        return []

    def resolve_tool(self, name: str) -> ToolDescriptor | None:
        return next((item for item in self._tools if item.name == name), None)

    def resolve_skill(self, name: str) -> SkillDescriptor | None:
        return next((item for item in self._skills if item.name == name), None)

    def resolve_agent(self, name: str) -> AgentRoleDescriptor | None:
        return None

    def enabled_tools(self) -> list[ToolDescriptor]:
        return [item for item in self._tools if item.enabled and item.available and (not item.admin_only or self.admin_enabled)]

    def enabled_skills(self) -> list[SkillDescriptor]:
        return [item for item in self._skills if item.enabled and item.available]

    def enabled_agents(self) -> list[AgentRoleDescriptor]:
        return []

    def build_tools_summary(self) -> str:
        return "\n".join(f"- `{tool.name}` ({tool.label}): {tool.description}" for tool in self.enabled_tools())

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Manifest must be a mapping: {path}")
        return data

    def _read_capability(self, capability_dir: Path, source_type: str) -> CapabilityDescriptor | None:
        manifest_path = capability_dir / "capability.yaml"
        if not manifest_path.exists():
            return None
        data = self._read_yaml(manifest_path)
        name = str(data.get("name") or capability_dir.name).strip()
        record = self.state_store.get_record(name)
        compat = dict(data.get("compat") or {})
        requires = dict(data.get("requires") or {})
        compat_errors, compat_warnings = check_compat(compat)
        require_errors, require_warnings = check_requires(requires)
        source = dict(data.get("source") or {})
        exports = dict(data.get("exports") or {})
        defaults = dict(data.get("defaults") or {})
        enabled = bool(defaults.get("enabled", True))
        if record is not None:
            enabled = bool(record.enabled)
        descriptor = CapabilityDescriptor(
            name=name,
            display_name=str(data.get("display_name") or name),
            version=str(data.get("version") or "0.0.0"),
            root=capability_dir,
            manifest_path=manifest_path,
            kind=[str(item) for item in (data.get("kind") or [])],
            compat=compat,
            requires={
                "bins": [str(item) for item in (requires.get("bins") or [])],
                "env": [str(item) for item in (requires.get("env") or [])],
                "python": [str(item) for item in (requires.get("python") or [])],
            },
            exported_tools=[str(item) for item in (exports.get("tools") or [])],
            exported_skills=[str(item) for item in (exports.get("skills") or [])],
            exported_agents=[],
            source=CapabilitySource(
                type=str(source.get("type") or source_type),
                uri=source.get("uri"),
                ref=source.get("ref"),
            ),
            enabled=enabled,
            available=not compat_errors and not require_errors,
            defaults=defaults,
            healthcheck=dict(data.get("healthcheck") or {}),
            metadata=data,
            errors=[*compat_errors, *require_errors],
            warnings=[*compat_warnings, *require_warnings],
        )
        return descriptor

    def _read_tools(self, capability: CapabilityDescriptor) -> list[ToolDescriptor]:
        tools_dir = capability.root / "tools"
        if not tools_dir.exists():
            return []
        items: list[ToolDescriptor] = []
        for manifest_path in sorted(tools_dir.glob("*/tool.yaml")):
            data = self._read_yaml(manifest_path)
            descriptor = ToolDescriptor(
                name=str(data.get("name") or manifest_path.parent.name),
                label=str(data.get("label") or data.get("name") or manifest_path.parent.name),
                description=str(data.get("description") or "").strip(),
                entrypoint=str(data.get("entrypoint") or "").strip(),
                capability_name=capability.name,
                capability_version=capability.version,
                manifest_path=manifest_path,
                root=manifest_path.parent,
                config_schema_path=self._relative_if_exists(manifest_path.parent, data.get("config_schema")),
                result_schema_path=self._relative_if_exists(manifest_path.parent, data.get("result_schema")),
                permissions=dict(data.get("permissions") or {}),
                streaming=bool(data.get("streaming", False)),
                admin_only=bool(data.get("admin_only", False)),
                enabled=capability.enabled,
                available=capability.available,
                metadata=data,
                errors=list(capability.errors),
                warnings=list(capability.warnings),
            )
            items.append(descriptor)
        return items

    def _read_skills(self, capability: CapabilityDescriptor) -> list[SkillDescriptor]:
        skills_dir = capability.root / "skills"
        if not skills_dir.exists():
            return []
        items: list[SkillDescriptor] = []
        for manifest_path in sorted(skills_dir.glob("*/skill.yaml")):
            data = self._read_yaml(manifest_path)
            trigger = dict(data.get("trigger") or {})
            requires = dict(data.get("requires") or {})
            content = dict(data.get("content") or {})
            main_path = self._relative_if_exists(manifest_path.parent, content.get("main") or "SKILL.md") or (manifest_path.parent / "SKILL.md")
            reference_paths = [
                path
                for ref in (content.get("references") or [])
                for path in [self._relative_if_exists(manifest_path.parent, ref)]
                if path is not None
            ]
            requires_bins = [str(item) for item in (requires.get("bins") or [])]
            requires_env = [str(item) for item in (requires.get("env") or [])]
            items.append(
                SkillDescriptor(
                    name=str(data.get("name") or manifest_path.parent.name),
                    description=str(data.get("description") or "").strip(),
                    capability_name=capability.name,
                    capability_version=capability.version,
                    manifest_path=manifest_path,
                    root=manifest_path.parent,
                    main_path=main_path,
                    reference_paths=reference_paths,
                    trigger_keywords=[str(item) for item in (trigger.get("keywords") or [])],
                    always=bool(trigger.get("always", data.get("always", False))),
                    requires_tools=[str(item) for item in (requires.get("tools") or [])],
                    requires_bins=requires_bins,
                    requires_env=requires_env,
                    enabled=capability.enabled,
                    available=capability.available and self._requirements_available(requires_bins, requires_env),
                    metadata=data,
                    errors=list(capability.errors),
                    warnings=list(capability.warnings),
                )
            )
        return items

    def _read_agents(self, capability: CapabilityDescriptor) -> list[AgentRoleDescriptor]:
        agents_dir = capability.root / "agents"
        if not agents_dir.exists():
            return []
        items: list[AgentRoleDescriptor] = []
        for manifest_path in sorted(agents_dir.glob("*/agent.yaml")):
            data = self._read_yaml(manifest_path)
            tools = dict(data.get("tools") or {})
            skills = dict(data.get("skills") or {})
            delegates = dict(data.get("delegates") or {})
            context_policy = dict(data.get("context_policy") or {})
            output = dict(data.get("output") or {})
            execution = dict(data.get("execution") or {})
            prompt_rel = data.get("system_prompt") or data.get("prompt") or "PROMPT.md"
            prompt_path = self._relative_if_exists(manifest_path.parent, prompt_rel) or (manifest_path.parent / str(prompt_rel))
            reference_paths = [
                path
                for ref in (data.get("references") or [])
                for path in [self._relative_if_exists(manifest_path.parent, ref)]
                if path is not None
            ]
            result_schema_path = self._relative_if_exists(manifest_path.parent, output.get("result_schema"))
            available = capability.available and prompt_path.exists()
            errors = list(capability.errors)
            warnings = list(capability.warnings)
            if not prompt_path.exists():
                available = False
                errors.append(f"missing agent prompt: {prompt_path}")
            items.append(
                AgentRoleDescriptor(
                    name=str(data.get("name") or manifest_path.parent.name),
                    label=str(data.get("label") or data.get("name") or manifest_path.parent.name),
                    description=str(data.get("description") or "").strip(),
                    role_type=str(data.get("role_type") or "worker").strip() or "worker",
                    capability_name=capability.name,
                    capability_version=capability.version,
                    manifest_path=manifest_path,
                    root=manifest_path.parent,
                    prompt_path=prompt_path,
                    pattern_affinity=[str(item) for item in (data.get("pattern_affinity") or [])],
                    delegate_roles=[str(item) for item in (delegates.get("allow") or [])],
                    reference_paths=reference_paths,
                    tools_allow=[str(item) for item in (tools.get("allow") or [])],
                    skills_allow=[str(item) for item in (skills.get("allow") or [])],
                    context_policy=context_policy,
                    output_policy=output,
                    execution=execution,
                    result_schema_path=result_schema_path,
                    enabled=capability.enabled,
                    available=available,
                    metadata=data,
                    errors=errors,
                    warnings=warnings,
                )
            )
        return items

    def _read_legacy_skills(self, current_skills: list[SkillDescriptor]) -> list[SkillDescriptor]:
        known = {item.name for item in current_skills}
        items: list[SkillDescriptor] = []
        for root, capability_name in ((self.workspace / "skills", "workspace-legacy-skills"), (BUILTIN_SKILLS_DIR, "builtin-legacy-skills")):
            if not root.exists():
                continue
            for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
                if skill_dir.name in known:
                    continue
                main_path = skill_dir / "SKILL.md"
                if not main_path.exists():
                    continue
                description, always, requires_bins, requires_env = self._parse_legacy_skill(main_path)
                items.append(
                    SkillDescriptor(
                        name=skill_dir.name,
                        description=description,
                        capability_name=capability_name,
                        capability_version="legacy",
                        manifest_path=main_path,
                        root=skill_dir,
                        main_path=main_path,
                        reference_paths=[],
                        always=always,
                        requires_bins=requires_bins,
                        requires_env=requires_env,
                        enabled=True,
                        available=self._requirements_available(requires_bins, requires_env),
                        legacy=True,
                    )
                )
        return items

    def _parse_legacy_skill(self, path: Path) -> tuple[str, bool, list[str], list[str]]:
        description = path.parent.name
        always = False
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].splitlines():
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip().strip("\"'")
                    if key == "description" and value:
                        description = value
                    elif key == "always":
                        always = value.lower() in {"1", "true", "yes", "on"}
        return description, always, [], []

    def _relative_if_exists(self, root: Path, rel: Any) -> Path | None:
        raw = str(rel or "").strip()
        if not raw:
            return None
        path = (root / raw).resolve()
        return path if path.exists() else None

    def _requirements_available(self, bins: list[str], env: list[str]) -> bool:
        for bin_name in bins:
            if not shutil.which(str(bin_name)):
                return False
        for env_name in env:
            if not os.environ.get(str(env_name)):
                return False
        return True
