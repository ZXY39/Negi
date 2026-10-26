"""Sticker catalog scanning and marker resolution."""

from __future__ import annotations

import json
import random
from pathlib import Path

from g3ku.stickers import (
    render_catalog_lines,
    resolve_markers,
    scan_packs,
    strip_markers_for_stream,
)
from g3ku.stickers.catalog import invalidate_scan_cache


def _write_pack(
    root: Path,
    pack: str,
    categories: dict[str, int],
    *,
    theme: str | None = None,
    descriptions: dict[str, str] | None = None,
) -> Path:
    pack_dir = root / pack
    for category, count in categories.items():
        category_dir = pack_dir / category
        category_dir.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (category_dir / f"{category}_{index}.png").write_bytes(b"\x89PNG-fake")
    if theme is not None or descriptions is not None:
        manifest = {
            "schema_version": 1,
            "pack": pack,
            "theme": theme or "",
            "categories": {
                key: {"description": value} for key, value in (descriptions or {}).items()
            },
        }
        (pack_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
    return pack_dir


def test_scan_packs_reads_directories_and_skips_soft_deleted(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 2, "awkward": 1}, theme="动漫少女")
    _write_pack(tmp_path, "panda", {"happy": 1}, theme="熊猫头")
    # 软删：包级与分类级都不进目录
    _write_pack(tmp_path, "_parked", {"happy": 3}, theme="停用")
    _write_pack(tmp_path, "atri2", {"_review": 2, "happy": 1}, theme="复核中")
    # 空分类不入表
    (tmp_path / "empty_pack" / "sad").mkdir(parents=True)
    # 旁挂元数据目录不是分类
    (tmp_path / "atri" / "semantic").mkdir()
    (tmp_path / "atri" / "semantic" / "metadata.json").write_text("{}", encoding="utf-8")
    invalidate_scan_cache()

    packs = scan_packs(tmp_path)

    assert [pack.key for pack in packs] == ["atri", "atri2", "panda"]
    atri = packs[0]
    assert atri.theme == "动漫少女"
    assert [category.key for category in atri.categories] == ["awkward", "happy"]
    assert [category.count for category in atri.categories] == [1, 2]
    assert [category.key for category in packs[1].categories] == ["happy"]


def test_scan_packs_without_manifest_uses_placeholder_theme(tmp_path):
    _write_pack(tmp_path, "loose", {"happy": 1})
    invalidate_scan_cache()

    packs = scan_packs(tmp_path)

    assert len(packs) == 1
    assert packs[0].theme == "（未设置主题）"


def test_scan_packs_tolerates_broken_manifest(tmp_path):
    pack_dir = _write_pack(tmp_path, "broken", {"happy": 1})
    (pack_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    invalidate_scan_cache()

    packs = scan_packs(tmp_path)

    assert packs[0].theme == "（未设置主题）"


def test_scan_packs_cache_and_invalidate(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    invalidate_scan_cache()
    first = scan_packs(tmp_path)

    _write_pack(tmp_path, "panda", {"happy": 1}, theme="p")
    assert scan_packs(tmp_path) == first

    invalidate_scan_cache()
    assert [pack.key for pack in scan_packs(tmp_path)] == ["atri", "panda"]


def test_render_catalog_lines_groups_and_truncates(tmp_path):
    long_description = "尴尬、被说中、当场社死的复合语气与典型触发语境补充说明"
    _write_pack(
        tmp_path,
        "atri",
        {"happy": 1, "awkward": 1},
        theme="动漫少女 ATRI",
        descriptions={"happy": "开心大笑", "awkward": long_description},
    )
    invalidate_scan_cache()

    lines = render_catalog_lines(scan_packs(tmp_path))

    assert lines[0] == "- `atri` | 动漫少女 ATRI"
    assert lines[1] == "  - `awkward` | " + long_description[:19].rstrip() + "…"
    assert lines[2] == "  - `happy` | 开心大笑"
    assert not any("张" in line or "2" in line for line in lines)


def test_resolve_markers_picks_an_image_and_drops_the_line(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 3}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers(
        "今天心情不错。\n[[sticker:atri/happy]]\n", root=tmp_path, rng=random.Random(1)
    )

    assert text == "今天心情不错。\n"
    assert len(images) == 1
    assert images[0].is_file()
    assert images[0].parent.name == "happy"
    assert images[0].parent.parent.name == "atri"


def test_resolve_markers_accepts_unique_short_form(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    _write_pack(tmp_path, "panda", {"smug": 1}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers("好耶\n[[sticker:happy]]\n", root=tmp_path)

    assert text == "好耶\n"
    assert [path.parent.name for path in images] == ["happy"]


def test_resolve_markers_rejects_ambiguous_short_form(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    _write_pack(tmp_path, "panda", {"happy": 1}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers("好耶\n[[sticker:happy]]\n", root=tmp_path)

    assert text == "好耶\n"
    assert images == ()


def test_resolve_markers_drops_unknown_keys_without_leaking(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers(
        "a\n[[sticker:atri/happpy]]\nb\n[[sticker:nope/happy]]\nc\n", root=tmp_path
    )

    assert text == "a\nb\nc\n"
    assert images == ()
    assert "sticker" not in text


def test_resolve_markers_caps_per_reply(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    _write_pack(tmp_path, "panda", {"smug": 1}, theme="t")
    invalidate_scan_cache()

    content = "x\n[[sticker:atri/happy]]\n[[sticker:panda/smug]]\n"
    text, images = resolve_markers(content, root=tmp_path, max_per_reply=1)
    assert text == "x\n"
    assert len(images) == 1

    text, images = resolve_markers(content, root=tmp_path, max_per_reply=0)
    assert text == "x\n"
    assert images == ()


def test_resolve_markers_ignores_inline_mentions(tmp_path):
    """只认独占一行：正文里顺带提到的标记不触发发送，也不被吞掉。"""
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    invalidate_scan_cache()

    content = "语法是 [[sticker:atri/happy]] 这样写\n"
    text, images = resolve_markers(content, root=tmp_path)

    assert text == content
    assert images == ()


def test_strip_markers_for_stream_removes_complete_and_holds_partial():
    assert (
        strip_markers_for_stream("说完了\n[[sticker:atri/happy]]\n")
        == "说完了\n"
    )
    assert strip_markers_for_stream("说完了\n[[sticker:atri/ha") == "说完了\n"
    assert strip_markers_for_stream("说完了\n[[sticker:") == "说完了\n"
    assert strip_markers_for_stream("说完了\n[[") == "说完了\n"
    # 普通中文里的方括号与提示词里的示例都不动
    assert strip_markers_for_stream("见 [[1]] 和 [[sticker:x]]") == "见 [[1]] 和 [[sticker:x]]"
    assert strip_markers_for_stream("没有标记") == "没有标记"


def test_strip_markers_for_stream_is_idempotent():
    once = strip_markers_for_stream("正文\n[[sticker:atri/happy]]\n尾巴\n[[sticker:at")
    twice = strip_markers_for_stream(once)
    assert once == twice == "正文\n尾巴\n"
