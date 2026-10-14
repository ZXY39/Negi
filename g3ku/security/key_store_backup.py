"""Rolling backups and an evidence trail for the project's own key material.

The credential store is a handful of small files under `.g3ku/`: the master-key envelope, the
auto-unlock copy, the model-config index, its record files and the secret realm overlays. None
of it is in git, none of it has a volume snapshot on a typical box, and losing it makes a
running project look exactly like a brand-new one in the UI. When that happened once there was
no copy to roll back to and no record of when the files disappeared, so the loss could not even
be attributed to a moment in time.

This module keeps the last few generations of those files and appends one line per event, so
the next occurrence is answerable from disk: when the material went away, what was configured
before, and which copy to restore.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
from pathlib import Path
from typing import Any

BACKUP_DIR_NAME = "llm-config-backups"
EVENT_LOG_NAME = "security-events.jsonl"
KEEP_GENERATIONS = 12

# Relative to the workspace root. The list is deliberately literal: a missing entry is an
# event, so it must not be derived from whatever happens to exist at the time.
KEY_STORE_PATHS = (
    ".g3ku/llm-config/master.key",
    ".g3ku/llm-config/auto-unlock.key",
    ".g3ku/llm-config/index.json",
    ".g3ku/llm-config/records",
    ".g3ku/secret-realms",
)
# Losing either of these means the store can no longer be opened, so their absence is a finding
# even while other files of the store are still on disk.
LOSS_EVIDENCE_PATHS = (".g3ku/llm-config/master.key",)


def backup_root(workspace: Path) -> Path:
    return Path(workspace) / ".g3ku" / BACKUP_DIR_NAME


def event_log_path(workspace: Path) -> Path:
    return Path(workspace) / ".g3ku" / EVENT_LOG_NAME


def record_event(workspace: Path, event: str, **fields: Any) -> None:
    """Append one evidence line. Best-effort: a failed log must not block a key operation."""
    path = event_log_path(workspace)
    payload = {
        "at": datetime.datetime.now(datetime.UTC).isoformat(),
        "event": str(event or "").strip(),
        "pid": int(os.getpid()),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(workspace: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    path = event_log_path(workspace)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, int(limit)):]
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            items.append(parsed)
    return items


def configured_key_material(workspace: Path) -> dict[str, list[str]]:
    """What key material is on disk right now, split into present, empty and missing.

    An empty directory or a zero-byte file counts as *not* material: a wipe can leave the
    husk of `records/` behind, and one ENOSPC leaves a 0-byte envelope that decrypts to
    nothing. Both have to read as damage rather than as an intact store.
    """
    root = Path(workspace)
    present: list[str] = []
    empty: list[str] = []
    missing: list[str] = []
    for relative in KEY_STORE_PATHS:
        path = root / relative
        if path.is_dir():
            if any(child.is_file() and child.stat().st_size > 0 for child in path.rglob("*")):
                present.append(relative)
            elif any(child.is_file() for child in path.rglob("*")):
                empty.append(relative)
            else:
                missing.append(relative)
        elif path.exists():
            if path.stat().st_size > 0:
                present.append(relative)
            else:
                empty.append(relative)
        else:
            missing.append(relative)
    return {"present": present, "empty": empty, "missing": missing}


def holds_material(workspace: Path, relative: str) -> bool:
    """Whether that path exists with at least one non-empty file behind it."""
    path = Path(workspace) / relative
    if path.is_dir():
        return any(child.is_file() and child.stat().st_size > 0 for child in path.rglob("*"))
    return path.is_file() and path.stat().st_size > 0


def store_is_worth_keeping(workspace: Path) -> bool:
    """Whether a snapshot of this store deserves a generation.

    Snapshotting a half-present store (an empty index or a 0-byte envelope left behind by a
    wipe) would push the good generations out of the retention window, so the test is
    deliberately strict: a non-empty envelope, a non-empty record file, or a non-empty realm
    overlay.
    """
    root = Path(workspace)
    if any(
        path.is_file() and path.stat().st_size > 0
        for path in (root / ".g3ku" / "llm-config" / "records").glob("*")
    ):
        return True
    if any(
        path.is_file() and path.stat().st_size > 0
        for path in (root / ".g3ku" / "secret-realms").glob("*")
    ):
        return True
    master_key = root / ".g3ku" / "llm-config" / "master.key"
    return master_key.is_file() and master_key.stat().st_size > 0


def store_was_configured(workspace: Path) -> bool:
    """True when usable key material exists now, or when its remains prove it used to.

    A fresh project must stay quiet, but a zero-length envelope is not a fresh project: that
    file is only ever written by an initialization, so its presence - even damaged - is the
    evidence that something was configured and is now unreadable.
    """
    root = Path(workspace)
    if store_is_worth_keeping(root):
        return True
    if any((root / relative).exists() for relative in LOSS_EVIDENCE_PATHS):
        return True
    for event in read_events(root, limit=400):
        if str(event.get("event") or "") == "key_store_backed_up" and int(event.get("files") or 0) > 0:
            return True
    return False


def _material_signature(workspace: Path) -> dict[str, int]:
    root = Path(workspace)
    signature: dict[str, int] = {}
    for relative in configured_key_material(root)["present"]:
        path = root / relative
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    signature[str(child.relative_to(root)).replace("\\", "/")] = child.stat().st_size
        elif path.is_file():
            signature[relative] = path.stat().st_size
    return signature


def snapshot_key_store(workspace: Path, *, reason: str, force: bool = False) -> Path | None:
    """Copy the current key material into a timestamped generation; None when not worth it.

    Skipping a generation whose file sizes already match the newest one keeps a process that
    restarts every few minutes from cycling the good copies out of the retention window. Size
    equality is a cheap tripwire, not a content hash: an accidental wipe changes it, and
    `force` is there for the operator who wants a copy regardless.
    """
    root = Path(workspace)
    if not store_is_worth_keeping(root):
        return None
    signature = _material_signature(root)
    if not signature:
        return None
    if not force:
        for existing in list_backups(root)[:1]:
            existing_signature = {
                str(path.relative_to(existing)).replace("\\", "/"): path.stat().st_size
                for path in sorted(existing.rglob("*"))
                if path.is_file()
            }
            if existing_signature == signature:
                return existing
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = backup_root(root) / stamp
    copied = 0
    for relative in configured_key_material(root)["present"]:
        source = root / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
            copied += sum(1 for child in destination.rglob("*") if child.is_file())
        else:
            shutil.copy2(source, destination)
            copied += 1
    if not copied:
        return None
    record_event(root, "key_store_backed_up", reason=str(reason or ""), files=copied, generation=stamp)
    prune_backups(root)
    return target


def list_backups(workspace: Path) -> list[Path]:
    root = backup_root(workspace)
    if not root.exists():
        return []
    generations = [path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")]
    return sorted(generations, key=lambda path: path.name, reverse=True)


def prune_backups(workspace: Path) -> int:
    """Drop generations older than KEEP_GENERATIONS - unless the live store is unusable.

    Every generation is created from a store worth keeping, so the newest few normally always
    hold a usable copy. That assumption is exactly wrong while the live envelope is missing or
    0 bytes: there, no generation is redundant, and pruning would be the step that turns a
    recoverable incident into a permanent one.
    """
    root = Path(workspace)
    if not holds_material(root, ".g3ku/llm-config/master.key"):
        return 0
    removed = 0
    for path in list_backups(root)[KEEP_GENERATIONS:]:
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError:
            continue
    return removed


def audit_key_store_continuity(workspace: Path, *, reason: str = "boot") -> dict[str, Any]:
    """One entry per process: back the store up, or report and record a loss.

    Returns a small finding dict for the caller to surface. It never restores: replacing key
    material is an operator decision, and a boot-time write into the credential store is
    exactly the surprise that turns a partial loss into a total one.
    """
    root = Path(workspace)
    generations = list_backups(root)
    lost = [relative for relative in LOSS_EVIDENCE_PATHS if not holds_material(root, relative)]
    if lost and store_was_configured(root):
        record_event(root, "key_store_missing_material", reason=reason, missing=lost, backups=len(generations))
        return {
            "state": "lost",
            "missing": lost,
            "backups": len(generations),
            "newest_backup": str(generations[0]) if generations else "",
        }
    if store_is_worth_keeping(root):
        snapshot_key_store(root, reason=reason)
        return {"state": "ok", "backups": len(list_backups(root))}
    return {"state": "unconfigured", "backups": len(generations)}


def restore_from_backup(workspace: Path, generation: Path | str, *, dry_run: bool = True) -> dict[str, Any]:
    """Copy one generation's key material back. Refuses to overwrite a store that has a master key.

    The refusal is the point: a wrong restore must not silently replace a working envelope, and
    the operator who means to do it can move the live file aside in one step.
    """
    root = Path(workspace)
    source = Path(generation)
    if not source.is_absolute():
        source = backup_root(root) / str(generation)
    if not source.is_dir():
        raise FileNotFoundError(f"no such key store backup: {source}")
    if not dry_run and holds_material(root, ".g3ku/llm-config/master.key"):
        raise ValueError("refusing to overwrite an existing master.key; move it aside first")
    restored: list[str] = []
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative = path.relative_to(source)
        destination = root / relative
        name = str(relative).replace("\\", "/")
        if dry_run:
            restored.append(name)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        restored.append(name)
    if not dry_run:
        record_event(root, "key_store_restored", from_generation=source.name, files=len(restored))
    return {"ok": True, "dry_run": bool(dry_run), "source": str(source), "files": restored}
