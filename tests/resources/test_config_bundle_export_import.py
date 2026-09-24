from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.config import config_bundle
from g3ku.security import derive_password_key, get_bootstrap_security_service
from main.api import bootstrap_rest
from main.governance.store import GovernanceStore

BUNDLE_PASSWORD = "bundle-pass-1234"
OWNER_PASSWORD = "owner-password"


def _write_workspace(workspace: Path) -> Path:
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku" / "config.json").write_text(
        json.dumps({"web": {"port": 18790}, "resources": {"statePath": ".g3ku/resources.state.json"}}),
        encoding="utf-8",
    )
    records = workspace / ".g3ku" / "llm-config" / "records"
    records.mkdir(parents=True, exist_ok=True)
    (records / "prov-1.json").write_text(
        json.dumps({"id": "prov-1", "provider_id": "openai", "parameters": {"api_key": ""}}),
        encoding="utf-8",
    )
    (workspace / ".g3ku" / "resources.state.json").write_text(json.dumps({"skills": {}}), encoding="utf-8")
    return workspace


def _governance_path(workspace: Path) -> Path:
    return workspace / ".g3ku" / "main-runtime" / "governance.sqlite3"


def _seed_governance(workspace: Path, value: str) -> None:
    store = GovernanceStore(_governance_path(workspace))
    try:
        store.set_meta("ceo_frontdoor_regulatory_mode_enabled", value)
    finally:
        store.close()


def _read_governance(workspace: Path, key: str) -> str | None:
    store = GovernanceStore(_governance_path(workspace))
    try:
        return store.get_meta(key)
    finally:
        store.close()


def _unlock(workspace: Path) -> None:
    service = get_bootstrap_security_service(workspace)
    service.setup_initial_realm(password=OWNER_PASSWORD)
    service.set_overlay_values({"config.qqBot.appSecret": "super-secret-app"})


def _export_custom(workspace: Path, password: str = BUNDLE_PASSWORD) -> Path:
    return Path(
        config_bundle.export_bundle(workspace, password=password, use_project_password=False)["path"]
    )


def _fresh_target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return target


def test_export_bundle_requires_unlocked_project(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "source")
    service = get_bootstrap_security_service(workspace)
    service.setup_initial_realm(password=OWNER_PASSWORD)
    service.lock()

    with pytest.raises(ValueError, match="project is locked"):
        config_bundle.export_bundle(workspace, password=BUNDLE_PASSWORD, use_project_password=False)


def test_project_lane_exports_without_any_password(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)

    item = config_bundle.export_bundle(workspace)

    assert item["key_source"] == config_bundle.KEY_SOURCE_PROJECT_PASSWORD
    assert item["entry_count"] >= 3


def test_project_lane_carries_the_envelope_and_not_the_plain_key(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)
    master_key = get_bootstrap_security_service(workspace).active_master_key()

    archive = Path(config_bundle.export_bundle(workspace)["path"])
    text = archive.read_text(encoding="utf-8")
    envelope = json.loads(text)

    assert envelope["salt_b64"] == ""
    assert envelope["kdf"] == {}
    assert set(envelope["unlock_envelope"]) == {
        "version",
        "unlock_scope",
        "salt_b64",
        "kdf",
        "wrapped_master_key_b64",
        "created_at",
        "updated_at",
    }
    assert master_key not in text
    assert "super-secret-app" not in text


