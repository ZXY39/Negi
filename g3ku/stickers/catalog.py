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

from loguru import logger

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
    """``<workspace>/stickers``.

    Resolved through the live config rather than ``web_ceo_sessions``: importing
    that module pulls the external-session stack, which re-enters
    ``frontdoor.tool_contract`` through ``main.runtime`` and dead-ends in a
    partially initialised module when this is the first import in a process.
    """
    try:
        from g3ku.config.live_runtime import get_runtime_config

        config, _revision, _changed = get_runtime_config()
        return Path(config.workspace_path) / STICKERS_DIRNAME
    except Exception:
        logger.warning("workspace path unavailable; resolving stickers against the cwd")
        return Path.cwd() / STICKERS_DIRNAME


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


def safe_scan_packs() -> tuple[Pack, ...]:
    """``scan_packs()`` that never raises.

    The pinned contract head must assemble even when the sticker directory is
    unreadable: an exception here would take out the whole stable prefix.
    """
    try:
        return scan_packs()
    except Exception:
        logger.warning("sticker catalog scan failed; treating the catalog as empty")
        return ()


def category_owners(packs: Iterable[Pack], category_key: str) -> tuple[Pack, ...]:
    """Packs that expose ``category_key`` -- used for the short-marker rule."""
    return tuple(pack for pack in packs if pack.category(category_key) is not None)


def pack_images(pack: Pack, category_key: str) -> tuple[Path, ...]:
    """Existing image files for one category, sorted for deterministic picks.

    Paths are resolved and required to stay inside the pack directory: a symlink
    planted in a category folder must not be able to hand out a file from
    elsewhere (the channel egress only trusts the sticker root to sit in the
    workspace).
    """
    target = pack.path / category_key
    root = pack.path.resolve()
    images: list[Path] = []
    for path in sorted(_image_paths(target), key=lambda item: item.name):
        try:
            resolved = path.resolve()
            size = resolved.stat().st_size
        except OSError:
            continue
        if resolved.is_relative_to(root) and resolved.is_file() and size > 0:
            images.append(resolved)
    return tuple(images)


def _clip(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    if limit > 0 and len(compact) > limit:
        return compact[: limit - 1].rstrip() + "…"
    return compact


def _flatten(text: str, limit: int) -> str:
    """Single-line, pipe-free: the head line doubles as a parseable section."""
    return _clip(str(text or "").replace("|", "/"), limit)


STICKER_HEADER = (
    "sticker_packs (emit `[[sticker:<pack>/<category>]]` as the last line; "
    "`pack | theme | category(description), ...`):"
)
# 头部与增量通知共用这一份预算判据：两边都按同一个形状渲染，摘要才对得上。
STICKER_SECTION_BUDGET = 2000


def _build_sections(packs: Iterable[Pack], *, include_descriptions: bool) -> dict[str, str]:
    sections: dict[str, str] = {}
    for pack in packs:
        theme = _flatten(pack.theme, 40) or _THEME_PLACEHOLDER
        parts: list[str] = []
        # 分类按 key 排序：头部字节稳定性不能依赖调用方给元组的顺序。
        for category in sorted(pack.categories, key=lambda item: item.key):
            description = _flatten(category.description, 20) if include_descriptions else ""
            parts.append(f"{category.key}({description})" if description else category.key)
        sections[pack.key] = f"{theme} | {', '.join(parts)}"
    return sections


def sticker_pack_sections(packs: Iterable[Pack]) -> dict[str, str]:
    """``{pack_key: "theme | category(description), ..."}`` — the single rendering.

    The pinned head line and the runtime-notice section come from this same
    function, so "the head already says X" and "the current value is X" are byte
    identical without a second renderer. Over budget, descriptions are dropped
    for every pack at once so both sides keep agreeing.
    """
    ordered = tuple(packs)
    sections = _build_sections(ordered, include_descriptions=True)
    if sum(len(key) + len(value) + 8 for key, value in sections.items()) <= STICKER_SECTION_BUDGET:
        return sections
    return _build_sections(ordered, include_descriptions=False)


def render_catalog_lines(packs: Iterable[Pack]) -> tuple[str, ...]:
    """Pinned-contract rendering: one parseable line per pack.

    Counts stay out on purpose: a count changes on every single-file add, which
    would break the byte-stability the pinned block depends on.
    """
    sections = sticker_pack_sections(packs)
    if not sections:
        return ()
    return (STICKER_HEADER, *(f"- `{key}` | {value}" for key, value in sections.items()))


def sticker_sections_from_pinned_text(pinned_text: Any) -> dict[str, str]:
    """Read the pinned head back into ``{pack_key: section_text}``.

    Used to seed the notice ledger with "the head already covers this", so the
    first turn after a reprint does not announce the whole catalog again.
    """
    lines = str(pinned_text or "").splitlines()
    start = -1
    for index, line in enumerate(lines):
        if line.strip().startswith("sticker_packs ("):
            start = index
            break
    if start < 0:
        return {}
    sections: dict[str, str] = {}
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("- `"):
            break
        key_part, separator, value = stripped[2:].partition("` | ")
        if not separator:
            continue
        key = key_part.strip().strip("`").strip()
        if key and PACK_KEY_RE.match(key):
            sections[key] = value.strip()
    return sections


# 运行时合同通知的分节键前缀：一包一节，`record_contract_sections` 按摘要 diff，
# 只有真变化的包会进下一次通知。
STICKER_SECTION_PREFIX = "sticker_pack:"


def sticker_contract_sections(packs: Iterable[Pack]) -> dict[str, str]:
    return {
        f"{STICKER_SECTION_PREFIX}{key}": value
        for key, value in sticker_pack_sections(packs).items()
    }


def sticker_contract_sections_from_text(pinned_text: Any) -> dict[str, str]:
    return {
        f"{STICKER_SECTION_PREFIX}{key}": value
        for key, value in sticker_sections_from_pinned_text(pinned_text).items()
    }


__all__ = [
    "DISABLED_PREFIX",
    "IMAGE_SUFFIXES",
    "MANIFEST_NAME",
    "PACK_KEY_RE",
    "STICKERS_DIRNAME",
    "STICKER_HEADER",
    "STICKER_SECTION_BUDGET",
    "STICKER_SECTION_PREFIX",
    "Pack",
    "StickerCategory",
    "category_owners",
    "find_pack",
    "invalidate_scan_cache",
    "pack_images",
    "render_catalog_lines",
    "safe_scan_packs",
    "scan_packs",
    "sticker_contract_sections",
    "sticker_contract_sections_from_text",
    "sticker_pack_sections",
    "sticker_sections_from_pinned_text",
    "stickers_root",
]
