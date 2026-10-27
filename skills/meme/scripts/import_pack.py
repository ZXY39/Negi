#!/usr/bin/env python3
"""Import an external sticker pack into ``<workspace>/stickers``.

The script owns only the deterministic half of an import: materialising the
source (directory / zip / git), shape detection, path safety, suffix whitelist,
size cap, sha256 dedupe, idempotent merge, manifest write, and a short report.

The two judgement calls stay with the agent, because it is the multimodal model
in this loop:

- ``--theme``: one line describing what the pack *is*. Without it the pack
  renders as "（未设置主题）" in the pinned contract head, and the model has no
  way to pick it by style. The report hands back 8 stratified sample images so
  the agent can look before deciding.
- ``--classify-map``: a JSON ``{relative_path: category}`` mapping for flat
  piles. Anything unmapped lands in ``_review/``, which is invisible to the
  catalog until someone files it.

Usage (run from the workspace root)::

    python skills/meme/scripts/import_pack.py <src> --dry-run
    python skills/meme/scripts/import_pack.py <src> --theme "一句话主题"
    python skills/meme/scripts/import_pack.py <src> --pack atri --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SLUG_RE = re.compile(r"[^a-z0-9._-]+")
# 零宽 / 方向控制字符：手工编辑过的 manifest 常混进去（实测一个 AstrBot 包的三个
# 分类键前面带 ZWNJ），留在键里会让描述按目录名查不到，目录名肉眼也看不出差别。
INVISIBLE_CHARS_RE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
DISABLED_PREFIX = "_"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
# 与 filesystem 工具的 managed-artifact 启发式同名：这些目录名做包/分类会被工具层拦。
FORBIDDEN_NAMES = frozenset(
    {"build", "dist", "node_modules", ".venv", "venv", "site-packages", "__pycache__"}
)
FORBIDDEN_PREFIXES = ("tmp_", "temp_", ".tmp_")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
REVIEW_CATEGORY = "_review"
RESERVED_PACK_DIRS = frozenset({"semantic"})
SAMPLES_DIRNAME = ".import"
THEME_SAMPLE_COUNT = 8
THEME_MAX_CHARS = 60
DEFAULT_DESCRIPTION = ""


@dataclass
class Source:
    kind: str  # astrbot | nested | flat
    root: Path
    pack_id: str = ""
    version: str = ""
    url: str = ""
    commit: str = ""
    categories: dict[str, list[Path]] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    previews: int = 0
    semantic_meta: Path | None = None


@dataclass
class Plan:
    actions: list[tuple[Path, str, str]] = field(default_factory=list)  # (src, category, name)
    skipped_suffix: int = 0
    skipped_size: int = 0
    skipped_unsafe: int = 0
    duplicates: int = 0
    review: int = 0


def slug_key(raw: str, *, salt: str = "") -> str:
    """ASCII key for a directory name; unrepresentable names get a stable hash.

    Names that are already legal keys are returned untouched: AstrBot category
    keys must not be silently lowercased or rewritten. A leading ``_`` survives,
    because AstrBot uses it for "special scene" categories and Negi uses the same
    prefix for the hidden lane -- slugging it away would quietly publish images
    the pack author kept out of the normal rotation.
    """
    original = INVISIBLE_CHARS_RE.sub("", str(raw or "")).strip()
    hidden = original.startswith(DISABLED_PREFIX)
    body = original[len(DISABLED_PREFIX):] if hidden else original
    if (
        KEY_RE.match(body)
        and body not in FORBIDDEN_NAMES
        and not body.startswith(FORBIDDEN_PREFIXES)
    ):
        return f"{DISABLED_PREFIX}{body}" if hidden else body
    candidate = SLUG_RE.sub("-", body.lower()).strip("-._")
    if candidate and not candidate[0].isalnum():
        candidate = candidate.lstrip("-._")
    if not candidate or not KEY_RE.match(candidate):
        digest = hashlib.sha1(f"{original}|{salt}".encode("utf-8")).hexdigest()[:8]
        candidate = f"cat-{digest}"
    if candidate in FORBIDDEN_NAMES or candidate.startswith(FORBIDDEN_PREFIXES):
        candidate = f"x-{candidate}"
    candidate = candidate[:63]
    return f"{DISABLED_PREFIX}{candidate}" if hidden else candidate


def display_key(key: str) -> str:
    """Manifest keys are the display keys: the catalog and the admin page look a
    category's description up by its Stripped name, so ``_kg/`` is described
    under ``kg``."""
    return key[len(DISABLED_PREFIX):] if key.startswith(DISABLED_PREFIX) else key


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _images_in(directory: Path) -> list[Path]:
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    return [item for item in entries if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES]


def _inside(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _safe_zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    safe: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        raw = info.filename.replace("\\", "/")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            continue
        parts = Path(raw).parts
        if ".." in parts:
            continue
        safe.append(info)
    return safe


def _extract_zip(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for info in _safe_zip_members(archive):
            if info.is_dir():
                continue
            target = destination / info.filename.replace("\\", "/")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as reader, target.open("wb") as writer:
                shutil.copyfileobj(reader, writer)


def _clone(url: str, destination: Path) -> str:
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(destination)],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("import_pack: git 不可用，无法从 URL 导入；请先下载成 zip 或目录") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SystemExit(f"import_pack: git clone 失败：{message[-1] if message else exc}") from exc
    try:
        result = subprocess.run(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
        )
        return result.stdout.decode("ascii", "replace").strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


def materialize(src: str, workspace: Path) -> tuple[Path, str, str, str, Path | None]:
    """Return (source_root, origin_kind, url, commit, temp_dir_to_clean)."""
    stripped = str(src or "").strip()
    if not stripped:
        raise SystemExit("import_pack: 需要 <src>")
    if re.match(r"^(https://|git@)", stripped):
        temp_root = Path(tempfile.mkdtemp(prefix="sticker-import-", dir=str(workspace / "temp")))
        checkout = temp_root / "checkout"
        commit = _clone(stripped, checkout)
        return checkout, "git", stripped, commit, temp_root
    path = Path(stripped).expanduser()
    if not path.exists():
        raise SystemExit(f"import_pack: 源不存在：{path}")
    if path.is_dir():
        return path, "dir", "", "", None
    if path.suffix.lower() == ".zip":
        temp_root = Path(tempfile.mkdtemp(prefix="sticker-import-", dir=str(workspace / "temp")))
        extracted = temp_root / "checkout"
        _extract_zip(path, extracted)
        return extracted, "zip", "", "", temp_root
    raise SystemExit(f"import_pack: 不支持的源类型：{path.suffix or path}")


def detect(source_root: Path) -> Source:
    manifest_path = source_root / "manifest.json"
    memes_dir = source_root / "memes"
    if manifest_path.is_file() and memes_dir.is_dir():
        try:
            # utf-8-sig：手工编辑过的 manifest 常带 BOM，严格 utf-8 会直接解析失败。
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            raise SystemExit("import_pack: manifest.json 读不出来，拒绝猜测包结构")
        schema_version = manifest.get("schema_version")
        # 早期 / 非官方 AstrBot 包不带这个字段（实测一个 138 张的社区包就没有）：
        # 按 1 处理；写了别的版本号才是真的不认识，继续拒绝。
        if schema_version is not None and str(schema_version) != "1":
            raise SystemExit(
                f"import_pack: 不认识的 schema_version={schema_version!r}（只认 1）"
            )
        raw_descriptions = manifest.get("categories")
        # 源 manifest 的分类键可能带零宽字符或 `_` 前缀，和目录名对不上；
        # 一律按 slug_key 归一化后再查，免得描述成片空掉。
        normalized_descriptions: dict[str, str] = {}
        if isinstance(raw_descriptions, dict):
            for raw_key, entry in raw_descriptions.items():
                if isinstance(entry, dict):
                    text = str(entry.get("description") or "").strip()
                else:
                    text = str(entry or "").strip()
                key = slug_key(str(raw_key))
                if text and key not in normalized_descriptions:
                    normalized_descriptions[key] = text
        categories: dict[str, list[Path]] = {}
        descriptions: dict[str, str] = {}
        for category_dir in sorted(memes_dir.iterdir(), key=lambda item: item.name):
            if not category_dir.is_dir():
                continue
            images = _images_in(category_dir)
            if not images:
                continue
            key = slug_key(category_dir.name)
            categories[key] = images
            descriptions[key] = normalized_descriptions.get(key, DEFAULT_DESCRIPTION)
        previews = 0
        preview_dir = source_root / "previews"
        if preview_dir.is_dir():
            previews = len([item for item in preview_dir.iterdir() if item.is_file()])
        semantic = source_root / "semantic_metadata.json"
        return Source(
            kind="astrbot",
            root=source_root,
            pack_id=str(manifest.get("id") or "").strip(),
            version=str(manifest.get("version") or "").strip(),
            categories=categories,
            descriptions=descriptions,
            previews=previews,
            semantic_meta=semantic if semantic.is_file() else None,
        )

    nested: dict[str, list[Path]] = {}
    for child in sorted(source_root.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        images = _images_in(child)
        if images:
            nested[child.name] = images
    if nested:
        return Source(
            kind="nested",
            root=source_root,
            categories={slug_key(name): images for name, images in nested.items()},
            descriptions={
                slug_key(name): f"来源目录：{name}" for name in nested
            },
        )

    flat = _images_in(source_root)
    if flat:
        return Source(
            kind="flat",
            root=source_root,
            categories={REVIEW_CATEGORY: flat},
        )
    raise SystemExit("import_pack: 源里没有可用的图片（.png/.jpg/.jpeg/.gif/.webp）")


def _existing_index(pack_dir: Path) -> tuple[dict[str, set[str]], dict[str, Path]]:
    """Per-category sha256 sets already on disk.

    Dedupe is scoped to a category on purpose: meme packs legitimately reuse the
    same image under several categories (a "laugh" reaction also lives under
    "happy"), and a pack-wide set silently drops those memberships -- one real
    AstrBot pack lost 33 images and three whole categories that way.
    """
    hashes: dict[str, set[str]] = {}
    by_category: dict[str, Path] = {}
    if not pack_dir.is_dir():
        return hashes, by_category
    for category_dir in pack_dir.iterdir():
        if not category_dir.is_dir() or category_dir.name in RESERVED_PACK_DIRS:
            continue
        for image in _images_in(category_dir):
            try:
                hashes.setdefault(category_dir.name, set()).add(_sha256(image))
            except OSError:
                continue
            by_category.setdefault(category_dir.name, category_dir)
    return hashes, by_category


def build_plan(
    source: Source,
    pack_dir: Path,
    *,
    classify_map: dict[str, str],
) -> Plan:
    plan = Plan()
    seen = {key: set(value) for key, value in _existing_index(pack_dir)[0].items()}
    known_categories = {path.name for path in pack_dir.iterdir() if path.is_dir()} if pack_dir.is_dir() else set()
    for category, images in source.categories.items():
        for image in images:
            if not _inside(source.root, image):
                plan.skipped_unsafe += 1
                continue
            if image.suffix.lower() not in IMAGE_SUFFIXES:
                plan.skipped_suffix += 1
                continue
            try:
                size = image.stat().st_size
            except OSError:
                plan.skipped_unsafe += 1
                continue
            if size <= 0 or size > MAX_IMAGE_BYTES:
                plan.skipped_size += 1
                continue
            target_category = category
            if source.kind == "flat":
                mapped = classify_map.get(image.name) or classify_map.get(
                    str(image.relative_to(source.root))
                )
                if mapped:
                    target_category = slug_key(mapped)
                else:
                    target_category = REVIEW_CATEGORY
            digest = _sha256(image)
            bucket = seen.setdefault(target_category, set())
            if digest in bucket:
                plan.duplicates += 1
                continue
            bucket.add(digest)
            if target_category == REVIEW_CATEGORY:
                plan.review += 1
            if target_category not in known_categories and target_category != REVIEW_CATEGORY:
                known_categories.add(target_category)
            plan.actions.append((image, target_category, image.name))
    return plan


def _unique_name(category_dir: Path, name: str, reserved: set[str]) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = name
    counter = 1
    while candidate in reserved or (category_dir / candidate).exists():
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    reserved.add(candidate)
    return candidate


def write_pack(
    source: Source,
    plan: Plan,
    pack_dir: Path,
    *,
    pack_key: str,
    theme: str,
    theme_source: str,
    source_url: str,
    commit: str,
    semantic_meta: Path | None,
) -> dict[str, object]:
    manifest_path = pack_dir / "manifest.json"
    existing: dict[str, object] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing_categories = dict(existing.get("categories") or {})
    per_category: dict[str, int] = {}
    reserved: dict[str, set[str]] = {}
    for src_path, category, name in plan.actions:
        category_dir = pack_dir / category
        category_dir.mkdir(parents=True, exist_ok=True)
        final_name = _unique_name(category_dir, name, reserved.setdefault(category, set()))
        shutil.copy2(src_path, category_dir / final_name)
        per_category[category] = per_category.get(category, 0) + 1
    for category in per_category:
        existing_categories.setdefault(
            display_key(category),
            {"description": source.descriptions.get(category, DEFAULT_DESCRIPTION)},
        )
    if theme:
        theme_value, theme_origin = theme.strip()[:THEME_MAX_CHARS], theme_source
    else:
        theme_value = str(existing.get("theme") or "").strip()
        theme_origin = str(existing.get("theme_source") or "").strip()
    manifest = {
        "schema_version": 1,
        "pack": pack_key,
        "theme": theme_value,
        "theme_source": theme_origin,
        "source": {
            "kind": source.kind,
            "id": source.pack_id or pack_key,
            "version": source.version,
            "url": source_url,
            "commit": commit,
            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "license": str(existing.get("source", {}).get("license") or ""),
        },
        "categories": existing_categories,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    semantic_copied = False
    if semantic_meta is not None:
        target = pack_dir / "semantic" / "metadata.source.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(semantic_meta, target)
        semantic_copied = True
    return {"per_category": per_category, "semantic_copied": semantic_copied}


def write_theme_samples(source: Source, root: Path, pack_key: str) -> list[Path]:
    """Stratified sample (one per category) for the agent to look at."""
    samples: list[Path] = []
    out_dir = root / SAMPLES_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    for category in sorted(source.categories):
        images = source.categories[category]
        if not images:
            continue
        samples.append(images[len(images) // 2])
        if len(samples) >= THEME_SAMPLE_COUNT:
            break
    written: list[Path] = []
    for index, image in enumerate(samples, start=1):
        target = out_dir / f"{pack_key}-theme-{index}{image.suffix.lower()}"
        try:
            shutil.copy2(image, target)
        except OSError:
            continue
        written.append(target)
    return written


def clear_theme_samples(root: Path, pack_key: str) -> None:
    out_dir = root / SAMPLES_DIRNAME
    if not out_dir.is_dir():
        return
    for item in out_dir.glob(f"{pack_key}-theme-*"):
        try:
            item.unlink()
        except OSError:
            pass


def _short_summary(per_category: dict[str, int]) -> str:
    ordered = sorted(per_category.items(), key=lambda item: (-item[1], item[0]))[:6]
    return ", ".join(f"{key} {count}" for key, count in ordered)


def format_report(
    *,
    pack_key: str,
    created: bool,
    source: Source,
    plan: Plan,
    theme: str,
    theme_source: str,
    samples: list[Path],
    semantic_copied: bool,
) -> str:
    lines = [f"pack: {pack_key} ({'new' if created else 'merge'})"]
    origin = f"source: {source.kind}"
    if source.pack_id:
        origin += f" id={source.pack_id}"
    if source.version:
        origin += f" v{source.version}"
    lines.append(origin)
    lines.append(f"categories: {len(source.categories)}")
    counts: dict[str, int] = {}
    for _src, category, _name in plan.actions:
        counts[category] = counts.get(category, 0) + 1
    hidden = {key: value for key, value in counts.items() if key.startswith(DISABLED_PREFIX)}
    if hidden:
        lines.append(
            "hidden: {} 个分类以 `_` 开头，共 {} 张（按约定隐身，可在表情包页启用）".format(
                len(hidden), sum(hidden.values())
            )
        )
    lines.append(
        "images: copied={} skipped_suffix={} skipped_size={} skipped_unsafe={} duplicate={}".format(
            len(plan.actions),
            plan.skipped_suffix,
            plan.skipped_size,
            plan.skipped_unsafe,
            plan.duplicates,
        )
    )
    if source.previews:
        lines.append(f"previews: {source.previews} ignored")
    if plan.review:
        lines.append(f"review: {plan.review} 张进了 {REVIEW_CATEGORY}/（隐身，待分拣）")
    if theme:
        lines.append(f"theme: {theme} ({theme_source})")
    elif samples:
        lines.append("theme: 未设置 — 先看这几张，再用 --theme 定稿：")
        lines.extend(f"  {path}" for path in samples)
    else:
        lines.append("theme: 未设置（包里没有可抽样的图？）")
    if semantic_copied:
        lines.append("semantic: copied verbatim -> semantic/metadata.source.json (entry_id stale)")
    if counts:
        lines.append(f"top: {_short_summary(counts)}")
    return "\n".join(lines)


def _rmtree(path: Path) -> bool:
    """Remove a checkout, clearing read-only bits first.

    ``git clone`` writes its pack files read-only, so a plain ``rmtree`` dies on
    ``WinError 5`` and leaves the whole 78 MB checkout behind.
    """
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                continue
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _cleanup_temp(path: Path) -> None:
    """Best effort, but never silent: an interrupted clone leaves the whole
    checkout behind and ``rmtree(ignore_errors=True)`` would report nothing."""
    if not _rmtree(path):
        print(
            f"import_pack: 临时目录没删干净（git 克隆进程或杀软可能还占着）：{path}",
            file=sys.stderr,
        )


def sweep_stale_temp_dirs(workspace: Path, *, max_age_seconds: float = 6 * 3600) -> None:
    """Clean checkouts left by interrupted runs; a real import never takes 6h,
    so anything that old belongs to nobody."""
    base = workspace / "temp"
    if not base.is_dir():
        return
    cutoff = time.time() - max_age_seconds
    for item in base.glob("sticker-import-*"):
        try:
            if item.is_dir() and item.stat().st_mtime < cutoff:
                _rmtree(item)
        except OSError:
            continue


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import an external sticker pack.")
    parser.add_argument("src", help="本地目录 / .zip / git URL")
    parser.add_argument("--pack", default="", help="目标包键（默认取源 id 或目录名）")
    parser.add_argument("--theme", default="", help="一句 ≤60 字的包主题（人工定稿）")
    parser.add_argument("--root", default="", help="图库根（默认 <cwd>/stickers）")
    parser.add_argument("--force", action="store_true", help="目标包已存在时合并")
    parser.add_argument("--dry-run", action="store_true", help="只出报告，不落盘")
    parser.add_argument("--classify-map", default="", help="JSON：{相对路径: 分类键}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    workspace = Path.cwd()
    root = Path(args.root).expanduser().resolve() if args.root else (workspace / "stickers").resolve()
    classify_map: dict[str, str] = {}
    if args.classify_map:
        try:
            payload = json.loads(Path(args.classify_map).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"import_pack: --classify-map 读不出来：{exc}") from exc
        if isinstance(payload, dict):
            classify_map = {str(key): str(value) for key, value in payload.items()}

    (workspace / "temp").mkdir(parents=True, exist_ok=True)
    sweep_stale_temp_dirs(workspace)
    source_root, _origin_kind, source_url, commit, temp_dir = materialize(args.src, workspace)
    try:
        source = detect(source_root)
        default_pack = source.pack_id or Path(str(args.src).rstrip("/\\")).stem
        pack_key = slug_key(args.pack or default_pack, salt=str(source_root))
        pack_dir = root / pack_key
        exists = (pack_dir / "manifest.json").is_file() or (
            pack_dir.is_dir() and any(pack_dir.iterdir())
        )
        if exists and not args.force:
            print(
                f"import_pack: 包 {pack_key} 已存在。用 --force 合并，或用 --pack 另起一个包键。",
                file=sys.stderr,
            )
            return 2
        plan = build_plan(source, pack_dir, classify_map=classify_map)
        theme_source = "human" if args.theme else ""
        samples = [] if args.theme else write_theme_samples(source, root, pack_key)
        if args.dry_run:
            report = format_report(
                pack_key=pack_key,
                created=not exists,
                source=source,
                plan=plan,
                theme=args.theme,
                theme_source=theme_source,
                samples=samples,
                semantic_copied=source.semantic_meta is not None,
            )
            print(report)
            return 0
        written = write_pack(
            source,
            plan,
            pack_dir,
            pack_key=pack_key,
            theme=args.theme,
            theme_source=theme_source,
            source_url=source_url,
            commit=commit,
            semantic_meta=source.semantic_meta,
        )
        if args.theme:
            clear_theme_samples(root, pack_key)
        report = format_report(
            pack_key=pack_key,
            created=not exists,
            source=source,
            plan=plan,
            theme=args.theme,
            theme_source=theme_source,
            samples=samples,
            semantic_copied=bool(written["semantic_copied"]),
        )
        print(report)
        return 0
    finally:
        if temp_dir is not None:
            _cleanup_temp(temp_dir)


if __name__ == "__main__":
    raise SystemExit(main())
