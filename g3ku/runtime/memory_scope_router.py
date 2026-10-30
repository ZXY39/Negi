"""Per-session memory scopes: local memory + one manager per group.

Owner/local sessions read and write the existing workspace manager. Group
sessions get a dedicated manager under ``<workspace>/memory/groups/<scope>``
and write only there; their model-visible block is local + group. Third-party
DMs read local memory but have no writable manager (local memory is owner-only).

The router is deliberately a thin routing layer: the memory runtime itself
(sqlite, mirror, notes, queue, worker) is unchanged. Each group manager is a
normal ``MemoryManager`` rooted at its own mini-workspace.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.runtime.session_trust import TRUST_OWNER, session_trust
from g3ku.utils.helpers import safe_filename

GROUP_MEMORY_DIRNAME = "groups"
_GROUP_SCOPE_MAX = 32
# 作用域目录名最多 80 字符可读段 + `-` + 8 位摘要。不要复用表情包目录校验
# （那里按包名限 64 字符）：长 external_key 的群会被静默挡在盘外/包外。
GROUP_SCOPE_NAME_MAX = 128

# 进程内唯一活动路由：工具调用点（`memory_update` / `memory_note`）拿不到 loop 引用，
# 节点道也不携带前门视图；没有视图时按会话键问这里，写入才不会掉回本地域。
_ACTIVE_ROUTER: "MemoryScopeRouter | None" = None


def set_active_memory_scope_router(router: "MemoryScopeRouter | None") -> None:
    global _ACTIVE_ROUTER
    _ACTIVE_ROUTER = router


def active_memory_scope_router() -> "MemoryScopeRouter | None":
    return _ACTIVE_ROUTER


@dataclass(frozen=True)
class MemoryScopeView:
    session_key: str
    kind: str  # local / group / readonly_local
    local_manager: Any
    group_manager: Any | None = None
    group_key: str = ""

    @property
    def review_manager(self) -> Any | None:
        if self.kind == "group":
            return self.group_manager
        if self.kind == "local":
            return self.local_manager
        return None

    @property
    def writable_manager(self) -> Any | None:
        return self.review_manager

    def snapshot_text(self) -> str:
        local_text = str(self.local_manager.snapshot_text() or "").strip()
        group_text = (
            str(self.group_manager.snapshot_text() or "").strip()
            if self.group_manager is not None
            else ""
        )
        if local_text and group_text:
            return f"{local_text}\n\n## 群聊长期记忆\n{group_text}"
        if group_text:
            return f"## 群聊长期记忆\n{group_text}"
        return local_text

    def persona_text(self) -> str:
        return str(self.local_manager.persona_text() or "")

    def read_note(self, ref: str) -> str:
        managers = [manager for manager in (self.group_manager, self.local_manager) if manager is not None]
        last_error: Exception | None = None
        for manager in managers:
            try:
                return manager.load_note(str(ref or "").strip())
            except FileNotFoundError as exc:
                last_error = exc
                continue
            except OSError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise FileNotFoundError(str(ref or ""))


class MemoryScopeRouter:
    def __init__(
        self,
        *,
        workspace: Path | str,
        local_manager: Any,
        config: Any,
        max_group_managers: int = _GROUP_SCOPE_MAX,
    ) -> None:
        self.workspace = Path(workspace)
        self.local_manager = local_manager
        self._config = config
        self._max_group_managers = max(1, int(max_group_managers or 1))
        self._groups: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.RLock()

    @property
    def group_root(self) -> Path:
        return self.workspace / "memory" / GROUP_MEMORY_DIRNAME

    def view_for_session(self, session_key: Any) -> MemoryScopeView:
        key = str(session_key or "").strip()
        if not key:
            return MemoryScopeView(
                session_key="",
                kind="local",
                local_manager=self.local_manager,
            )
        entry = self._entry_for_session(key)
        if _is_group_entry(entry):
            group_key = _group_key_for_entry(entry, fallback=key)
            manager = self._group_manager(
                group_key,
                label=str(getattr(entry, "title", "") or "").strip()
                or str(getattr(entry, "external_key", "") or "").strip()
                or key,
            )
            return MemoryScopeView(
                session_key=key,
                kind="group",
                local_manager=self.local_manager,
                group_manager=manager,
                group_key=group_key,
            )
        if session_trust(key) == TRUST_OWNER:
            return MemoryScopeView(session_key=key, kind="local", local_manager=self.local_manager)
        # Third-party DM: local memory is readable but owner-only for writes.
        return MemoryScopeView(session_key=key, kind="readonly_local", local_manager=self.local_manager)

    def review_manager_for_session(self, session_key: Any) -> Any | None:
        """会话边界的冲刷目标：本地 → 本地；群聊 → 本群；第三方私聊 → 无。

        群聊的复核窗口存在本群 manager 上，冲本地 manager 是空操作。只有本群目录
        已经存在时才载入 manager，避免删掉一个从未写过记忆的群时凭空建出一个空域。
        """
        key = str(session_key or "").strip()
        if not key:
            return self.local_manager
        entry = self._entry_for_session(key)
        if _is_group_entry(entry):
            group_key = _group_key_for_entry(entry, fallback=key)
            with self._lock:
                existing = self._groups.get(group_key)
                if existing is not None:
                    self._groups.move_to_end(group_key)
                    return existing
            if not self._disk_scope_exists(group_key):
                return None
            return self._group_manager(
                group_key,
                label=str(getattr(entry, "title", "") or "").strip()
                or str(getattr(entry, "external_key", "") or "").strip()
                or group_key,
            )
        if session_trust(key) == TRUST_OWNER:
            return self.local_manager
        return None

    def list_scopes(self) -> list[dict[str, Any]]:
        """已知记忆作用域：本地 + 外部会话注册表里出现过的群。

        只读：这里不创建管理器，避免管理页一打开就为每个群起一个 worker。
        """
        scopes: list[dict[str, Any]] = [
            {"scope": "", "kind": "local", "title": "本地记忆", "session_key": ""},
        ]
        seen: set[str] = set()
        try:
            from g3ku.runtime.external_sessions import get_external_session_registry

            entries = list(get_external_session_registry().list_entries() or [])
        except Exception:
            entries = []
        for entry in entries:
            if not _is_group_entry(entry):
                continue
            group_key = _group_key_for_entry(entry, fallback=str(getattr(entry, "session_key", "") or ""))
            if not group_key or group_key in seen:
                continue
            seen.add(group_key)
            scopes.append(
                {
                    "scope": group_key,
                    "kind": "group",
                    "title": str(getattr(entry, "title", "") or "").strip()
                    or str(getattr(entry, "external_key", "") or "").strip()
                    or group_key,
                    "session_key": str(getattr(entry, "session_key", "") or ""),
                    "bridge_id": str(getattr(entry, "bridge_id", "") or ""),
                }
            )
        # 磁盘发现：配置包导入的群记忆在注册表里还没有会话（新设备尚未见到该群），
        # 管理页仍要能查看与整理它。只列目录，不创建 manager/worker。
        for group_key in self._disk_group_scopes():
            if group_key in seen:
                continue
            seen.add(group_key)
            scopes.append(
                {
                    "scope": group_key,
                    "kind": "group",
                    "title": group_key,
                    "session_key": "",
                    "bridge_id": "",
                }
            )
        return scopes

    def _disk_group_scopes(self) -> list[str]:
        try:
            children = sorted(self.group_root.iterdir(), key=lambda item: item.name)
        except OSError:
            return []
        return [
            child.name
            for child in children
            if child.is_dir() and not child.is_symlink() and group_scope_name_ok(child.name)
        ]

    def _disk_scope_exists(self, group_key: str) -> bool:
        if not group_scope_name_ok(group_key):
            return False
        root = self.group_root / group_key
        return root.is_dir() and not root.is_symlink()

    def manager_for_scope(self, scope_key: Any) -> Any | None:
        """管理面按作用域取管理器；未知群作用域返回 None（不回落到本地）。"""
        key = str(scope_key or "").strip()
        if not key or key.lower() == "local":
            return self.local_manager
        entry = self._entry_for_scope(key)
        if entry is None:
            if not self._disk_scope_exists(key):
                return None
            return self._group_manager(key, label=key)
        return self._group_manager(
            key,
            label=str(getattr(entry, "title", "") or "").strip()
            or str(getattr(entry, "external_key", "") or "").strip(),
        )

    def _entry_for_scope(self, group_key: str) -> Any:
        try:
            from g3ku.runtime.external_sessions import get_external_session_registry

            entries = list(get_external_session_registry().list_entries() or [])
        except Exception:
            return None
        for entry in entries:
            if not _is_group_entry(entry):
                continue
            if _group_key_for_entry(
                entry, fallback=str(getattr(entry, "session_key", "") or "")
            ) == group_key:
                return entry
        return None

    def _entry_for_session(self, session_key: str) -> Any:
        try:
            from g3ku.runtime.external_sessions import get_external_session_registry

            return get_external_session_registry().get_by_session_key(session_key)
        except Exception:
            return None

    def _group_manager(self, group_key: str, *, label: str) -> Any:
        with self._lock:
            existing = self._groups.get(group_key)
            if existing is not None:
                self._groups.move_to_end(group_key)
                return existing
            root = self.group_root / group_key
            cfg = _clone_config(self._config)
            from g3ku.agent.memory_agent_runtime import MemoryManager

            manager = MemoryManager(
                root,
                cfg,
                scope_label=label,
                persona_enabled=False,
            )
            start = getattr(manager, "start", None)
            if callable(start):
                start()
            self._groups[group_key] = manager
            while len(self._groups) > self._max_group_managers:
                _evicted_key, evicted = self._groups.popitem(last=False)
                try:
                    evicted.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("memory group manager close failed for {}: {}", _evicted_key, exc)
            return manager

    def group_managers(self) -> list[Any]:
        with self._lock:
            return list(self._groups.values())

    def close(self) -> None:
        with self._lock:
            managers = list(self._groups.values())
            self._groups.clear()
        for manager in managers:
            try:
                manager.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("memory group manager close failed: {}", exc)


def _is_group_entry(entry: Any) -> bool:
    return str(getattr(entry, "chat_type", "") or "").strip().lower() in {"group", "guild"}


def _group_key_for_entry(entry: Any, *, fallback: str) -> str:
    external_key = str(getattr(entry, "external_key", "") or "").strip() or str(fallback or "").strip()
    bridge_id = str(getattr(entry, "bridge_id", "") or "").strip()
    return _group_key(f"{bridge_id}:{external_key}" if bridge_id else external_key)


def group_scope_name_ok(name: Any) -> bool:
    """A group scope directory name that is safe on disk and inside a bundle.

    Generated keys are ``safe_filename(...)[:80] + '-' + 8 hex``; this accepts
    them while rejecting path escapes, drive/stream colons, Windows device
    names and names the platform cannot create.
    """
    raw = str(name or "").strip()
    if not raw or raw != str(name) or raw in {".", ".."}:
        return False
    if len(raw) > GROUP_SCOPE_NAME_MAX or raw.startswith(".") or raw.endswith("."):
        return False
    if any(char in raw for char in '<>:"/\\|?*') or any(ord(char) < 32 for char in raw):
        return False
    stem = raw.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", "CLOCK$"}:
        return False
    if len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3].isdigit():
        return False
    return True


def _group_key(external_key: str) -> str:
    raw = str(external_key or "").strip() or "unknown"
    readable = safe_filename(raw.replace(":", "_")).strip("._")[:80] or "group"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def _clone_config(config: Any) -> Any:
    clone = getattr(config, "model_copy", None)
    if callable(clone):
        return clone(deep=True)
    import copy

    return copy.deepcopy(config)


__all__ = [
    "GROUP_MEMORY_DIRNAME",
    "GROUP_SCOPE_NAME_MAX",
    "MemoryScopeRouter",
    "MemoryScopeView",
    "active_memory_scope_router",
    "group_scope_name_ok",
    "set_active_memory_scope_router",
]