def test_project_lane_imports_with_the_project_password(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_governance(source, "true")
    archive = Path(config_bundle.export_bundle(source)["path"])

    target = _fresh_target(tmp_path)
    restored = config_bundle.import_bundle(target, archive_path=archive, password=OWNER_PASSWORD)

    assert restored["key_source"] == config_bundle.KEY_SOURCE_PROJECT_PASSWORD
    assert restored["status"]["mode"] == "unlocked"
    service = get_bootstrap_security_service(target)
    assert service.current_overlay()["config.qqBot.appSecret"] == "super-secret-app"
    assert _read_governance(target, "ceo_frontdoor_regulatory_mode_enabled") == "true"
    # 目标机沿用同一个项目解锁密码，改密不会顺带把包口令改掉。
    service.lock()
    service.unlock(password=OWNER_PASSWORD)
    assert service.is_unlocked()


def test_project_lane_rejects_a_workspace_without_a_password_envelope(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "source")
    get_bootstrap_security_service(workspace).activate_with_master_key(
        master_key=Fernet.generate_key().decode("utf-8")
    )

    with pytest.raises(ValueError, match="unlock password is not configured"):
        config_bundle.export_bundle(workspace)


def test_project_lane_rejects_the_wrong_project_password(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = Path(config_bundle.export_bundle(source)["path"])

    target = _fresh_target(tmp_path)
    with pytest.raises(ValueError, match="invalid password"):
        config_bundle.import_bundle(target, archive_path=archive, password="not-the-owner-password")
    assert not (target / ".g3ku" / "config.json").exists()


def test_custom_lane_round_trip_restores_config_overlay_and_governance(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_governance(source, "true")
    original_config = (source / ".g3ku" / "config.json").read_text(encoding="utf-8")

    archive = _export_custom(source)
    assert "super-secret-app" not in archive.read_text(encoding="utf-8")

    target = _fresh_target(tmp_path)
    restored = config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD)

    assert (target / ".g3ku" / "config.json").read_text(encoding="utf-8") == original_config
    service = get_bootstrap_security_service(target)
    assert service.is_unlocked()
    assert service.current_overlay()["config.qqBot.appSecret"] == "super-secret-app"
    assert str(_read_governance(target, "ceo_frontdoor_regulatory_mode_enabled")) == "true"
    assert restored["status"]["mode"] == "unlocked"


def test_custom_lane_accepts_short_passwords_but_never_an_empty_one(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)

    archive = _export_custom(workspace, password="7")
    target = _fresh_target(tmp_path)
    restored = config_bundle.import_bundle(target, archive_path=archive, password="7")
    assert restored["status"]["mode"] == "unlocked"

    with pytest.raises(ValueError, match="password is required"):
        config_bundle.export_bundle(workspace, password="", use_project_password=False)


def test_custom_lane_target_unlocks_with_bundle_password_only(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = _export_custom(source)

    target = _fresh_target(tmp_path)
    config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD)

    service = get_bootstrap_security_service(target)
    service.lock()
    with pytest.raises(ValueError, match="invalid password"):
        service.unlock(password=OWNER_PASSWORD)
    service.unlock(password=BUNDLE_PASSWORD)
    assert service.current_overlay()["config.qqBot.appSecret"] == "super-secret-app"


def test_export_bundle_keeps_master_key_file_and_auto_unlock_out_of_entries(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)
    (workspace / ".g3ku" / "llm-config" / "auto-unlock.key").write_text("bearer-credential", encoding="utf-8")

    item = config_bundle.export_bundle(workspace)

    names = {Path(entry).name for entry in item["entries"]}
    assert {"master.key", "auto-unlock.key"}.isdisjoint(names)
    assert ".g3ku/config.json" in item["entries"]


def test_import_with_wrong_password_leaves_target_untouched(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = _export_custom(source)

    target = _write_workspace(tmp_path / "target")
    (target / ".g3ku" / "config.json").write_text(json.dumps({"web": {"port": 9999}}), encoding="utf-8")
    before = (target / ".g3ku" / "config.json").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="invalid password"):
        config_bundle.import_bundle(target, archive_path=archive, password="wrong-pass-1234")

    assert (target / ".g3ku" / "config.json").read_text(encoding="utf-8") == before


def test_import_backs_up_replaced_files(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = _export_custom(source)

    target = _write_workspace(tmp_path / "target")
    (target / ".g3ku" / "config.json").write_text(json.dumps({"web": {"port": 9999}}), encoding="utf-8")

    restored = config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD)

    backup = Path(restored["backup_dir"])
    assert json.loads((backup / ".g3ku" / "config.json").read_text(encoding="utf-8"))["web"]["port"] == 9999
    assert json.loads((target / ".g3ku" / "config.json").read_text(encoding="utf-8"))["web"]["port"] == 18790


def test_safe_relative_path_rejects_escapes() -> None:
    for bad in ("../outside.json", "/abs/config.json", "C:/windows/config.json", "sessions/x.jsonl", ""):
        with pytest.raises(ValueError):
            config_bundle._safe_relative_path(bad)


def test_preflight_rejects_master_key_that_cannot_read_overlay() -> None:
    unrelated = Fernet(Fernet.generate_key())
    payload = {
        "master_key": Fernet.generate_key().decode("utf-8"),
        "entries": {".g3ku/secret-realms/default.enc": base64.b64encode(unrelated.encrypt(b"{}")).decode("ascii")},
    }

    with pytest.raises(ValueError, match="secret overlay"):
        config_bundle._preflight(payload)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(bootstrap_rest.router, prefix="/api")
    return TestClient(app)


def test_export_route_defaults_to_the_project_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)
    monkeypatch.chdir(workspace)

    exported = _client().post("/api/bootstrap/config-bundle/export", json={})

    assert exported.status_code == 200
    item = exported.json()["item"]
    assert item["key_source"] == config_bundle.KEY_SOURCE_PROJECT_PASSWORD
    assert "path" not in item
    assert ".g3ku/config.json" in item["entries"]


def test_export_route_reports_a_missing_password_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _write_workspace(tmp_path / "source")
    get_bootstrap_security_service(workspace).activate_with_master_key(
        master_key=Fernet.generate_key().decode("utf-8")
    )
    monkeypatch.chdir(workspace)

    unavailable = _client().post("/api/bootstrap/config-bundle/export", json={"use_project_password": True})

    assert unavailable.status_code == 400
    assert unavailable.json()["detail"] == "bundle_project_password_unavailable"
    assert not (workspace / config_bundle.BUNDLE_OUTPUT_DIR).exists()


def test_export_route_returns_summary_and_downloadable_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)
    monkeypatch.chdir(workspace)
    client = _client()

    exported = client.post(
        "/api/bootstrap/config-bundle/export",
        json={"password": BUNDLE_PASSWORD, "use_project_password": False},
    )

    assert exported.status_code == 200
    item = exported.json()["item"]
    assert "path" not in item
    assert item["entry_count"] >= 3
    assert ".g3ku/config.json" in item["entries"]

    downloaded = client.get("/api/bootstrap/config-bundle/download", params={"filename": item["filename"]})
    assert downloaded.status_code == 200
    assert "attachment" in downloaded.headers["content-disposition"]
    assert json.loads(downloaded.text)["kind"] == config_bundle.BUNDLE_KIND
    assert client.get(
        "/api/bootstrap/config-bundle/download", params={"filename": "../config.json"}
    ).status_code == 400


