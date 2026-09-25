"""Tests for the qq-bot admin endpoints (settings + status).

Locks down the same secret-overlay contract the external-token admin tests
already pin — per AppID: each account's AppSecret is write-only through the
admin surface, stored in the overlay under its own key, and never re-echoed
(only ``has_secret`` + a mask). Also pins the whole-table PUT semantics and the
legacy single-account fold surviving the overlay round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.security.bootstrap import get_bootstrap_security_service

import main.api.admin_rest as admin_rest


def _config_payload(qq_bot: dict) -> dict:
    return {
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
        "qqBot": qq_bot,
    }


def _write_config(workspace: Path, qq_bot: dict | None = None) -> None:
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    payload = _config_payload(qq_bot if qq_bot is not None else {"enabled": False, "accounts": {}})
    (workspace / ".g3ku" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _client_for(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.chdir(workspace)
    get_bootstrap_security_service(workspace).setup_initial_realm(password="owner-password")
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    _write_config(workspace)
    return _client_for(workspace, monkeypatch)


def _row(payload: dict, app_id: str) -> dict:
    return next(row for row in payload["accounts"] if row["app_id"] == app_id)


def test_qq_bot_settings_accept_secret_into_overlay_and_mask(client: TestClient) -> None:
    initial = client.get("/api/qq-bot/settings")
    assert initial.status_code == 200
    assert initial.json()["enabled"] is False
    assert initial.json()["accounts"] == []

    saved = client.put(
        "/api/qq-bot/settings",
        json={"enabled": True, "accounts": [{"app_id": "123456", "app_secret": "super-secret-app"}]},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["enabled"] is True
    row = _row(body, "123456")
    assert row["has_secret"] is True
    assert row["app_secret_masked"]
    assert row["bridge_id"] == "qq-official-123456"
    assert "super-secret-app" not in json.dumps(body, ensure_ascii=False)

    # On-disk config keeps an empty placeholder; the secret lives in the overlay
    # under a per-AppID key.
    on_disk = json.loads((Path(".g3ku") / "config.json").read_text(encoding="utf-8"))
    assert on_disk["qqBot"]["accounts"]["123456"]["appSecret"] == ""
    overlay = get_bootstrap_security_service(Path.cwd()).current_overlay()
    assert overlay.get("config.qqBot.accounts.123456.appSecret") == "super-secret-app"

    reread = client.get("/api/qq-bot/settings").json()
    assert _row(reread, "123456")["has_secret"] is True
    assert "super-secret-app" not in json.dumps(reread, ensure_ascii=False)


def test_second_account_does_not_touch_first_secret(client: TestClient) -> None:
    client.put(
        "/api/qq-bot/settings",
        json={"enabled": True, "accounts": [{"app_id": "111", "app_secret": "secret-one", "label": "主号"}]},
    )
    saved = client.put(
        "/api/qq-bot/settings",
        json={
            "enabled": True,
            "accounts": [
                {"app_id": "111", "app_secret": "", "label": "主号"},
                {"app_id": "222", "app_secret": "secret-two", "sandbox": True},
            ],
        },
    )
    body = saved.json()
    assert _row(body, "111")["has_secret"] is True
    assert _row(body, "222")["has_secret"] is True
    assert _row(body, "222")["sandbox"] is True
    overlay = get_bootstrap_security_service(Path.cwd()).current_overlay()
    assert overlay.get("config.qqBot.accounts.111.appSecret") == "secret-one"
    assert overlay.get("config.qqBot.accounts.222.appSecret") == "secret-two"

    # 删掉 222 后它的桥凭证被停用（不删除），111 的仍在启用。
    from g3ku.config.loader import load_config, save_config
    from g3ku.config.schema import ExternalApiTokenConfig

    seeded = load_config()
    seeded.external_api.tokens["qq-official-111"] = ExternalApiTokenConfig(token="t-one", label="官方 QQ 机器人 111")
    seeded.external_api.tokens["qq-official-222"] = ExternalApiTokenConfig(token="t-two", label="官方 QQ 机器人 222")
    save_config(seeded)

    client.put("/api/qq-bot/settings", json={"enabled": True, "accounts": [{"app_id": "111", "app_secret": ""}]})
    cfg = client.get("/api/external-api/settings").json()
    tokens = {entry["bridge_id"]: entry for entry in cfg["items"]}
    assert tokens["qq-official-222"]["enabled"] is False
    assert tokens["qq-official-111"]["enabled"] is True


def test_legacy_single_account_folds_without_losing_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移必须发生在覆盖层回填之后，否则账号折出来了但密钥丢了。"""
    workspace = tmp_path / "workspace"
    _write_config(workspace, {"enabled": True, "appId": "654321", "appSecret": "", "sandbox": False})
    client = _client_for(workspace, monkeypatch)
    security = get_bootstrap_security_service(workspace)
    security.set_overlay_values({"config.qqBot.appSecret": "legacy-secret"})

    body = client.get("/api/qq-bot/settings").json()
    assert body["enabled"] is True
    row = _row(body, "654321")
    assert row["has_secret"] is True
    assert row["bridge_id"] == "qq-official-654321"

    on_disk = json.loads((Path(".g3ku") / "config.json").read_text(encoding="utf-8"))
    assert "appId" not in on_disk["qqBot"]
    overlay = security.current_overlay()
    assert overlay.get("config.qqBot.accounts.654321.appSecret") == "legacy-secret"
    assert "config.qqBot.appSecret" not in overlay


def test_qq_bot_status_endpoint_reports_accounts(client: TestClient) -> None:
    status = client.get("/api/qq-bot/status")
    assert status.status_code == 200
    assert isinstance(status.json()["accounts"], list)
