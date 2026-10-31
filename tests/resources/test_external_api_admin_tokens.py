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

import main.api.admin_rest as admin_rest
from g3ku.security.bootstrap import get_bootstrap_security_service


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


def test_external_api_token_create_accepts_custom_token_and_dedupes_values(client: TestClient) -> None:
    custom = "my-custom-bridge-token-123"
    created = client.post("/api/external-api/tokens", json={"bridge_id": "napcat", "token": custom})
    assert created.status_code == 200
    assert created.json()["token"] == custom

    listing = client.get("/api/external-api/settings").json()
    item = listing["items"][0]
    assert item["has_token"] is True
    # The one-time reveal contract holds for custom tokens too.
    assert custom not in json.dumps(listing, ensure_ascii=False)
    assert item["token_masked"].startswith("my-c")

    duplicate = client.post("/api/external-api/tokens", json={"bridge_id": "other", "token": custom})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "token_exists"

    # Auto-generation stays the default when no token is supplied.
    auto = client.post("/api/external-api/tokens", json={"bridge_id": "auto"})
    assert auto.status_code == 200
    assert auto.json()["token"] not in ("", custom)


def test_cross_session_scope_roundtrip_per_token(client: TestClient) -> None:
    """作用域只按号裁决：新签的默认放开，false 是真关，旧的"交回全局"写法被拒。"""
    settings = client.get("/api/external-api/settings").json()
    assert "cross_session_enabled" not in settings

    created = client.post("/api/external-api/tokens", json={"bridge_id": "relay"}).json()
    row = next(item for item in created["items"] if item["bridge_id"] == "relay")
    assert row["cross_session"] is True

    deny = client.patch("/api/external-api/tokens/relay", json={"crossSession": False}).json()
    row = next(item for item in deny["items"] if item["bridge_id"] == "relay")
    assert row["cross_session"] is False

    rejected = client.patch("/api/external-api/tokens/relay", json={"crossSession": None})
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "invalid_cross_session"

    allow = client.patch("/api/external-api/tokens/relay", json={"crossSession": True}).json()
    row = next(item for item in allow["items"] if item["bridge_id"] == "relay")
    assert row["cross_session"] is True

    on_disk = json.loads((Path.cwd() / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    external_api = on_disk["externalApi"]
    assert "crossSessionEnabled" not in external_api
    assert external_api["tokens"]["relay"]["crossSession"] is True


def test_legacy_global_scope_folds_into_each_token() -> None:
    """老配置里关掉过全局的，不能被新默认静默放开。"""
    from g3ku.config.schema import ExternalApiConfig

    legacy = ExternalApiConfig.model_validate(
        {
            "enabled": True,
            "crossSessionEnabled": False,
            "tokens": {"quiet": {"token": "t"}, "loud": {"token": "u", "crossSession": True}},
        }
    )
    assert legacy.tokens["quiet"].cross_session is False
    assert legacy.tokens["loud"].cross_session is True

    opened = ExternalApiConfig.model_validate(
        {"enabled": True, "crossSessionEnabled": True, "tokens": {"relay": {"token": "t"}}}
    )
    assert opened.tokens["relay"].cross_session is True


def _saved_token_entry() -> dict:
    payload = json.loads((Path.cwd() / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    return payload["externalApi"]["tokens"]["mcp"]


def test_external_api_token_operator_grant_requires_unlock_password(client: TestClient) -> None:
    """「当操作员」开启要项目口令，关闭不要（收回权限不该被自己锁在门外）。"""
    client.post("/api/external-api/tokens", json={"bridge_id": "mcp"})
    assert client.get("/api/external-api/settings").json()["items"][0]["operator"] is False

    missing = client.patch("/api/external-api/tokens/mcp", json={"operator": True})
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "unlock_password_required"

    wrong = client.patch(
        "/api/external-api/tokens/mcp",
        json={"operator": True, "unlock_password": "not-the-password"},
    )
    assert wrong.status_code == 403
    assert wrong.json()["detail"]["code"] == "unlock_password_invalid"

    granted = client.patch(
        "/api/external-api/tokens/mcp",
        json={"operator": True, "unlock_password": "owner-password"},
    )
    assert granted.status_code == 200
    assert granted.json()["items"][0]["operator"] is True
    # 「当操作员」就是把网关固定身份写进白名单；开关与常量必须同一个字符串。
    from g3ku.runtime.session_trust import GATEWAY_SENDER_ID

    assert GATEWAY_SENDER_ID == "negi-mcp"
    assert _saved_token_entry()["trustedSenders"] == [GATEWAY_SENDER_ID]

    revoked = client.patch("/api/external-api/tokens/mcp", json={"operator": False})
    assert revoked.status_code == 200
    assert revoked.json()["items"][0]["operator"] is False
    assert _saved_token_entry()["trustedSenders"] == []


def test_external_api_token_operator_grant_preserves_manual_senders(client: TestClient) -> None:
    """开关只增删网关身份；手工写进去的发送者 id 不能被顺手清掉。"""
    client.post("/api/external-api/tokens", json={"bridge_id": "mcp"})
    path = Path.cwd() / ".g3ku" / "config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["externalApi"]["tokens"]["mcp"]["trustedSenders"] = ["manual-id"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    granted = client.patch(
        "/api/external-api/tokens/mcp",
        json={"operator": True, "unlock_password": "owner-password"},
    )
    assert granted.status_code == 200
    assert _saved_token_entry()["trustedSenders"] == ["manual-id", "negi-mcp"]

    client.patch("/api/external-api/tokens/mcp", json={"operator": False})
    assert _saved_token_entry()["trustedSenders"] == ["manual-id"]


def test_builtin_qq_bridge_tokens_are_not_listed(client: TestClient) -> None:
    """列表只画操作员签发的桥：内置 QQ 那条凭证由建号流程自动签发，账号的增删/启停/Master
    都在「QQ 机器人」那一页管，混在这里等于让操作员去管一个他不认识的 AppID。"""
    client.post("/api/external-api/tokens", json={"bridge_id": "napcat", "label": "家里 NapCat"})
    path = Path.cwd() / ".g3ku" / "config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["externalApi"]["tokens"]["qq-official-1903529517"] = {
        "token": "qq-issued-secret",
        "label": "官方 QQ 机器人 1903529517",
        "enabled": True,
    }
    payload["externalApi"]["tokens"]["qq-official"] = {
        "token": "legacy-qq-secret",
        "label": "官方 QQ 机器人",
        "enabled": False,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    listing = client.get("/api/external-api/settings").json()
    assert [item["bridge_id"] for item in listing["items"]] == ["napcat"]
    rendered = json.dumps(listing, ensure_ascii=False)
    assert "qq-official" not in rendered
    assert "qq-issued-secret" not in rendered
    # 被摘掉的只是显示面：该桥的凭证仍然有效，列表不画不等于停用。
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["externalApi"]["tokens"]["qq-official-1903529517"]["enabled"] is True


def test_external_api_token_create_reserves_the_qq_bridge_prefix(client: TestClient) -> None:
    """`qq-official` 前缀归内置桥占用：列表把它藏起来之后，放行一次手工签发就等于造一条
    没人看得见、也没人停得掉的凭证。只认这个前缀，叫 `qq` 的自建桥照旧能签。"""
    rejected = client.post("/api/external-api/tokens", json={"bridge_id": "qq-official-111"})
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "bridge_id_reserved"

    created = client.post("/api/external-api/tokens", json={"bridge_id": "qq"})
    assert created.status_code == 200
    assert [item["bridge_id"] for item in created.json()["items"]] == ["qq"]
