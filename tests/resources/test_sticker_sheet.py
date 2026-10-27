"""Contact-sheet rendering and the show_sticker_category tool contract."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from g3ku.agent.tools import sticker_sheet as tool_module
from g3ku.stickers import registry
from g3ku.stickers import sheet as sheet_render
from g3ku.stickers.catalog import scan_packs


def _png(seed: int, size: tuple[int, int] = (64, 64)) -> bytes:
    image = Image.new("RGBA", size, (seed * 7 % 255, seed * 13 % 255, 90, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _pack(
    root: Path, categories: dict[str, int] | None = None, *, bootstrap: bool = True
) -> Path:
    pack = root / "stickers" / "demo"
    layout = categories if categories is not None else {"happy": 3}
    for category, count in layout.items():
        target = pack / category
        target.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (target / f"raw-{index}.png").write_bytes(_png(index + len(category)))
    (pack / "manifest.json").write_text(
        json.dumps({"theme": "测试", "categories": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    if bootstrap:
        registry.ensure_bootstrap(pack)
    return pack


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sheet_render, "stickers_root", lambda: tmp_path / "stickers")
    monkeypatch.setattr(tool_module, "safe_scan_packs", lambda: scan_packs(tmp_path / "stickers"))
    return tmp_path


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def test_render_sheet_numbers_tiles_and_caches(workspace: Path):
    _pack(workspace)
    target = scan_packs(workspace / "stickers")[0]

    first = sheet_render.render_sheet(target, "happy")
    assert first is not None
    assert (first.count, first.columns, first.rows) == (3, 2, 2)
    assert first.path.is_file() and first.bytes > 0
    assert first.path.parent == workspace / ".g3ku" / "sticker-sheets"
    assert first.mime_type in {"image/png", "image/jpeg"}

    stamp = first.path.stat().st_mtime_ns
    again = sheet_render.render_sheet(target, "happy")
    assert again is not None
    assert again.path == first.path
    assert again.path.stat().st_mtime_ns == stamp


def test_render_sheet_is_none_for_empty_category(workspace: Path):
    _pack(workspace, {"happy": 1, "empty": 0})
    target = scan_packs(workspace / "stickers")[0]

    assert sheet_render.render_sheet(target, "empty") is None


def test_tool_refuses_without_multimodal_and_points_at_category_marker(workspace: Path):
    _pack(workspace)
    tool = tool_module.StickerSheetTool(
        runtime={"image_multimodal_enabled": False, "image_model_display_name": "flash1"}
    )

    payload = _run(tool.execute(pack="demo", category="happy"))

    assert payload["ok"] is False
    assert "flash1" in payload["error"]
    assert "[[sticker:<包>/<类别>]]" in payload["error"]


def test_tool_returns_the_overlay_payload(workspace: Path):
    _pack(workspace)
    tool = tool_module.StickerSheetTool(
        runtime={"image_multimodal_enabled": True, "image_model_display_name": "flash1"}
    )

    payload = _run(tool.execute(pack="demo", category="happy"))

    assert payload["ok"] is True
    assert payload["content_kind"] == "image"
    assert payload["multimodal_open_pending"] is True
    assert Path(payload["runtime_image_target"]["path"]).is_file()
    assert payload["count"] == 3
    assert "[[sticker:demo/happy/<序号>]]" in payload["how_to_send"]


def test_tool_rejects_hidden_category_and_single_image(workspace: Path):
    _pack(workspace, {"happy": 2, "_secret": 1, "one": 1})
    tool = tool_module.StickerSheetTool(
        runtime={"image_multimodal_enabled": True, "image_model_display_name": "flash1"}
    )

    hidden_payload = _run(tool.execute(pack="demo", category="_secret"))
    assert hidden_payload["ok"] is False

    single_payload = _run(tool.execute(pack="demo", category="one"))
    assert single_payload["ok"] is False
    assert "只有 1 张" in single_payload["reason"]
