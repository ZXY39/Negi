"""Registered images per pack: the visibility truth.

The directory says what exists; ``registry.json`` says what the model may see
and send. Files dropped straight into a folder stay invisible until registered
(import commit / admin page / ``register_stickers`` tool), which is what lets
the per-category cap be enforced at write time instead of patched up at render
time.

Identity is the file name: ``<category>/<NNN>.<ext>``. Numbers are minted as
``max(existing) + 1`` and never renumbered, so deleting an image leaves a hole
(a tombstone) and an index can never come to mean a different picture.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.stickers.catalog import IMAGE_SUFFIXES

REGISTRY_NAME = "registry.json"
REGISTRY_SCHEMA_VERSION = 1
MAX_IMAGES_PER_CATEGORY = 36
# 只认恰好三位：真实社区包里大量 13 位时间戳文件名（1790955203428.gif），
# 用 \d{3,} 会把它们误当成"已编号"而保留，索引就永远超出标记语法的 1–3 位。
_INDEX_RE = re.compile(r"^(\d{3})\.")
_RESERVED_DIRS = frozenset({"semantic"})


class RegistryError(ValueError):
    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


def registry_path(pack_dir: Path) -> Path:
    return Path(pack_dir) / REGISTRY_NAME


def read_registry(pack_dir: Path) -> dict[str, Any]:
    """Tolerant read: a missing or malformed registry means "nothing registered"."""
    path = registry_path(pack_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": REGISTRY_SCHEMA_VERSION, "categories": {}}
    if not isinstance(payload, dict):
        return {"schema_version": REGISTRY_SCHEMA_VERSION, "categories": {}}
    categories = payload.get("categories")
    if not isinstance(categories, dict):
        categories = {}
    return {
        "schema_version": int(payload.get("schema_version") or REGISTRY_SCHEMA_VERSION),
        "categories": {
            str(key): dict(rows)
            for key, rows in categories.items()
            if isinstance(rows, dict)
        },
    }


def write_registry(pack_dir: Path, payload: dict[str, Any]) -> None:
    """Atomic write: a crash mid-write must not leave a half registry.

    ``os.replace`` on Windows intermittently fails with WinError 5 while an
    antivirus or indexer holds the destination open; retry briefly before
    surfacing it.
    """
    path = registry_path(pack_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    last: OSError | None = None
    for attempt in range(4):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            if attempt == 3:
                break
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last if last is not None else RuntimeError("registry write failed")


def index_of(filename: str) -> int | None:
    match = _INDEX_RE.match(str(filename or ""))
    if match is None:
        return None
    return int(match.group(1))


def category_rows(pack_dir: Path, category: str) -> dict[str, dict[str, Any]]:
    rows = read_registry(pack_dir)["categories"].get(str(category))
    return dict(rows) if isinstance(rows, dict) else {}


def _inside(pack_dir: Path, path: Path) -> Path | None:
    """Resolve and require the file to stay inside the pack (symlink escape guard)."""
    try:
        root = Path(pack_dir).resolve()
        resolved = Path(path).resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        logger.warning("sticker path {!r} escapes the pack or is gone; ignored", str(path))
        return None
    return resolved


def registered_entries(pack_dir: Path, category: str) -> tuple[tuple[int, Path], ...]:
    """``(index, path)`` pairs ordered by index; same checks as registered_files."""
    category_dir = Path(pack_dir) / str(category)
    rows = category_rows(pack_dir, category)
    found: list[tuple[int, Path]] = []
    for filename, row in rows.items():
        index = index_of(filename)
        if index is None or not isinstance(row, dict):
            continue
        path = category_dir / filename
        try:
            size = path.stat().st_size
        except OSError:
            continue
        expected = row.get("size")
        if isinstance(expected, int) and expected >= 0 and expected != size:
            logger.warning("sticker {} content changed size; needs re-register", path)
            continue
        resolved = _inside(pack_dir, path)
        if resolved is not None and size > 0:
            found.append((index, resolved))
    found.sort(key=lambda item: item[0])
    return tuple(found)


def registered_files(pack_dir: Path, category: str) -> tuple[Path, ...]:
    """Registered images that still exist, ordered by index."""
    return tuple(path for _index, path in registered_entries(pack_dir, category))


def unregistered_files(pack_dir: Path, category: str) -> tuple[Path, ...]:
    category_dir = Path(pack_dir) / str(category)
    rows = category_rows(pack_dir, category)
    if not category_dir.is_dir():
        return ()
    pending: list[Path] = []
    for path in sorted(category_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if path.name in rows:
            continue
        pending.append(path)
    return tuple(pending)


def next_index(pack_dir: Path, category: str) -> int:
    highest = 0
    for filename in category_rows(pack_dir, category):
        index = index_of(filename)
        if index is not None and index > highest:
            highest = index
    return highest + 1


def _free_target(category_dir: Path, index: int, suffix: str) -> tuple[int, Path]:
    """Skip numbers whose file name is already taken (holes are fine, collisions not)."""
    while True:
        target = category_dir / f"{index:03d}{suffix}"
        if not target.exists():
            return index, target
        index += 1


def _row_for(path: Path, payload: bytes) -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _store_rows(pack_dir: Path, category: str, rows: dict[str, dict[str, Any]]) -> None:
    payload = read_registry(pack_dir)
    payload["schema_version"] = REGISTRY_SCHEMA_VERSION
    payload["categories"][str(category)] = rows
    write_registry(pack_dir, payload)


def register_payload(
    pack_dir: Path, category: str, filename: str, payload: bytes
) -> dict[str, Any]:
    """Write one upload under a freshly minted index and record it."""
    category_dir = Path(pack_dir) / str(category)
    category_dir.mkdir(parents=True, exist_ok=True)
    rows = category_rows(pack_dir, str(category))
    if len(rows) >= MAX_IMAGES_PER_CATEGORY:
        raise RegistryError(
            "category_full",
            f"分类 {category} 已满 {MAX_IMAGES_PER_CATEGORY} 张，需先拆分分类",
            detail={"limit": MAX_IMAGES_PER_CATEGORY, "category": str(category)},
        )
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise RegistryError("unsupported_image_type", f"不支持的图片后缀：{filename}")
    index, target = _free_target(category_dir, next_index(pack_dir, str(category)), suffix)
    target.write_bytes(payload)
    rows[target.name] = _row_for(target, payload)
    _store_rows(pack_dir, str(category), rows)
    return {
        "name": target.name,
        "index": index,
        "size": len(payload),
        "category": str(category),
    }


def register_existing(pack_dir: Path, category: str, path: Path) -> dict[str, Any]:
    """Register a file that is already on disk (hand-dropped or migrated).

    Already-numbered names keep their number; everything else is renamed to the
    next free index under a fresh number, so bootstrap is idempotent even if a
    previous run was interrupted halfway.
    """
    path = Path(path)
    category_dir = Path(pack_dir) / str(category)
    rows = category_rows(pack_dir, str(category))
    if path.name in rows:
        return {"name": path.name, "index": index_of(path.name), "skipped": True}
    if len(rows) >= MAX_IMAGES_PER_CATEGORY:
        raise RegistryError(
            "category_full",
            f"分类 {category} 已满 {MAX_IMAGES_PER_CATEGORY} 张，需先拆分分类",
            detail={"limit": MAX_IMAGES_PER_CATEGORY, "category": str(category)},
        )
    payload = path.read_bytes()
    suffix = path.suffix.lower()
    if not _inside(pack_dir, path):
        raise RegistryError("path_escape", f"{path.name} 不在包目录内，拒绝注册")
    if suffix not in IMAGE_SUFFIXES:
        raise RegistryError("unsupported_image_type", f"不支持的图片后缀：{path.name}")
    index = index_of(path.name)
    if index is None:
        index, target = _free_target(
            category_dir, next_index(pack_dir, str(category)), suffix
        )
        path.rename(target)
    else:
        target = path
    rows[target.name] = _row_for(target, payload)
    _store_rows(pack_dir, str(category), rows)
    return {"name": target.name, "index": index, "size": len(payload)}


def unregister_image(pack_dir: Path, category: str, filename: str) -> bool:
    rows = category_rows(pack_dir, category)
    if filename not in rows:
        return False
    rows.pop(filename, None)
    _store_rows(pack_dir, category, rows)
    return True


def drop_category(pack_dir: Path, category: str) -> None:
    payload = read_registry(pack_dir)
    if payload["categories"].pop(str(category), None) is not None:
        write_registry(pack_dir, payload)


def resolve_index(pack_dir: Path, category: str, index: int) -> Path | None:
    """Index → file, with the full hash re-check: a swapped file must not be sent."""
    rows = category_rows(pack_dir, category)
    for filename, row in rows.items():
        if index_of(filename) != int(index) or not isinstance(row, dict):
            continue
        path = Path(pack_dir) / str(category) / filename
        resolved = _inside(pack_dir, path)
        if resolved is None:
            logger.warning("sticker {}/{} is gone or escapes; nothing sent", category, filename)
            return None
        try:
            payload = resolved.read_bytes()
        except OSError:
            logger.warning("sticker {}/{} is gone; nothing sent", category, filename)
            return None
        expected = str(row.get("sha256") or "")
        if expected and hashlib.sha256(payload).hexdigest() != expected:
            logger.warning(
                "sticker {}/{} content changed; nothing sent", category, filename
            )
            return None
        return resolved
    return None


_BOOTSTRAPPED: set[str] = set()


def ensure_bootstrap(pack_dir: Path) -> None:
    """One-time, idempotent migration for packs that predate the registry."""
    root = Path(pack_dir).resolve()
    key = str(root)
    if key in _BOOTSTRAPPED:
        return
    if registry_path(root).is_file():
        _BOOTSTRAPPED.add(key)
        return
    registered = 0
    overflow = 0
    for category_dir in sorted(root.iterdir(), key=lambda item: item.name):
        if not category_dir.is_dir() or category_dir.name in _RESERVED_DIRS:
            continue
        for path in sorted(category_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                register_existing(root, category_dir.name, path)
                registered += 1
            except RegistryError:
                overflow += 1
    if registered or overflow:
        logger.info(
            "sticker registry bootstrap {}: registered={} over_cap={}",
            root.name,
            registered,
            overflow,
        )
    _BOOTSTRAPPED.add(key)


def visible_categories(pack_dir: Path) -> tuple[str, ...]:
    rows = read_registry(pack_dir)["categories"]
    return tuple(sorted(key for key, value in rows.items() if value))


def category_summary(pack_dir: Path) -> dict[str, dict[str, int]]:
    """Per-category counts for the admin page: registered vs pending."""
    summary: dict[str, dict[str, int]] = {}
    root = Path(pack_dir)
    for category_dir in sorted(root.iterdir(), key=lambda item: item.name):
        if not category_dir.is_dir() or category_dir.name in _RESERVED_DIRS:
            continue
        summary[category_dir.name] = {
            "registered": len(category_rows(root, category_dir.name)),
            "pending": len(unregistered_files(root, category_dir.name)),
        }
    return summary


__all__ = [
    "MAX_IMAGES_PER_CATEGORY",
    "REGISTRY_NAME",
    "REGISTRY_SCHEMA_VERSION",
    "RegistryError",
    "category_rows",
    "category_summary",
    "drop_category",
    "ensure_bootstrap",
    "index_of",
    "next_index",
    "read_registry",
    "register_existing",
    "register_payload",
    "registered_entries",
    "registered_files",
    "registry_path",
    "resolve_index",
    "unregister_image",
    "unregistered_files",
    "visible_categories",
    "write_registry",
]
