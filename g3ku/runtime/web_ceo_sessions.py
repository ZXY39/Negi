from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.config.loader import load_config
from g3ku.runtime.memory_scope import DEFAULT_WEB_MEMORY_SCOPE, normalize_memory_scope
from g3ku.utils.helpers import ensure_dir, safe_filename

DEFAULT_CEO_SESSION_TITLE = "新会话"
WEB_CEO_STATE_FILE = Path(".g3ku") / "web-ceo-state.json"
WEB_CEO_UPLOAD_ROOT = Path(".g3ku") / "web-ceo-uploads"
DEFAULT_TASK_MAX_DEPTH = 1
DEFAULT_TASK_HARD_MAX_DEPTH = 4


def workspace_path() -> Path:
    try:
        return Path(load_config().workspace_path).resolve()
    except Exception:
        return Path.cwd().resolve()


def new_web_ceo_session_id() -> str:
    return f"web:ceo-{uuid.uuid4().hex[:12]}"


def summarize_session_title(text: str, *, max_chars: int = 24) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return DEFAULT_CEO_SESSION_TITLE
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}…"


def summarize_preview_text(text: str, *, max_chars: int = 96) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}…"


def main_runtime_depth_limits() -> dict[str, int]:
    try:
        cfg = load_config()
        default_max_depth = int(getattr(getattr(cfg, "main_runtime", None), "default_max_depth", DEFAULT_TASK_MAX_DEPTH) or DEFAULT_TASK_MAX_DEPTH)
        hard_max_depth = int(getattr(getattr(cfg, "main_runtime", None), "hard_max_depth", DEFAULT_TASK_HARD_MAX_DEPTH) or DEFAULT_TASK_HARD_MAX_DEPTH)
    except Exception:
        default_max_depth = DEFAULT_TASK_MAX_DEPTH
        hard_max_depth = DEFAULT_TASK_HARD_MAX_DEPTH
    default_max_depth = max(0, default_max_depth)
    hard_max_depth = max(default_max_depth, hard_max_depth)
    return {
        "default_max_depth": default_max_depth,
        "hard_max_depth": hard_max_depth,
    }


def normalize_task_defaults(
    payload: Any,
    *,
    default_max_depth: int,
    hard_max_depth: int,
) -> dict[str, int]:
    source = payload if isinstance(payload, dict) else {}
    raw_depth = source.get("max_depth", source.get("maxDepth", default_max_depth))
    try:
        max_depth = int(raw_depth)
    except (TypeError, ValueError):
        max_depth = int(default_max_depth)
    max_depth = max(0, min(max_depth, int(hard_max_depth)))
    return {"max_depth": max_depth}


def normalize_ceo_metadata(metadata: Any, *, session_key: str) -> dict[str, Any]:
    payload = dict(metadata or {}) if isinstance(metadata, dict) else {}
    title = str(payload.get("title") or "").strip() or DEFAULT_CEO_SESSION_TITLE
    preview_text = summarize_preview_text(payload.get("last_preview_text") or payload.get("preview_text") or "")
    depth_limits = main_runtime_depth_limits()
    if str(session_key or "").startswith("web:"):
        memory_scope = normalize_memory_scope(
            payload.get("memory_scope"),
            fallback_channel=DEFAULT_WEB_MEMORY_SCOPE["channel"],
            fallback_chat_id=DEFAULT_WEB_MEMORY_SCOPE["chat_id"],
        )
    else:
        memory_scope = normalize_memory_scope(payload.get("memory_scope"), fallback_session_key=session_key)
    task_defaults = normalize_task_defaults(
        payload.get("task_defaults", payload.get("taskDefaults")),
        default_max_depth=depth_limits["default_max_depth"],
        hard_max_depth=depth_limits["hard_max_depth"],
    )
    return {
        **payload,
        "title": title,
        "last_preview_text": preview_text,
        "memory_scope": memory_scope,
        "task_defaults": task_defaults,
    }


def ensure_ceo_session_metadata(session: Any) -> bool:
    normalized = normalize_ceo_metadata(getattr(session, "metadata", None), session_key=str(getattr(session, "key", "") or ""))
    current = getattr(session, "metadata", None)
    if current == normalized:
        return False
    session.metadata = normalized
    return True


