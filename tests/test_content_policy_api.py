"""GET/PUT /api/content-policy：读回、规范化、原子写与非法载荷。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.deployment import data_root as data_root_module
from g3ku.security import content_policy as cp
from main.api import admin_rest


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    cp.load_policy(force=True)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    try:
        yield TestClient(app)
    finally:
        data_root_module.reset_cache()


def test_get_reports_disabled_when_file_missing(client) -> None:
    response = client.get("/api/content-policy")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "enabled": False,
        "words": [],
        "count": 0,
        "updated_at": "",
    }


def test_put_normalizes_and_get_reads_back(client) -> None:
    response = client.put(
        "/api/content-policy",
        json={"enabled": True, "words": [" 六四 ", "", "六四", "卐", "six,four"]},
    )
    assert response.status_code == 200
    saved = response.json()
    assert saved["enabled"] is True
    assert saved["words"] == ["六四", "卐", "six,four"]
    assert saved["count"] == 3
    assert saved["updated_at"]

    readback = client.get("/api/content-policy").json()
    assert readback == saved
    on_disk = json.loads(cp.policy_path().read_text(encoding="utf-8"))
    assert on_disk["enabled"] is True
    assert on_disk["words"] == ["六四", "卐", "six,four"]
    assert not list(cp.policy_path().parent.glob("content-policy.json.tmp"))


def test_clearing_words_keeps_enabled(client) -> None:
    client.put("/api/content-policy", json={"enabled": True, "words": ["六四"]})
    response = client.put("/api/content-policy", json={"enabled": True, "words": []})
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["words"] == []
    assert payload["count"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"enabled": "true", "words": []},
        {"enabled": 1, "words": []},
        {"enabled": True},
        {"enabled": True, "words": "六四"},
        {"enabled": True, "words": ["六四", 1]},
        {"enabled": True, "words": [None]},
    ],
)
def test_put_rejects_invalid_payload(client, body) -> None:
    response = client.put("/api/content-policy", json=body)
    assert response.status_code == 400
    assert client.get("/api/content-policy").json()["enabled"] is False
