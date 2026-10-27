"""Operator-facing sticker administration.

``catalog`` answers "what may the model use"; this module answers "what is on
disk" -- including the ``_``-prefixed packs and categories the catalog hides.
Every mutation is a plain filesystem edit against the same tree an agent edits
with its file tools, so there is no second registry to drift: a change reaches
the model the next time ``invalidate_scan_cache()`` has run and the turn's
contract-notice diff re-reads the per-pack sections.

All resolvers are fail-closed. A caller-supplied name is checked against the
same key rules the catalog scans with, then the joined path is resolved and
required to stay under the sticker root, so ``..``, absolute names and symlinks
pointing elsewhere are refused rather than followed.

Scans here are deliberately uncached: the admin surface must show the truth the
instant a mutation returns, and a 60-second stale row reads as a failed write.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

from g3ku.stickers.catalog import (
    CATEGORY_DESCRIPTION_MAX_CHARS,
    DISABLED_PREFIX,
    IMAGE_SUFFIXES,
    MANIFEST_NAME,
    PACK_KEY_RE,
    RESERVED_PACK_DIRS,
    category_description,
    invalidate_scan_cache,
    normalize_description,
    read_manifest,
    stickers_root,
)
from g3ku.utils.helpers import safe_filename

STICKER_IMAGE_ROUTE = "/api/resources/stickers/image"
# 与 filesystem 工具的 managed-artifact 启发式同名：这些目录名做包/分类会被工具层拦。
# 这份名单与 skills/meme/scripts/import_pack.py 的同名常量必须一致（有测试比对），
# 否则界面能建出 agent 建不出的目录。
FORBIDDEN_NAMES = frozenset(
    {"build", "dist", "node_modules", ".venv", "venv", "site-packages", "__pycache__"}
)
FORBIDDEN_PREFIXES = ("tmp_", "temp_", ".tmp_")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_FILES = 20
THEME_MAX_CHARS = 60
# 描述上限与清洗口径的唯一归属地是 catalog（导入/界面/注册三条写路径共用）；
# 这里只是转出给 API 回包与测试用。


class StickerAdminError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = str(code or '').strip() or 'sticker_admin_error'
        self.message = str(message or '').strip()
        self.status_code = int(status_code)
        self.detail = dict(detail or {})

    def payload(self) -> dict[str, Any]:
        return {'code': self.code, 'message': self.message, **self.detail}


@dataclass(frozen=True)
class AdminImage:
    name: str
    size: int
    mtime: float


@dataclass(frozen=True)
class AdminCategory:
    dir_name: str
    key: str
    description: str
    enabled: bool
    count: int
    images: tuple[AdminImage, ...] = ()


@dataclass(frozen=True)
class AdminPack:
    dir_name: str
    key: str
    theme: str
    theme_source: str
    source: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    categories: tuple[AdminCategory, ...] = ()
    path: Path | None = None

    @property
    def total(self) -> int:
        return sum(item.count for item in self.categories)


def _root_path(root: Path | None) -> Path:
    return Path(root) if root is not None else stickers_root()


def _root_resolved(root: Path | None) -> Path:
    return _root_path(root).resolve(strict=False)


def display_key(name: str) -> str:
    return name[len(DISABLED_PREFIX):] if name.startswith(DISABLED_PREFIX) else name


def admin_segment_ok(name: str) -> bool:
    """Legal pack/category directory name for the admin surface.

    The ``_`` prefix is part of the name -- it *is* the soft-delete marker -- so
    the key rules apply to the stripped key: ``_atri`` is legal, ``__`` is not.
    """
    raw = str(name or '').strip()
    if not raw or raw in {'.', '..'} or '/' in raw or '\\' in raw:
        return False
    key = display_key(raw)
    if not PACK_KEY_RE.match(key):
        return False
    if key in RESERVED_PACK_DIRS or key in FORBIDDEN_NAMES:
        return False
    return not key.startswith(FORBIDDEN_PREFIXES)


def _child(base: Path, name: str) -> Path:
    if not admin_segment_ok(name):
        raise StickerAdminError('invalid_sticker_name', f'不合法的名称：{name}')
    candidate = (base / str(name).strip()).resolve(strict=False)
    if not candidate.is_relative_to(base):
        raise StickerAdminError('sticker_path_escaped', f'路径越出表情包根目录：{name}')
    return candidate


def pack_dir(pack_dir_name: str, *, root: Path | None = None) -> Path:
    path = _child(_root_resolved(root), pack_dir_name)
    if not path.is_dir():
        raise StickerAdminError('pack_not_found', f'表情包不存在：{pack_dir_name}', status_code=404)
    return path


def category_dir(
    pack_dir_name: str, category_dir_name: str, *, root: Path | None = None
) -> Path:
    pack = pack_dir(pack_dir_name, root=root)
    path = _child(pack, category_dir_name)
    if not path.is_dir():
        raise StickerAdminError(
            'category_not_found', f'分类不存在：{category_dir_name}', status_code=404
        )
    return path


def image_path(
    pack_dir_name: str, category_dir_name: str, name: str, *, root: Path | None = None
) -> Path:
    category = category_dir(pack_dir_name, category_dir_name, root=root)
    raw = str(name or '').strip()
    if not raw or raw in {'.', '..'} or '/' in raw or '\\' in raw:
        raise StickerAdminError('invalid_sticker_name', f'不合法的文件名：{name}')
    path = (category / raw).resolve(strict=False)
    if not path.is_relative_to(category) or not path.is_file():
        raise StickerAdminError('image_not_found', f'图片不存在：{name}', status_code=404)
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise StickerAdminError('unsupported_image_type', f'不支持的图片后缀：{name}')
    return path


def sticker_image_url(pack_dir_name: str, category_dir_name: str, name: str) -> str:
    query = urlencode(
        {'pack': pack_dir_name, 'category': category_dir_name, 'name': name}
    )
    return f'{STICKER_IMAGE_ROUTE}?{query}'


def _image_names(directory: Path) -> list[str]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(
        item.name
        for item in entries
        if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
    )


def _all_file_count(directory: Path) -> int:
    try:
        return sum(1 for item in directory.iterdir() if item.is_file())
    except OSError:
        return 0


def _image_entries(
    directory: Path, names: list[str], *, per_category: int | None
) -> tuple[AdminImage, ...]:
    take = names if per_category is None else names[: max(0, int(per_category))]
    entries: list[AdminImage] = []
    for name in take:
        try:
            stat = (directory / name).stat()
        except OSError:
            continue
        entries.append(
            AdminImage(name=name, size=int(stat.st_size), mtime=float(stat.st_mtime))
        )
    return tuple(entries)


def _scan_pack_entry(
    entry: Path, base: Path, *, per_category: int | None
) -> AdminPack | None:
    try:
        resolved = entry.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(base):
        return None
    manifest = read_manifest(resolved)
    categories: list[AdminCategory] = []
    try:
        children = sorted(resolved.iterdir(), key=lambda item: item.name)
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or not admin_segment_ok(child.name):
            continue
        try:
            if not child.resolve().is_relative_to(resolved):
                continue
        except OSError:
            continue
        key = display_key(child.name)
        names = _image_names(child)
        categories.append(
            AdminCategory(
                dir_name=child.name,
                key=key,
                description=category_description(manifest, key),
                enabled=not child.name.startswith(DISABLED_PREFIX),
                count=len(names),
                images=_image_entries(child, names, per_category=per_category),
            )
        )
    source = manifest.get('source')
    return AdminPack(
        dir_name=entry.name,
        key=display_key(entry.name),
        theme=str(manifest.get('theme') or '').strip(),
        theme_source=str(manifest.get('theme_source') or '').strip(),
        source=dict(source) if isinstance(source, dict) else {},
        enabled=not entry.name.startswith(DISABLED_PREFIX),
        # 启用的分类排在停用的前面：ASCII 里 `_` 比字母小，不排的话一个带 8 个
        # 隐藏分类的包，第一屏全是被停用的。
        categories=tuple(sorted(categories, key=lambda item: (not item.enabled, item.key))),
        path=resolved,
    )


def scan_admin_packs(
    *, root: Path | None = None, per_category: int | None = None
) -> tuple[AdminPack, ...]:
    """Every pack on disk, soft-deleted ones included.

    ``per_category`` caps the returned image rows (``0`` returns counts only);
    ``count`` always reports the true total, so a capped payload still tells the
    operator how many images the category holds.
    """
    base = _root_resolved(root)
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name)
    except OSError:
        return ()
    packs: list[AdminPack] = []
    for entry in entries:
        if not entry.is_dir() or not admin_segment_ok(entry.name):
            continue
        pack = _scan_pack_entry(entry, base, per_category=per_category)
        if pack is not None:
            packs.append(pack)
    return tuple(packs)


def read_admin_pack(
    pack_dir_name: str, *, root: Path | None = None, per_category: int | None = None
) -> AdminPack:
    base = _root_resolved(root)
    path = pack_dir(pack_dir_name, root=root)
    pack = _scan_pack_entry(path, base, per_category=per_category)
    if pack is None:
        raise StickerAdminError('pack_not_found', f'表情包不存在：{pack_dir_name}', status_code=404)
    return pack


def list_category_images(
    pack_dir_name: str,
    category_dir_name: str,
    *,
    offset: int = 0,
    limit: int = 60,
    root: Path | None = None,
) -> tuple[list[AdminImage], int]:
    category = category_dir(pack_dir_name, category_dir_name, root=root)
    names = _image_names(category)
    start = max(0, int(offset))
    window = names[start : start + max(0, int(limit))]
    return list(_image_entries(category, window, per_category=None)), len(names)


def _write_manifest(pack: Path, manifest: dict[str, Any]) -> None:
    target = pack / MANIFEST_NAME
    tmp = pack / f'.{MANIFEST_NAME}.tmp'
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    os.replace(tmp, target)


def _clean_text(raw: Any) -> str:
    return ' '.join(str(raw or '').split())


def set_theme(
    pack_dir_name: str, theme: Any, *, root: Path | None = None
) -> dict[str, Any]:
    pack = pack_dir(pack_dir_name, root=root)
    text = _clean_text(theme)
    if len(text) > THEME_MAX_CHARS:
        raise StickerAdminError(
            'theme_too_long', f'主题最多 {THEME_MAX_CHARS} 字', detail={'limit': THEME_MAX_CHARS}
        )
    manifest = read_manifest(pack)
    if text:
        manifest['theme'] = text
        # 界面写主题与人工定稿同义：导入脚本据此不再覆盖。
        manifest['theme_source'] = 'human'
    else:
        # 清空主题等于"未设置"：连 theme_source 一起撤掉，否则 human 定稿会让
        # 导入脚本永远拒绝回填一个不存在的主题。
        manifest.pop('theme', None)
        manifest.pop('theme_source', None)
    _write_manifest(pack, manifest)
    invalidate_scan_cache()
    return manifest


def set_category_description(
    pack_dir_name: str,
    category_dir_name: str,
    description: Any,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    pack = pack_dir(pack_dir_name, root=root)
    category = category_dir(pack_dir_name, category_dir_name, root=root)
    key = display_key(category.name)
    text, too_long = normalize_description(_clean_text(description), truncate=False)
    if too_long:
        raise StickerAdminError(
            'description_too_long',
            f'分类描述最多 {CATEGORY_DESCRIPTION_MAX_CHARS} 字',
            detail={'limit': CATEGORY_DESCRIPTION_MAX_CHARS},
        )
    manifest = read_manifest(pack)
    categories = manifest.get('categories')
    if not isinstance(categories, dict):
        categories = {}
    existing = categories.get(key)
    row = dict(existing) if isinstance(existing, dict) else {}
    if not isinstance(existing, dict) and isinstance(existing, str) and existing.strip():
        row['description'] = existing.strip()
    if text:
        row['description'] = text
    else:
        row.pop('description', None)
    if row:
        categories[key] = row
    else:
        categories.pop(key, None)
    if categories:
        manifest['categories'] = categories
    else:
        manifest.pop('categories', None)
    _write_manifest(pack, manifest)
    invalidate_scan_cache()
    return manifest


def _rename_flag(base: Path, path: Path, enabled: bool) -> str:
    name = path.name
    target_name = display_key(name) if enabled else f'{DISABLED_PREFIX}{display_key(name)}'
    if target_name == name:
        return name
    target = base / target_name
    if not target.resolve(strict=False).is_relative_to(base):
        raise StickerAdminError('sticker_path_escaped', f'路径越出表情包根目录：{target_name}')
    if target.exists():
        raise StickerAdminError(
            'sticker_name_conflict', f'{target_name} 已存在，先处理重名', status_code=409
        )
    path.rename(target)
    invalidate_scan_cache()
    return target_name


def set_enabled(
    pack_dir_name: str, enabled: bool, *, root: Path | None = None
) -> str:
    base = _root_resolved(root)
    return _rename_flag(base, pack_dir(pack_dir_name, root=root), bool(enabled))


def set_category_enabled(
    pack_dir_name: str,
    category_dir_name: str,
    enabled: bool,
    *,
    root: Path | None = None,
) -> str:
    pack = pack_dir(pack_dir_name, root=root)
    category = category_dir(pack_dir_name, category_dir_name, root=root)
    return _rename_flag(pack, category, bool(enabled))


def create_category(
    pack_dir_name: str,
    key: Any,
    description: Any = '',
    *,
    root: Path | None = None,
) -> str:
    pack = pack_dir(pack_dir_name, root=root)
    raw = str(key or '').strip()
    if not admin_segment_ok(raw) or raw.startswith(DISABLED_PREFIX):
        raise StickerAdminError(
            'invalid_sticker_name',
            '分类名只能是小写/大写字母、数字、点、下划线、连字符，且不能以下划线开头',
        )
    target = _child(pack, raw)
    if target.exists():
        raise StickerAdminError('sticker_name_conflict', f'{raw} 已存在', status_code=409)
    target.mkdir(parents=True)
    text = _clean_text(description)
    if text:
        set_category_description(pack_dir_name, raw, text, root=root)
    else:
        invalidate_scan_cache()
    return raw


def _target_name(
    directory: Path, name: str, suffix: str, *, reserved: set[str] | None = None
) -> str:
    stem = name[: -len(suffix)] if suffix else name
    taken = reserved or set()
    candidate = f'{stem}{suffix}'
    index = 2
    while (directory / candidate).exists() or candidate in taken:
        candidate = f'{stem}_{index}{suffix}'
        index += 1
        if index > 999:
            raise StickerAdminError('sticker_name_conflict', f'{name} 重名太多')
    return candidate


def store_uploads(
    pack_dir_name: str,
    category_dir_name: str,
    uploads: Iterable[tuple[str, bytes]],
    *,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Validate the whole batch before writing any byte.

    A rejected 3rd file must not leave the first two on disk: the operator sees
    one failure toast and would have no idea half the batch landed.
    """
    category = category_dir(pack_dir_name, category_dir_name, root=root)
    items = list(uploads)
    if not items:
        raise StickerAdminError('no_files_uploaded', '没有收到文件')
    if len(items) > MAX_UPLOAD_FILES:
        raise StickerAdminError(
            'too_many_files',
            f'一次最多上传 {MAX_UPLOAD_FILES} 张',
            detail={'limit': MAX_UPLOAD_FILES},
        )
    planned: list[tuple[str, bytes]] = []
    taken: set[str] = set()
    for raw_name, payload in items:
        base_name = safe_filename(Path(str(raw_name or '')).name) or 'image'
        suffix = Path(base_name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise StickerAdminError(
                'unsupported_image_type',
                f'不支持的图片后缀：{base_name}',
                detail={'allowed': sorted(IMAGE_SUFFIXES)},
            )
        if len(payload) > MAX_IMAGE_BYTES:
            raise StickerAdminError(
                'image_too_large',
                f'{base_name} 超过 20 MiB',
                status_code=413,
                detail={'limit': MAX_IMAGE_BYTES},
            )
        if not payload:
            raise StickerAdminError('empty_file', f'{base_name} 是空文件')
        name = _target_name(category, base_name, suffix, reserved=taken)
        taken.add(name)
        planned.append((name, payload))
    for name, payload in planned:
        (category / name).write_bytes(payload)
    invalidate_scan_cache()
    return [
        {
            'name': name,
            'size': len(payload),
            'url': sticker_image_url(pack_dir_name, category_dir_name, name),
        }
        for name, payload in planned
    ]


def _prune_manifest_category(pack: Path, key: str) -> None:
    manifest = read_manifest(pack)
    categories = manifest.get('categories')
    if not isinstance(categories, dict) or key not in categories:
        return
    categories.pop(key, None)
    if categories:
        manifest['categories'] = categories
    else:
        manifest.pop('categories', None)
    _write_manifest(pack, manifest)


def delete_image(
    pack_dir_name: str, category_dir_name: str, name: str, *, root: Path | None = None
) -> None:
    image_path(pack_dir_name, category_dir_name, name, root=root).unlink()
    invalidate_scan_cache()


def delete_category(
    pack_dir_name: str, category_dir_name: str, *, root: Path | None = None
) -> dict[str, int]:
    pack = pack_dir(pack_dir_name, root=root)
    category = category_dir(pack_dir_name, category_dir_name, root=root)
    key = display_key(category.name)
    images = len(_image_names(category))
    files = _all_file_count(category)
    shutil.rmtree(category)
    _prune_manifest_category(pack, key)
    invalidate_scan_cache()
    return {'images': images, 'files': files}


def delete_pack(pack_dir_name: str, *, root: Path | None = None) -> dict[str, int]:
    pack = pack_dir(pack_dir_name, root=root)
    images = 0
    files = 0
    for child in pack.iterdir():
        if not child.is_dir() or not admin_segment_ok(child.name):
            continue
        images += len(_image_names(child))
        files += _all_file_count(child)
    shutil.rmtree(pack)
    invalidate_scan_cache()
    return {'images': images, 'files': files}


__all__ = [
    "CATEGORY_DESCRIPTION_MAX_CHARS",
    "FORBIDDEN_NAMES",
    "FORBIDDEN_PREFIXES",
    "MAX_IMAGE_BYTES",
    "MAX_UPLOAD_FILES",
    "STICKER_IMAGE_ROUTE",
    "THEME_MAX_CHARS",
    "AdminCategory",
    "AdminImage",
    "AdminPack",
    "StickerAdminError",
    "admin_segment_ok",
    "category_dir",
    "create_category",
    "delete_category",
    "delete_image",
    "delete_pack",
    "display_key",
    "image_path",
    "list_category_images",
    "pack_dir",
    "read_admin_pack",
    "scan_admin_packs",
    "set_category_description",
    "set_category_enabled",
    "set_enabled",
    "set_theme",
    "sticker_image_url",
    "store_uploads",
]
