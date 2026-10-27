"""The meme import script: category shape only, two-phase commit, registry numbering."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

from g3ku.stickers import registry

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "meme" / "scripts" / "import_pack.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sticker_import_pack", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def importer():
    return _load_module()


def _source(root: Path, layout: dict[str, int], *, memes: bool = False) -> Path:
    base = root / "src"
    if memes:
        base.mkdir(parents=True, exist_ok=True)
        (base / "manifest.json").write_text("{}", encoding="utf-8")
        base = base / "memes"
    for category, count in layout.items():
        target = base / category
        target.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (target / f"{category}-{index}.png").write_bytes(b"\x89PNG" + bytes([index, len(category)]))
    return root / "src"


def _map(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _commit(importer, source: Path, mapping: Path, workspace: Path, *extra: str) -> int:
    return importer.main(
        [str(source), "--commit", "--map", str(mapping), "--pack", "atri", "--workspace", str(workspace), *extra]
    )


def test_detect_lists_categories_and_ignores_flat_files(tmp_path, importer, capsys):
    source = _source(tmp_path, {"happy": 2, "awkward": 1})
    (source / "cover.png").write_bytes(b"\x89PNG-cover")

    assert importer.main([str(source), "--detect", "--workspace", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {row["source"]: row for row in payload["categories"]}

    assert sorted(rows) == ["awkward", "happy"]
    assert rows["happy"]["images"] == 2
    assert payload["flat_images"] == 1
    assert payload["pack_exists"] is False
    assert not (tmp_path / "stickers").exists()


def test_commit_registers_numbered_files_and_writes_descriptions(tmp_path, importer, capsys):
    source = _source(tmp_path, {"happy": 2, "awkward": 1})
    mapping = _map(
        tmp_path / "map.json",
        {"happy": "开心大笑、庆祝", "awkward": {"description": "尴尬、被说中"}},
    )

    assert _commit(importer, source, mapping, tmp_path, "--theme", "测试包") == 0
    out = capsys.readouterr().out
    assert "registered: 3" in out
    assert "theme: 测试包" in out

    pack = tmp_path / "stickers" / "atri"
    assert sorted(registry.category_rows(pack, "happy")) == ["001.png", "002.png"]
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["theme"] == "测试包"
    assert manifest["categories"]["happy"]["description"] == "开心大笑、庆祝"
    assert manifest["categories"]["awkward"]["description"] == "尴尬、被说中"


def test_reimport_is_idempotent(tmp_path, importer):
    source = _source(tmp_path, {"happy": 2})
    mapping = _map(tmp_path / "map.json", {"happy": "开心"})

    assert _commit(importer, source, mapping, tmp_path) == 0
    assert _commit(importer, source, mapping, tmp_path) == 0

    pack = tmp_path / "stickers" / "atri"
    assert len(registry.category_rows(pack, "happy")) == 2


def test_missing_mapping_entry_is_refused_without_writing(tmp_path, importer, capsys):
    source = _source(tmp_path, {"happy": 1, "sad": 1})
    mapping = _map(tmp_path / "map.json", {"happy": "开心"})

    assert _commit(importer, source, mapping, tmp_path) == 2
    assert "sad" in capsys.readouterr().out
    assert not (tmp_path / "stickers" / "atri").exists()


def test_hidden_category_prefix_is_preserved(tmp_path, importer):
    source = _source(tmp_path, {"_special": 1, "happy": 1})
    mapping = _map(
        tmp_path / "map.json",
        {"_special": {"category": "_special", "description": "特殊场景"}, "happy": "开心"},
    )

    assert _commit(importer, source, mapping, tmp_path) == 0

    pack = tmp_path / "stickers" / "atri"
    assert len(registry.category_rows(pack, "_special")) == 1


def test_same_image_in_two_categories_is_kept_in_both(tmp_path, importer):
    source = tmp_path / "src"
    payload = b"\x89PNG-shared"
    for category in ("happy", "laugh"):
        (source / category).mkdir(parents=True)
        (source / category / "same.png").write_bytes(payload)
    mapping = _map(
        tmp_path / "map.json",
        {"happy": "开心", "laugh": "大笑"},
    )

    assert _commit(importer, source, mapping, tmp_path) == 0

    pack = tmp_path / "stickers" / "atri"
    assert len(registry.category_rows(pack, "happy")) == 1
    assert len(registry.category_rows(pack, "laugh")) == 1


def test_cap_refuses_the_import_before_writing(tmp_path, importer, capsys):
    source = _source(tmp_path, {"happy": registry.MAX_IMAGES_PER_CATEGORY + 1})
    mapping = _map(tmp_path / "map.json", {"happy": "开心"})

    assert _commit(importer, source, mapping, tmp_path) == 2
    assert "拒绝导入" in capsys.readouterr().out
    pack = tmp_path / "stickers" / "atri"
    assert not registry.category_rows(pack, "happy")


def test_long_description_is_truncated_and_reported(tmp_path, importer, capsys):
    source = _source(tmp_path, {"happy": 1})
    mapping = _map(tmp_path / "map.json", {"happy": "字" * 400})

    assert _commit(importer, source, mapping, tmp_path) == 0
    assert "已截断" in capsys.readouterr().out

    from g3ku.stickers.catalog import CATEGORY_DESCRIPTION_MAX_CHARS

    manifest = json.loads((tmp_path / "stickers" / "atri" / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["categories"]["happy"]["description"]) == CATEGORY_DESCRIPTION_MAX_CHARS


def test_astrbot_memes_root_is_descended(tmp_path, importer, capsys):
    source = _source(tmp_path, {"happy": 2}, memes=True)
    mapping = _map(tmp_path / "map.json", {"happy": "开心"})

    assert _commit(importer, source, mapping, tmp_path) == 0
    assert "registered: 2" in capsys.readouterr().out


def test_flat_pile_is_refused(tmp_path, importer, capsys):
    source = tmp_path / "src"
    source.mkdir()
    for index in range(3):
        (source / f"flat-{index}.png").write_bytes(b"\x89PNG" + bytes([index]))

    assert importer.main([str(source), "--detect", "--workspace", str(tmp_path)]) == 2
    assert "分类形状" in capsys.readouterr().out


def test_zip_slip_members_are_dropped(tmp_path, importer):
    archive_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("happy/ok.png", b"\x89PNG-ok")
        archive.writestr("../evil.png", b"\x89PNG-evil")
        archive.writestr("/abs.png", b"\x89PNG-abs")

    checkout = tmp_path / "checkout"
    importer._extract_zip(archive_path, checkout)

    assert (checkout / "happy" / "ok.png").is_file()
    assert not (tmp_path / "evil.png").exists()
    assert not (checkout / "abs.png").exists()
