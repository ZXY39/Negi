#!/usr/bin/env python3
"""Write per-image captions/tags for a sticker pack.

``<pack>/semantic/metadata.json`` is what ``search_stickers`` reads. Identity
(``entry_id``) and ``sha256`` are derived from the bytes on disk, so this script
never trusts them from the map file — re-running it after replacing an image
yields a new id, which is the point.

Usage (run from the workspace root)::

    python skills/meme/scripts/caption_pack.py --list atri
    python skills/meme/scripts/caption_pack.py atri --map captions.json
    python skills/meme/scripts/caption_pack.py atri --map captions.json --dry-run

``captions.json`` maps a pack-relative path to its caption::

    {"happy/h1.png": {"caption": "笑得很大声，真心高兴",
                      "tags": ["开心", "庆祝"], "visible_text": ""}}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.stickers.catalog import DISABLED_PREFIX, pack_images, scan_packs  # noqa: E402
from g3ku.stickers.semantic import load_entries, write_metadata  # noqa: E402


def _find_pack(pack_key: str):
    for pack in scan_packs():
        if pack.key == pack_key:
            return pack
    return None


def _image_relpaths(pack) -> list[str]:
    relpaths: list[str] = []
    for category in pack.categories:
        if category.key.startswith(DISABLED_PREFIX):
            continue
        for image in pack_images(pack, category.key):
            relpaths.append(f"{category.key}/{image.name}")
    return relpaths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write sticker captions for one pack.")
    parser.add_argument("pack", help="pack key under <workspace>/stickers")
    parser.add_argument("--map", default="", help="JSON {relpath: {caption, tags, visible_text}}")
    parser.add_argument("--list", action="store_true", help="print image paths still missing captions")
    parser.add_argument("--dry-run", action="store_true", help="report only, do not write")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    pack = _find_pack(args.pack)
    if pack is None:
        print(f"caption_pack: 包 {args.pack!r} 不在图库里", file=sys.stderr)
        return 2

    relpaths = _image_relpaths(pack)
    covered = {entry.relpath for entry in load_entries(pack)}
    missing = [relpath for relpath in relpaths if relpath not in covered]

    if args.list or not args.map:
        print(f"pack: {pack.key}")
        print(f"images: {len(relpaths)} total / {len(covered)} with metadata")
        for relpath in missing:
            print(f"  missing  {relpath}")
        return 0

    try:
        raw = json.loads(Path(args.map).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"caption_pack: --map 读不出来：{exc}", file=sys.stderr)
        return 2
    if not isinstance(raw, dict):
        print("caption_pack: --map 必须是 {relpath: {...}} 的 JSON 对象", file=sys.stderr)
        return 2

    rows: dict[str, dict] = {}
    skipped: list[str] = []
    for relpath, row in raw.items():
        normalized = str(relpath or "").strip().replace("\\", "/")
        if not isinstance(row, dict) or normalized not in relpaths:
            skipped.append(normalized or "<empty>")
            continue
        rows[normalized] = {
            "caption": row.get("caption") or "",
            "tags": row.get("tags") or [],
            "visible_text": row.get("visible_text") or "",
        }

    print(f"pack: {pack.key}")
    print(f"images: {len(relpaths)} total")
    print(f"map: {len(rows)} usable / {len(skipped)} skipped")
    if skipped:
        print("skipped: " + ", ".join(skipped[:8]) + ("" if len(skipped) <= 8 else " …"))
    if args.dry_run:
        print("dry-run: nothing written")
        return 0

    # 合并：map 没覆盖的既有条目继续保留，避免分批补描述时把上一批冲掉。
    existing = {
        entry.relpath: {
            "caption": entry.caption,
            "tags": list(entry.tags),
            "visible_text": entry.visible_text,
        }
        for entry in load_entries(pack)
    }
    existing.update(rows)
    payload = write_metadata(pack, existing)
    print(f"metadata: {len(payload['entries'])} entries -> {pack.path / 'semantic' / 'metadata.json'}")
    print("next: 需要精确取图时把 stickers.semanticSearch 打开，模型就能用 search_stickers。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
