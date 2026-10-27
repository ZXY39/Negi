"""Sticker catalog scanning and marker resolution."""

from __future__ import annotations

import json
import random
from pathlib import Path

from g3ku.stickers import (
    STICKER_HEADER,
    render_catalog_lines,
    render_marker_images,
    resolve_markers,
    resolve_reply_stickers,
    scan_packs,
    sticker_pack_sections,
    sticker_sections_from_pinned_text,
    strip_markers_for_stream,
)
from g3ku.stickers.catalog import (
    CATEGORY_DESCRIPTION_MAX_CHARS,
    injected_chars,
    invalidate_scan_cache,
    normalize_description,
)


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


def test_manifest_with_bom_still_yields_theme_and_descriptions(tmp_path):
    """manifest 是人手改的，Windows 编辑器会给它加 BOM；读不出来的话整包主题和
    分类描述会静默消失，模型只能看到「（未设置主题）」。"""
    pack_dir = _write_pack(tmp_path, "atri", {"happy": 1}, theme="动漫少女", descriptions={"happy": "开心"})
    manifest_path = pack_dir / "manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text("\ufeff" + raw, encoding="utf-8")
    invalidate_scan_cache()

    pack = scan_packs(tmp_path)[0]

    assert pack.theme == "动漫少女"
    assert pack.category("happy").description == "开心"


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


def test_render_catalog_lines_is_one_parseable_line_per_pack(tmp_path):
    long_description = "尴尬、被说中、当场社死的复合语气与典型触发语境补充说明"
    _write_pack(
        tmp_path,
        "atri",
        {"happy": 1, "awkward": 1},
        theme="动漫少女 ATRI",
        descriptions={"happy": "开心大笑", "awkward": long_description},
    )
    invalidate_scan_cache()

    packs = scan_packs(tmp_path)
    lines = render_catalog_lines(packs)

    assert lines[0] == STICKER_HEADER
    assert lines[1] == (
        f"- `atri` | 动漫少女 ATRI | awkward({long_description}), happy(开心大笑)"
    )
    # 头部 ≡ 通知分节：解析回来必须逐字相等
    assert sticker_sections_from_pinned_text("\n".join(lines)) == sticker_pack_sections(packs)
    assert not any("张" in line for line in lines)


def test_catalog_sections_keep_descriptions_and_flag_over_limit(tmp_path):
    _write_pack(
        tmp_path,
        "atri",
        {f"cat{index:03d}": 1 for index in range(100)},
        theme="长主题",
        descriptions={f"cat{index:03d}": "很长的分类描述还要再长一些才能撑爆预算" for index in range(100)},
    )
    invalidate_scan_cache()

    packs = scan_packs(tmp_path)
    sections = sticker_pack_sections(packs)

    # 描述永不降级：超量只体现在提醒口径上，不删内容。
    assert sections["atri"].count("(") == 100
    assert injected_chars(packs) == sum(
        len(key) + len(value) + 8 for key, value in sections.items()
    )


def test_normalize_description_truncates_only_when_asked():
    long_text = "字" * (CATEGORY_DESCRIPTION_MAX_CHARS + 5)

    kept, over = normalize_description(long_text)
    assert over is True
    assert len(kept) == CATEGORY_DESCRIPTION_MAX_CHARS

    raw, over_raw = normalize_description(long_text, truncate=False)
    assert over_raw is True
    assert len(raw) == len(long_text)

    compact, over_short = normalize_description("  开心 | 大笑  ")
    assert (compact, over_short) == ("开心 / 大笑", False)


def test_resolve_markers_picks_an_image_and_drops_the_line(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 3}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers(
        "今天心情不错。\n[[sticker:atri/happy]]\n", root=tmp_path, rng=random.Random(1)
    )

    assert text == "今天心情不错。"
    assert len(images) == 1
    assert images[0].is_file()
    assert images[0].parent.name == "happy"
    assert images[0].parent.parent.name == "atri"


