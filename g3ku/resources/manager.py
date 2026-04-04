from __future__ import annotations

import asyncio
import inspect
import json
import threading
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from g3ku.resources.loader import ResourceLoader
from g3ku.resources.locks import ResourceLockManager
from g3ku.resources.models import ResourceKind, ResourceSnapshot, SkillResourceDescriptor, ToolResourceDescriptor
from g3ku.resources.registry import DiscoveryResult, ResourceRegistry

_MANAGER_CACHE: dict[str, "ResourceManager"] = {}
_MANAGER_CACHE_LOCK = threading.RLock()


def get_shared_resource_manager(
    workspace: Path,
    *,
    app_config: Any = None,
    service_getter: Callable[[], dict[str, Any]] | None = None,
    on_change: Callable[[ResourceSnapshot], None] | None = None,
) -> "ResourceManager":
    key = str(Path(workspace).resolve())
    with _MANAGER_CACHE_LOCK:
        manager = _MANAGER_CACHE.get(key)
        if manager is None:
            manager = ResourceManager(Path(workspace), app_config=app_config)
            _MANAGER_CACHE[key] = manager
        if app_config is not None:
            manager.bind_app_config(app_config)
        if service_getter is not None:
            manager.bind_service_getter(service_getter)
        if on_change is not None:
            manager.add_on_change(on_change)
        return manager