def test_export_route_rejects_locked_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _write_workspace(tmp_path / "source")
    service = get_bootstrap_security_service(workspace)
    service.setup_initial_realm(password=OWNER_PASSWORD)
    service.lock()
    monkeypatch.chdir(workspace)

    locked = _client().post("/api/bootstrap/config-bundle/export", json={"password": BUNDLE_PASSWORD})

    assert locked.status_code == 423
    assert locked.json()["detail"] == "project_locked"


def test_import_route_applies_bundle_and_rejects_wrong_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_governance(source, "true")
    archive = Path(config_bundle.export_bundle(source)["path"])

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(target)
    client = _client()

    with archive.open("rb") as handle:
        wrong = client.post(
            "/api/bootstrap/config-bundle/import",
            data={"password": "wrong-password-1"},
            files={"file": ("bundle.g3kucb", handle, "application/octet-stream")},
        )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "bundle_password_invalid"
    assert not (target / ".g3ku" / "config.json").exists()

    with archive.open("rb") as handle:
        applied = client.post(
            "/api/bootstrap/config-bundle/import",
            data={"password": OWNER_PASSWORD},
            files={"file": ("bundle.g3kucb", handle, "application/octet-stream")},
        )

    assert applied.status_code == 200
    item = applied.json()["item"]
    assert item["status"]["mode"] == "unlocked"
    assert item["key_source"] == config_bundle.KEY_SOURCE_PROJECT_PASSWORD
    assert item["restart_required"] is True
    assert json.loads((target / ".g3ku" / "config.json").read_text(encoding="utf-8"))["web"]["port"] == 18790
    assert get_bootstrap_security_service(target).current_overlay()["config.qqBot.appSecret"] == "super-secret-app"
    assert _read_governance(target, "ceo_frontdoor_regulatory_mode_enabled") == "true"
    assert not list((target / config_bundle.BUNDLE_OUTPUT_DIR / "incoming").iterdir())


def test_bundle_envelope_shape_and_tamper_rejection(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)

    archive = _export_custom(workspace)
    envelope = json.loads(archive.read_text(encoding="utf-8"))

    assert envelope["kind"] == config_bundle.BUNDLE_KIND
    assert set(envelope) == {
        "kind",
        "version",
        "created_at",
        "workspace_label",
        "key_source",
        "kdf",
        "salt_b64",
        "unlock_envelope",
        "payload_b64",
    }
    # 手工按同一 KDF 复算一次，确认自定义口令就是这条车道的唯一凭据。
    key = derive_password_key(
        BUNDLE_PASSWORD,
        salt=base64.b64decode(envelope["salt_b64"]),
        n=int(envelope["kdf"]["n"]),
        r=int(envelope["kdf"]["r"]),
        p=int(envelope["kdf"]["p"]),
    )
    payload = json.loads(Fernet(key.encode()).decrypt(base64.b64decode(envelope["payload_b64"])).decode("utf-8"))
    assert payload["master_key"] == get_bootstrap_security_service(workspace).active_master_key()

    tampered = archive.with_name("tampered" + config_bundle.BUNDLE_EXTENSION)
    shutil.copy2(archive, tampered)
    broken = json.loads(tampered.read_text(encoding="utf-8"))
    broken["payload_b64"] = base64.b64encode(b"not-a-fernet-token").decode("ascii")
    tampered.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid password"):
        config_bundle.import_bundle(
            tmp_path / "target",
            archive_path=tampered,
            password=BUNDLE_PASSWORD,
        )
