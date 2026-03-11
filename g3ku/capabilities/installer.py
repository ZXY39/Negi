from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.capabilities.models import CapabilityIndexCandidate, CapabilityInstallResult, CapabilityRecord
from g3ku.capabilities.index_registry import CapabilityIndexRegistry
from g3ku.capabilities.registry import CapabilityRegistry
from g3ku.capabilities.source_registry import CapabilitySourceRegistry
from g3ku.capabilities.utils import sha256_path
from g3ku.capabilities.validator import CapabilityValidator


class CapabilityInstaller:
    """Install, update, enable, disable, and remove capability packs."""

    def __init__(self, registry: CapabilityRegistry, source_registry: CapabilitySourceRegistry | None = None, index_registry: CapabilityIndexRegistry | None = None):
        self.registry = registry
        self.workspace = registry.workspace
        self.workspace_dir = registry.workspace_dir
        self.staging_dir = self.workspace / ".g3ku" / "capability-staging"
        self.backups_dir = self.workspace / ".g3ku" / "capability-backups"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.validator = CapabilityValidator(registry)
        self.source_registry = source_registry or CapabilitySourceRegistry()
        self.index_registry = index_registry or CapabilityIndexRegistry(self.workspace)

    def list_sources(self) -> list[dict]:
        return self.source_registry.describe_sources()

    def search(self, query: str | None = None) -> list[CapabilityIndexCandidate]:
        return self.index_registry.list_latest(query)

    def list(self) -> list[dict]:
        items = []
        for capability in self.registry.list_capabilities():
            record = self.registry.state_store.get_record(capability.name)
            items.append(
                {
                    "name": capability.name,
                    "display_name": capability.display_name,
                    "version": capability.version,
                    "enabled": capability.enabled,
                    "available": capability.available,
                    "source": capability.source.type,
                    "uri": capability.source.uri,
                    "installed_path": record.installed_path if record else None,
                }
            )
        return items

    def init_capability(self, name: str, capability_type: str = "hybrid", *, dry_run: bool = False) -> CapabilityInstallResult:
        target = self.workspace_dir / name
        if target.exists():
            return CapabilityInstallResult(ok=False, name=name, action="init", message=f"Capability already exists: {target}", dry_run=dry_run)
        if dry_run:
            return self._plan_result(name=name, action="init", message=f"Dry run: would initialize capability at {target}", path=str(target))
        target.mkdir(parents=True, exist_ok=True)
        tool_exports = [name.replace('-', '_')] if capability_type in {"tool", "hybrid"} else []
        skill_exports = [name] if capability_type in {"skill", "hybrid"} else []

        capability_lines = [
            "schema_version: 1",
            f"name: {name}",
            f"display_name: {name}",
            "version: 0.1.0",
            "kind:",
            *[f"  - {value}" for value in ([capability_type] if capability_type != "hybrid" else ["tool", "skill"])],
            "compat:",
            "  g3ku_core: '>=1.0.0,<2.0.0'",
            "  capability_api: '1.x'",
            "source:",
            "  type: workspace",
            "  uri: null",
            "defaults:",
            "  enabled: true",
            "requires:",
            "  bins: []",
            "  env: []",
            "  python: []",
            "exports:",
            "  tools:" if tool_exports else "  tools: []",
            *[f"    - {value}" for value in tool_exports],
            "  skills:" if skill_exports else "  skills: []",
            *[f"    - {value}" for value in skill_exports],
        ]
        (target / "capability.yaml").write_text("\n".join(capability_lines), encoding="utf-8")
        (target / "README.md").write_text(f"# {name}\n\nWorkspace capability pack.\n", encoding="utf-8")
        if capability_type in {"tool", "hybrid"}:
            tool_name = tool_exports[0]
            tool_dir = target / "tools" / tool_name
            (tool_dir / "schemas").mkdir(parents=True, exist_ok=True)
            (tool_dir / "tool.yaml").write_text(
                "\n".join(
                    [
                        "schema_version: 1",
                        f"name: {tool_name}",
                        f"label: {tool_name}",
                        "description: Workspace tool",
                        f"entrypoint: capabilities.{name.replace('-', '_')}.tools.{tool_name}.implementation:build_tool",
                        "config_schema: schemas/params.schema.json",
                        "result_schema: schemas/result.schema.json",
                        "permissions:",
                        "  network: false",
                        "  filesystem:",
                        "    - workspace",
                        "streaming: false",
                        "admin_only: false",
                    ]
                ),
                encoding="utf-8",
            )
            (tool_dir / "implementation.py").write_text(
                """from g3ku.agent.tools.base import Tool\n\n\nclass WorkspaceTool(Tool):\n    @property\n    def name(self):\n        return '" + tool_name + "'\n\n    @property\n    def description(self):\n        return 'Workspace capability tool'\n\n    @property\n    def parameters(self):\n        return {'type': 'object', 'properties': {}, 'required': []}\n\n    async def execute(self, **kwargs):\n        return 'Not implemented yet'\n\n\ndef build_tool(**kwargs):\n    return WorkspaceTool()\n""",
                encoding="utf-8",
            )
            (tool_dir / "schemas" / "params.schema.json").write_text('{"type":"object","properties":{},"required":[]}', encoding="utf-8")
            (tool_dir / "schemas" / "result.schema.json").write_text('{"type":"object"}', encoding="utf-8")
        if capability_type in {"skill", "hybrid"}:
            skill_dir = target / "skills" / name
            (skill_dir / "references").mkdir(parents=True, exist_ok=True)
            (skill_dir / "skill.yaml").write_text(
                "\n".join(
                    [
                        "schema_version: 1",
                        f"name: {name}",
                        f"description: {name} skill",
                        "trigger:",
                        "  keywords: []",
                        "  always: false",
                        "requires:",
                        "  tools: []",
                        "  bins: []",
                        "  env: []",
                        "content:",
                        "  main: SKILL.md",
                        "  references: []",
                    ]
                ),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\n", encoding="utf-8")
        return CapabilityInstallResult(ok=True, name=name, action="init", message=f"Initialized capability at {target}", path=str(target))

    def _plan_result(
        self,
        *,
        name: str,
        action: str,
        message: str,
        path: str | None = None,
        version: str | None = None,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> CapabilityInstallResult:
        return CapabilityInstallResult(
            ok=not bool(errors),
            name=name,
            action=action,
            message=message,
            path=path,
            version=version,
            dry_run=True,
            errors=list(errors or []),
            warnings=list(warnings or []),
        )

    def enable(self, name: str, *, dry_run: bool = False) -> CapabilityInstallResult:
        capability = self.registry.get_capability(name)
        if capability is None:
            return CapabilityInstallResult(ok=False, name=name, action="enable", message=f"Capability not found: {name}", dry_run=dry_run)
        if dry_run:
            return self._plan_result(
                name=name,
                action="enable",
                message=f"Dry run: would enable capability {name}",
                path=str(capability.root),
                version=capability.version,
            )
        record = self.registry.state_store.get_record(name) or CapabilityRecord(version=capability.version, source={"type": capability.source.type, "uri": capability.source.uri, "ref": capability.source.ref}, installed_path=str(capability.root))
        record.enabled = True
        self.registry.state_store.upsert(name, record)
        self.registry.refresh()
        return CapabilityInstallResult(ok=True, name=name, action="enable", message=f"Enabled capability {name}", path=str(capability.root), version=capability.version)

    def disable(self, name: str, *, dry_run: bool = False) -> CapabilityInstallResult:
        capability = self.registry.get_capability(name)
        if capability is None:
            return CapabilityInstallResult(ok=False, name=name, action="disable", message=f"Capability not found: {name}", dry_run=dry_run)
        if dry_run:
            return self._plan_result(
                name=name,
                action="disable",
                message=f"Dry run: would disable capability {name}",
                path=str(capability.root),
                version=capability.version,
            )
        record = self.registry.state_store.get_record(name) or CapabilityRecord(version=capability.version, source={"type": capability.source.type, "uri": capability.source.uri, "ref": capability.source.ref}, installed_path=str(capability.root))
        record.enabled = False
        self.registry.state_store.upsert(name, record)
        self.registry.refresh()
        return CapabilityInstallResult(ok=True, name=name, action="disable", message=f"Disabled capability {name}", path=str(capability.root), version=capability.version)

    def install_from_registry(
        self,
        name: str,
        *,
        version: str | None = None,
        index_name: str | None = None,
        index_path: str | None = None,
        enable: bool = True,
        dry_run: bool = False,
    ) -> CapabilityInstallResult:
        candidate = self.index_registry.select_version(
            name,
            version=version,
            index_name=index_name,
            index_path=index_path,
        )
        if candidate is None:
            return CapabilityInstallResult(
                ok=False,
                name=name,
                action="install",
                message="No compatible capability version found in configured indexes",
                dry_run=dry_run,
            )
        record_source = {
            "type": "registry",
            "uri": candidate.index_path,
            "index_name": candidate.index_name,
            "package": candidate.name,
            "version": candidate.version,
            "resolved_type": candidate.source.type,
            "resolved_uri": candidate.source.uri,
            "resolved_ref": candidate.source.ref,
        }
        if candidate.source.type == "git":
            result = self.install_from_git(
                candidate.source.uri,
                ref=candidate.source.ref,
                enable=enable,
                dry_run=dry_run,
                record_source=record_source,
            )
        else:
            result = self.install_from_path(
                candidate.source.uri,
                enable=enable,
                dry_run=dry_run,
                record_source=record_source,
            )
        if result.ok:
            result.message = (
                f"Dry run: would install capability {candidate.name}@{candidate.version} from registry"
                if dry_run
                else f"Installed capability {candidate.name}@{candidate.version} from registry"
            )
            result.version = candidate.version
        return result

    def install_from_path(self, source_path: str, *, enable: bool = True, dry_run: bool = False, record_source: dict[str, Any] | None = None) -> CapabilityInstallResult:
        errors, warnings = self.source_registry.validate_request(source_type="local", source_uri=source_path)
        if errors:
            return CapabilityInstallResult(ok=False, name=Path(source_path).name, action="install", message="Capability source rejected by policy", dry_run=dry_run, errors=errors, warnings=warnings)
        source = Path(source_path).expanduser().resolve()
        if not source.exists():
            return CapabilityInstallResult(ok=False, name=source.name, action="install", message=f"Source not found: {source}", dry_run=dry_run)
        capability_root = source if (source / "capability.yaml").exists() else self._find_single_capability(source)
        if capability_root is None:
            return CapabilityInstallResult(ok=False, name=source.name, action="install", message="No capability.yaml found in source", dry_run=dry_run)
        return self._stage_and_activate(capability_root, source_type="local", source_uri=str(source), enable=enable, action="install", dry_run=dry_run, record_source=record_source)

    def install_from_git(self, repo: str, ref: str | None = None, *, enable: bool = True, dry_run: bool = False, record_source: dict[str, Any] | None = None) -> CapabilityInstallResult:
        errors, warnings = self.source_registry.validate_request(source_type="git", source_uri=repo)
        if errors:
            return CapabilityInstallResult(ok=False, name=repo, action="install", message="Capability source rejected by policy", dry_run=dry_run, errors=errors, warnings=warnings)
        with tempfile.TemporaryDirectory(dir=str(self.staging_dir)) as tmp:
            clone_dir = Path(tmp) / "repo"
            cmd = ["git", "clone", "--depth", "1", repo, str(clone_dir)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return CapabilityInstallResult(ok=False, name=repo, action="install", message=result.stderr.strip() or "git clone failed", dry_run=dry_run)
            if ref:
                checkout = subprocess.run(["git", "-C", str(clone_dir), "checkout", ref], capture_output=True, text=True)
                if checkout.returncode != 0:
                    return CapabilityInstallResult(ok=False, name=repo, action="install", message=checkout.stderr.strip() or "git checkout failed", dry_run=dry_run)
            capability_root = clone_dir if (clone_dir / "capability.yaml").exists() else self._find_single_capability(clone_dir)
            if capability_root is None:
                return CapabilityInstallResult(ok=False, name=repo, action="install", message="No capability.yaml found in repository", dry_run=dry_run)
            return self._stage_and_activate(capability_root, source_type="git", source_uri=repo, source_ref=ref, enable=enable, action="install", dry_run=dry_run, record_source=record_source)

    def update(self, name: str, *, dry_run: bool = False) -> CapabilityInstallResult:
        capability = self.registry.get_capability(name)
        record = self.registry.state_store.get_record(name)
        if capability is None or record is None:
            return CapabilityInstallResult(ok=False, name=name, action="update", message=f"Capability not installed: {name}", dry_run=dry_run)
        source_type = str((record.source or {}).get("type") or capability.source.type)
        source_uri = (record.source or {}).get("uri") or capability.source.uri
        source_ref = (record.source or {}).get("ref") or capability.source.ref
        if source_type == "local" and source_uri:
            result = self.install_from_path(source_uri, enable=record.enabled, dry_run=dry_run)
            result.action = "update"
            if result.ok:
                result.message = f"Dry run: would update capability {name}" if dry_run else f"Updated capability {name}"
            return result
        if source_type == "git" and source_uri:
            result = self.install_from_git(source_uri, source_ref, enable=record.enabled, dry_run=dry_run)
            result.action = "update"
            if result.ok:
                result.message = f"Dry run: would update capability {name}" if dry_run else f"Updated capability {name}"
            return result
        if source_type == "registry":
            package = str((record.source or {}).get("package") or name)
            result = self.install_from_registry(
                package,
                index_name=(record.source or {}).get("index_name"),
                index_path=(record.source or {}).get("uri"),
                enable=record.enabled,
                dry_run=dry_run,
            )
            result.action = "update"
            if result.ok:
                result.message = f"Dry run: would update capability {name} from registry" if dry_run else f"Updated capability {name} from registry"
            return result
        validation = self.validator.validate_capability(name)
        health_errors, health_warnings = self._run_healthcheck(capability)
        errors = [*validation.errors, *health_errors]
        warnings = [*validation.warnings, *health_warnings]
        if dry_run:
            return self._plan_result(
                name=name,
                action="update",
                message=f"Dry run: would validate builtin capability {name}",
                path=str(capability.root),
                version=capability.version,
                errors=errors,
                warnings=warnings,
            )
        return CapabilityInstallResult(ok=not errors, name=name, action="update", message="Builtin capability validated" if not errors else "Builtin capability failed validation", errors=errors, warnings=warnings, path=str(capability.root), version=capability.version)

    def remove(self, name: str, *, dry_run: bool = False) -> CapabilityInstallResult:
        capability = self.registry.get_capability(name)
        if capability is None:
            return CapabilityInstallResult(ok=False, name=name, action="remove", message=f"Capability not found: {name}", dry_run=dry_run)
        if capability.source.type == "builtin":
            if dry_run:
                return self._plan_result(
                    name=name,
                    action="remove",
                    message=f"Dry run: would remove override for builtin capability {name}",
                    path=str(capability.root),
                    version=capability.version,
                )
            self.registry.state_store.remove(name)
            self.registry.refresh()
            return CapabilityInstallResult(ok=True, name=name, action="remove", message=f"Removed override for builtin capability {name}; package remains builtin")
        blockers = self._dependency_blockers(capability)
        if blockers:
            return CapabilityInstallResult(
                ok=False,
                name=name,
                action="remove",
                message="Cannot remove capability while other skills depend on it",
                errors=blockers,
                path=str(capability.root),
                version=capability.version,
                dry_run=dry_run,
            )
        target = capability.root
        if dry_run:
            return self._plan_result(
                name=name,
                action="remove",
                message=f"Dry run: would remove capability {name}",
                path=str(target),
                version=capability.version,
            )
        if target.exists():
            shutil.rmtree(target)
        self.registry.state_store.remove(name)
        self.registry.refresh()
        return CapabilityInstallResult(ok=True, name=name, action="remove", message=f"Removed capability {name}", path=str(target), version=capability.version)

    def _dependency_blockers(self, capability: Any) -> list[str]:
        exported_tools = set(getattr(capability, "exported_tools", []) or [])
        if not exported_tools:
            return []
        blockers: list[str] = []
        for skill in self.registry.list_skills():
            if skill.capability_name == capability.name:
                continue
            required = sorted(exported_tools.intersection(skill.requires_tools))
            if required:
                blockers.append(
                    f"skill {skill.name} ({skill.capability_name}) requires tools: {', '.join(required)}"
                )
        return blockers

    def _run_healthcheck(self, capability: Any, *, root: Path | None = None) -> tuple[list[str], list[str]]:
        healthcheck = dict(getattr(capability, "healthcheck", {}) or {})
        smoke_rel = str(healthcheck.get("smoke_test") or "").strip()
        if not smoke_rel:
            return [], []
        base_dir = root or getattr(capability, "root", self.workspace)
        smoke_path = (base_dir / smoke_rel).resolve()
        if not smoke_path.exists():
            return [f"healthcheck smoke test missing: {smoke_path}"], []
        cmd = [sys.executable, str(smoke_path)] if smoke_path.suffix.lower() == ".py" else [str(smoke_path)]
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.workspace) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(base_dir), env=env)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            return [f"healthcheck smoke test failed: {detail}"], []
        return [], []

    def _rollback_activation(self, target: Path, backup: Path | None, previous_record: CapabilityRecord | None, name: str) -> None:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backup and backup.exists():
            shutil.move(str(backup), str(target))
        if previous_record is not None:
            self.registry.state_store.upsert(name, previous_record)
        else:
            self.registry.state_store.remove(name)
        self.registry.refresh()

    def _find_single_capability(self, root: Path) -> Path | None:
        candidates = [path.parent for path in root.rglob("capability.yaml")]
        return candidates[0] if len(candidates) == 1 else None

    def _stage_and_activate(
        self,
        capability_root: Path,
        *,
        source_type: str,
        source_uri: str,
        enable: bool,
        action: str,
        source_ref: str | None = None,
        dry_run: bool = False,
        record_source: dict[str, Any] | None = None,
    ) -> CapabilityInstallResult:
        staged = self.staging_dir / f"{capability_root.name}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copytree(capability_root, staged)

        temp_registry = CapabilityRegistry(self.workspace, builtin_dir=self.registry.builtin_dir, workspace_dir=staged.parent, state_path=self.registry.state_store.path, admin_enabled=self.registry.admin_enabled)
        staged_descriptor = temp_registry.get_capability(staged.name)
        if staged_descriptor is None:
            staged_descriptor = next((item for item in temp_registry.list_capabilities() if item.root == staged), None)
        if staged_descriptor is None:
            return CapabilityInstallResult(ok=False, name=capability_root.name, action=action, message="Failed to stage capability", dry_run=dry_run)

        validation = CapabilityValidator(temp_registry).validate_capability(staged_descriptor.name)
        pre_health_errors, pre_health_warnings = self._run_healthcheck(staged_descriptor, root=staged)
        if not validation.ok or pre_health_errors:
            shutil.rmtree(staged, ignore_errors=True)
            return CapabilityInstallResult(
                ok=False,
                name=staged_descriptor.name,
                action=action,
                message="Capability validation failed",
                errors=[*validation.errors, *pre_health_errors],
                warnings=[*validation.warnings, *pre_health_warnings],
                dry_run=dry_run,
            )

        if dry_run:
            shutil.rmtree(staged, ignore_errors=True)
            return self._plan_result(
                name=staged_descriptor.name,
                action=action,
                message=f"Dry run: would install capability {staged_descriptor.name}",
                path=str(self.workspace_dir / staged_descriptor.name),
                version=staged_descriptor.version,
                warnings=[*validation.warnings, *pre_health_warnings],
            )

        target = self.workspace_dir / staged_descriptor.name
        backup = None
        previous_record = deepcopy(self.registry.state_store.get_record(staged_descriptor.name))
        try:
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = self.backups_dir / f"{staged_descriptor.name}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                shutil.move(str(target), str(backup))
            shutil.move(str(staged), str(target))
            record = CapabilityRecord(
                enabled=enable,
                version=staged_descriptor.version,
                install_hash=sha256_path(target),
                last_validated_at=datetime.now().isoformat(),
                compatibility={"ok": True},
                source=dict(record_source or {"type": source_type, "uri": source_uri, "ref": source_ref}),
                installed_path=str(target),
            )
            self.registry.state_store.upsert(staged_descriptor.name, record)
            self.registry.refresh()
            active_descriptor = self.registry.get_capability(staged_descriptor.name)
            post_validation = self.validator.validate_capability(staged_descriptor.name)
            post_health_errors, post_health_warnings = self._run_healthcheck(active_descriptor or staged_descriptor, root=target)
            errors = [*post_validation.errors, *post_health_errors]
            warnings = [*validation.warnings, *pre_health_warnings, *post_validation.warnings, *post_health_warnings]
            if errors:
                self._rollback_activation(target, backup, previous_record, staged_descriptor.name)
                return CapabilityInstallResult(
                    ok=False,
                    name=staged_descriptor.name,
                    action=action,
                    message="Activation failed validation after switch; rolled back",
                    errors=errors,
                    warnings=warnings,
                )
            return CapabilityInstallResult(ok=True, name=staged_descriptor.name, action=action, message=f"Installed capability {staged_descriptor.name}", path=str(target), version=staged_descriptor.version, warnings=warnings)
        except Exception as exc:
            self._rollback_activation(target, backup, previous_record, staged_descriptor.name)
            return CapabilityInstallResult(ok=False, name=staged_descriptor.name, action=action, message=f"Activation failed: {exc}")
