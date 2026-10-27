"""The meme pack import script: shape detection, safety, idempotency, report."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "skills" / "meme" / "scripts" / "import_pack.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("sticker_import_pack", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses 解析字符串注解时要能在 sys.modules 里找到这个模块
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def importer():
    return _load_module()


def _astrbot_pack(root: Path, *, pack_id: str = "atri-meme-pack") -> Path:
    pack = root / "src"
    (pack / "memes" / "happy").mkdir(parents=True)
    (pack / "memes" / "awkward").mkdir(parents=True)
    (pack / "previews").mkdir(parents=True)
    (pack / "memes" / "happy" / "h1.png").write_bytes(b"\x89PNG-happy-1")
    (pack / "memes" / "happy" / "h2.png").write_bytes(b"\x89PNG-happy-2")
    (pack / "memes" / "awkward" / "a1.gif").write_bytes(b"GIF89a-awkward")
    (pack / "previews" / "cover.png").write_bytes(b"\x89PNG-cover")
    (pack / "semantic_metadata.json").write_text('{"images": {}}', encoding="utf-8")
    (pack / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": pack_id,
                "version": "1.4.2",
                "categories": {
                    "happy": {"description": "开心大笑、庆祝"},
                    "awkward": {"description": "尴尬、被说中"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return pack


def test_imports_astrbot_pack_with_categories_and_descriptions(tmp_path, importer, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = _astrbot_pack(tmp_path)

    code = importer.main([str(source), "--theme", "动漫少女，日常卖萌"])

    assert code == 0
    report = capsys.readouterr().out
    assert "source: astrbot" in report
    assert "previews: 1 ignored" in report
    assert "images: copied=3" in report

    pack_dir = tmp_path / "stickers" / "atri-meme-pack"
    assert sorted(item.name for item in (pack_dir / "happy").iterdir()) == ["h1.png", "h2.png"]
    assert [item.name for item in (pack_dir / "awkward").iterdir()] == ["a1.gif"]
    assert not (pack_dir / "previews").exists()
    assert (pack_dir / "semantic" / "metadata.source.json").is_file()

    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["theme"] == "动漫少女，日常卖萌"
    assert manifest["theme_source"] == "human"
    assert manifest["categories"]["happy"]["description"] == "开心大笑、庆祝"
    assert manifest["source"]["id"] == "atri-meme-pack"
    assert manifest["source"]["version"] == "1.4.2"
    assert "imported_at" in manifest["source"]


def test_reimport_is_idempotent_and_refuses_without_force(tmp_path, importer, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = _astrbot_pack(tmp_path)
    assert importer.main([str(source), "--theme", "主题"]) == 0

    capsys.readouterr()
    assert importer.main([str(source), "--theme", "主题"]) == 2
    assert "已存在" in capsys.readouterr().err

    assert importer.main([str(source), "--theme", "主题", "--force"]) == 0
    report = capsys.readouterr().out
    assert "images: copied=0" in report
    assert "duplicate=3" in report


def test_flat_pile_lands_in_review_and_classify_map_routes_it(tmp_path, importer, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "pile"
    source.mkdir()
    (source / "one.png").write_bytes(b"\x89PNG-one")
    (source / "two.png").write_bytes(b"\x89PNG-two")

    assert importer.main([str(source), "--pack", "pile", "--theme", "杂图"]) == 0
    pack_dir = tmp_path / "stickers" / "pile"
    assert len(list((pack_dir / "_review").iterdir())) == 2
    assert sorted(item.name for item in pack_dir.iterdir() if item.is_dir()) == ["_review"]

    mapping = tmp_path / "map.json"
    mapping.write_text(json.dumps({"one.png": "happy"}), encoding="utf-8")
    assert (
        importer.main(
            [str(source), "--pack", "pile2", "--theme", "杂图", "--classify-map", str(mapping)]
        )
        == 0
    )
    assert [item.name for item in (tmp_path / "stickers" / "pile2" / "happy").iterdir()] == [
        "one.png"
    ]
    assert len(list((tmp_path / "stickers" / "pile2" / "_review").iterdir())) == 1


def test_dry_run_reports_theme_samples_without_writing_the_pack(tmp_path, importer, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = _astrbot_pack(tmp_path)

    assert importer.main([str(source), "--dry-run"]) == 0
    report = capsys.readouterr().out

    assert not (tmp_path / "stickers" / "atri-meme-pack").exists()
    assert "theme: 未设置" in report
    samples = list((tmp_path / "stickers" / ".import").glob("atri-meme-pack-theme-*"))
    assert samples and all(item.is_file() for item in samples)
    assert str(samples[0]) in report


def test_zip_slip_members_are_dropped(tmp_path, importer, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../evil.png", b"\x89PNG-evil")
        handle.writestr("memes/happy/ok.png", b"\x89PNG-ok")
        handle.writestr(
            "manifest.json",
            json.dumps({"schema_version": 1, "id": "zip-pack", "categories": {}}),
        )

    assert importer.main([str(archive), "--theme", "zip 包"]) == 0

    assert (tmp_path / "stickers" / "zip-pack" / "happy" / "ok.png").is_file()
    assert not (tmp_path / "evil.png").exists()
    assert not (tmp_path.parent / "evil.png").exists()


def test_unknown_schema_version_is_refused(tmp_path, importer, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = _astrbot_pack(tmp_path)
    manifest_path = source / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SystemExit):
        importer.main([str(source)])


def _astrbot_manifest(pack: Path, payload: dict, *, bom: bool = False) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    (pack / "manifest.json").write_text(
        ("\ufeff" if bom else "") + text, encoding="utf-8"
    )


def test_manifest_with_bom_and_no_schema_version_is_accepted(tmp_path, importer, monkeypatch, capsys):
    """实测的社区包同时命中两条：Windows 编辑器写了 BOM，且没有 schema_version。"""
    monkeypatch.chdir(tmp_path)
    source = _astrbot_pack(tmp_path)
    _astrbot_manifest(
        source,
        {"id": "community-pack", "version": "1.0.0", "categories": {"happy": {"description": "开心"}}},
        bom=True,
    )

    assert importer.main([str(source), "--theme", "社区包"]) == 0

    pack_dir = tmp_path / "stickers" / "community-pack"
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["categories"]["happy"]["description"] == "开心"


def test_zero_width_characters_do_not_break_description_lookup(tmp_path, importer, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src"
    # 目录名与 manifest 键都带 ZWNJ：按原始字符串匹配描述会整片落空。
    (source / "memes" / "\u200bkg").mkdir(parents=True)
    (source / "memes" / "\u200bkg" / "k1.png").write_bytes(b"\x89PNG-kg")
    _astrbot_manifest(
        source,
        {
            "id": "zwnj-pack",
            "categories": {"\u200bkg": {"description": "公斤、压力梗"}},
        },
    )

    assert importer.main([str(source), "--theme", "零宽包"]) == 0

    pack_dir = tmp_path / "stickers" / "zwnj-pack"
    assert [item.name for item in (pack_dir / "kg").iterdir()] == ["k1.png"]
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["categories"]["kg"]["description"] == "公斤、压力梗"


def test_hidden_categories_keep_their_prefix(tmp_path, importer, monkeypatch, capsys):
    """AstrBot 的 `_` = 特殊场景，Negi 的 `_` = 隐身：两者语义对齐，导入必须保留。"""
    monkeypatch.chdir(tmp_path)
    source = _astrbot_pack(tmp_path)
    (source / "memes" / "_stock").mkdir(parents=True)
    (source / "memes" / "_stock" / "s1.png").write_bytes(b"\x89PNG-stock")
    payload = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    payload["categories"]["_stock"] = {"description": "股票梗，特殊场景"}
    _astrbot_manifest(source, payload)

    assert importer.main([str(source), "--theme", "带隐藏分类"]) == 0

    report = capsys.readouterr().out
    assert "hidden: 1 个分类以 `_` 开头，共 1 张" in report

    pack_dir = tmp_path / "stickers" / "atri-meme-pack"
    assert (pack_dir / "_stock" / "s1.png").is_file()
    # manifest 的键是展示键（去掉 `_`），catalog 与管理页都按它查描述。
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["categories"]["stock"]["description"] == "股票梗，特殊场景"
    assert "_stock" not in manifest["categories"]


def test_same_image_in_two_categories_is_kept_in_both(tmp_path, importer, monkeypatch):
    """全包去重会吃掉"一张图属于多个分类"的成员关系，实测丢过 33 张、3 个分类。"""
    monkeypatch.chdir(tmp_path)
    source = _astrbot_pack(tmp_path)
    shared = b"\x89PNG-shared-bytes"
    (source / "memes" / "happy" / "shared.png").write_bytes(shared)
    (source / "memes" / "awkward" / "shared.png").write_bytes(shared)

    assert importer.main([str(source), "--theme", "跨分类同图"]) == 0

    pack_dir = tmp_path / "stickers" / "atri-meme-pack"
    assert (pack_dir / "happy" / "shared.png").is_file()
    assert (pack_dir / "awkward" / "shared.png").is_file()

    # 重跑仍然幂等：两个分类各自都已经有这张图。
    assert importer.main([str(source), "--theme", "跨分类同图", "--force"]) == 0


def test_interrupted_checkout_is_reported_not_silently_leaked(tmp_path, importer, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = _astrbot_pack(tmp_path)
    (tmp_path / "temp").mkdir()
    stale = tmp_path / "temp" / "sticker-import-stale"
    (stale / "checkout").mkdir(parents=True)
    old = 1_000_000
    import os

    os.utime(stale, (old, old))

    assert importer.main([str(source), "--theme", "清临时"]) == 0

    # 六小时以上的遗留 checkout 在下次运行开头就被清掉。
    assert not stale.exists()
