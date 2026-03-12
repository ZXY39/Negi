from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from g3ku.resources.manifest import ResourceManifestError, load_manifest
from g3ku.resources.models import ResourceKind, SkillResourceDescriptor, ToolResourceDescriptor


@dataclass(slots=True)
class DiscoveryResult:
    tools: dict[str, ToolResourceDescriptor]
    skills: dict[str, SkillResourceDescriptor]


class ResourceRegistry:
    def __init__(self, workspace: Path, *, skills_dir: Path, tools_dir: Path, manifest_name: str = "resource.yaml"):
        self.workspace = Path(workspace)
        self.skills_dir = Path(skills_dir)
        self.tools_dir = Path(tools_dir)
        self.manifest_name = manifest_name

    def discover(self) -> DiscoveryResult:
        tools = self._discover_tools()
        skills = self._discover_skills(tool_names=set(tools.keys()))
        return DiscoveryResult(tools=tools, skills=skills)

    def _discover_skills(self, *, tool_names: set[str]) -> dict[str, SkillResourceDescriptor]:
        items: dict[str, SkillResourceDescriptor] = {}
        if not self.skills_dir.exists():
            return items
        for skill_dir in sorted(path for path in self.skills_dir.iterdir() if path.is_dir()):
            manifest_path = skill_dir / self.manifest_name
            if not manifest_path.exists():
                continue
            descriptor = self._build_skill(skill_dir, manifest_path)
            if descriptor.name in items:
                descriptor.available = False
                descriptor.errors.append(f"duplicate skill name: {descriptor.name}")
                continue
            missing_tools = [name for name in descriptor.requires_tools if name not in tool_names]
            if missing_tools:
                descriptor.available = False
                descriptor.warnings.append("missing required tools: " + ", ".join(missing_tools))
            if any(shutil.which(name) is None for name in descriptor.requires_bins):
                descriptor.available = False
                descriptor.warnings.append("missing required bins")
            if any(not os.environ.get(name) for name in descriptor.requires_env):
                descriptor.available = False
                descriptor.warnings.append("missing required env")
            items[descriptor.name] = descriptor
        return items

    def _discover_tools(self) -> dict[str, ToolResourceDescriptor]:
        items: dict[str, ToolResourceDescriptor] = {}
        if not self.tools_dir.exists():
            return items
        for tool_dir in sorted(path for path in self.tools_dir.iterdir() if path.is_dir()):
            manifest_path = tool_dir / self.manifest_name
            if not manifest_path.exists():
                continue
            descriptor = self._build_tool(tool_dir, manifest_path)
            if descriptor.name in items:
                descriptor.available = False
                descriptor.errors.append(f"duplicate tool name: {descriptor.name}")
                continue
            if any(shutil.which(name) is None for name in descriptor.requires_bins):
                descriptor.available = False
                descriptor.warnings.append("missing required bins")
            if any(not os.environ.get(name) for name in descriptor.requires_env):
                descriptor.available = False
                descriptor.warnings.append("missing required env")
            items[descriptor.name] = descriptor
        return items

    def _build_skill(self, root: Path, manifest_path: Path) -> SkillResourceDescriptor:
        data = self._safe_manifest(manifest_path)
        main_path = root / "SKILL.md"
        descriptor = SkillResourceDescriptor(
            kind=ResourceKind.SKILL,
            name=str(data.get("name") or root.name),
            description=str(data.get("description") or "").strip(),
            root=root,
            manifest_path=manifest_path,
            fingerprint=self._tree_fingerprint(root),
            main_path=main_path if main_path.exists() else None,
            references_root=(root / "references") if (root / "references").exists() else None,
            scripts_root=(root / "scripts") if (root / "scripts").exists() else None,
            assets_root=(root / "assets") if (root / "assets").exists() else None,
            trigger_keywords=[str(item) for item in ((data.get("trigger") or {}).get("keywords") or [])],
            always=bool((data.get("trigger") or {}).get("always", False)),
            requires_tools=[str(item) for item in ((data.get("requires") or {}).get("tools") or [])],
            requires_bins=[str(item) for item in ((data.get("requires") or {}).get("bins") or [])],
            requires_env=[str(item) for item in ((data.get("requires") or {}).get("env") or [])],
            metadata=data,
            exposure={
                "agent": bool((data.get("exposure") or {}).get("agent", True)),
                "org_graph": bool((data.get("exposure") or {}).get("org_graph", True)),
            },
        )
        manifest_error = str(data.get("_manifest_error") or "").strip()
        if manifest_error:
            descriptor.available = False
            descriptor.errors.append(manifest_error)
        if descriptor.main_path is None:
            descriptor.available = False
            descriptor.errors.append(f"missing SKILL.md: {root}")
        return descriptor

    def _build_tool(self, root: Path, manifest_path: Path) -> ToolResourceDescriptor:
        data = self._safe_manifest(manifest_path)
        main_root = root / "main"
        entrypoint = main_root / "tool.py"
        toolskills_root = root / "toolskills"
        toolskills_main = toolskills_root / "SKILL.md"
        descriptor = ToolResourceDescriptor(
            kind=ResourceKind.TOOL,
            name=str(data.get("name") or root.name),
            description=str(data.get("description") or "").strip(),
            root=root,
            manifest_path=manifest_path,
            fingerprint=self._tree_fingerprint(root),
            main_root=main_root if main_root.exists() else None,
            entrypoint_path=entrypoint if entrypoint.exists() else None,
            entrypoint_hash=self._file_hash(entrypoint) if entrypoint.exists() else "",
            toolskills_root=toolskills_root if toolskills_root.exists() else None,
            toolskills_main_path=toolskills_main if toolskills_main.exists() else None,
            toolskills_references_root=(toolskills_root / "references") if (toolskills_root / "references").exists() else None,
            toolskills_scripts_root=(toolskills_root / "scripts") if (toolskills_root / "scripts").exists() else None,
            toolskills_assets_root=(toolskills_root / "assets") if (toolskills_root / "assets").exists() else None,
            parameters=dict(data.get("parameters") or {"type": "object", "properties": {}, "required": []}),
            permissions=dict(data.get("permissions") or {}),
            requires_tools=[str(item) for item in ((data.get("requires") or {}).get("tools") or [])],
            requires_bins=[str(item) for item in ((data.get("requires") or {}).get("bins") or [])],
            requires_env=[str(item) for item in ((data.get("requires") or {}).get("env") or [])],
            config_namespace=str(data.get("config_namespace") or "").strip(),
            toolskill_enabled=bool((data.get("toolskill") or {}).get("enabled", True)),
            metadata=data,
            exposure={
                "agent": bool((data.get("exposure") or {}).get("agent", True)),
                "org_graph": bool((data.get("exposure") or {}).get("org_graph", True)),
            },
        )
        manifest_error = str(data.get("_manifest_error") or "").strip()
        if manifest_error:
            descriptor.available = False
            descriptor.errors.append(manifest_error)
        allowed_children = {self.manifest_name, "toolskills", "main"}
        extra_children = [path.name for path in root.iterdir() if path.name not in allowed_children]
        if extra_children:
            descriptor.warnings.append("unexpected root entries: " + ", ".join(sorted(extra_children)))
        if descriptor.entrypoint_path is None:
            descriptor.available = False
            descriptor.errors.append(f"missing main/tool.py: {root}")
        if descriptor.toolskill_enabled and descriptor.toolskills_main_path is None:
            descriptor.warnings.append(f"missing toolskills/SKILL.md: {root}")
        return descriptor

    @staticmethod
    def _safe_manifest(path: Path) -> dict[str, Any]:
        try:
            return load_manifest(path)
        except ResourceManifestError as exc:
            return {
                "schema_version": 1,
                "kind": "tool" if path.parent.parent.name == "tools" else "skill",
                "name": path.parent.name,
                "description": "",
                "_manifest_error": str(exc),
            }

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            digest.update(path.read_bytes())
        except Exception:
            digest.update(str(path).encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _tree_fingerprint(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            stat = path.stat()
            digest.update(rel.encode("utf-8"))
            digest.update(str(stat.st_mtime_ns).encode("utf-8"))
            digest.update(str(stat.st_size).encode("utf-8"))
        return digest.hexdigest()
