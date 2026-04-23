from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from g3ku.security import BOOTSTRAP_MASTER_KEY_ENV, get_bootstrap_security_service
from g3ku.utils.helpers import ensure_dir, sync_workspace_templates

BOOTSTRAP_PASSWORD_ENV = "G3KU_BOOTSTRAP_PASSWORD"
RESOURCE_SEED_ROOT_ENV = "G3KU_RESOURCE_SEED_ROOT"
PERSISTENT_WORKSPACE_DIRS = (
    ".g3ku",
    "memory",
    "sessions",
    "temp",
    "externaltools",
    "skills",
    "tools",
)


def ensure_persistent_workspace_dirs(workspace: Path) -> dict[str, str]:
    root = Path(workspace).resolve()
    created: dict[str, str] = {}
    for relative in PERSISTENT_WORKSPACE_DIRS:
        path = ensure_dir(root / relative)
        created[relative] = str(path)
    sync_workspace_templates(root, silent=True)
    return created


def _resolved_seed_root(seed_root: Path | None = None) -> Path | None:
    if seed_root is not None:
        return Path(seed_root).expanduser().resolve(strict=False)
    raw = str(os.getenv(RESOURCE_SEED_ROOT_ENV, "") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve(strict=False)


def seed_workspace_resources(workspace: Path, *, seed_root: Path | None = None) -> dict[str, list[str]]:
    root = Path(workspace).resolve()
    source_root = _resolved_seed_root(seed_root)
    copied: dict[str, list[str]] = {"skills": [], "tools": []}
    if source_root is None or not source_root.exists():
        return copied

    for kind in ("skills", "tools"):
        source_dir = source_root / kind
        target_dir = root / kind
        if not source_dir.exists():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        for source_path in source_dir.rglob("*"):
            if not source_path.is_file():
                continue
            relative = source_path.relative_to(source_dir)
            target_path = target_dir / relative
            if target_path.exists():
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            copied[kind].append(relative.as_posix())
    return copied


def auto_unlock_from_env(
    *,
    workspace: Path | None = None,
    security_service: Any | None = None,
) -> str:
    security = security_service or get_bootstrap_security_service(workspace)
    if security.is_unlocked():
        return "already_unlocked"

    master_key = str(os.getenv(BOOTSTRAP_MASTER_KEY_ENV, "") or "").strip()
    if master_key:
        security.activate_with_master_key(master_key=master_key)
        return "master_key"

    password = str(os.getenv(BOOTSTRAP_PASSWORD_ENV, "") or "").strip()
    if password and str((security.status() or {}).get("mode") or "").strip() == "locked":
        security.unlock(password=password)
        return "password"

    return ""
