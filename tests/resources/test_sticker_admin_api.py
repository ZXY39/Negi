from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.config.schema import Config
from g3ku.stickers import admin as sticker_admin
from main.api import admin_rest

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _write_pack(root: Path, name: str = "demo", *, theme: str = "测试包") -> Path:
    pack = root / name
    for category in ("happy", "awkward"):
        target = pack / category
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{category}-a.png").write_bytes(PNG)
    (pack / "manifest.json").write_text(
        json.dumps(
            {"theme": theme, "theme_source": "human", "categories": {"happy": {"description": "开心"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return pack


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    stickers = tmp_path / "stickers"
    stickers.mkdir()
    _write_pack(stickers)

    monkeypatch.setattr(sticker_admin, "stickers_root", lambda: stickers)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app)


def test_list_packs_returns_skeleton_without_images(client: TestClient):
    payload = client.get("/api/resources/stickers/packs").json()

    assert payload["ok"] is True
    pack = payload["items"][0]
    assert pack["dir_name"] == "demo"
    assert pack["total"] == 2
    assert [category["key"] for category in pack["categories"]] == ["awkward", "happy"]
    assert all(category["images"] == [] for category in pack["categories"])


def test_pack_detail_carries_capped_image_urls(client: TestClient):
    payload = client.get("/api/resources/stickers/packs/demo", params={"per_category": 1}).json()

    happy = next(item for item in payload["item"]["categories"] if item["key"] == "happy")
    assert happy["count"] == 1
    assert happy["images"][0]["name"] == "happy-a.png"
    assert happy["images"][0]["url"].startswith("/api/resources/stickers/image?pack=demo&category=happy&name=")


def test_image_route_serves_bytes_inline(client: TestClient):
    response = client.get(
        "/api/resources/stickers/image",
        params={"pack": "demo", "category": "happy", "name": "happy-a.png"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == PNG


@pytest.mark.parametrize("name", ["../../manifest.json", "..", "nope.png"])
def test_image_route_rejects_traversal_and_missing_files(client: TestClient, name: str):
    response = client.get(
        "/api/resources/stickers/image",
        params={"pack": "demo", "category": "happy", "name": name},
    )
    assert response.status_code in {400, 404}
    assert response.json()["detail"]["code"] in {"invalid_sticker_name", "image_not_found"}


def test_theme_update_returns_the_refreshed_pack(client: TestClient):
    response = client.put("/api/resources/stickers/packs/demo/theme", json={"theme": "新主题"})

    assert response.status_code == 200
    assert response.json()["item"]["theme"] == "新主题"
    assert response.json()["item"]["theme_source"] == "human"


def test_disable_pack_renames_and_returns_new_dir_name(client: TestClient):
    response = client.post("/api/resources/stickers/packs/demo/enabled", json={"enabled": False})

    assert response.status_code == 200
    body = response.json()
    assert body["dir_name"] == "_demo"
    assert body["item"]["dir_name"] == "_demo"
    assert body["item"]["enabled"] is False
    listed = client.get("/api/resources/stickers/packs").json()["items"]
    assert [pack["dir_name"] for pack in listed] == ["_demo"]


def test_enabled_flag_must_be_boolean(client: TestClient):
    response = client.post("/api/resources/stickers/packs/demo/enabled", json={"enabled": "false"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_enabled"


def test_create_category_conflict_is_409(client: TestClient):
    response = client.post("/api/resources/stickers/packs/demo/categories", json={"key": "happy"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "sticker_name_conflict"


def test_upload_adds_images_to_the_category(client: TestClient):
    response = client.post(
        "/api/resources/stickers/upload",
        params={"pack": "demo", "category": "happy"},
        files=[("files", ("party.png", PNG, "image/png")), ("files", ("party.png", PNG, "image/png"))],
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["items"]] == ["party.png", "party_2.png"]
    happy = next(item for item in body["item"]["categories"] if item["key"] == "happy")
    assert happy["count"] == 3


def test_upload_rejects_non_image(client: TestClient):
    response = client.post(
        "/api/resources/stickers/upload",
        params={"pack": "demo", "category": "happy"},
        files=[("files", ("notes.txt", b"nope", "text/plain"))],
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unsupported_image_type"


def test_delete_image_then_category_then_pack(client: TestClient):
    first = client.delete(
        "/api/resources/stickers/packs/demo/categories/happy/images",
        params={"name": "happy-a.png"},
    )
    assert first.status_code == 200
    assert first.json()["item"]["total"] == 1

    second = client.delete("/api/resources/stickers/packs/demo/categories/awkward")
    assert second.status_code == 200
    assert second.json()["deleted"] == {"images": 1, "files": 1}

    third = client.delete("/api/resources/stickers/packs/demo")
    assert third.status_code == 200
    assert third.json()["deleted"] == {"images": 0, "files": 0}
    assert client.get("/api/resources/stickers/packs").json()["items"] == []


def test_delete_emits_an_audit_event(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    captured: list[dict] = []

    def _capture(subsystem, level, event_type, summary, **kwargs):
        captured.append({"subsystem": subsystem, "level": level, "type": event_type, "summary": summary})
        return True

    monkeypatch.setattr(admin_rest.audit_events, "emit_audit_event", _capture)

    client.delete("/api/resources/stickers/packs/demo/categories/awkward")

    assert captured == [
        {
            "subsystem": "stickers",
            "level": "warning",
            "type": "sticker_category_deleted",
            "summary": "删除表情包分类：demo/awkward（1 张图）",
        }
    ]


def test_settings_round_trip(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    config = Config()
    saved: list[Config] = []
    refreshed: list[str] = []

    async def _refresh(reason: str, **_kwargs):
        refreshed.append(reason)
        return {"saved": True}

    monkeypatch.setattr(admin_rest, "load_config", lambda: config)
    monkeypatch.setattr(admin_rest, "save_config", lambda cfg: saved.append(cfg))
    monkeypatch.setattr(admin_rest, "_refresh_runtime_after_save", _refresh)

    initial = client.get("/api/resources/stickers/settings").json()
    assert initial["max_per_reply"] == 1
    assert initial["max_per_reply_limit"] == 4
    assert isinstance(initial["injected_chars"], int)
    assert initial["injected_limit"] > 0
    assert initial["over_injected_limit"] is False
    assert "semantic_search" not in initial

    response = client.put("/api/resources/stickers/settings", json={"max_per_reply": 3})
    assert response.status_code == 200
    assert config.stickers.max_per_reply == 3
    assert len(saved) == 1
    assert refreshed == ["admin_stickers_settings_update"]

    # 改回默认值也要落盘：_tuned_block 会把空块从 payload 里删掉，下次读回 schema 默认。
    client.put("/api/resources/stickers/settings", json={"max_per_reply": 1})
    assert config.stickers.max_per_reply == 1


@pytest.mark.parametrize("value", [-1, 5, "junk"])
def test_settings_rejects_out_of_range(client: TestClient, monkeypatch: pytest.MonkeyPatch, value):
    config = Config()
    monkeypatch.setattr(admin_rest, "load_config", lambda: config)
    monkeypatch.setattr(admin_rest, "save_config", lambda cfg: None)

    response = client.put("/api/resources/stickers/settings", json={"max_per_reply": value})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_max_per_reply"


def test_settings_flags_over_injected_limit(monkeypatch: pytest.MonkeyPatch):
    config = Config()
    monkeypatch.setattr(admin_rest.sticker_catalog, "STICKER_INJECTED_CHAR_WARN", 1)

    payload = admin_rest._stickers_settings_payload(config)

    assert payload["injected_limit"] == 1
    assert payload["over_injected_limit"] is (payload["injected_chars"] > 1)