def update_ceo_session_after_turn(session: Any, *, user_text: str, assistant_text: str) -> bool:
    changed = ensure_ceo_session_metadata(session)
    metadata = dict(getattr(session, "metadata", {}) or {})
    if metadata.get("title") == DEFAULT_CEO_SESSION_TITLE and str(user_text or "").strip():
        next_title = summarize_session_title(user_text)
        if metadata.get("title") != next_title:
            metadata["title"] = next_title
            changed = True
    preview_source = str(assistant_text or "").strip() or str(user_text or "").strip()
    next_preview = summarize_preview_text(preview_source)
    if metadata.get("last_preview_text") != next_preview:
        metadata["last_preview_text"] = next_preview
        changed = True
    if changed:
        session.metadata = metadata
    return changed


def upload_dir_for_session(session_id: str, *, create: bool = True) -> Path:
    safe_session = safe_filename(str(session_id or "web_shared").replace(":", "_")) or "web_shared"
    path = workspace_path() / WEB_CEO_UPLOAD_ROOT / safe_session
    return ensure_dir(path) if create else path


def build_session_summary(session: Any, *, is_active: bool) -> dict[str, Any]:
    ensure_ceo_session_metadata(session)
    preview_text = str(session.metadata.get("last_preview_text") or "").strip()
    if not preview_text:
        for item in reversed(list(getattr(session, "messages", []) or [])):
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                preview_text = summarize_preview_text(content)
                break
    created_at = getattr(session, "created_at", None)
    updated_at = getattr(session, "updated_at", None)
    return {
        "session_id": str(getattr(session, "key", "") or ""),
        "title": str(session.metadata.get("title") or DEFAULT_CEO_SESSION_TITLE),
        "preview_text": preview_text,
        "message_count": len(list(getattr(session, "messages", []) or [])),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at or ""),
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else str(updated_at or ""),
        "is_active": bool(is_active),
        "task_defaults": dict(session.metadata.get("task_defaults") or {}),
    }


class WebCeoStateStore:
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace or workspace_path()).resolve()
        self.path = ensure_dir(self.workspace / ".g3ku") / "web-ceo-state.json"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def write(self, *, active_session_id: str | None) -> dict[str, Any]:
        payload = {
            "active_session_id": str(active_session_id or "").strip(),
            "updated_at": datetime.now().isoformat(),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def get_active_session_id(self) -> str:
        return str(self.read().get("active_session_id") or "").strip()

    def set_active_session_id(self, session_id: str | None) -> dict[str, Any]:
        return self.write(active_session_id=session_id)


def create_web_ceo_session(session_manager: Any, *, session_id: str | None = None, title: str | None = None) -> Any:
    key = str(session_id or "").strip() or new_web_ceo_session_id()
    session = session_manager.get_or_create(key)
    ensure_ceo_session_metadata(session)
    next_title = str(title or "").strip()
    if next_title:
        session.metadata["title"] = next_title
    session.updated_at = datetime.now()
    session_manager.save(session)
    return session


def delete_web_ceo_session_artifacts(*, session_manager: Any, session_id: str) -> None:
    path = session_manager.get_path(session_id)
    if path.exists():
        path.unlink()
    session_manager.invalidate(session_id)
    upload_dir = upload_dir_for_session(session_id, create=False)
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)


def list_web_ceo_sessions(session_manager: Any, *, active_session_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    changed_keys: list[str] = []
    for item in session_manager.list_sessions():
        key = str(item.get("key") or "").strip()
        if not key.startswith("web:"):
            continue
        session = session_manager.get_or_create(key)
        if ensure_ceo_session_metadata(session):
            changed_keys.append(key)
        rows.append(build_session_summary(session, is_active=key == active_session_id))
    for key in changed_keys:
        session_manager.save(session_manager.get_or_create(key))
    rows.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("session_id") or "")), reverse=True)
    return rows


def ensure_active_web_ceo_session(session_manager: Any, state_store: WebCeoStateStore) -> str:
    active_session_id = state_store.get_active_session_id()
    available_ids = [
        str(item.get("key") or "").strip()
        for item in session_manager.list_sessions()
        if str(item.get("key") or "").strip().startswith("web:")
    ]
    if active_session_id and active_session_id in available_ids:
        return active_session_id
    if available_ids:
        fallback = available_ids[0]
        state_store.set_active_session_id(fallback)
        return fallback
    created = create_web_ceo_session(session_manager)
    state_store.set_active_session_id(created.key)
    return str(created.key)
