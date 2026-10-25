"""First-run onboarding state and the persona photo folder.

The onboarding record lives beside the web session state (`.g3ku/onboarding.json`),
so it is per workspace and survives restarts. It holds only pending operator input:
the persona note itself is written by the memory runtime, and photos live in
`<workspace>/my_image/`. Nothing here writes memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.agent.persona import PERSONA_NOTE_REF, PERSONA_PHOTO_DIR_NAME
from g3ku.deployment.data_root import data_root
from g3ku.utils.helpers import safe_filename

ONBOARDING_STATE_DIR = ".g3ku"
ONBOARDING_STATE_FILE = "onboarding.json"
PERSONA_DESCRIPTION_MAX_CHARS = 39000
PERSONA_PHOTO_MAX_BYTES = 5 * 1024 * 1024
PERSONA_PHOTO_MAX_FILES = 20
PERSONA_PHOTO_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _fresh_state() -> dict[str, Any]:
    return {
        "completed": False,
        "persona_step_done": False,
        "description": "",
        "photos": [],
        "persona_request_id": "",
        "photo_session_started_at": "",
        "photo_session_error": "",
        "created_at": "",
        "updated_at": "",
    }


def normalize_onboarding_state(payload: Any) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    state = _fresh_state()
    state["completed"] = bool(source.get("completed"))
    state["persona_step_done"] = bool(source.get("persona_step_done"))
    state["description"] = str(source.get("description") or "")
    photos: list[str] = []
    for item in list(source.get("photos") or []):
        name = str(item or "").strip()
        if name and name not in photos:
            photos.append(name)
    state["photos"] = photos
    for key in ("persona_request_id", "photo_session_started_at", "photo_session_error", "created_at", "updated_at"):
        state[key] = str(source.get(key) or "")
    return state


class OnboardingStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / ONBOARDING_STATE_DIR / ONBOARDING_STATE_FILE

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _fresh_state()
        try:
            return normalize_onboarding_state(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            return _fresh_state()

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_onboarding_state(state)
        now = _now_iso()
        if not normalized["created_at"]:
            normalized["created_at"] = now
        normalized["updated_at"] = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex[:12]}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return normalized

    def update(self, **changes: Any) -> dict[str, Any]:
        state = self.load()
        state.update(changes)
        return self.save(state)

    def reset(self) -> dict[str, Any]:
        return self.save(_fresh_state())

    def should_run(self, *, has_models: bool) -> bool:
        state = self.load()
        if state["completed"]:
            return False
        if self.path.exists():
            return True
        if not has_models:
            self.save(state)
            return True
        state["completed"] = True
        self.save(state)
        return False


def persona_photos_dir(workspace: Path) -> Path:
    return Path(workspace).resolve() / PERSONA_PHOTO_DIR_NAME


def write_persona_file(workspace: Path, text: str) -> Path:
    """Direct note write used only when the memory runtime is not available yet.

    A fresh project has no CEO model, so the settings dialog can still initialize the
    persona body before the memory manager exists. The default notes directory is the
    landing point; the memory runtime reads the same file once it boots.
    """
    body = str(text or "").strip()
    if not body:
        raise ValueError("persona text must not be empty")
    path = Path(workspace).resolve() / "memory" / "notes" / f"{PERSONA_NOTE_REF}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def _persona_thumb_path(source: Path) -> Path:
    stat = source.stat()
    digest = hashlib.sha1(
        f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    return data_root() / "temp" / "persona-thumbs" / f"{digest}.jpg"


def _persona_thumb_url(source: Path) -> str:
    from g3ku.runtime.api.ceo_media import _build_thumbnail, original_view_url

    thumb = _persona_thumb_path(source)
    if not thumb.exists():
        try:
            thumb.parent.mkdir(parents=True, exist_ok=True)
            _build_thumbnail(source, thumb)
        except Exception:
            return ""
    return original_view_url(thumb) if thumb.exists() else ""


def persona_photo_descriptor(path: Path) -> dict[str, Any]:
    from g3ku.runtime.api.ceo_media import original_view_url

    resolved = Path(path).resolve()
    stat = resolved.stat()
    url = original_view_url(resolved)
    thumb_url = _persona_thumb_url(resolved) or url
    return {
        "name": resolved.name,
        "path": str(resolved),
        "size": int(stat.st_size),
        "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        "url": url,
        "thumb_url": thumb_url,
    }


def list_persona_photos(workspace: Path) -> list[dict[str, Any]]:
    directory = persona_photos_dir(workspace)
    if not directory.is_dir():
        return []
    rows: list[tuple[int, str, Path]] = []
    for path in directory.iterdir():
        try:
            if not path.is_file() or path.suffix.lower() not in PERSONA_PHOTO_SUFFIXES:
                continue
            rows.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    rows.sort(reverse=True)
    items: list[dict[str, Any]] = []
    for _mtime, _name, path in rows:
        try:
            items.append(persona_photo_descriptor(path))
        except OSError:
            continue
    return items


def _unique_photo_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        next_candidate = directory / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1


def store_persona_photo(
    workspace: Path,
    data: bytes,
    *,
    filename: str,
    mime_type: str = "",
    max_bytes: int = PERSONA_PHOTO_MAX_BYTES,
) -> dict[str, Any]:
    if len(data) > max_bytes:
        raise ValueError(f"image exceeds {max_bytes} bytes")
    safe_name = safe_filename(str(filename or "").strip()) or "persona.jpg"
    suffix = Path(safe_name).suffix.lower()
    if suffix not in PERSONA_PHOTO_SUFFIXES:
        raise ValueError(f"unsupported image type: {suffix or 'none'}")
    if mime_type and not str(mime_type).lower().startswith("image/"):
        raise ValueError(f"unsupported image mime: {mime_type}")
    directory = persona_photos_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    target = _unique_photo_path(directory, safe_name)
    target.write_bytes(data)
    return persona_photo_descriptor(target)


def build_photo_session_prompt(workspace: Path, photo_names: list[str]) -> str:
    directory = persona_photos_dir(workspace)
    names = "、".join(str(name) for name in list(photo_names or []) if str(name).strip())
    return (
        f"现在用户上传了你的图片：{names}，都在 {directory} 文件夹。"
        "照片已经保存在这里，不要再复制。"
        "去读图，把形象与内容描述出来，然后作为人设提交记忆"
        "（memory_update，正文写明这是人设更新，不要新增普通记忆）。"
        "完成后调用 silent，不要向用户显示任何内容。"
    )


def append_onboarding_photos(store: OnboardingStore, names: list[str]) -> dict[str, Any]:
    state = store.load()
    photos = list(state.get("photos") or [])
    for name in list(names or []):
        normalized = str(name or "").strip()
        if normalized and normalized not in photos:
            photos.append(normalized)
    state["photos"] = photos
    return store.save(state)


def record_photo_session_error(store: OnboardingStore, error: str) -> None:
    try:
        store.update(photo_session_error=str(error or "")[:2000])
    except Exception:
        logger.warning("failed to record persona photo session error")