class ResourceManager:
    def __init__(self, workspace: Path, *, app_config: Any = None):
        self.workspace = Path(workspace)
        self.app_config = app_config
        self._lock = threading.RLock()
        self._services_getter: Callable[[], dict[str, Any]] | None = None
        self._callbacks: list[Callable[[ResourceSnapshot], None]] = []
        self._snapshot = ResourceSnapshot(generation=0)
        self._started = False
        self._state_path = self.workspace / str(self._resources_cfg_value("state_path", ".g3ku/resources.state.json"))
        self._registry = self._build_registry()
        self._loader = ResourceLoader(self.workspace, app_config=app_config)
        self._locks = ResourceLockManager(windows_fs_lock=bool(self._resources_cfg_value("locks.windows_fs_lock", True)))
        self.reload_now(trigger="init")

    def bind_app_config(self, app_config: Any) -> None:
        stale_instances: list[Any] = []
        with self._lock:
            self.app_config = app_config
            self._loader.app_config = app_config
            self._registry = self._build_registry()
            if self._snapshot.tool_instances:
                stale_instances = list(self._snapshot.tool_instances.values())
                self._snapshot = ResourceSnapshot(
                    generation=self._snapshot.generation,
                    tools=dict(self._snapshot.tools),
                    skills=dict(self._snapshot.skills),
                    tool_instances={},
                )
        for instance in stale_instances:
            self._close_tool_instance(instance)

    def bind_service_getter(self, getter: Callable[[], dict[str, Any]]) -> None:
        stale_instances: list[Any] = []
        with self._lock:
            self._services_getter = getter
            if self._snapshot.tool_instances:
                stale_instances = list(self._snapshot.tool_instances.values())
                self._snapshot = ResourceSnapshot(
                    generation=self._snapshot.generation,
                    tools=dict(self._snapshot.tools),
                    skills=dict(self._snapshot.skills),
                    tool_instances={},
                )
        for instance in stale_instances:
            self._close_tool_instance(instance)

    def add_on_change(self, callback: Callable[[ResourceSnapshot], None]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def start(self) -> None:
        if self._started:
            return
        self._started = True

    def close(self) -> None:
        for instance in list(self._snapshot.tool_instances.values()):
            self._close_tool_instance(instance)
        self._started = False

    def list_tools(self) -> list[ToolResourceDescriptor]:
        self.ensure_fresh()
        with self._lock:
            return list(self._snapshot.tools.values())

    def list_skills(self) -> list[SkillResourceDescriptor]:
        self.ensure_fresh()
        with self._lock:
            return list(self._snapshot.skills.values())

    def get_tool(self, name: str):
        self.ensure_fresh()
        with self._lock:
            return self._snapshot.tool_instances.get(name)

    def get_tool_descriptor(self, name: str) -> ToolResourceDescriptor | None:
        self.ensure_fresh()
        with self._lock:
            return self._snapshot.tools.get(name)

    def get_skill(self, name: str) -> SkillResourceDescriptor | None:
        self.ensure_fresh()
        with self._lock:
            return self._snapshot.skills.get(name)

    def tool_instances(self) -> dict[str, Any]:
        self.ensure_fresh()
        with self._lock:
            return dict(self._snapshot.tool_instances)

    def load_skill_body(self, name: str) -> str:
        self.ensure_fresh()
        descriptor = self.get_skill(name)
        if descriptor is None or descriptor.main_path is None or not descriptor.main_path.exists():
            raise FileNotFoundError(f"skill not found: {name}")
        with self.acquire_skill(name, reload_on_release="never"):
            return descriptor.main_path.read_text(encoding="utf-8")

    def load_toolskill_body(self, name: str) -> str:
        self.ensure_fresh()
        descriptor = self.get_tool_descriptor(name)
        if descriptor is None or descriptor.toolskills_main_path is None or not descriptor.toolskills_main_path.exists():
            raise FileNotFoundError(f"toolskill not found: {name}")
        with self.acquire_tool(name, reload_on_release="never"):
            return descriptor.toolskills_main_path.read_text(encoding="utf-8")

    def _resolved_services(self) -> dict[str, Any]:
        services = self._services_getter() if self._services_getter is not None else {}
        normalized = dict(services or {})
        normalized.setdefault("app_config", self.app_config)

        loop = normalized.get("loop")
        main_task_service = normalized.get("main_task_service")
        if main_task_service is None and loop is not None:
            main_task_service = getattr(loop, "main_task_service", None)
            if main_task_service is not None:
                normalized["main_task_service"] = main_task_service

        if normalized.get("memory_manager") is None and main_task_service is not None:
            memory_manager = getattr(main_task_service, "memory_manager", None)
            if memory_manager is not None:
                normalized["memory_manager"] = memory_manager

        if normalized.get("memory_manager") is None and loop is not None:
            memory_manager = getattr(loop, "memory_manager", None)
            if memory_manager is not None:
                normalized["memory_manager"] = memory_manager

        if normalized.get("app_config") is None and main_task_service is not None:
            app_config = getattr(main_task_service, "_app_config", None)
            if app_config is not None:
                normalized["app_config"] = app_config

        return normalized

    def acquire_tool(self, name: str, *, reload_on_release: str = "always"):
        handle = self._locks.acquire(ResourceKind.TOOL, name)
        return _ManagedAccessHandle(self, handle, reload_on_release=reload_on_release)

    def acquire_skill(self, name: str, *, reload_on_release: str = "always"):
        handle = self._locks.acquire(ResourceKind.SKILL, name)
        return _ManagedAccessHandle(self, handle, reload_on_release=reload_on_release)

    def busy_state(self, kind: ResourceKind, name: str):
        return self._locks.busy_state(kind, name)

    def capture_resource_tree_state(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {
                "skills": self._resource_dir_state(self._registry.skills_dir),
                "tools": self._resource_dir_state(self._registry.tools_dir),
            }

    def refresh_paths(self, paths: list[str | Path], *, trigger: str = "path-change") -> ResourceSnapshot:
        skill_roots: set[Path] = set()
        tool_roots: set[Path] = set()
        for raw_path in paths:
            if raw_path in (None, ""):
                continue
            skill_root = self._registry.skill_root_for_path(Path(raw_path))
            if skill_root is not None:
                skill_roots.add(skill_root.resolve(strict=False))
            tool_root = self._registry.tool_root_for_path(Path(raw_path))
            if tool_root is not None:
                tool_roots.add(tool_root.resolve(strict=False))
        if not skill_roots and not tool_roots:
            with self._lock:
                return self._snapshot
        return self.refresh_roots(skill_roots=skill_roots, tool_roots=tool_roots, trigger=trigger)

    def refresh_changed_tree_state(
        self,
        before_state: dict[str, dict[str, str]] | None,
        *,
        trigger: str = "path-change",
    ) -> ResourceSnapshot:
        previous = before_state if isinstance(before_state, dict) else {}
        after_state = self.capture_resource_tree_state()
        skill_roots = self._changed_roots(previous.get("skills"), after_state.get("skills"), self._registry.skills_dir)
        tool_roots = self._changed_roots(previous.get("tools"), after_state.get("tools"), self._registry.tools_dir)
        if not skill_roots and not tool_roots:
            with self._lock:
                return self._snapshot
        return self.refresh_roots(skill_roots=skill_roots, tool_roots=tool_roots, trigger=trigger)

    def refresh_roots(
        self,
        *,
        skill_roots: set[Path] | None = None,
        tool_roots: set[Path] | None = None,
        trigger: str = "targeted",
    ) -> ResourceSnapshot:
        normalized_skill_roots = {Path(root).resolve(strict=False) for root in (skill_roots or set())}
        normalized_tool_roots = {Path(root).resolve(strict=False) for root in (tool_roots or set())}
        if not normalized_skill_roots and not normalized_tool_roots:
            with self._lock:
                return self._snapshot

        with self._lock:
            old_snapshot = self._snapshot
            snapshot = self._refresh_selected_locked(
                skill_roots=normalized_skill_roots,
                tool_roots=normalized_tool_roots,
            )
            self._snapshot = snapshot
            self._persist_state(snapshot)
        self._finish_reload(old_snapshot, snapshot, trigger=trigger)
        return snapshot

    def ensure_fresh(self) -> None:
        if not self._started and bool(self._resources_cfg_value("reload.lazy_reload_on_access", True)):
            self.reload_now(trigger="lazy")

    def reload_now(self, *, trigger: str = "manual") -> ResourceSnapshot:
        with self._lock:
            old_snapshot = self._snapshot
            discovery = self._registry.discover()
            snapshot = self._merge_discovery(discovery)
            self._snapshot = snapshot
            self._persist_state(snapshot)
        self._finish_reload(old_snapshot, snapshot, trigger=trigger)
        return snapshot

    def _merge_discovery(self, discovery: DiscoveryResult) -> ResourceSnapshot:
        old = self._snapshot
        generation = old.generation + 1
        new_tools: dict[str, ToolResourceDescriptor] = {}
        new_tool_instances: dict[str, Any] = {}
        new_skills: dict[str, SkillResourceDescriptor] = {}

        for name, descriptor in discovery.skills.items():
            descriptor.generation = generation
            old_descriptor = old.skills.get(name)
            if old_descriptor and old_descriptor.fingerprint == descriptor.fingerprint:
                descriptor = old_descriptor
            new_skills[name] = descriptor
            self._locks.register_path(ResourceKind.SKILL, name, descriptor.main_path or descriptor.manifest_path)
            self._locks.clear_pending_delete(ResourceKind.SKILL, name)

        for name, old_descriptor in old.skills.items():
            if name in new_skills:
                continue
            if self._locks.is_busy(ResourceKind.SKILL, name):
                self._locks.mark_pending_delete(ResourceKind.SKILL, name)
                new_skills[name] = old_descriptor
            else:
                self._locks.unregister_path(ResourceKind.SKILL, name)

        services = self._resolved_services()
        for name, descriptor in discovery.tools.items():
            old_descriptor = old.tools.get(name)
            old_instance = old.tool_instances.get(name)
            if self._can_reuse_tool_instance(old_descriptor=old_descriptor, descriptor=descriptor, old_instance=old_instance):
                descriptor = old_descriptor
                new_tools[name] = descriptor
                new_tool_instances[name] = old_instance
            elif self._locks.is_busy(ResourceKind.TOOL, name) and old_descriptor is not None and old_descriptor.fingerprint != descriptor.fingerprint and old_instance is not None:
                self._locks.mark_pending_delete(ResourceKind.TOOL, name)
                new_tools[name] = old_descriptor
                new_tool_instances[name] = old_instance
            else:
                descriptor.generation = generation
                instance = self._load_tool_instance(descriptor, services=services)
                new_tools[name] = descriptor
                if instance is not None:
                    new_tool_instances[name] = instance
                    self._locks.clear_pending_delete(ResourceKind.TOOL, name)
            self._locks.register_path(ResourceKind.TOOL, name, descriptor.manifest_path)

        for name, old_descriptor in old.tools.items():
            if name in new_tools:
                continue
            old_instance = old.tool_instances.get(name)
            if self._locks.is_busy(ResourceKind.TOOL, name) and old_instance is not None:
                self._locks.mark_pending_delete(ResourceKind.TOOL, name)
                new_tools[name] = old_descriptor
                new_tool_instances[name] = old_instance
            else:
                self._locks.unregister_path(ResourceKind.TOOL, name)

        return ResourceSnapshot(generation=generation, tools=new_tools, skills=new_skills, tool_instances=new_tool_instances)

    def _cleanup_stale_tool_instances(
        self,
        old_snapshot: ResourceSnapshot,
        new_snapshot: ResourceSnapshot,
    ) -> None:
        active_instances = {id(instance) for instance in new_snapshot.tool_instances.values()}
        for instance in old_snapshot.tool_instances.values():
            if id(instance) in active_instances:
                continue
            self._close_tool_instance(instance)

    @staticmethod
    def _close_tool_instance(instance: Any) -> None:
        if instance is None or not hasattr(instance, "close"):
            return
        try:
            result = instance.close()
            if not inspect.isawaitable(result):
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
                return
            loop.create_task(result)
        except Exception as exc:
            logger.debug("tool instance close skipped: {}", exc)

    def _finish_reload(self, old_snapshot: ResourceSnapshot, snapshot: ResourceSnapshot, *, trigger: str) -> None:
        self._cleanup_stale_tool_instances(old_snapshot, snapshot)
        for callback in list(self._callbacks):
            try:
                callback(snapshot)
            except Exception as exc:
                logger.debug("resource snapshot callback failed: {}", exc)
        logger.debug("resources reloaded via {}: {} tools, {} skills", trigger, len(snapshot.tools), len(snapshot.skills))

    def _refresh_selected_locked(
        self,
        *,
        skill_roots: set[Path],
        tool_roots: set[Path],
    ) -> ResourceSnapshot:
        old = self._snapshot
        generation = old.generation + 1
        new_tools = dict(old.tools)
        new_tool_instances = dict(old.tool_instances)
        new_skills = dict(old.skills)

        old_skill_names_by_root = self._resource_names_by_root(old.skills)
        old_tool_names_by_root = self._resource_names_by_root(old.tools)

        discovered_tools_by_root: dict[Path, ToolResourceDescriptor | None] = {}
        changed_tool_names: set[str] = set()
        for root in sorted(tool_roots):
            descriptor = self._registry.build_tool_descriptor(root)
            discovered_tools_by_root[root] = descriptor
            changed_tool_names.update(old_tool_names_by_root.get(root, []))
            if descriptor is not None:
                changed_tool_names.add(descriptor.name)

        final_tool_names = set(old.tools.keys())
        final_tool_names.difference_update(changed_tool_names)
        final_tool_names.update(
            descriptor.name
            for descriptor in discovered_tools_by_root.values()
            if descriptor is not None
        )

        if changed_tool_names:
            for descriptor in old.skills.values():
                if set(descriptor.requires_tools) & changed_tool_names:
                    skill_roots.add(descriptor.root.resolve(strict=False))

        discovered_skills_by_root: dict[Path, SkillResourceDescriptor | None] = {}
        for root in sorted(skill_roots):
            descriptor = self._registry.build_skill_descriptor(root, tool_names=final_tool_names)
            discovered_skills_by_root[root] = descriptor

        services = self._resolved_services()

        occupied_tool_names = set(new_tools.keys())
        for root in tool_roots:
            occupied_tool_names.difference_update(old_tool_names_by_root.get(root, []))
        for root in sorted(tool_roots):
            for name in old_tool_names_by_root.get(root, []):
                self._remove_tool_name(name, new_tools=new_tools, new_tool_instances=new_tool_instances, old_snapshot=old)
            descriptor = discovered_tools_by_root.get(root)
            if descriptor is None:
                continue
            if descriptor.name in occupied_tool_names:
                logger.debug("skip duplicate tool name during targeted refresh: {}", descriptor.name)
                continue
            self._apply_tool_descriptor(
                descriptor,
                generation=generation,
                new_tools=new_tools,
                new_tool_instances=new_tool_instances,
                old_snapshot=old,
                services=services,
            )
            occupied_tool_names.add(descriptor.name)

        occupied_skill_names = set(new_skills.keys())
        for root in skill_roots:
            occupied_skill_names.difference_update(old_skill_names_by_root.get(root, []))
        for root in sorted(skill_roots):
            for name in old_skill_names_by_root.get(root, []):
                self._remove_skill_name(name, new_skills=new_skills, old_snapshot=old)
            descriptor = discovered_skills_by_root.get(root)
            if descriptor is None:
                continue
            if descriptor.name in occupied_skill_names:
                logger.debug("skip duplicate skill name during targeted refresh: {}", descriptor.name)
                continue
            descriptor.generation = generation
            new_skills[descriptor.name] = descriptor
            self._locks.register_path(ResourceKind.SKILL, descriptor.name, descriptor.main_path or descriptor.manifest_path)
            self._locks.clear_pending_delete(ResourceKind.SKILL, descriptor.name)
            occupied_skill_names.add(descriptor.name)

        return ResourceSnapshot(generation=generation, tools=new_tools, skills=new_skills, tool_instances=new_tool_instances)

    @staticmethod
    def _resource_names_by_root(descriptors: dict[str, SkillResourceDescriptor] | dict[str, ToolResourceDescriptor]) -> dict[Path, list[str]]:
        mapping: dict[Path, list[str]] = {}
        for name, descriptor in descriptors.items():
            root = descriptor.root.resolve(strict=False)
            mapping.setdefault(root, []).append(name)
        return mapping

    def _remove_skill_name(
        self,
        name: str,
        *,
        new_skills: dict[str, SkillResourceDescriptor],
        old_snapshot: ResourceSnapshot,
    ) -> None:
        old_descriptor = old_snapshot.skills.get(name)
        if old_descriptor is None:
            return
        if self._locks.is_busy(ResourceKind.SKILL, name):
            self._locks.mark_pending_delete(ResourceKind.SKILL, name)
            new_skills[name] = old_descriptor
            return
        new_skills.pop(name, None)
        self._locks.unregister_path(ResourceKind.SKILL, name)

    def _remove_tool_name(
        self,
        name: str,
        *,
        new_tools: dict[str, ToolResourceDescriptor],
        new_tool_instances: dict[str, Any],
        old_snapshot: ResourceSnapshot,
    ) -> None:
        old_descriptor = old_snapshot.tools.get(name)
        old_instance = old_snapshot.tool_instances.get(name)
        if old_descriptor is None:
            return
        if self._locks.is_busy(ResourceKind.TOOL, name) and old_instance is not None:
            self._locks.mark_pending_delete(ResourceKind.TOOL, name)
            new_tools[name] = old_descriptor
            new_tool_instances[name] = old_instance
            return
        new_tools.pop(name, None)
        new_tool_instances.pop(name, None)
        self._locks.unregister_path(ResourceKind.TOOL, name)

    def _apply_tool_descriptor(
        self,
        descriptor: ToolResourceDescriptor,
        *,
        generation: int,
        new_tools: dict[str, ToolResourceDescriptor],
        new_tool_instances: dict[str, Any],
        old_snapshot: ResourceSnapshot,
        services: dict[str, Any],
    ) -> None:
        old_descriptor = old_snapshot.tools.get(descriptor.name)
        old_instance = old_snapshot.tool_instances.get(descriptor.name)
        if self._can_reuse_tool_instance(old_descriptor=old_descriptor, descriptor=descriptor, old_instance=old_instance):
            descriptor = old_descriptor
            new_tools[descriptor.name] = descriptor
            new_tool_instances[descriptor.name] = old_instance
        elif self._locks.is_busy(ResourceKind.TOOL, descriptor.name) and old_descriptor is not None and old_descriptor.fingerprint != descriptor.fingerprint and old_instance is not None:
            self._locks.mark_pending_delete(ResourceKind.TOOL, descriptor.name)
            new_tools[descriptor.name] = old_descriptor
            new_tool_instances[descriptor.name] = old_instance
        else:
            descriptor.generation = generation
            instance = self._load_tool_instance(descriptor, services=services)
            new_tools[descriptor.name] = descriptor
            if instance is not None:
                new_tool_instances[descriptor.name] = instance
                self._locks.clear_pending_delete(ResourceKind.TOOL, descriptor.name)
            else:
                new_tool_instances.pop(descriptor.name, None)
        self._locks.register_path(ResourceKind.TOOL, descriptor.name, descriptor.manifest_path)

    @staticmethod
    def _can_reuse_tool_instance(*, old_descriptor: ToolResourceDescriptor | None, descriptor: ToolResourceDescriptor, old_instance: Any) -> bool:
        if old_descriptor is None or old_instance is None:
            return False
        if type(old_instance).__name__ == "RepairRequiredTool":
            return False
        if old_descriptor.fingerprint != descriptor.fingerprint:
            return False
        if bool(old_descriptor.available) != bool(descriptor.available):
            return False
        if list(old_descriptor.warnings or []) != list(descriptor.warnings or []):
            return False
        if list(old_descriptor.errors or []) != list(descriptor.errors or []):
            return False
        return True

    def _load_tool_instance(self, descriptor: ToolResourceDescriptor, *, services: dict[str, Any]) -> Any:
        try:
            if descriptor.available:
                instance = self._loader.load_tool(descriptor, services=services)
                if instance is not None:
                    return instance
                return self._loader.load_repair_required_tool(descriptor, reason='tool_handler_unavailable')
            return self._loader.load_repair_required_tool(descriptor, reason='resource_unavailable')
        except Exception as exc:
            descriptor.available = False
            descriptor.errors.append(str(exc))
            return self._loader.load_repair_required_tool(descriptor, reason=str(exc))

    @staticmethod
    def _resource_dir_state(base_dir: Path) -> dict[str, str]:
        state: dict[str, str] = {}
        root = Path(base_dir).resolve(strict=False)
        if not root.exists():
            return state
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            state[entry.name] = ResourceRegistry._tree_fingerprint(entry)
        return state

    @staticmethod
    def _changed_roots(
        before: dict[str, str] | None,
        after: dict[str, str] | None,
        base_dir: Path,
    ) -> set[Path]:
        previous = dict(before or {})
        current = dict(after or {})
        changed = {
            name
            for name in set(previous) | set(current)
            if previous.get(name) != current.get(name)
        }
        base = Path(base_dir).resolve(strict=False)
        return {base / name for name in changed}

    def _persist_state(self, snapshot: ResourceSnapshot) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "generation": snapshot.generation,
                "tools": [
                    {
                        "name": descriptor.name,
                        "available": descriptor.available,
                        "busy": self._locks.busy_state(ResourceKind.TOOL, descriptor.name).busy,
                        "pendingDelete": self._locks.busy_state(ResourceKind.TOOL, descriptor.name).pending_delete,
                        "warnings": list(descriptor.warnings),
                        "errors": list(descriptor.errors),
                    }
                    for descriptor in snapshot.tools.values()
                ],
                "skills": [
                    {
                        "name": descriptor.name,
                        "available": descriptor.available,
                        "busy": self._locks.busy_state(ResourceKind.SKILL, descriptor.name).busy,
                        "pendingDelete": self._locks.busy_state(ResourceKind.SKILL, descriptor.name).pending_delete,
                        "warnings": list(descriptor.warnings),
                        "errors": list(descriptor.errors),
                    }
                    for descriptor in snapshot.skills.values()
                ],
            }
            self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("resource state persistence skipped: {}", exc)

    def _build_registry(self) -> ResourceRegistry:
        skills_dir = self.workspace / str(self._resources_cfg_value("skills_dir", "skills"))
        tools_dir = self.workspace / str(self._resources_cfg_value("tools_dir", "tools"))
        manifest_name = str(self._resources_cfg_value("manifest_name", "resource.yaml"))
        return ResourceRegistry(self.workspace, skills_dir=skills_dir, tools_dir=tools_dir, manifest_name=manifest_name)

    def _resources_cfg_value(self, key: str, default: Any) -> Any:
        config = getattr(self.app_config, "resources", None)
        if config is None:
            return default
        current: Any = config
        for part in key.split("."):
            if current is None:
                return default
            current = getattr(current, part, None)
        return default if current is None else current


class _ManagedAccessHandle:
    def __init__(self, manager: ResourceManager, handle, *, reload_on_release: str = "always"):
        self._manager = manager
        self._handle = handle
        self._reload_on_release = str(reload_on_release or "always").strip().lower() or "always"

    def release(self) -> None:
        needs_reload = bool(self._handle.release())
        if self._reload_on_release == "never":
            return
        if self._reload_on_release == "pending_delete" and not needs_reload:
            return
        if self._reload_on_release == "always":
            self._manager.reload_now(trigger="release")
            return
        raise ValueError(f"unsupported reload_on_release mode: {self._reload_on_release}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
