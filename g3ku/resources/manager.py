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
        with self.acquire_skill(name):
            return descriptor.main_path.read_text(encoding="utf-8")

    def load_toolskill_body(self, name: str) -> str:
        self.ensure_fresh()
        descriptor = self.get_tool_descriptor(name)
        if descriptor is None or descriptor.toolskills_main_path is None or not descriptor.toolskills_main_path.exists():
            raise FileNotFoundError(f"toolskill not found: {name}")
        with self.acquire_tool(name):
            return descriptor.toolskills_main_path.read_text(encoding="utf-8")

    def acquire_tool(self, name: str):
        handle = self._locks.acquire(ResourceKind.TOOL, name)
        return _ManagedAccessHandle(self, handle)

    def acquire_skill(self, name: str):
        handle = self._locks.acquire(ResourceKind.SKILL, name)
        return _ManagedAccessHandle(self, handle)

    def busy_state(self, kind: ResourceKind, name: str):
        return self._locks.busy_state(kind, name)

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
        self._cleanup_stale_tool_instances(old_snapshot, snapshot)
        for callback in list(self._callbacks):
            try:
                callback(snapshot)
            except Exception as exc:
                logger.debug("resource snapshot callback failed: {}", exc)
        logger.debug("resources reloaded via {}: {} tools, {} skills", trigger, len(snapshot.tools), len(snapshot.skills))
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

        services = self._services_getter() if self._services_getter is not None else {}
        services.setdefault("app_config", self.app_config)
        for name, descriptor in discovery.tools.items():
            old_descriptor = old.tools.get(name)
            old_instance = old.tool_instances.get(name)
            if old_descriptor and old_descriptor.fingerprint == descriptor.fingerprint and old_instance is not None:
                descriptor = old_descriptor
                new_tools[name] = descriptor
                new_tool_instances[name] = old_instance
            elif self._locks.is_busy(ResourceKind.TOOL, name) and old_descriptor is not None and old_descriptor.fingerprint != descriptor.fingerprint and old_instance is not None:
                self._locks.mark_pending_delete(ResourceKind.TOOL, name)
                new_tools[name] = old_descriptor
                new_tool_instances[name] = old_instance
            else:
                descriptor.generation = generation
                try:
                    instance = self._loader.load_tool(descriptor, services=services)
                except Exception as exc:
                    descriptor.available = False
                    descriptor.errors.append(str(exc))
                    instance = None
                new_tools[name] = descriptor
                if descriptor.available and instance is not None:
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
    def __init__(self, manager: ResourceManager, handle):
        self._manager = manager
        self._handle = handle

    def release(self) -> None:
        try:
            self._handle.release()
        finally:
            self._manager.reload_now(trigger="release")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
