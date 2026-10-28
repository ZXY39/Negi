"""Registry: numbering, bootstrap migration, cap, and fail-closed resolution."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from g3ku.stickers import registry
from g3ku.stickers.marker import resolve_markers, resolve_reply_stickers

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _category(root: Path, pack: str, category: str, count: int, *, suffix: str = ".png") -> Path:
    target = root / pack / category
    target.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (target / f"raw-{index}{suffix}").write_bytes(PNG + bytes([index]))
    return target


def _bare_pack(root: Path, pack: str = "demo") -> None:
    (root / pack).mkdir(parents=True, exist_ok=True)


def test_bootstrap_numbers_existing_files_and_is_idempotent(tmp_path: Path):
    _category(tmp_path, "demo", "happy", 3)
    _bare_pack(tmp_path)

    registry.ensure_bootstrap(tmp_path / "demo")
    rows = registry.category_rows(tmp_path / "demo", "happy")

    assert sorted(rows) == ["001.png", "002.png", "003.png"]
    assert sorted(path.name for path in (tmp_path / "demo" / "happy").iterdir()) == [
        "001.png",
        "002.png",
        "003.png",
    ]

    first = dict(rows)
    registry._BOOTSTRAPPED.clear()
    registry.ensure_bootstrap(tmp_path / "demo")
    assert registry.category_rows(tmp_path / "demo", "happy") == first


def test_timestamp_like_names_are_minted_not_trusted(tmp_path: Path):
    """社区包大量用 13 位时间戳当文件名，不能当成"已编号"保留。"""
    target = tmp_path / "demo" / "happy"
    target.mkdir(parents=True)
    (target / "1790955203428.gif").write_bytes(PNG + b"a")
    (target / "007.png").write_bytes(PNG + b"b")

    registry.ensure_bootstrap(tmp_path / "demo")

    rows = registry.category_rows(tmp_path / "demo", "happy")
    # 按文件名顺序处理：007 先入表，时间戳那张从 8 起铸号
    assert sorted(rows) == ["007.png", "008.gif"]
    assert (target / "008.gif").is_file()
    assert registry.index_of("1790955203428.gif") is None


def test_numbering_is_append_only_and_leaves_tombstones(tmp_path: Path):
    _category(tmp_path, "demo", "happy", 2)
    _bare_pack(tmp_path)
    registry.ensure_bootstrap(tmp_path / "demo")

    (tmp_path / "demo" / "happy" / "001.png").unlink()
    registry.unregister_image(tmp_path / "demo", "happy", "001.png")
    row = registry.register_payload(tmp_path / "demo", "happy", "new.png", PNG + b"x")

    assert row["index"] == 3
    assert (tmp_path / "demo" / "happy" / "003.png").is_file()
    # 空洞保留：002 还是原来那张，不重排
    assert sorted(registry.category_rows(tmp_path / "demo", "happy")) == [
        "002.png",
        "003.png",
    ]


def test_cap_rejects_the_thirty_seventh_image(tmp_path: Path):
    _category(tmp_path, "demo", "happy", registry.MAX_IMAGES_PER_CATEGORY)
    _bare_pack(tmp_path)
    registry.ensure_bootstrap(tmp_path / "demo")

    with pytest.raises(registry.RegistryError) as excinfo:
        registry.register_payload(tmp_path / "demo", "happy", "extra.png", PNG + b"z")

    assert excinfo.value.code == "category_full"


def test_resolve_index_fails_closed_on_missing_or_changed_bytes(tmp_path: Path):
    _category(tmp_path, "demo", "happy", 1)
    _bare_pack(tmp_path)
    registry.ensure_bootstrap(tmp_path / "demo")

    assert registry.resolve_index(tmp_path / "demo", "happy", 1) is not None
    assert registry.resolve_index(tmp_path / "demo", "happy", 9) is None

    target = tmp_path / "demo" / "happy" / "001.png"
    target.write_bytes(PNG + b"tampered")
    assert registry.resolve_index(tmp_path / "demo", "happy", 1) is None


def test_index_marker_resolves_and_fails_closed(tmp_path: Path):
    _category(tmp_path, "demo", "happy", 2)
    _bare_pack(tmp_path)

    text, images = resolve_markers(
        "送你了\n[[sticker:demo/happy/2]]", root=tmp_path, rng=random.Random(1)
    )
    assert text == "送你了"
    assert [path.name for path in images] == ["002.png"]

    explicit = resolve_reply_stickers(
        "送你了\n[[sticker:demo/happy/2]]", root=tmp_path, rng=random.Random(1)
    )
    assert explicit.context_text == "送你了\n[[sticker:demo/happy/2]]"
    assert explicit.delivered == ("demo/happy/2",)

    reply = resolve_reply_stickers(
        "送你了\n[[sticker:demo/happy/9]]", root=tmp_path, rng=random.Random(1)
    )
    assert reply.text == "送你了"
    assert reply.context_text == "送你了"
    assert reply.images == ()
    assert reply.undelivered == ("demo/happy/9",)


def test_random_lane_only_picks_registered_files(tmp_path: Path):
    target = _category(tmp_path, "demo", "happy", 1)
    _bare_pack(tmp_path)
    registry.ensure_bootstrap(tmp_path / "demo")
    (target / "stray.png").write_bytes(PNG + b"stray")

    picked = set()
    for seed in range(30):
        _text, images = resolve_markers(
            "[[sticker:demo/happy]]", root=tmp_path, rng=random.Random(seed)
        )
        picked.update(path.name for path in images)

    assert picked == {"001.png"}


def test_register_tool_registers_pending_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import asyncio
    import json

    from g3ku.agent.tools import sticker_register as tool_module
    from g3ku.stickers.catalog import scan_packs

    target = _category(tmp_path, "demo", "happy", 1)
    _bare_pack(tmp_path)
    registry.ensure_bootstrap(tmp_path / "demo")
    (target / "late.png").write_bytes(PNG + b"late")
    monkeypatch.setattr(tool_module, "safe_scan_packs", lambda: scan_packs(tmp_path))

    payload = json.loads(
        asyncio.run(tool_module.StickerRegisterTool().execute(pack="demo"))
    )

    assert payload["ok"] is True
    assert [row["name"] for row in payload["registered"]] == ["002.png"]
    assert payload["pending"] == 0


def test_unregistered_file_is_invisible_until_registered(tmp_path: Path):
    target = _category(tmp_path, "demo", "happy", 1)
    _bare_pack(tmp_path)
    registry.ensure_bootstrap(tmp_path / "demo")
    (target / "late.png").write_bytes(PNG + b"late")

    assert registry.registered_files(tmp_path / "demo", "happy") == (
        (target / "001.png").resolve(),
    )
    assert [path.name for path in registry.unregistered_files(tmp_path / "demo", "happy")] == [
        "late.png"
    ]

    registry.register_existing(tmp_path / "demo", "happy", target / "late.png")
    assert [path.name for path in registry.registered_files(tmp_path / "demo", "happy")] == [
        "001.png",
        "002.png",
    ]
