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
    _FINGERPRINT_IGNORED_DIRS = frozenset({
        "__pycache__",
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".pytest_cache",
        ".ruff_cache",
    })
    _TOOL_RESULT_DELIVERY_CONTRACTS = frozenset({
        "runtime_managed",
        "direct_load_inline",
        "inline_full",
        "preview_with_ref",
    })

    def __init__(self, workspace: Path, *, skills_dir: Path, tools_dir: Path, manifest_name: str = "resource.yaml"):
        self.workspace = Path(workspace)
        self.skills_dir = Path(skills_dir)
        self.tools_dir = Path(tools_dir)
        self.manifest_name = manifest_name

    def discover(self) -> DiscoveryResult:
        tools = self._discover_tools()
        skills = self._discover_skills(tool_names=set(tools.keys()))
        return DiscoveryResult(tools=tools, skills=skills)

    def skill_root_for_path(self, path: Path | str) -> Path | None:
        return self._resource_root_for_path(path, self.skills_dir)

    def tool_root_for_path(self, path: Path | str) -> Path | None:
        return self._resource_root_for_path(path, self.tools_dir)

    def build_skill_descriptor(self, root: Path | str, *, tool_names: set[str]) -> SkillResourceDescriptor | None:
        skill_root = Path(root)
        manifest_path = skill_root / self.manifest_name
        if not skill_root.is_dir() or not manifest_path.exists():
            return None
        descriptor = self._build_skill(skill_root, manifest_path)
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
        return descriptor

    def build_tool_descriptor(self, root: Path | str) -> ToolResourceDescriptor | None:
        tool_root = Path(root)
        manifest_path = tool_root / self.manifest_name
        if not tool_root.is_dir() or not manifest_path.exists():
            return None
        descriptor = self._build_tool(tool_root, manifest_path)
        self._apply_tool_runtime_requirements(descriptor)
        return descriptor

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
            self._apply_tool_runtime_requirements(descriptor)
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
                "main_runtime": bool((data.get("exposure") or {}).get("main_runtime", True)),
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
        tool_result_inline_full = bool(data.get("tool_result_inline_full", False))
        tool_result_delivery_contract = str(data.get("tool_result_delivery_contract") or "").strip().lower()
        tool_result_output_ref_paths = [
            str(item or "").strip()
            for item in list(data.get("tool_result_output_ref_paths") or [])
            if str(item or "").strip()
        ]
        metadata = dict(data)
        metadata["tool_result_inline_full"] = tool_result_inline_full
        metadata["tool_result_delivery_contract"] = tool_result_delivery_contract
        metadata["tool_result_output_ref_paths"] = list(tool_result_output_ref_paths)
        main_root = root / "main"
        entrypoint = main_root / "tool.py"
        toolskills_root = root / "toolskills"
        toolskills_main = toolskills_root / "SKILL.md"
        tool_type = str(data.get("tool_type") or "internal").strip().lower() or "internal"
        install_dir = self._resolve_install_dir(data.get("install_dir"))
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
            protocol=str(data.get("protocol") or "mcp").strip().lower() or "mcp",
            mcp_transport=str(((data.get("mcp") or {}).get("transport") or "embedded")).strip().lower() or "embedded",
            tool_type=tool_type,
            install_dir=install_dir,
            callable=tool_type == "internal",
            toolskills_root=toolskills_root if toolskills_root.exists() else None,
            toolskills_main_path=toolskills_main if toolskills_main.exists() else None,
            toolskills_references_root=(toolskills_root / "references") if (toolskills_root / "references").exists() else None,
            toolskills_scripts_root=(toolskills_root / "scripts") if (toolskills_root / "scripts").exists() else None,
            toolskills_assets_root=(toolskills_root / "assets") if (toolskills_root / "assets").exists() else None,
            parameters=dict(data.get("parameters") or {"type": "object", "properties": {}, "required": []}),
            permissions=dict(data.get("permissions") or {}),
            requires_tools=[str(item) for item in ((data.get("requires") or {}).get("tools") or [])],
            requires_bins=[str(item) for item in ((data.get("requires") or {}).get("bins") or [])],
            requires_paths=[str(item) for item in ((data.get("requires") or {}).get("paths") or [])],
            requires_env=[str(item) for item in ((data.get("requires") or {}).get("env") or [])],
            toolskill_enabled=bool((data.get("toolskill") or {}).get("enabled", True)),
            tool_result_inline_full=tool_result_inline_full,
            tool_result_delivery_contract=tool_result_delivery_contract,
            tool_result_output_ref_paths=tool_result_output_ref_paths,
            metadata=metadata,
            exposure={
                "agent": bool((data.get("exposure") or {}).get("agent", True)),
                "main_runtime": bool((data.get("exposure") or {}).get("main_runtime", True)),
            },
        )
        manifest_error = str(data.get("_manifest_error") or "").strip()
        if manifest_error:
            descriptor.available = False
            descriptor.errors.append(manifest_error)
        if descriptor.protocol != "mcp":
            descriptor.available = False
            descriptor.errors.append(
                f"unsupported protocol '{descriptor.protocol}': only 'mcp' is allowed"
            )
        if descriptor.mcp_transport != "embedded":
            descriptor.available = False
            descriptor.errors.append(
                f"unsupported mcp.transport '{descriptor.mcp_transport}': only 'embedded' is allowed"
            )
        if descriptor.tool_type not in {"internal", "external"}:
            descriptor.available = False
            descriptor.errors.append(
                f"unsupported tool_type '{descriptor.tool_type}': only 'internal' or 'external' is allowed"
            )

        source = dict(data.get("source") or {})
        source_vendor_dir = str(source.get("vendor_dir") or "").strip()
        if descriptor.tool_type == "internal":
            if data.get("install_dir") not in (None, ""):
                descriptor.available = False
                descriptor.errors.append("internal tool must not declare install_dir")
        elif descriptor.tool_type == "external":
            if source_vendor_dir:
                descriptor.available = False
                descriptor.errors.append("external tool must not declare source.vendor_dir")
            if descriptor.install_dir is None:
                descriptor.available = False
                descriptor.errors.append("external tool requires install_dir")
            else:
                if self._is_relative_to(descriptor.install_dir, self.tools_dir.resolve()):
                    descriptor.available = False
                    descriptor.errors.append("external tool install_dir must be outside the tools directory")
                elif not descriptor.install_dir.exists():
                    descriptor.warnings.append("external tool install_dir does not exist yet")
            if descriptor.main_root is not None:
                descriptor.available = False
                descriptor.errors.append(f"external tool must not contain main/: {root}")

        allowed_children = {self.manifest_name, "toolskills"}
        if descriptor.tool_type == "internal":
            allowed_children.add("main")
        extra_children = [path.name for path in root.iterdir() if path.name not in allowed_children]
        if extra_children:
            descriptor.warnings.append("unexpected root entries: " + ", ".join(sorted(extra_children)))
        if descriptor.tool_type == "internal" and descriptor.entrypoint_path is None:
            descriptor.available = False
            descriptor.errors.append(f"missing main/tool.py: {root}")
        if descriptor.tool_type == "external" and descriptor.toolskills_main_path is None:
            descriptor.available = False
            descriptor.errors.append(f"external tool missing toolskills/SKILL.md: {root}")
        elif descriptor.toolskill_enabled and descriptor.toolskills_main_path is None:
            descriptor.warnings.append(f"missing toolskills/SKILL.md: {root}")
        self._apply_tool_result_delivery_contract_requirements(descriptor)
        return descriptor

    def _apply_tool_result_delivery_contract_requirements(self, descriptor: ToolResourceDescriptor) -> None:
        if not bool(descriptor.callable):
            return
        contract = str(descriptor.tool_result_delivery_contract or "").strip().lower()
        if descriptor.tool_result_inline_full:
            if contract and contract != "inline_full":
                descriptor.available = False
                descriptor.errors.append(
                    "tool_result_delivery_contract must be 'inline_full' when tool_result_inline_full=true"
                )
            else:
                descriptor.tool_result_delivery_contract = "inline_full"
                descriptor.metadata["tool_result_delivery_contract"] = "inline_full"
            return
        if not contract:
            descriptor.available = False
            descriptor.errors.append(
                "missing tool_result_delivery_contract for callable tool; declare runtime_managed, direct_load_inline, inline_full, or preview_with_ref"
            )
            return
        if contract not in self._TOOL_RESULT_DELIVERY_CONTRACTS:
            descriptor.available = False
            descriptor.errors.append(
                "unsupported tool_result_delivery_contract "
                f"'{contract}': expected one of {', '.join(sorted(self._TOOL_RESULT_DELIVERY_CONTRACTS))}"
            )
            return
        if contract == "inline_full":
            descriptor.available = False
            descriptor.errors.append(
                "tool_result_delivery_contract=inline_full requires tool_result_inline_full=true"
            )
            return
        if contract == "preview_with_ref" and not list(descriptor.tool_result_output_ref_paths or []):
            descriptor.available = False
            descriptor.errors.append(
                "tool_result_delivery_contract=preview_with_ref requires non-empty tool_result_output_ref_paths"
            )

    def _apply_tool_runtime_requirements(self, descriptor: ToolResourceDescriptor) -> None:
        if any(shutil.which(name) is None for name in descriptor.requires_bins):
            descriptor.available = False
            descriptor.warnings.append("missing required bins")
        missing_paths = self._missing_required_paths(descriptor.requires_paths)
        if missing_paths:
            descriptor.available = False
            descriptor.warnings.append("missing required paths: " + ", ".join(missing_paths))
        if any(not os.environ.get(name) for name in descriptor.requires_env):
            descriptor.available = False
            descriptor.warnings.append("missing required env")

    def _missing_required_paths(self, paths: list[str]) -> list[str]:
        missing: list[str] = []
        for raw in list(paths or []):
            text = str(raw or "").strip()
            if not text:
                continue
            resolved = self._resolve_required_path(text)
            if resolved.exists():
                continue
            missing.append(text)
        return missing

    def _resolve_required_path(self, raw_value: Any) -> Path:
        text = str(raw_value or "").strip()
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve(strict=False)

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
        files: list[tuple[str, Path]] = []
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in ResourceRegistry._FINGERPRINT_IGNORED_DIRS
            )
            current_path = Path(current)
            rel_base = current_path.relative_to(root)
            for filename in sorted(filenames):
                path = current_path / filename
                rel = (rel_base / filename).as_posix() if str(rel_base) != "." else filename
                files.append((rel, path))
        for rel, path in files:
            stat = path.stat()
            digest.update(rel.encode("utf-8"))
            digest.update(str(stat.st_mtime_ns).encode("utf-8"))
            digest.update(str(stat.st_size).encode("utf-8"))
        return digest.hexdigest()

    def _resolve_install_dir(self, raw_value: Any) -> Path | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve(strict=False)

    @staticmethod
    def _is_relative_to(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False

    @staticmethod
    def _resource_root_for_path(path: Path | str, base_dir: Path) -> Path | None:
        candidate = Path(path).expanduser().resolve(strict=False)
        base = Path(base_dir).resolve(strict=False)
        try:
            rel = candidate.relative_to(base)
        except ValueError:
            return None
        if not rel.parts:
            return None
        return base / rel.parts[0]
