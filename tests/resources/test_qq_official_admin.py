"""Tests for the qq-bot admin endpoints (settings + status).

Locks down the same secret-overlay contract the external-token admin tests
already pin: the AppSecret is write-only through the admin surface, stored in
the overlay, and never re-echoed — only an ``has_secret`` flag + a mask.
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
        "qqBot": {"enabled": False, "appId": "", "appSecret": "", "sandbox": False},
    }
    (workspace / ".g3ku" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    _write_config(workspace)
    monkeypatch.chdir(workspace)
    get_bootstrap_security_service(workspace).setup_initial_realm(password="owner-password")
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app)


def test_qq_bot_settings_accept_secret_into_overlay_and_mask(client: TestClient) -> None:
    initial = client.get("/api/qq-bot/settings")
    assert initial.status_code == 200
    assert initial.json()["enabled"] is False
    assert initial.json()["has_secret"] is False

    saved = client.put("/api/qq-bot/settings", json={"enabled": True, "app_id": "123456", "app_secret": "super-secret-app"})
    assert saved.status_code == 200
    body = saved.json()
    assert body["enabled"] is True
    assert body["app_id"] == "123456"
    assert body["has_secret"] is True
    assert body["app_secret_masked"]
    assert "super-secret-app" not in json.dumps(body, ensure_ascii=False)

    # On-disk config keeps an empty placeholder; the secret lives in the overlay.
    on_disk = json.loads((Path(".g3ku") / "config.json").read_text(encoding="utf-8"))
    assert on_disk["qqBot"]["appSecret"] == ""
    assert on_disk["qqBot"]["appId"] == "123456"
    overlay = get_bootstrap_security_service(Path.cwd()).current_overlay()
    assert overlay.get("config.qqBot.appSecret") == "super-secret-app"

    reread = client.get("/api/qq-bot/settings").json()
    assert reread["has_secret"] is True
    assert "super-secret-app" not in json.dumps(reread, ensure_ascii=False)


def test_qq_bot_status_endpoint_reports_service_state(client: TestClient) -> None:
    status = client.get("/api/qq-bot/status")
    assert status.status_code == 200
    assert "state" in status.json()["service"]