def test_resolve_markers_accepts_unique_short_form(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    _write_pack(tmp_path, "panda", {"smug": 1}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers("好耶\n[[sticker:happy]]\n", root=tmp_path)

    assert text == "好耶"
    assert [path.parent.name for path in images] == ["happy"]


def test_resolve_markers_rejects_ambiguous_short_form(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    _write_pack(tmp_path, "panda", {"happy": 1}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers("好耶\n[[sticker:happy]]\n", root=tmp_path)

    assert text == "好耶"
    assert images == ()


def test_resolve_markers_drops_unknown_keys_without_leaking(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers(
        "a\n[[sticker:atri/happpy]]\nb\n[[sticker:nope/happy]]\nc\n", root=tmp_path
    )

    assert text == "a\nb\nc"
    assert images == ()
    assert "sticker" not in text


def test_malformed_marker_lines_are_dropped_not_leaked(tmp_path):
    """语法不成立也算我们的标记：漏到用户面前比不发更糟。

    `_` 开头的分类是隐身的（AstrBot 导入会保留这个前缀），模型照着写出来的
    `[[sticker:atri/_secret]]` 不被段落模式接受；若按"不认识就原样保留"处理，
    正文里就会露出一坨标记。多一段的 `happy/extra` 同理。
    """
    _write_pack(tmp_path, "atri", {"happy": 1, "_secret": 1}, theme="t")
    invalidate_scan_cache()

    text, images = resolve_markers(
        "a\n[[sticker:atri/_secret]]\n[[sticker:atri/happy/extra]]\nb\n", root=tmp_path
    )

    assert text == "a\nb"
    assert images == ()
    assert "[[" not in text


def test_stream_drops_malformed_marker_lines(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1, "_secret": 1}, theme="t")
    invalidate_scan_cache()

    assert strip_markers_for_stream("a\n[[sticker:atri/_secret]]\nb") == "a\nb"


def test_sticker_reply_memory_text_replaces_markers_with_breadcrumbs(tmp_path):
    """记忆窗口收的是出站投影：标记语法对摘要模型没有意义。"""
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    invalidate_scan_cache()

    reply = resolve_reply_stickers("好耶\n[[sticker:atri/happy]]", root=tmp_path)

    assert reply.text == "好耶"
    assert reply.delivered == ("atri/happy",)
    assert reply.undelivered == ()
    assert reply.memory_text == "好耶\n\n[已发表情包：atri/happy]"


def test_sticker_reply_marks_failed_sends_instead_of_recording_them(tmp_path):
    """没发出去的图不能在记忆里变成"发过"；未知键与隐身分类都算未发出。"""
    _write_pack(tmp_path, "atri", {"happy": 1, "_secret": 1}, theme="t")
    invalidate_scan_cache()

    reply = resolve_reply_stickers(
        "嗯\n[[sticker:atri/nope]]\n[[sticker:atri/_secret]]", root=tmp_path
    )

    assert reply.delivered == ()
    assert reply.undelivered == ("atri/nope", "atri/_secret")
    assert reply.memory_text == (
        "嗯\n\n[表情包未发出：atri/nope]\n[表情包未发出：atri/_secret]"
    )


def test_sticker_reply_without_markers_is_left_alone(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    invalidate_scan_cache()

    reply = resolve_reply_stickers("就是一句普通回复", root=tmp_path)

    assert reply.memory_text == "就是一句普通回复"
    assert reply.delivered == () and reply.undelivered == ()


def test_resolve_markers_caps_per_reply(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    _write_pack(tmp_path, "panda", {"smug": 1}, theme="t")
    invalidate_scan_cache()

    content = "x\n[[sticker:atri/happy]]\n[[sticker:panda/smug]]\n"
    text, images = resolve_markers(content, root=tmp_path, max_per_reply=1)
    assert text == "x"
    assert len(images) == 1

    text, images = resolve_markers(content, root=tmp_path, max_per_reply=0)
    assert text == "x"
    assert images == ()


def test_resolve_markers_ignores_inline_mentions(tmp_path):
    """只认独占一行：正文里顺带提到的标记不触发发送，也不被吞掉。"""
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    invalidate_scan_cache()

    content = "语法是 [[sticker:atri/happy]] 这样写\n"
    text, images = resolve_markers(content, root=tmp_path)

    assert text == content
    assert images == ()


def test_short_form_picks_are_deterministic_for_the_same_reply(tmp_path):
    """同一段回复两次解析必须取同一张图：实时帧与转录各解析一次。"""
    _write_pack(tmp_path, "atri", {"happy": 5}, theme="t")
    invalidate_scan_cache()
    content = "好耶\n[[sticker:atri/happy]]\n"

    first = resolve_markers(content, root=tmp_path)
    second = resolve_markers(content, root=tmp_path)

    assert first[1] == second[1]
    assert len(first[1]) == 1


def test_render_marker_images_rewrites_for_the_transcript(tmp_path):
    _write_pack(tmp_path, "atri", {"happy": 2}, theme="t")
    invalidate_scan_cache()
    content = "好耶\n[[sticker:atri/happy]]\n"

    rendered = render_marker_images(content, root=tmp_path)

    assert "[[sticker:" not in rendered
    assert rendered.startswith("好耶\n\n![](")
    _, paths = resolve_markers(content, root=tmp_path)
    assert f"![]({paths[0]})" in rendered


def test_max_per_reply_comes_from_config_when_not_passed(tmp_path, monkeypatch):
    """两条出站车道不传预算，统一读 stickers.max_per_reply。"""
    _write_pack(tmp_path, "atri", {"happy": 1}, theme="t")
    invalidate_scan_cache()
    monkeypatch.setattr("g3ku.stickers.config.max_per_reply", lambda: 0)

    text, images = resolve_markers("x\n[[sticker:atri/happy]]\n", root=tmp_path)

    assert text == "x"
    assert images == ()


def test_stickers_config_clamps_max_per_reply():
    from g3ku.config.schema import StickersConfig

    assert StickersConfig().max_per_reply == 1
    assert StickersConfig(max_per_reply=9).max_per_reply == 4
    assert StickersConfig(max_per_reply=-3).max_per_reply == 0
    assert StickersConfig(max_per_reply="junk").max_per_reply == 1


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


def test_web_reply_delta_frame_never_carries_a_marker():
    """网页流式帧：完整标记剔除、半截标记扣住、纯标记帧整帧丢弃。"""
    from g3ku.runtime.api.websocket_ceo import _reply_delta_frame

    partial = _reply_delta_frame({"text": "好耶\n[[sticker:atri/ha", "turn_id": "t1", "seq": 3})
    assert partial == {"turn_id": "t1", "source": "user", "text": "好耶\n", "seq": 3}

    complete = _reply_delta_frame({"text": "好耶\n[[sticker:atri/happy]]\n", "seq": 4})
    assert complete is not None and "[[" not in complete["text"]

    # 整帧只有半截标记：这一帧没有可显示内容，别推空文本
    assert _reply_delta_frame({"text": "[[sticker:atri/ha"}) is None
