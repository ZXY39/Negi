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
# 随仓库分发的预置包：资产入库、由升级维护，不属于操作员数据。配置包导入导出
# 都按这个名单排除，否则旧设备导出的旧版本会把新装设备的预置包整个覆盖回去。
# 只看去前缀后的键：`_<key>` 是停用态，仍算同一个预置包。
#
# 名单为空：`stickers/` 整棵树都是操作员数据，仓库不分发任何包。机制保留——只有
# 拿到明确再分发授权的内容资产才能进这个名单，包键写进来即恢复排除语义。
BUILTIN_PACK_KEYS: frozenset[str] = frozenset()
# Sidecar directories that live beside the category folders and must never be
# mistaken for one.
RESERVED_PACK_DIRS = frozenset({"semantic"})
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


def bare_pack_key(name: str) -> str:
    """去掉软删除前缀后的包键：`_foo` 与 `foo` 是同一个包。"""
    raw = str(name or "").strip()
    if raw.startswith(DISABLED_PREFIX):
        return raw[len(DISABLED_PREFIX):]
    return raw


def is_builtin_pack(name: str) -> bool:
    return bare_pack_key(name) in BUILTIN_PACK_KEYS


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


def read_manifest(pack_dir: Path) -> dict[str, Any]:
    manifest_path = pack_dir / MANIFEST_NAME
    try:
        # utf-8-sig：manifest 是给人手改的，Windows 编辑器常给它加 BOM；严格 utf-8
        # 会解析失败，主题与分类描述就整包消失了。
        raw = manifest_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def category_description(manifest: dict[str, Any], key: str) -> str:
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
    manifest = read_manifest(pack_dir)
    categories: list[StickerCategory] = []
    try:
        entries = sorted(pack_dir.iterdir(), key=lambda item: item.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir() or not _is_catalog_name(entry.name):
            continue
        if entry.name in RESERVED_PACK_DIRS:
            continue
        images = _image_paths(entry)
        if not images:
            continue
        categories.append(
            StickerCategory(
                key=entry.name,
                description=category_description(manifest, entry.name),
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


def _clip(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    if limit > 0 and len(compact) > limit:
        return compact[: limit - 1].rstrip() + "…"
    return compact


def _flatten(text: str, limit: int) -> str:
    """Single-line, pipe-free: the head line doubles as a parseable section."""
    return _clip(str(text or "").replace("|", "/"), limit)


# 分类描述在渲染侧不截断：描述是模型选分类的唯一路由信号，兄弟分类的区分词
# （"不好/很好"）常落在句子后半，按字数砍等于每轮静默丢掉路由信息。
# 长度约束只放在写入侧（见 normalize_description），由写入方一次性可见地处理。
CATEGORY_DESCRIPTION_MAX_CHARS = 120

# 注入总量只提醒不删除：超过这个字符数时表情包页出 banner，引导运维停用。
# 钉住头的体积会随库增长线性变大，这条是运营护栏，不是硬闸。
STICKER_INJECTED_CHAR_WARN = 16000


def normalize_description(text: str, *, truncate: bool = True) -> tuple[str, bool]:
    """Compact a category description; ``(value, was_too_long)``.

    所有写路径（导入 commit、管理 API/界面、注册工具）都过这一处，
    ``truncate=False`` 时保留原文并让调用方报错（界面/API），
    ``truncate=True`` 时截到上限并让调用方在报告里列出（批量导入）。
    """
    compact = " ".join(str(text or "").replace("|", "/").split())
    if len(compact) <= CATEGORY_DESCRIPTION_MAX_CHARS:
        return compact, False
    if not truncate:
        return compact, True
    return compact[:CATEGORY_DESCRIPTION_MAX_CHARS], True


STICKER_HEADER = (
    "sticker_packs (emit `[[sticker:<pack>/<category>]]` as the last line; "
    "`pack | theme | category(description), ...`):"
)


def _build_sections(packs: Iterable[Pack]) -> dict[str, str]:
    sections: dict[str, str] = {}
    for pack in packs:
        theme = _flatten(pack.theme, 40) or _THEME_PLACEHOLDER
        parts: list[str] = []
        # 分类按 key 排序：头部字节稳定性不能依赖调用方给元组的顺序。
        for category in sorted(pack.categories, key=lambda item: item.key):
            description = _flatten(category.description, 0)
            parts.append(f"{category.key}({description})" if description else category.key)
        sections[pack.key] = f"{theme} | {', '.join(parts)}"
    return sections


def sticker_pack_sections(packs: Iterable[Pack]) -> dict[str, str]:
    """``{pack_key: "theme | category(description), ..."}`` — the single rendering.

    The pinned head line and the runtime-notice section come from this same
    function, so "the head already says X" and "the current value is X" are byte
    identical without a second renderer. Descriptions are never dropped here:
    the size guardrail is a UI hint at ``STICKER_INJECTED_CHAR_WARN``.
    """
    return _build_sections(tuple(packs))


def injected_chars(packs: Iterable[Pack] | None = None) -> int:
    """Rendered injected size of the catalog, in characters (the 16000 guardrail)."""
    resolved = tuple(packs) if packs is not None else scan_packs()
    sections = sticker_pack_sections(resolved)
    return sum(len(key) + len(value) + 8 for key, value in sections.items())


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
    "BUILTIN_PACK_KEYS",
    "CATEGORY_DESCRIPTION_MAX_CHARS",
    "DISABLED_PREFIX",
    "IMAGE_SUFFIXES",
    "MANIFEST_NAME",
    "PACK_KEY_RE",
    "RESERVED_PACK_DIRS",
    "STICKERS_DIRNAME",
    "STICKER_HEADER",
    "STICKER_INJECTED_CHAR_WARN",
    "STICKER_SECTION_PREFIX",
    "Pack",
    "StickerCategory",
    "bare_pack_key",
    "category_description",
    "category_owners",
    "find_pack",
    "injected_chars",
    "invalidate_scan_cache",
    "is_builtin_pack",
    "normalize_description",
    "read_manifest",
    "render_catalog_lines",
    "safe_scan_packs",
    "scan_packs",
    "sticker_contract_sections",
    "sticker_contract_sections_from_text",
    "sticker_pack_sections",
    "sticker_sections_from_pinned_text",
    "stickers_root",
]
