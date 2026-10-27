#!/usr/bin/env python3
"""Import an external sticker pack into ``<workspace>/stickers``.

Only one source shape is accepted: ``<src>/<category-dir>/<images>``. AstrBot
packs qualify by pointing at the pack root (their ``memes/`` is that shape and
is descended into automatically). Flat piles are refused — classify first.

The import is two-phase so descriptions are never guessed:

    python skills/meme/scripts/import_pack.py <src> --detect
    python skills/meme/scripts/import_pack.py <src> --commit --map map.json --theme "一句话主题"

``map.json`` keys are the source directory names::

    {"happy": {"description": "开心大笑、庆祝"},
     "stock": {"category": "stocks", "description": "讨论股票基金，低频"}}

Every detected source category must appear in the map; a category without a
description is not written. Files land through the registry (``NNN.<ext>``,
append-only numbering, 36 per category), so the model can address them by
index and nothing is silently truncated.
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.stickers import registry  # noqa: E402
from g3ku.stickers.catalog import (  # noqa: E402
    CATEGORY_DESCRIPTION_MAX_CHARS,
    IMAGE_SUFFIXES,
    normalize_description,
    scan_packs,
)

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SLUG_RE = re.compile(r"[^a-z0-9._-]+")
# 零宽 / 方向控制字符：手工编辑过的 manifest 常混进去，留在键里会让描述对不上目录名。
INVISIBLE_CHARS_RE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
DISABLED_PREFIX = "_"
FORBIDDEN_NAMES = frozenset(
    {"build", "dist", "node_modules", ".venv", "venv", "site-packages", "__pycache__"}
)
FORBIDDEN_PREFIXES = ("tmp_", "temp_", ".tmp_")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
RESERVED_PACK_DIRS = frozenset({"semantic"})
TEMP_PREFIX = "sticker-import-"
TEMP_MAX_AGE_SECONDS = 6 * 3600


def slug_key(raw: str, *, salt: str = "") -> str:
    """ASCII key for a directory name; unrepresentable names get a stable hash."""
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
        if ".." in Path(raw).parts:
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


def _rmtree(path: Path) -> bool:
    def _onexc(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_onexc)
        return not path.exists()
    except OSError:
        return False


def sweep_stale_temp_dirs(workspace: Path) -> None:
    temp_root = workspace / "temp"
    if not temp_root.is_dir():
        return
    now = time.time()
    for entry in temp_root.iterdir():
        if not entry.is_dir() or not entry.name.startswith(TEMP_PREFIX):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > TEMP_MAX_AGE_SECONDS:
            _rmtree(entry)


def materialize(src: str, workspace: Path) -> tuple[Path, str, str, str, Path | None]:
    """Return (source_root, origin_kind, url, commit, temp_dir_to_clean)."""
    stripped = str(src or "").strip()
    if not stripped:
        raise SystemExit("import_pack: 需要 <src>")
    sweep_stale_temp_dirs(workspace)
    if re.match(r"^(https://|git@)", stripped):
        temp_root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=str(workspace / "temp")))
        checkout = temp_root / "checkout"
        commit_hash = _clone(stripped, checkout)
        return checkout, "git", stripped, commit_hash, temp_root
    path = Path(stripped).expanduser()
    if not path.exists():
        raise SystemExit(f"import_pack: 源不存在：{path}")
    if path.is_dir():
        return path, "dir", "", "", None
    if path.suffix.lower() == ".zip":
        temp_root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=str(workspace / "temp")))
        extracted = temp_root / "checkout"
        _extract_zip(path, extracted)
        return extracted, "zip", "", "", temp_root
    raise SystemExit(f"import_pack: 不支持的源类型：{path.suffix or path}")


@dataclass
class Source:
    root: Path
    kind: str = "category"
    url: str = ""
    commit: str = ""
    temp_dir: Path | None = None
    categories: dict[str, list[Path]] = field(default_factory=dict)
    flat_images: int = 0


def detect(source_root: Path) -> Source:
    root = source_root
    memes = root / "memes"
    if memes.is_dir():
        root = memes
    categories: dict[str, list[Path]] = {}
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or child.name in RESERVED_PACK_DIRS:
            continue
        images = [image for image in _images_in(child) if _inside(root, image)]
        if images:
            categories[child.name] = images
    return Source(
        root=root,
        categories=categories,
        flat_images=len(_images_in(root)),
    )


def _pack_key_hint(src: str, source: Source) -> str:
    raw = str(src or "").rstrip("/\\").split("/")[-1].split("\\")[-1]
    if raw.endswith(".zip"):
        raw = raw[: -len(".zip")]
    return slug_key(raw or "pack", salt=str(source.root))


def _detect_payload(src: str, source: Source, pack_key: str, workspace: Path) -> dict:
    packs = {pack.key: pack for pack in scan_packs(workspace / "stickers")}
    existing = packs.get(pack_key)
    rows = []
    for name in sorted(source.categories):
        images = source.categories[name]
        target = slug_key(name)
        current = 0
        if existing is not None:
            current = len(registry.category_rows(existing.path, target))
        rows.append(
            {
                "source": name,
                "proposed_category": target,
                "images": len(images),
                "target_registered": current,
                "fits": current + len(images) <= registry.MAX_IMAGES_PER_CATEGORY,
            }
        )
    return {
        "pack": pack_key,
        "pack_exists": existing is not None,
        "source_root": str(source.root),
        "categories": rows,
        "flat_images": source.flat_images,
        "limit_per_category": registry.MAX_IMAGES_PER_CATEGORY,
        "description_max_chars": CATEGORY_DESCRIPTION_MAX_CHARS,
    }


def _valid_target_key(target: str) -> bool:
    """`_` 前缀是隐身车道，主体才受键字符集约束。"""
    body = target[1:] if target.startswith(DISABLED_PREFIX) else target
    return bool(
        KEY_RE.match(body)
        and body not in FORBIDDEN_NAMES
        and not body.startswith(FORBIDDEN_PREFIXES)
    )


def _load_map(path: str) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"import_pack: --map 读不出来：{exc}")
    if not isinstance(raw, dict):
        raise SystemExit("import_pack: --map 必须是 {源目录: {category?, description}}")
    mapping: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            mapping[str(key)] = {"category": "", "description": value}
        elif isinstance(value, dict):
            mapping[str(key)] = {
                "category": str(value.get("category") or "").strip(),
                "description": str(value.get("description") or ""),
            }
        else:
            raise SystemExit(f"import_pack: --map 里 {key!r} 必须是字符串或对象")
    return mapping


def commit(
    source: Source,
    *,
    pack_key: str,
    mapping: dict[str, dict[str, str]],
    theme: str,
    workspace: Path,
) -> int:
    missing = [name for name in source.categories if name not in mapping]
    if missing:
        print("import_pack: 以下源分类没有描述，拒绝导入（先补 --map）：")
        for name in missing:
            print(f"  - {name}（{len(source.categories[name])} 张）")
        return 2
    for name in mapping:
        if name not in source.categories:
            print(f"import_pack: 忽略 --map 里源中不存在的分类 {name!r}")

    targets: dict[str, dict] = {}
    for name, images in source.categories.items():
        row = mapping[name]
        target = row.get("category") or slug_key(name)
        if not _valid_target_key(target):
            print(f"import_pack: 非法目标分类键 {target!r}（源 {name!r}）")
            return 2
        description, truncated = normalize_description(row.get("description") or "")
        if truncated:
            print(
                f"import_pack: 分类 {target!r} 的描述超过 {CATEGORY_DESCRIPTION_MAX_CHARS} 字，已截断"
            )
        entry = targets.setdefault(
            target, {"description": description, "sources": [], "images": []}
        )
        entry["sources"].append(name)
        entry["images"].extend(images)
        if description and not entry["description"]:
            entry["description"] = description

    pack_dir = workspace / "stickers" / pack_key
    pack_dir.mkdir(parents=True, exist_ok=True)
    registry.ensure_bootstrap(pack_dir)

    planned: dict[str, list[Path]] = {}
    for target, entry in targets.items():
        rows = registry.category_rows(pack_dir, target)
        known = {
            str((row or {}).get("sha256") or "")
            for row in rows.values()
            if isinstance(row, dict)
        }
        unique: list[Path] = []
        seen: set[str] = set()
        for image in entry["images"]:
            try:
                size = image.stat().st_size
            except OSError:
                continue
            if size <= 0 or size > MAX_IMAGE_BYTES:
                print(f"import_pack: 跳过超限/空文件 {image.name}")
                continue
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            if digest in known or digest in seen:
                continue
            seen.add(digest)
            unique.append(image)
        if len(rows) + len(unique) > registry.MAX_IMAGES_PER_CATEGORY:
            print(
                f"import_pack: 分类 {target!r} 将超过 {registry.MAX_IMAGES_PER_CATEGORY} 张"
                f"（已有 {len(rows)} + 新增 {len(unique)}），拒绝导入；请拆分分类后重试"
            )
            return 2
        planned[target] = unique

    added: dict[str, int] = {}
    for target, images in planned.items():
        count = 0
        for image in images:
            try:
                registry.register_payload(pack_dir, target, image.name, image.read_bytes())
            except registry.RegistryError as exc:
                print(f"import_pack: {target}/{image.name}: {exc.message}")
                return 2
            count += 1
        added[target] = count

    manifest_path = pack_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            manifest = {}
    except (OSError, json.JSONDecodeError):
        manifest = {}
    if theme.strip():
        manifest["theme"] = theme.strip()[:120]
    categories = manifest.get("categories")
    if not isinstance(categories, dict):
        categories = {}
    for target, entry in targets.items():
        if entry["description"]:
            categories[target] = {"description": entry["description"]}
    manifest["categories"] = categories
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    total = sum(added.values())
    print(f"pack: {pack_key}")
    origin = " ".join(filter(None, [source.kind, source.url, source.commit])).strip()
    print(f"source: {origin}")
    if theme.strip():
        print(f"theme: {manifest.get('theme', '')}")
    else:
        print("theme: 沿用已有值；没有就补 --theme，模型按主题选包")
    for target in sorted(added):
        sources = ", ".join(targets[target]["sources"])
        print(f"  {target}: +{added[target]}（源 {sources}）")
    print(f"registered: {total}")
    if source.flat_images:
        print(f"flat: {source.flat_images} 张平铺图未处理（本脚本只收分类形状）")
    print("next: 在「表情包」页核对描述；模型侧下一轮会收到该包的清单变化通知。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a sticker pack (category-shaped sources).")
    parser.add_argument("src", help="目录 / .zip / git URL")
    parser.add_argument("--detect", action="store_true", help="只探测分类与张数，不写盘")
    parser.add_argument("--commit", action="store_true", help="按 --map 落盘")
    parser.add_argument("--map", default="", help='JSON：{"<源目录>": {"category"?, "description"}}')
    parser.add_argument("--pack", default="", help="包目录名（默认由源名推导）")
    parser.add_argument("--theme", default="", help="一句话主题（≤120 字）")
    parser.add_argument("--workspace", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    workspace = Path(args.workspace).resolve()
    (workspace / "temp").mkdir(parents=True, exist_ok=True)
    source_root, kind, url, commit_hash, temp_dir = materialize(args.src, workspace)
    try:
        source = detect(source_root)
        source.kind = kind
        source.url = url
        source.commit = commit_hash
        pack_key = (
            slug_key(args.pack, salt=str(source.root))
            if args.pack
            else _pack_key_hint(args.src, source)
        )
        if not source.categories:
            print("import_pack: 源里没有分类形状（<源>/<子目录>/<图片>）")
            if source.flat_images:
                print(f"import_pack: 根目录有 {source.flat_images} 张平铺图；请先归类到子目录")
            return 2
        if args.commit:
            if not args.map:
                print("import_pack: --commit 需要 --map")
                return 2
            return commit(
                source,
                pack_key=pack_key,
                mapping=_load_map(args.map),
                theme=args.theme,
                workspace=workspace,
            )
        print(
            json.dumps(
                _detect_payload(args.src, source, pack_key, workspace),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        if temp_dir is not None and not _rmtree(temp_dir):
            print(f"import_pack: 临时目录没清掉：{temp_dir}（下次运行会清超过 6 小时的）")


if __name__ == "__main__":
    raise SystemExit(main())
