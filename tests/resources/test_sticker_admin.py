from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from g3ku.stickers import admin as sticker_admin
from g3ku.stickers.catalog import PACK_KEY_RE, invalidate_scan_cache, scan_packs

REPO_ROOT = Path(__file__).resolve().parents[2]
IMPORT_SCRIPT = REPO_ROOT / "skills" / "meme" / "scripts" / "import_pack.py"


def _load_import_script():
    spec = importlib.util.spec_from_file_location("meme_import_pack_under_test", IMPORT_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass 会回头查 sys.modules[__module__]，不先登记就抛 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, payload: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _pack(root: Path, name: str = "demo", *, theme: str = "测试包", categories=("happy",)) -> Path:
    pack = root / name
    for category in categories:
        _write(pack / category / f"{category}-a.png", b"\x89PNG" + b"0" * 64)
    (pack / "manifest.json").write_text(
        json.dumps({"theme": theme, "categories": {"happy": {"description": "开心"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return pack


@pytest.fixture(autouse=True)
def _fresh_scan_cache():
    invalidate_scan_cache()
    yield
    invalidate_scan_cache()


# --------------------------------------------------------------- scan


def test_scan_admin_packs_keeps_disabled_packs_and_empty_categories(tmp_path: Path):
    _pack(tmp_path, "demo", categories=("happy", "awkward"))
    _pack(tmp_path, "_parked", categories=("calm",))
    (tmp_path / "demo" / "empty").mkdir()
    # 保留名与禁用名不是包：界面不该把它们列成一个可以管理的包。
    _write(tmp_path / "semantic" / "x" / "a.png")
    _write(tmp_path / "build" / "x" / "a.png")

    packs = sticker_admin.scan_admin_packs(root=tmp_path)

    assert [pack.dir_name for pack in packs] == ["_parked", "demo"]
    parked, demo = packs
    assert (parked.key, parked.enabled, parked.total) == ("parked", False, 1)
    assert demo.enabled is True
    assert [category.dir_name for category in demo.categories] == ["awkward", "empty", "happy"]
    empty = next(category for category in demo.categories if category.dir_name == "empty")
    assert (empty.count, empty.images) == (0, ())


def test_scan_admin_packs_caps_images_but_keeps_true_count(tmp_path: Path):
    pack = tmp_path / "demo"
    for index in range(5):
        _write(pack / "happy" / f"{index}.png")

    counts = sticker_admin.scan_admin_packs(root=tmp_path, per_category=0)
    assert (counts[0].categories[0].count, counts[0].categories[0].images) == (5, ())

    capped = sticker_admin.scan_admin_packs(root=tmp_path, per_category=2)
    assert (capped[0].categories[0].count, len(capped[0].categories[0].images)) == (5, 2)


def test_read_admin_pack_and_list_category_images(tmp_path: Path):
    _pack(tmp_path, "demo")
    pack = sticker_admin.read_admin_pack("demo", root=tmp_path, per_category=12)
    # bootstrap 把存量文件按序号接管：序号就是文件名
    assert pack.categories[0].images[0].name == "001.png"
    assert pack.categories[0].images[0].registered is True
    assert pack.categories[0].images[0].size > 0
    assert (pack.categories[0].registered, pack.categories[0].pending) == (1, 0)

    images, total = sticker_admin.list_category_images("demo", "happy", root=tmp_path)
    assert total == 1 and images[0].name == "001.png"
    assert images[0].registered is True
    window, total = sticker_admin.list_category_images("demo", "happy", root=tmp_path, offset=1, limit=5)
    assert (window, total) == ([], 1)


def test_enabled_categories_do_not_reorder(tmp_path: Path):
    pack = tmp_path / "demo"
    for name in ("_hidden", "zebra", "alpha"):
        _write(pack / name / "a.png")
    _write(pack / "manifest.json", b"{}")

    categories = sticker_admin.read_admin_pack("demo", root=tmp_path).categories

    # 启停只是原地状态：顺序恒按 key，停用的分类不会因为排在后面而换位。
    assert [category.dir_name for category in categories] == ["alpha", "_hidden", "zebra"]


def test_read_admin_pack_missing_is_404(tmp_path: Path):
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.read_admin_pack("nope", root=tmp_path)
    assert (excinfo.value.code, excinfo.value.status_code) == ("pack_not_found", 404)


# --------------------------------------------------------------- path guards


@pytest.mark.parametrize(
    "name",
    ["..", "../escape", "a/b", "a\\b", "", "__", "semantic", "build", "tmp_x", ".hidden", "存在中文"],
)
def test_pack_dir_rejects_illegal_names(tmp_path: Path, name: str):
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.pack_dir(name, root=tmp_path)
    assert excinfo.value.status_code == 400


def test_image_path_rejects_traversal_and_non_images(tmp_path: Path):
    _pack(tmp_path, "demo")
    _write(tmp_path / "demo" / "happy" / "notes.txt")

    for name in ["../../manifest.json", "..", "sub/x.png"]:
        with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
            sticker_admin.image_path("demo", "happy", name, root=tmp_path)
        assert excinfo.value.status_code == 400

    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.image_path("demo", "happy", "notes.txt", root=tmp_path)
    assert excinfo.value.code == "unsupported_image_type"


def test_symlinked_category_outside_root_is_not_listed_and_not_resolvable(tmp_path: Path):
    outside = tmp_path / "outside"
    _write(outside / "x.png")
    pack = tmp_path / "stickers" / "demo"
    pack.mkdir(parents=True)
    try:
        (pack / "linked").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 不可用")

    assert sticker_admin.scan_admin_packs(root=tmp_path / "stickers")[0].categories == ()
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.image_path("demo", "linked", "x.png", root=tmp_path / "stickers")
    assert excinfo.value.code == "category_not_found"


def test_symlinked_image_outside_root_is_refused(tmp_path: Path):
    outside = _write(tmp_path / "outside.png", b"\x89PNG" + b"0" * 8)
    pack = tmp_path / "stickers" / "demo" / "happy"
    pack.mkdir(parents=True)
    try:
        (pack / "link.png").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 不可用")

    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.image_path("demo", "happy", "link.png", root=tmp_path / "stickers")
    assert excinfo.value.code == "image_not_found"


# --------------------------------------------------------------- manifest edits


def test_set_theme_marks_human_and_keeps_provenance(tmp_path: Path):
    pack = _pack(tmp_path, "demo")
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"] = {"kind": "astrbot_pack", "id": "demo"}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    sticker_admin.set_theme("demo", "  猫猫   日常  ", root=tmp_path)

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["theme"] == "猫猫 日常"
    assert saved["theme_source"] == "human"
    assert saved["source"] == {"kind": "astrbot_pack", "id": "demo"}


def test_set_theme_empty_clears_theme_and_source(tmp_path: Path):
    pack = _pack(tmp_path, "demo")
    sticker_admin.set_theme("demo", "", root=tmp_path)
    saved = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert "theme" not in saved and "theme_source" not in saved
    # 分类描述不归主题管，清主题不能顺手抹掉它。
    assert saved["categories"]["happy"]["description"] == "开心"


def test_set_theme_rejects_overlong(tmp_path: Path):
    _pack(tmp_path, "demo")
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.set_theme("demo", "字" * (sticker_admin.THEME_MAX_CHARS + 1), root=tmp_path)
    assert excinfo.value.code == "theme_too_long"


def test_set_category_description_writes_and_clears(tmp_path: Path):
    pack = _pack(tmp_path, "demo")
    sticker_admin.set_category_description("demo", "happy", "庆祝、大笑", root=tmp_path)
    saved = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert saved["categories"]["happy"]["description"] == "庆祝、大笑"

    sticker_admin.set_category_description("demo", "happy", "", root=tmp_path)
    saved = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert "categories" not in saved


def test_set_category_description_accepts_legacy_string_entry(tmp_path: Path):
    pack = _pack(tmp_path, "demo")
    (pack / "manifest.json").write_text(
        json.dumps({"categories": {"happy": "旧字符串描述"}}, ensure_ascii=False), encoding="utf-8"
    )
    sticker_admin.set_category_description("demo", "happy", "新描述", root=tmp_path)
    saved = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert saved["categories"]["happy"] == {"description": "新描述"}


def test_set_category_description_rejects_overlong(tmp_path: Path):
    _pack(tmp_path, "demo")
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.set_category_description(
            "demo", "happy", "字" * (sticker_admin.CATEGORY_DESCRIPTION_MAX_CHARS + 1), root=tmp_path
        )
    assert excinfo.value.code == "description_too_long"


def test_manifest_never_gets_counts(tmp_path: Path):
    pack = _pack(tmp_path, "demo", categories=("happy", "awkward"))
    sticker_admin.set_theme("demo", "主题", root=tmp_path)
    sticker_admin.set_category_description("demo", "happy", "开心", root=tmp_path)
    saved = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert set(saved) == {"theme", "theme_source", "categories"}
    assert set(saved["categories"]) == {"happy"}


# --------------------------------------------------------------- enable / disable


def test_disable_and_enable_pack_round_trip(tmp_path: Path):
    _pack(tmp_path, "demo")

    assert sticker_admin.set_enabled("demo", False, root=tmp_path) == "_demo"
    assert not (tmp_path / "demo").exists()
    assert sticker_admin.scan_admin_packs(root=tmp_path)[0].enabled is False
    # 停用后契约头里就没有这个包了 —— 这正是软删的意义。
    assert scan_packs(tmp_path) == ()

    assert sticker_admin.set_enabled("_demo", True, root=tmp_path) == "demo"
    assert [pack.key for pack in scan_packs(tmp_path)] == ["demo"]


def test_disable_is_idempotent(tmp_path: Path):
    _pack(tmp_path, "_demo")
    assert sticker_admin.set_enabled("_demo", False, root=tmp_path) == "_demo"


def test_disable_conflicts_when_target_exists(tmp_path: Path):
    _pack(tmp_path, "demo")
    _pack(tmp_path, "_demo")
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.set_enabled("demo", False, root=tmp_path)
    assert (excinfo.value.code, excinfo.value.status_code) == ("sticker_name_conflict", 409)


def test_stale_name_retry_after_the_rename_landed_is_success(tmp_path: Path):
    _pack(tmp_path, "demo")

    assert sticker_admin.set_enabled("demo", False, root=tmp_path) == "_demo"
    # 第一次请求已改名、响应丢了；重试仍带旧名时按“已达成”返回，而不是 404。
    assert sticker_admin.set_enabled("demo", False, root=tmp_path) == "_demo"
    assert sticker_admin.set_enabled("_demo", True, root=tmp_path) == "demo"
    assert sticker_admin.set_enabled("_demo", True, root=tmp_path) == "demo"


def test_windows_sharing_violation_is_a_typed_busy_error(tmp_path: Path, monkeypatch):
    _pack(tmp_path, "demo")

    def deny(self, target):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "rename", deny)
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.set_enabled("demo", False, root=tmp_path)
    assert (excinfo.value.code, excinfo.value.status_code) == ("sticker_rename_busy", 409)


def test_category_enable_disable_keeps_manifest(tmp_path: Path):
    pack = _pack(tmp_path, "demo")
    sticker_admin.set_category_enabled("demo", "happy", False, root=tmp_path)
    saved = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert saved["categories"]["happy"]["description"] == "开心"
    categories = sticker_admin.read_admin_pack("demo", root=tmp_path).categories
    assert (categories[0].dir_name, categories[0].enabled) == ("_happy", False)

    sticker_admin.set_category_enabled("demo", "_happy", True, root=tmp_path)
    assert sticker_admin.read_admin_pack("demo", root=tmp_path).categories[0].dir_name == "happy"


# --------------------------------------------------------------- create / upload


def test_create_category_is_admin_visible_before_it_has_images(tmp_path: Path):
    _pack(tmp_path, "demo")

    assert sticker_admin.create_category("demo", "celebrate", "庆祝", root=tmp_path) == "celebrate"

    # 契约头只收有图的分类：空分类是管理端的占位，不进模型清单。
    assert scan_packs(tmp_path)[0].category("celebrate") is None
    created = next(
        category
        for category in sticker_admin.read_admin_pack("demo", root=tmp_path).categories
        if category.key == "celebrate"
    )
    assert (created.count, created.description) == (0, "庆祝")


def test_upload_invalidates_the_scan_cache(tmp_path: Path):
    _pack(tmp_path, "demo")
    sticker_admin.create_category("demo", "celebrate", "庆祝", root=tmp_path)
    assert scan_packs(tmp_path)[0].category("celebrate") is None

    sticker_admin.store_uploads(
        "demo", "celebrate", [("party.png", b"\x89PNG" + b"1" * 8)], root=tmp_path
    )

    # 扫描缓存有 60 秒 TTL：写操作不自己失效，界面与模型都会读到旧值。
    created = scan_packs(tmp_path)[0].category("celebrate")
    assert created is not None
    assert (created.count, created.description) == (1, "庆祝")


@pytest.mark.parametrize("key", ["_hidden", "semantic", "build", "tmp_x", "a/b", "中文"])
def test_create_category_rejects_illegal_keys(tmp_path: Path, key: str):
    _pack(tmp_path, "demo")
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.create_category("demo", key, root=tmp_path)
    assert excinfo.value.status_code == 400


def test_create_category_conflict_is_409(tmp_path: Path):
    _pack(tmp_path, "demo")
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.create_category("demo", "happy", root=tmp_path)
    assert excinfo.value.status_code == 409


def test_store_uploads_writes_and_dedupes(tmp_path: Path):
    _pack(tmp_path, "demo")
    items = sticker_admin.store_uploads(
        "demo",
        "happy",
        [("new.png", b"\x89PNG-new"), ("new.png", b"\x89PNG-second")],
        root=tmp_path,
    )
    # 存量文件在首次写入时被 bootstrap 接管成 001，新图从 002 起铸号
    assert [item["name"] for item in items] == ["002.png", "003.png"]
    assert (tmp_path / "demo" / "happy" / "003.png").read_bytes() == b"\x89PNG-second"
    assert (tmp_path / "demo" / "happy" / "001.png").is_file()


def test_store_uploads_validates_the_whole_batch_before_writing(tmp_path: Path):
    _pack(tmp_path, "demo")
    before = sorted(path.name for path in (tmp_path / "demo" / "happy").iterdir())

    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.store_uploads(
            "demo",
            "happy",
            [("ok.png", b"\x89PNG"), ("notes.txt", b"nope")],
            root=tmp_path,
        )
    assert excinfo.value.code == "unsupported_image_type"
    assert sorted(path.name for path in (tmp_path / "demo" / "happy").iterdir()) == before


def test_store_uploads_rejects_oversize_and_too_many(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _pack(tmp_path, "demo")
    monkeypatch.setattr(sticker_admin, "MAX_IMAGE_BYTES", 4)

    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.store_uploads("demo", "happy", [("big.png", b"12345")], root=tmp_path)
    assert (excinfo.value.code, excinfo.value.status_code) == ("image_too_large", 413)

    monkeypatch.setattr(sticker_admin, "MAX_IMAGE_BYTES", 1024)
    monkeypatch.setattr(sticker_admin, "MAX_UPLOAD_FILES", 1)
    with pytest.raises(sticker_admin.StickerAdminError) as excinfo:
        sticker_admin.store_uploads(
            "demo", "happy", [("a.png", b"1"), ("b.png", b"2")], root=tmp_path
        )
    assert excinfo.value.code == "too_many_files"


def test_uploaded_category_is_resolvable_by_marker(tmp_path: Path):
    """界面上传的图必须能被发送链路认出来，否则管理页就是在往一个死目录里写。"""
    from g3ku.stickers.marker import resolve_markers

    _pack(tmp_path, "demo")
    sticker_admin.create_category("demo", "celebrate", "庆祝", root=tmp_path)
    sticker_admin.store_uploads("demo", "celebrate", [("party.png", b"\x89PNG" + b"1" * 16)], root=tmp_path)

    text, paths = resolve_markers("祝你好运\n[[sticker:demo/celebrate]]", root=tmp_path)

    assert text == "祝你好运"
    assert [path.name for path in paths] == ["001.png"]


# --------------------------------------------------------------- delete


def test_delete_image(tmp_path: Path):
    _pack(tmp_path, "demo")
    sticker_admin.delete_image("demo", "happy", "001.png", root=tmp_path)
    assert not (tmp_path / "demo" / "happy" / "001.png").exists()
    assert sticker_admin.read_admin_pack("demo", root=tmp_path).categories[0].count == 0


def test_delete_category_prunes_manifest(tmp_path: Path):
    pack = _pack(tmp_path, "demo", categories=("happy", "awkward"))
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    manifest["categories"]["awkward"] = {"description": "尴尬"}
    (pack / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    deleted = sticker_admin.delete_category("demo", "happy", root=tmp_path)

    assert deleted == {"images": 1, "files": 1}
    assert not (pack / "happy").exists()
    saved = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert set(saved["categories"]) == {"awkward"}


def test_delete_pack_counts_and_removes(tmp_path: Path):
    _pack(tmp_path, "demo", categories=("happy", "awkward"))
    deleted = sticker_admin.delete_pack("demo", root=tmp_path)
    assert deleted == {"images": 2, "files": 2}
    assert not (tmp_path / "demo").exists()
    assert sticker_admin.scan_admin_packs(root=tmp_path) == ()


def test_delete_pack_cannot_reach_outside_root(tmp_path: Path):
    _pack(tmp_path / "stickers", "demo")
    with pytest.raises(sticker_admin.StickerAdminError):
        sticker_admin.delete_pack("../stickers", root=tmp_path / "stickers" / "demo")
    assert (tmp_path / "stickers" / "demo").exists()


# --------------------------------------------------------------- shared constants


def test_admin_key_rules_match_the_import_script():
    """界面和 agent 必须对"什么名字能建"给出同一个答案。

    两边是两份独立常量（skill 脚本刻意不依赖 g3ku 内部分层），任何一边改动都必须
    在同一批里改另一边，否则界面能建出 agent 建不出的目录。
    """
    script = _load_import_script()
    assert set(script.FORBIDDEN_NAMES) == set(sticker_admin.FORBIDDEN_NAMES)
    assert tuple(script.FORBIDDEN_PREFIXES) == tuple(sticker_admin.FORBIDDEN_PREFIXES)
    assert script.IMAGE_SUFFIXES == sticker_admin.IMAGE_SUFFIXES
    assert script.RESERVED_PACK_DIRS == {"semantic"}
    assert script.MAX_IMAGE_BYTES == sticker_admin.MAX_IMAGE_BYTES
    assert script.KEY_RE.pattern == PACK_KEY_RE.pattern


def test_slug_key_output_always_passes_admin_rules():
    script = _load_import_script()
    for raw in ["高兴", "happy  happy", "_review", "build", "", "a/b", "Cat-1"]:
        assert sticker_admin.admin_segment_ok(script.slug_key(raw))
