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

import main.api.admin_rest as admin_rest
from g3ku.security.bootstrap import get_bootstrap_security_service


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


def test_owner_openids_roundtrip_and_preserved_when_omitted(client: TestClient) -> None:
    """操作员名单走整表替换的同一个 PUT：回显、缺省保留、形状校验。"""
    owner = "EB6C8D4341C1238A627FF73CBE540DAE"
    saved = client.put(
        "/api/qq-bot/settings",
        json={
            "enabled": True,
            "accounts": [{"app_id": "111", "app_secret": "secret-one", "owner_openids": [owner]}],
        },
    )
    assert saved.status_code == 200
    assert _row(saved.json(), "111")["owner_openids"] == [owner]

    # 改备注/密钥的整表写入不带该字段时保留旧名单（此前会被清空）。
    saved = client.put(
        "/api/qq-bot/settings",
        json={"enabled": True, "accounts": [{"app_id": "111", "app_secret": "", "label": "主号"}]},
    )
    assert _row(saved.json(), "111")["owner_openids"] == [owner]

    # 逗号/换行分隔的字符串也接受，按大小写不敏感去重。
    second = "A73DE661112233445566"
    saved = client.put(
        "/api/qq-bot/settings",
        json={
            "enabled": True,
            "accounts": [
                {
                    "app_id": "111",
                    "app_secret": "",
                    "ownerOpenids": f"{owner},\n{second},{owner.lower()}",
                }
            ],
        },
    )
    assert saved.status_code == 200
    assert _row(saved.json(), "111")["owner_openids"] == [owner, second]

    bad = client.put(
        "/api/qq-bot/settings",
        json={"enabled": True, "accounts": [{"app_id": "111", "owner_openids": ["1234567890"]}]},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"] == "owner_openid_invalid"


_GROUP_OWNER = "EB6C8D4341C1238A627FF73CBE540DAE"
_GROUP_OTHER = "A73DE6610A4C2793C759D7DF525DB66D"


def _user_row(sender_id: str, trust: str = "third_party_group") -> dict:
    return {
        "role": "user",
        "content": "hi",
        "metadata": {
            "external_trust": trust,
            "external_sender": {"id": sender_id, "name": ""},
            "chat_type": "group",
        },
    }


def _install_qq_session(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[dict],
    *,
    bridge_id: str = "qq-official-111",
    chat_type: str = "group",
    external_key: str = "qq:group:ABC",
    session_key: str = "ext:qq-official-111:abc",
    title: str = "测试群",
):
    from types import SimpleNamespace

    import g3ku.runtime.external_sessions as external_sessions

    entry = SimpleNamespace(
        session_key=session_key,
        bridge_id=bridge_id,
        chat_type=chat_type,
        title=title,
        external_key=external_key,
    )
    monkeypatch.setattr(
        admin_rest,
        "_qq_owner_session_messages",
        lambda session_id: (entry, list(messages)),
    )
    monkeypatch.setattr(admin_rest, "_qq_owner_master_ids", lambda: frozenset())

    class _OwnerTrustRegistry:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def trust_owner(self, target_session_key: str):
            self.calls.append(target_session_key)
            return SimpleNamespace(trust="owner")

    registry = _OwnerTrustRegistry()
    monkeypatch.setattr(
        external_sessions,
        "get_external_session_registry",
        lambda *args, **kwargs: registry,
    )
    return entry, registry


def _seed_qq_account(client: TestClient) -> None:
    saved = client.put(
        "/api/qq-bot/settings",
        json={"enabled": True, "accounts": [{"app_id": "111", "app_secret": "secret-one"}]},
    )
    assert saved.status_code == 200


def test_register_master_previews_and_writes_sender_number(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_qq_account(client)
    _install_qq_session(monkeypatch, [_user_row(_GROUP_OWNER), _user_row(_GROUP_OTHER)])

    preview = client.post(
        "/api/qq-bot/register-owner/preview",
        json={"session_id": "ext:qq-official-111:abc", "number": "0002", "app_id": "111"},
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["session"]["app_id"] == "111"
    assert body["session"]["chat_type"] == "group"
    assert body["sender"]["number_label"] == "0002"
    assert body["sender"]["id_masked"] == "EB6C8D…0DAE"
    # 预览只给 masked id，完整 openid 不经这条只读口暴露。
    assert _GROUP_OWNER not in json.dumps(body, ensure_ascii=False)

    bad = client.post(
        "/api/qq-bot/register-owner",
        json={"session_id": "ext:qq-official-111:abc", "number": "0002", "app_id": "111", "unlock_password": "wrong"},
    )
    assert bad.status_code == 403
    assert bad.json()["detail"]["code"] == "unlock_password_invalid"

    saved = client.post(
        "/api/qq-bot/register-owner",
        json={
            "session_id": "ext:qq-official-111:abc",
            "number": "0002",
            "app_id": "111",
            "unlock_password": "owner-password",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["registered"] is True
    assert _row(saved.json(), "111")["owner_openids"] == [_GROUP_OWNER]

    again = client.post(
        "/api/qq-bot/register-owner",
        json={
            "session_id": "ext:qq-official-111:abc",
            "number": "0002",
            "app_id": "111",
            "unlock_password": "owner-password",
        },
    )
    assert again.status_code == 200
    assert again.json()["registered"] is False
    assert _row(again.json(), "111")["owner_openids"] == [_GROUP_OWNER]


def test_register_master_rejects_reserved_missing_and_foreign_number(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_qq_account(client)
    _install_qq_session(monkeypatch, [_user_row(_GROUP_OWNER)])

    reserved = client.post(
        "/api/qq-bot/register-owner/preview",
        json={"session_id": "ext:qq-official-111:abc", "number": "0001", "app_id": "111"},
    )
    assert reserved.status_code == 400
    assert reserved.json()["detail"]["code"] == "sender_number_reserved"

    missing = client.post(
        "/api/qq-bot/register-owner/preview",
        json={"session_id": "ext:qq-official-111:abc", "number": "0009", "app_id": "111"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "sender_number_not_found"

    foreign = client.post(
        "/api/qq-bot/register-owner/preview",
        json={"session_id": "ext:qq-official-111:abc", "number": "0002", "app_id": "222"},
    )
    assert foreign.status_code == 400
    assert foreign.json()["detail"]["code"] == "session_account_mismatch"


def test_register_master_upgrades_current_dm_session(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """私聊只有一个对方：编号可留空自动定位；旧 DM 的粘性信任当场抬成 owner。"""
    _seed_qq_account(client)
    entry, registry = _install_qq_session(
        monkeypatch,
        [_user_row(_GROUP_OWNER), _user_row("negi-mcp")],
        chat_type="dm",
        external_key=f"qq:c2c:{_GROUP_OWNER}",
        session_key="ext:qq-official-111:dm",
        title="",
    )
    preview = client.post(
        "/api/qq-bot/register-owner/preview",
        json={"session_id": "ext:qq-official-111:dm", "app_id": "111"},
    )
    assert preview.status_code == 200
    assert preview.json()["auto_number"] is True
    assert preview.json()["sender"]["id_masked"] == "EB6C8D…0DAE"
    saved = client.post(
        "/api/qq-bot/register-owner",
        json={
            "session_id": "ext:qq-official-111:dm",
            "app_id": "111",
            "unlock_password": "owner-password",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["session_trust_upgraded"] is True
    assert registry.calls == [entry.session_key]


def test_register_master_requires_number_for_group(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_qq_account(client)
    _install_qq_session(monkeypatch, [_user_row(_GROUP_OWNER)])
    preview = client.post(
        "/api/qq-bot/register-owner/preview",
        json={"session_id": "ext:qq-official-111:abc", "app_id": "111"},
    )
    assert preview.status_code == 400
    assert preview.json()["detail"]["code"] == "sender_number_required"


def test_register_master_does_not_upgrade_group_session(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_qq_account(client)
    _entry, registry = _install_qq_session(monkeypatch, [_user_row(_GROUP_OWNER)])
    saved = client.post(
        "/api/qq-bot/register-owner",
        json={
            "session_id": "ext:qq-official-111:abc",
            "number": "0002",
            "app_id": "111",
            "unlock_password": "owner-password",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["session_trust_upgraded"] is False
    assert registry.calls == []


@pytest.fixture(autouse=True)
def _reset_master_binding_registry():
    from g3ku.runtime import qq_master_binding as binding

    binding.reset_qq_master_binding_registry()
    yield
    binding.reset_qq_master_binding_registry()


def test_master_binding_requires_password_and_returns_code(client: TestClient) -> None:
    _seed_qq_account(client)
    missing = client.post("/api/qq-bot/master-binding", json={"app_id": "111"})
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "unlock_password_required"

    wrong = client.post(
        "/api/qq-bot/master-binding",
        json={"app_id": "111", "unlock_password": "wrong"},
    )
    assert wrong.status_code == 403
    assert wrong.json()["detail"]["code"] == "unlock_password_invalid"

    issued = client.post(
        "/api/qq-bot/master-binding",
        json={"app_id": "111", "unlock_password": "owner-password"},
    )
    assert issued.status_code == 200
    body = issued.json()
    assert body["token"].startswith("NEGI-MASTER-")
    assert body["expires_in_seconds"] == 300

    status = client.get("/api/qq-bot/master-binding/status", params={"app_id": "111"})
    assert status.status_code == 200
    assert status.json()["pending"] is True
    assert status.json()["registered"] is False
    assert status.json()["expires_in_seconds"] > 0


def test_master_binding_refuses_when_owner_exists(client: TestClient) -> None:
    _seed_qq_account(client)
    owner = "EB6C8D4341C1238A627FF73CBE540DAE"
    client.put(
        "/api/qq-bot/settings",
        json={"enabled": True, "accounts": [{"app_id": "111", "owner_openids": [owner]}]},
    )
    issued = client.post(
        "/api/qq-bot/master-binding",
        json={"app_id": "111", "unlock_password": "owner-password"},
    )
    assert issued.status_code == 409
    assert issued.json()["detail"]["code"] == "master_already_registered"


def test_unregister_master_clears_owners_and_dm_trust(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import g3ku.runtime.external_sessions as external_sessions
    from g3ku.runtime import qq_master_binding as binding

    _seed_qq_account(client)
    owner = "EB6C8D4341C1238A627FF73CBE540DAE"
    client.put(
        "/api/qq-bot/settings",
        json={"enabled": True, "accounts": [{"app_id": "111", "owner_openids": [owner]}]},
    )

    class _FakeRegistry:
        def __init__(self) -> None:
            self.cleared: list[str] = []

        def clear_owner_trust(self, bridge_id: str) -> int:
            self.cleared.append(bridge_id)
            return 1

    fake = _FakeRegistry()
    monkeypatch.setattr(
        external_sessions, "get_external_session_registry", lambda *args, **kwargs: fake
    )

    async def _noop_refresh(reason: str) -> None:
        return None

    monkeypatch.setattr(binding, "_refresh_after_save", _noop_refresh)

    result = client.post("/api/qq-bot/unregister-master", json={"app_id": "111"})
    assert result.status_code == 200
    body = result.json()
    assert body["cleared_owner_count"] == 1
    assert body["dm_sessions_downgraded"] == 1
    assert fake.cleared == ["qq-official-111"]
    assert _row(body, "111")["owner_openids"] == []

    status = client.get("/api/qq-bot/master-binding/status", params={"app_id": "111"})
    assert status.json()["registered"] is False


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
    # 读配置本身：内置 QQ 的凭证不出现在「外部接入」的 token 列表里（列表只画操作员签发的桥）。
    tokens = load_config().external_api.tokens
    assert tokens["qq-official-222"].enabled is False
    assert tokens["qq-official-111"].enabled is True


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
