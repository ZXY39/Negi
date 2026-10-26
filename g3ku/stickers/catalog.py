"""Filesystem-backed sticker catalog.

The directory tree is the only source of truth::

    <workspace>/stickers/<pack>/<category>/<image>

``manifest.json`` is a sidecar that carries the few facts which cannot be
derived (pack theme, category descriptions, import provenance). Counts, the
category list and the pack list are always re-read from disk, so deleting a
file is enough to remove it and there is no second registry to drift.

Anything whose directory name starts with ``_`` is invisible to the catalog:
that is the soft-delete lane (``_review`` for unreviewed imports, ``_atri`` to
park a whole pack without deleting it).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

STICKERS_DIRNAME = "stickers"
MANIFEST_NAME = "manifest.json"
# AstrBot's pack protocol uses the same character set for category keys, and it
# forbids `/` outright -- which is why `/` is our pack/category separator.
PACK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
DISABLED_PREFIX = "_"
# Sidecar directories that live beside the category folders and must never be
# mistaken for one.
_RESERVED_PACK_DIRS = frozenset({"semantic"})
SCAN_TTL_SECONDS = 60.0
_THEME_PLACEHOLDER = "（未设置主题）"


@dataclass(frozen=True)
class StickerCategory:
    key: str
    description: str
    count: int


@dataclass(frozen=True)
class Pack:
    key: str
    theme: str
    categories: tuple[StickerCategory, ...]
    path: Path

    def category(self, key: str) -> StickerCategory | None:
        for item in self.categories:
            if item.key == key:
                return item
        return None


_SCAN_CACHE: dict[str, tuple[float, tuple[Pack, ...]]] = {}


def stickers_root() -> Path:
    """``<workspace>/stickers``; resolved lazily to avoid an import cycle."""
    from g3ku.runtime.web_ceo_sessions import workspace_path

    return workspace_path() / STICKERS_DIRNAME


def invalidate_scan_cache() -> None:
    _SCAN_CACHE.clear()


def _is_catalog_name(name: str) -> bool:
    return (
        bool(PACK_KEY_RE.match(name))
        and not name.startswith(DISABLED_PREFIX)
        and name not in {".", ".."}
    )


def _image_paths(directory: Path) -> list[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return [
        item
        for item in entries
        if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
    ]


def _read_manifest(pack_dir: Path) -> dict[str, Any]:
    manifest_path = pack_dir / MANIFEST_NAME
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _category_description(manifest: dict[str, Any], key: str) -> str:
    categories = manifest.get("categories")
    if not isinstance(categories, dict):
        return ""
    entry = categories.get(key)
    if isinstance(entry, dict):
        return str(entry.get("description") or "").strip()
    if isinstance(entry, str):
        return entry.strip()
    return ""


def _scan_pack(pack_dir: Path) -> Pack | None:
    manifest = _read_manifest(pack_dir)
    categories: list[StickerCategory] = []
    try:
        entries = sorted(pack_dir.iterdir(), key=lambda item: item.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir() or not _is_catalog_name(entry.name):
            continue
        if entry.name in _RESERVED_PACK_DIRS:
            continue
        images = _image_paths(entry)
        if not images:
            continue
        categories.append(
            StickerCategory(
                key=entry.name,
                description=_category_description(manifest, entry.name),
                count=len(images),
            )
        )
    if not categories:
        return None
    theme = str(manifest.get("theme") or "").strip() or _THEME_PLACEHOLDER
    return Pack(key=pack_dir.name, theme=theme, categories=tuple(categories), path=pack_dir)


def scan_packs(
    root: Path | None = None, *, use_cache: bool = True, now: float | None = None
) -> tuple[Pack, ...]:
    """Sorted packs that currently contain at least one usable image."""
    base = Path(root) if root is not None else stickers_root()
    cache_key = str(base)
    clock = time.monotonic() if now is None else float(now)
    if use_cache:
        cached = _SCAN_CACHE.get(cache_key)
        if cached is not None and cached[0] > clock:
            return cached[1]
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name)
    except OSError:
        entries = []
    packs: list[Pack] = []
    for entry in entries:
        if not entry.is_dir() or not _is_catalog_name(entry.name):
            continue
        pack = _scan_pack(entry)
        if pack is not None:
            packs.append(pack)
    result = tuple(packs)
    if use_cache:
        _SCAN_CACHE[cache_key] = (clock + SCAN_TTL_SECONDS, result)
    return result


def find_pack(packs: Iterable[Pack], key: str) -> Pack | None:
    for pack in packs:
        if pack.key == key:
            return pack
    return None


def category_owners(packs: Iterable[Pack], category_key: str) -> tuple[Pack, ...]:
    """Packs that expose ``category_key`` -- used for the short-marker rule."""
    return tuple(pack for pack in packs if pack.category(category_key) is not None)


def pack_images(pack: Pack, category_key: str) -> tuple[Path, ...]:
    """Existing image files for one category, sorted for deterministic picks."""
    target = pack.path / category_key
    return tuple(
        path
        for path in sorted(_image_paths(target), key=lambda item: item.name)
        if path.is_file() and path.stat().st_size > 0
    )


def _clip(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    if limit > 0 and len(compact) > limit:
        return compact[: limit - 1].rstrip() + "…"
    return compact


def render_catalog_lines(packs: Iterable[Pack], *, description_chars: int = 20) -> tuple[str, ...]:
    """The pinned-contract rendering: pack line, then indented category lines.

    Counts stay out on purpose: a count changes on every single-file add, which
    would break the byte-stability the pinned block depends on.
    """
    lines: list[str] = []
    for pack in packs:
        lines.append(f"- `{pack.key}` | {pack.theme}")
        for category in pack.categories:
            description = _clip(category.description, description_chars)
            if description:
                lines.append(f"  - `{category.key}` | {description}")
            else:
                lines.append(f"  - `{category.key}`")
    return tuple(lines)


__all__ = [
    "DISABLED_PREFIX",
    "IMAGE_SUFFIXES",
    "MANIFEST_NAME",
    "PACK_KEY_RE",
    "STICKERS_DIRNAME",
    "Pack",
    "StickerCategory",
    "category_owners",
    "find_pack",
    "invalidate_scan_cache",
    "pack_images",
    "render_catalog_lines",
    "scan_packs",
    "stickers_root",
]
