"""Tests for the External Agent API token management endpoints.

Locks down the admin surface added after the China channel subsystem removal:
the `/api/external-api/*` endpoints manage `externalApi.enabled` and per-bridge
tokens. Token plaintext is returned exactly once (create/regenerate); every
other read exposes only a mask. Persistence reuses the config save path, so
plaintext lands in the bootstrap secret overlay and the on-disk config keeps
an empty placeholder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.security.bootstrap import get_bootstrap_security_service

import main.api.admin_rest as admin_rest


def _write_config(workspace: Path) -> None:
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    payload = {
        "agents": {
            "defaults": {
                "workspace": ".",
                "runtime": "langgraph",
                "maxTokens": 1,
                "temperature": 0.1,
                "maxToolIterations": 1,
                "memoryWindow": 1,
                "reasoningEffort": "low",
            },
            "roleIterations": {"ceo": 40, "execution": 16, "inspection": 16},
            "multiAgent": {"orchestratorModelKey": None},
        },
        "models": {
            "catalog": [
                {
                    "key": "m",
                    "providerModel": "openai:gpt-4.1",
                    "apiKey": "demo-key",
                    "apiBase": None,
                    "extraHeaders": None,
                    "enabled": True,
                    "maxTokens": 1,
                    "temperature": 0.1,
                    "reasoningEffort": "low",
                    "retryOn": [],
                    "description": "",
                    "contextWindowTokens": 128000,
                }
            ],
            "roles": {"ceo": ["m"], "execution": ["m"], "inspection": ["m"]},
        },
        "providers": {"openai": {"apiKey": "", "apiBase": None, "extraHeaders": None}},
        "web": {"host": "127.0.0.1", "port": 1},
        "toolSecrets": {},
        "resources": {
            "enabled": True,
            "skillsDir": "skills",
            "toolsDir": "tools",
            "manifestName": "resource.yaml",
            "reload": {
                "enabled": True,
                "pollIntervalMs": 1000,
                "debounceMs": 400,
                "lazyReloadOnAccess": True,
                "keepLastGoodVersion": True,
            },
            "locks": {"lockDir": ".g3ku/resource-locks", "logicalDeleteGuard": True, "windowsFsLock": True},
            "statePath": ".g3ku/resources.state.json",
        },
        "mainRuntime": {
            "enabled": True,
            "storePath": ".g3ku/main-runtime/runtime.sqlite3",
            "filesBaseDir": ".g3ku/main-runtime/tasks",
            "artifactDir": ".g3ku/main-runtime/artifacts",
            "governanceStorePath": ".g3ku/main-runtime/governance.sqlite3",
            "defaultMaxDepth": 1,
            "hardMaxDepth": 4,
            "nodeDispatchConcurrency": {"execution": 8, "inspection": 4},
        },
    }
    (workspace / ".g3ku" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    _write_config(workspace)
    monkeypatch.chdir(workspace)
    # Unlocked security service: token secrets round-trip through the overlay.
    get_bootstrap_security_service(workspace).setup_initial_realm(password="owner-password")
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app)


def test_external_api_settings_and_token_crud_roundtrip(client: TestClient) -> None:
    response = client.get("/api/external-api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["items"] == []

    assert client.put("/api/external-api/settings", json={"enabled": True}).status_code == 200

    created = client.post("/api/external-api/tokens", json={"bridge_id": "QQ Home", "label": "家里 NapCat"})
    assert created.status_code == 200
    created_body = created.json()
    assert created_body["bridge_id"] == "qq-home"  # normalized id
    plaintext = created_body["token"]
    assert len(plaintext) >= 32

    listing = client.get("/api/external-api/settings").json()
    assert listing["enabled"] is True
    assert len(listing["items"]) == 1
    item = listing["items"][0]
    assert item["bridge_id"] == "qq-home"
    assert item["label"] == "家里 NapCat"
    assert item["enabled"] is True
    assert item["has_token"] is True
    # Only a mask is exposed after the one-time reveal.
    assert plaintext not in json.dumps(listing, ensure_ascii=False)
    assert item["token_masked"]

    # On-disk config keeps an empty placeholder; the secret lives in the overlay.
    saved = json.loads((Path(".g3ku") / "config.json").read_text(encoding="utf-8"))
    assert saved["externalApi"]["tokens"]["qq-home"]["token"] == ""
    security = get_bootstrap_security_service(Path.cwd())
    assert security.current_overlay().get("config.externalApi.tokens.qq-home.token") == plaintext

    toggled = client.patch("/api/external-api/tokens/qq-home", json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["items"][0]["enabled"] is False

    regenerated = client.patch("/api/external-api/tokens/qq-home", json={"regenerate": True})
    assert regenerated.status_code == 200
    new_plaintext = regenerated.json()["token"]
    assert new_plaintext and new_plaintext != plaintext
    assert security.current_overlay().get("config.externalApi.tokens.qq-home.token") == new_plaintext

    deleted = client.delete("/api/external-api/tokens/qq-home")
    assert deleted.status_code == 200
    assert deleted.json()["items"] == []


def test_external_api_token_create_requires_bridge_id_and_rejects_duplicates(client: TestClient) -> None:
    missing = client.post("/api/external-api/tokens", json={"label": "no id"})
    assert missing.status_code == 400

    assert client.post("/api/external-api/tokens", json={"bridge_id": "qq"}).status_code == 200
    conflict = client.post("/api/external-api/tokens", json={"bridge_id": "qq"})
    assert conflict.status_code == 409


def test_external_api_token_update_and_delete_require_existing_bridge(client: TestClient) -> None:
    assert client.patch("/api/external-api/tokens/ghost", json={"enabled": False}).status_code == 404
    assert client.delete("/api/external-api/tokens/ghost").status_code == 404
