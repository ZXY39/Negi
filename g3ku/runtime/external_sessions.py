"""External bridge session registry.

Authoritative mapping between bridge-supplied ``external_key`` identifiers and
g3ku runtime session keys (``ext:{bridge}:{hash}``) for sessions driven
through the External Agent API. Transcripts themselves are owned by the shared
``SessionManager`` (``sessions/<safe_key>.jsonl``); this registry only owns
identity mapping plus small display metadata, persisted atomically at
``.g3ku/external-sessions/registry.json``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.config.live_runtime import get_runtime_config
from g3ku.runtime.session_keys import build_external_session_key, normalize_bridge_id
from g3ku.utils.helpers import ensure_dir

REGISTRY_DIRNAME = Path(".g3ku") / "external-sessions"
REGISTRY_FILENAME = "registry.json"
# Outbound bus channel value for messages addressed to external bridge
# sessions; the web shell drain resolves the target via this registry.
EXTERNAL_OUTBOUND_CHANNEL = "ext"


@dataclass(slots=True)
class ExternalSessionEntry:
    bridge_id: str
    external_key: str
    session_key: str
    created_at: str
    title: str = ""


def _registry_workspace() -> Path:
    try:
        config = get_runtime_config(force=False)[0]
        workspace = getattr(config, "workspace_path", None)
        if workspace:
            return Path(workspace).resolve()
    except Exception:
        pass
    return Path.cwd().resolve()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    temp_path.replace(path)


class ExternalSessionRegistry:
    def __init__(self, workspace: Path | None = None):
        self.workspace = (workspace or _registry_workspace()).resolve()
        self.path = ensure_dir(self.workspace / REGISTRY_DIRNAME) / REGISTRY_FILENAME
        self._lock = threading.RLock()
        self._entries: dict[str, ExternalSessionEntry] = {}
        self._index: dict[tuple[str, str], str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, dict):
            return
        for session_key, item in sessions.items():
            if not isinstance(item, dict):
                continue
            entry = ExternalSessionEntry(
                bridge_id=normalize_bridge_id(item.get("bridge_id")),
                external_key=str(item.get("external_key") or ""),
                session_key=str(session_key),
                created_at=str(item.get("created_at") or ""),
                title=str(item.get("title") or ""),
            )
            self._entries[entry.session_key] = entry
            self._index[(entry.bridge_id, entry.external_key)] = entry.session_key

    def _save(self) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(),
            "sessions": {
                session_key: {
                    "bridge_id": entry.bridge_id,
                    "external_key": entry.external_key,
                    "created_at": entry.created_at,
                    "title": entry.title,
                }
                for session_key, entry in self._entries.items()
            },
        }
        _atomic_write_json(self.path, payload)

    def resolve_or_create(
        self,
        *,
        bridge_id: str,
        external_key: str,
        title: str | None = None,
    ) -> tuple[ExternalSessionEntry, bool]:
        """Idempotent get-or-create. Returns ``(entry, created)``."""
        bridge = normalize_bridge_id(bridge_id)
        key = str(external_key or "").strip()
        if not key:
            raise ValueError("external_key is required")
        title_text = str(title or "").strip()
        with self._lock:
            existing_key = self._index.get((bridge, key))
            if existing_key is not None:
                entry = self._entries[existing_key]
                if title_text and entry.title != title_text:
                    entry.title = title_text
                    self._save()
                return entry, False

            digest_length = 16
            session_key = build_external_session_key(
                bridge_id=bridge, external_key=key, digest_length=digest_length
            )
            while session_key in self._entries:
                digest_length += 8
                if digest_length > 40:
                    raise RuntimeError("external session key space exhausted")
                session_key = build_external_session_key(
                    bridge_id=bridge, external_key=key, digest_length=digest_length
                )

            entry = ExternalSessionEntry(
                bridge_id=bridge,
                external_key=key,
                session_key=session_key,
                created_at=datetime.now().isoformat(),
                title=title_text,
            )
            self._entries[session_key] = entry
            self._index[(bridge, key)] = session_key
            self._save()
            return entry, True

    def get_by_session_key(self, session_key: str | None) -> ExternalSessionEntry | None:
        raw = str(session_key or "").strip()
        if not raw:
            return None
        with self._lock:
            return self._entries.get(raw)

    def get_session_key(self, *, bridge_id: str, external_key: str) -> str | None:
        bridge = normalize_bridge_id(bridge_id)
        key = str(external_key or "").strip()
        with self._lock:
            return self._index.get((bridge, key))

    def find_by_any_key(self, value: str | None) -> ExternalSessionEntry | None:
        """Resolve an outbound target that may be either a session_key or a
        registered external_key (any bridge)."""
        raw = str(value or "").strip()
        if not raw:
            return None
        with self._lock:
            entry = self._entries.get(raw)
            if entry is not None:
                return entry
            for candidate in self._entries.values():
                if candidate.external_key == raw:
                    return candidate
            return None

    def list_bridge_sessions(self, bridge_id: str) -> list[ExternalSessionEntry]:
        bridge = normalize_bridge_id(bridge_id)
        with self._lock:
            return [entry for entry in self._entries.values() if entry.bridge_id == bridge]

    def list_entries(self) -> list[ExternalSessionEntry]:
        with self._lock:
            return list(self._entries.values())

    def update_title(self, session_key: str, title: str) -> bool:
        raw = str(session_key or "").strip()
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                return False
            entry.title = str(title or "").strip()
            self._save()
            return True


_REGISTRY: ExternalSessionRegistry | None = None
_REGISTRY_LOCK = threading.RLock()


def get_external_session_registry(workspace: Path | None = None) -> ExternalSessionRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = ExternalSessionRegistry(workspace)
        return _REGISTRY


def reset_external_session_registry() -> None:
    """Test hook: drop the process-wide singleton."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None
