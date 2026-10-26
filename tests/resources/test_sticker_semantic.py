"""Precise sticker picking: metadata identity, lexical search, id markers."""

from __future__ import annotations

import json
from pathlib import Path

from g3ku.stickers import resolve_markers, scan_packs
from g3ku.stickers.catalog import invalidate_scan_cache
from g3ku.stickers.semantic import (
    entry_id_for,
    load_entries,
    resolve_entry,
    search,
    write_metadata,
)


def _pack(root: Path, name: str = "atri") -> Path:
    category = root / name / "happy"
    category.mkdir(parents=True)
    (category / "laugh.png").write_bytes(b"\x89PNG-laugh")
    (category / "victory.png").write_bytes(b"\x89PNG-victory")
    review = root / name / "_review"
    review.mkdir()
    (review / "unsorted.png").write_bytes(b"\x89PNG-unsorted")
    (root / name / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "pack": name, "theme": "t", "categories": {}}),
        encoding="utf-8",
    )
    invalidate_scan_cache()
    return root / name


def _first_pack(root: Path):
    return next(pack for pack in scan_packs(root) if pack.key == "atri")


def _seed_metadata(root: Path) -> None:
    pack = _first_pack(root)
    write_metadata(
        pack,
        {
            "happy/laugh.png": {
                "caption": "笑得很大声，真心高兴",
                "tags": ["开心", "庆祝", "大笑"],
                "visible_text": "哈哈哈",
            },
            "happy/victory.png": {
                "caption": "赢了，得意地比耶",
                "tags": ["得意", "胜利"],
            },
        },
    )


def test_write_metadata_derives_identity_from_disk(tmp_path):
    _pack(tmp_path)
    _seed_metadata(tmp_path)
    pack = _first_pack(tmp_path)

    entries = {entry.relpath: entry for entry in load_entries(pack)}

    assert set(entries) == {"happy/laugh.png", "happy/victory.png"}
    entry = entries["happy/laugh.png"]
    assert len(entry.entry_id) == 16
    assert entry.sha256
    assert entry.tags == ("开心", "庆祝", "大笑")
    # 身份跟着字节走：同一份内容重算必须一致
    payload = (pack.path / "happy" / "laugh.png").read_bytes()
    assert entry_id_for(image_bytes=payload, category="happy", relpath="happy/laugh.png") == entry.entry_id


def test_metadata_review_and_missing_rows_are_not_indexed(tmp_path):
    _pack(tmp_path)
    _seed_metadata(tmp_path)
    pack = _first_pack(tmp_path)
    payload = json.loads((pack.path / "semantic" / "metadata.json").read_text(encoding="utf-8"))
    # 手写一条指向不存在文件的记录：读侧应当丢掉而不是崩
    payload["entries"]["deadbeefdeadbeef"] = {
        "category": "happy",
        "path": "happy/gone.png",
        "caption": "x",
        "tags": [],
        "sha256": "",
    }
    (pack.path / "semantic" / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    entries = load_entries(pack)

    assert "deadbeefdeadbeef" not in {entry.entry_id for entry in entries}


def test_search_ranks_by_caption_and_tag_overlap(tmp_path):
    _pack(tmp_path)
    _seed_metadata(tmp_path)
    packs = scan_packs(tmp_path)

    happy_hits = search("开心 大笑", packs=packs)
    smug_hits = search("得意 胜利", packs=packs)

    assert [hit.entry_id for hit in happy_hits][0] == next(
        entry.entry_id for entry in load_entries(packs[0]) if entry.relpath == "happy/laugh.png"
    )
    assert [hit.category for hit in smug_hits] == ["happy"]
    assert "胜利" in smug_hits[0].tags


def test_search_returns_nothing_for_unrelated_query(tmp_path):
    _pack(tmp_path)
    _seed_metadata(tmp_path)
    assert search("linux 内核 panic", packs=scan_packs(tmp_path)) == ()


def test_resolve_entry_checks_prefix_uniqueness_and_bytes(tmp_path):
    _pack(tmp_path)
    _seed_metadata(tmp_path)
    pack = _first_pack(tmp_path)
    entry = next(item for item in load_entries(pack) if item.relpath == "happy/laugh.png")

    assert resolve_entry(pack, entry.entry_id) is not None
    assert resolve_entry(pack, entry.entry_id[:12]) is not None
    assert resolve_entry(pack, "ffffffffffff") is None

    # 内容被换掉：身份不再可信
    (pack.path / "happy" / "laugh.png").write_bytes(b"\x89PNG-swapped")
    assert resolve_entry(pack, entry.entry_id) is None


def test_id_marker_sends_the_exact_image(tmp_path):
    _pack(tmp_path)
    _seed_metadata(tmp_path)
    pack = _first_pack(tmp_path)
    entry = next(item for item in load_entries(pack) if item.relpath == "happy/victory.png")

    text, paths = resolve_markers(
        f"赢了\n[[sticker:atri/@{entry.entry_id}]]\n", root=tmp_path
    )

    assert text == "赢了"
    assert [path.name for path in paths] == ["victory.png"]


def test_unknown_id_marker_is_stripped_without_sending(tmp_path):
    _pack(tmp_path)
    _seed_metadata(tmp_path)

    text, paths = resolve_markers("赢了\n[[sticker:atri/@ffffffffffff]]\n", root=tmp_path)

    assert text == "赢了"
    assert paths == ()
