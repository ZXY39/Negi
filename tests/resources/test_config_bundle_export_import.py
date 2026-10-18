from __future__ import annotations

import base64
import json
import shutil
import sqlite3
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
TARGET_PASSWORD = "target-owner-password"


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


def test_import_keeps_the_targets_own_unlock_password(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = _export_custom(source)

    target = _write_workspace(tmp_path / "target")
    # 目标机自己那份 config.json 要能和包里的区分开，否则"导入落盘了"这条断言是空的。
    (target / ".g3ku" / "config.json").write_text(
        json.dumps({"web": {"port": 19001}, "resources": {"statePath": ".g3ku/resources.state.json"}}),
        encoding="utf-8",
    )
    get_bootstrap_security_service(target).setup_initial_realm(password=TARGET_PASSWORD)

    restored = config_bundle.import_bundle(
        target,
        archive_path=archive,
        password=BUNDLE_PASSWORD,
        current_password=TARGET_PASSWORD,
    )

    assert restored["password_kept"] is True
    assert json.loads((target / ".g3ku" / "config.json").read_text(encoding="utf-8"))["web"]["port"] == 18790
    service = get_bootstrap_security_service(target)
    assert service.current_overlay()["config.qqBot.appSecret"] == "super-secret-app"
    # 口令位没动：本机口令仍解得开新信封，包口令解不开。
    service.lock()
    service.unlock(password=TARGET_PASSWORD)
    assert service.is_unlocked()
    service.lock()
    with pytest.raises(Exception):
        service.unlock(password=BUNDLE_PASSWORD)


def test_import_requires_the_current_password_when_one_is_installed(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = _export_custom(source)

    target = _write_workspace(tmp_path / "target")
    service = get_bootstrap_security_service(target)
    service.setup_initial_realm(password=TARGET_PASSWORD)
    envelope_before = (target / ".g3ku" / "llm-config" / "master.key").read_text(encoding="utf-8")
    config_before = (target / ".g3ku" / "config.json").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="bundle_current_password_required"):
        config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD)

    # 没输入就不碰任何文件：整包替换不能靠一次失败的口令尝试起步。
    assert (target / ".g3ku" / "llm-config" / "master.key").read_text(encoding="utf-8") == envelope_before
    assert (target / ".g3ku" / "config.json").read_text(encoding="utf-8") == config_before
    service.lock()
    service.unlock(password=TARGET_PASSWORD)


def test_wrong_current_password_aborts_before_touching_files(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = _export_custom(source)

    target = _write_workspace(tmp_path / "target")
    service = get_bootstrap_security_service(target)
    service.setup_initial_realm(password=TARGET_PASSWORD)
    config_before = (target / ".g3ku" / "config.json").read_text(encoding="utf-8")
    backups_before = list((target / ".g3ku" / "config-bundle-imports").glob("*")) if (
        target / ".g3ku" / "config-bundle-imports"
    ).exists() else []

    with pytest.raises(ValueError, match="bundle_current_password_invalid"):
        config_bundle.import_bundle(
            target,
            archive_path=archive,
            password=BUNDLE_PASSWORD,
            current_password="wrong-target-password",
        )

    assert (target / ".g3ku" / "config.json").read_text(encoding="utf-8") == config_before
    assert list((target / ".g3ku" / "config-bundle-imports").glob("*")) == backups_before
    service.lock()
    service.unlock(password=TARGET_PASSWORD)


def test_a_target_without_any_password_still_adopts_the_bundle_password(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = _export_custom(source)

    target = _fresh_target(tmp_path)
    restored = config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD)

    assert restored["password_kept"] is False
    service = get_bootstrap_security_service(target)
    service.lock()
    service.unlock(password=BUNDLE_PASSWORD)
    assert service.is_unlocked()


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
        config_bundle._preflight(payload, (config_bundle.SCOPE_CONFIG,))


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


def test_import_route_survives_a_device_that_never_finished_onboarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新设备只设过解锁密码、config.json 还不存在时，导入不能变成 500。

    1.0.4 上 _running_work_snapshot() 的 load_config() 抛 FileNotFoundError，而它
    落在路由的 try 之外：界面只收到一个没有 detail 的 500，且配置根本没导入。
    """
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = Path(config_bundle.export_bundle(source)["path"])

    target = tmp_path / "target"
    (target / ".g3ku").mkdir(parents=True)
    get_bootstrap_security_service(target).setup_initial_realm(password=OWNER_PASSWORD)
    assert not (target / ".g3ku" / "config.json").exists()
    monkeypatch.chdir(target)

    with archive.open("rb") as handle:
        applied = _client().post(
            "/api/bootstrap/config-bundle/import",
            data={"password": OWNER_PASSWORD, "current_password": OWNER_PASSWORD},
            files={"file": ("bundle.g3kucb", handle, "application/octet-stream")},
        )

    assert applied.status_code == 200, applied.text
    item = applied.json()["item"]
    assert item["refresh"]["saved"] is True
    assert item["password_kept"] is True
    assert (target / ".g3ku" / "config.json").exists()
    assert get_bootstrap_security_service(target).current_overlay()["config.qqBot.appSecret"] == "super-secret-app"


def test_import_route_asks_for_the_local_password_before_touching_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本机已有解锁口令却不输入时，返回可读的 400 代码，且配置面一个字节都没动。"""
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = Path(config_bundle.export_bundle(source)["path"])

    target = tmp_path / "target"
    (target / ".g3ku").mkdir(parents=True)
    get_bootstrap_security_service(target).setup_initial_realm(password=TARGET_PASSWORD)
    monkeypatch.chdir(target)

    with archive.open("rb") as handle:
        refused = _client().post(
            "/api/bootstrap/config-bundle/import",
            data={"password": OWNER_PASSWORD},
            files={"file": ("bundle.g3kucb", handle, "application/octet-stream")},
        )

    assert refused.status_code == 400
    assert refused.json()["detail"] == "bundle_current_password_required"
    assert not (target / ".g3ku" / "config.json").exists()


def test_import_route_reports_a_source_version_mismatch_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = Path(config_bundle.export_bundle(source)["path"])

    target = _fresh_target(tmp_path)
    monkeypatch.chdir(target)
    with archive.open("rb") as handle:
        applied = _client().post(
            "/api/bootstrap/config-bundle/import",
            data={"password": OWNER_PASSWORD},
            files={"file": ("bundle.g3kucb", handle, "application/octet-stream")},
        )

    item = applied.json()["item"]
    assert item["source_app_version"] == item["local_app_version"] == config_bundle.__version__


def test_bundle_envelope_shape_and_tamper_rejection(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)

    archive = _export_custom(workspace)
    envelope = json.loads(archive.read_text(encoding="utf-8"))

    assert envelope["kind"] == config_bundle.BUNDLE_KIND
    assert set(envelope) == {
        "kind",
        "version",
        "app_version",
        "created_at",
        "workspace_label",
        "scopes",
        "key_source",
        "kdf",
        "salt_b64",
        "unlock_envelope",
        "payload_b64",
    }
    assert envelope["scopes"] == ["config"]
    assert envelope["app_version"] == config_bundle.__version__
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


MEMORY_KEYS = {
    "memory/memory_state.sqlite3",
    "memory/MEMORY.md",
    "memory/notes/note_alpha.md",
    "memory/notes/note_beta.md",
    "memory/index/note_category.md",
}


def _seed_memory(workspace: Path, *, marker: str, rows: int = 3) -> Path:
    """记忆面：权威在 sqlite，正文在 notes/index 两个目录，镜像是常驻块读的那份文件。"""
    mem = workspace / "memory"
    (mem / "notes").mkdir(parents=True, exist_ok=True)
    (mem / "index").mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(mem / "memory_state.sqlite3") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS memories (memory_id TEXT PRIMARY KEY, memory_body TEXT NOT NULL)")
        conn.execute("DELETE FROM memories")
        conn.executemany(
            "INSERT INTO memories (memory_id, memory_body) VALUES (?, ?)",
            [(f"m{i}", f"{marker} body {i}") for i in range(rows)],
        )
    (mem / "MEMORY.md").write_text(f"# mirror of {marker}\n", encoding="utf-8")
    for name in ("note_alpha", "note_beta"):
        (mem / "notes" / f"{name}.md").write_text(f"{marker} body of {name}\n", encoding="utf-8")
    (mem / "index" / "note_category.md").write_text(f"{marker} category body\n", encoding="utf-8")
    return mem


def _seed_memory_noise(mem_dir: Path, *, marker: str = "src") -> None:
    """运行历史、在飞状态与取证备份：都不是记忆本身，不该跟包走。"""
    (mem_dir / "ops.jsonl").write_text(f'{{"log": "{marker} round"}}\n', encoding="utf-8")
    (mem_dir / "queue.jsonl").write_text(f'{{"op": "{marker} consolidate"}}\n', encoding="utf-8")
    (mem_dir / "review_state.json").write_text(
        json.dumps({"sessions": {f"{marker}:ceo-abc": {}}}), encoding="utf-8"
    )
    (mem_dir / "notes" / "note_alpha.md.bak.20261015-015857").write_text(
        f"{marker} evidence\n", encoding="utf-8"
    )


def _memory_bodies(workspace: Path) -> list[str]:
    with sqlite3.connect(workspace / "memory" / "memory_state.sqlite3") as conn:
        return [str(row[0]) for row in conn.execute("SELECT memory_body FROM memories ORDER BY memory_id")]


def _export_scopes(workspace: Path, scopes: tuple[str, ...]) -> Path:
    return Path(
        config_bundle.export_bundle(
            workspace,
            password=BUNDLE_PASSWORD,
            use_project_password=False,
            scopes=scopes,
        )["path"]
    )


def _payload_of(archive: Path) -> dict:
    envelope = json.loads(archive.read_text(encoding="utf-8"))
    key = derive_password_key(
        BUNDLE_PASSWORD,
        salt=base64.b64decode(envelope["salt_b64"]),
        n=int(envelope["kdf"]["n"]),
        r=int(envelope["kdf"]["r"]),
        p=int(envelope["kdf"]["p"]),
    )
    return json.loads(Fernet(key.encode()).decrypt(base64.b64decode(envelope["payload_b64"])).decode("utf-8"))


def _rescoped(archive: Path, *, version: int, scopes: list[str] | None) -> Path:
    """改写外层声明，模拟旧版本包与"声明与条目不符"的包：载荷里的字节不动。"""
    envelope = json.loads(archive.read_text(encoding="utf-8"))
    envelope["version"] = version
    if scopes is None:
        envelope.pop("scopes", None)
    else:
        envelope["scopes"] = scopes
    forged = archive.with_name(f"forged-{version}-{archive.stem}.g3kucb")
    forged.write_text(json.dumps(envelope), encoding="utf-8")
    return forged


def test_memory_scope_carries_store_mirror_and_notes_but_not_history(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    mem = _seed_memory(source, marker="SRC")
    _seed_memory_noise(mem)

    archive = _export_scopes(source, (config_bundle.SCOPE_MEMORY,))

    envelope = json.loads(archive.read_text(encoding="utf-8"))
    payload = _payload_of(archive)
    assert envelope["scopes"] == ["memory"]
    assert set(payload["entries"]) == MEMORY_KEYS
    # 只带记忆的包不必再拷一份主密钥：那面根本不动钥匙。
    assert "master_key" not in payload


def test_memory_scope_export_without_a_store_fails_before_writing(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)

    with pytest.raises(ValueError, match="bundle_memory_store_missing"):
        _export_scopes(source, (config_bundle.SCOPE_MEMORY,))

    assert not (source / config_bundle.BUNDLE_OUTPUT_DIR).exists()


def test_both_scopes_count_each_face_separately(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_memory(source, marker="SRC")

    summary = config_bundle.export_bundle(
        source, password=BUNDLE_PASSWORD, use_project_password=False,
        scopes=(config_bundle.SCOPE_CONFIG, config_bundle.SCOPE_MEMORY),
    )

    assert summary["scopes"] == ["config", "memory"]
    assert summary["config_entry_count"] == len(summary["entries"]) - summary["memory_entry_count"]
    assert summary["memory_entry_count"] == len(MEMORY_KEYS)


def test_memory_only_import_replaces_memory_and_leaves_config_and_key(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_memory(source, marker="SRC")
    _seed_governance(source, "true")
    archive = _export_scopes(source, (config_bundle.SCOPE_MEMORY,))

    target = _write_workspace(tmp_path / "target")
    (target / ".g3ku" / "config.json").write_text(
        json.dumps({"web": {"port": 19001}, "resources": {"statePath": ".g3ku/resources.state.json"}}),
        encoding="utf-8",
    )
    _seed_memory(target, marker="TGT", rows=7)
    _seed_governance(target, "false")
    service = get_bootstrap_security_service(target)
    service.setup_initial_realm(password=TARGET_PASSWORD)
    config_before = (target / ".g3ku" / "config.json").read_text(encoding="utf-8")
    key_before = service.unwrap_with_password(TARGET_PASSWORD)

    item = config_bundle.import_bundle(
        target,
        archive_path=archive,
        password=BUNDLE_PASSWORD,
        current_password=TARGET_PASSWORD,
        scopes=(config_bundle.SCOPE_MEMORY,),
    )

    assert item["scopes"] == ["memory"]
    assert item["config_entry_count"] == 0 and item["memory_entry_count"] == len(MEMORY_KEYS)
    assert item["password_kept"] is True
    assert item["status"] is None
    # 记忆面整库换掉：条目、镜像、正文都来自包里
    assert _memory_bodies(target) == _memory_bodies(source)
    assert (target / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "# mirror of SRC\n"
    assert (target / "memory" / "notes" / "note_alpha.md").read_text(encoding="utf-8") == "SRC body of note_alpha\n"
    # 配置面与钥匙一概没动
    assert (target / ".g3ku" / "config.json").read_text(encoding="utf-8") == config_before
    assert _read_governance(target, "ceo_frontdoor_regulatory_mode_enabled") == "false"
    assert get_bootstrap_security_service(target).unwrap_with_password(TARGET_PASSWORD) == key_before


def test_memory_only_import_leaves_the_targets_extra_notes_for_the_sweep(tmp_path: Path) -> None:
    """整包替换换的是库；本机多出来的正文不属于任何行，交给运行时的 orphan 清扫，导入不删。"""
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_memory_noise(_seed_memory(source, marker="SRC"))
    archive = _export_scopes(source, (config_bundle.SCOPE_MEMORY,))

    target = _write_workspace(tmp_path / "target")
    mem = _seed_memory(target, marker="TGT")
    _seed_memory_noise(mem, marker="target")
    (mem / "notes" / "note_targets_own.md").write_text("target body\n", encoding="utf-8")

    config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD,
                                scopes=(config_bundle.SCOPE_MEMORY,))

    assert (target / "memory" / "notes" / "note_targets_own.md").exists()
    # 两本机各自的取证备份与流水都不动：包里没带，导入也就不碰。
    assert (target / "memory" / "notes" / "note_alpha.md.bak.20261015-015857").read_text(
        encoding="utf-8"
    ) == "target evidence\n"
    assert (target / "memory" / "ops.jsonl").read_text(encoding="utf-8") == '{"log": "target round"}\n'


def test_memory_only_import_still_proves_the_local_password(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_memory(source, marker="SRC")
    archive = _export_scopes(source, (config_bundle.SCOPE_MEMORY,))

    target = _write_workspace(tmp_path / "target")
    _seed_memory(target, marker="TGT", rows=7)
    get_bootstrap_security_service(target).setup_initial_realm(password=TARGET_PASSWORD)

    with pytest.raises(ValueError, match="bundle_current_password_required"):
        config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD,
                                    scopes=(config_bundle.SCOPE_MEMORY,))

    assert _memory_bodies(target) == [f"TGT body {i}" for i in range(7)]


def test_import_rejects_a_scope_the_bundle_does_not_carry(tmp_path: Path) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_memory(source, marker="SRC")
    archive = _export_scopes(source, (config_bundle.SCOPE_MEMORY,))

    target = _write_workspace(tmp_path / "target")
    _seed_memory(target, marker="TGT", rows=7)

    with pytest.raises(ValueError, match="bundle_scope_not_in_package:config"):
        config_bundle.import_bundle(
            target,
            archive_path=archive,
            password=BUNDLE_PASSWORD,
            scopes=(config_bundle.SCOPE_CONFIG, config_bundle.SCOPE_MEMORY),
        )

    assert _memory_bodies(target) == [f"TGT body {i}" for i in range(7)]


def test_v2_bundle_without_scopes_still_imports_as_config_only(tmp_path: Path) -> None:
    """已经导出过的 v2 文件不能因为我们多加了一面就失效。"""
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    archive = _rescoped(_export_scopes(source, (config_bundle.SCOPE_CONFIG,)), version=2, scopes=None)

    target = _write_workspace(tmp_path / "target")

    item = config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD)

    assert item["scopes"] == ["config"]
    assert item["memory_entry_count"] == 0
    assert json.loads((target / ".g3ku" / "config.json").read_text(encoding="utf-8"))["web"]["port"] == 18790


def test_memory_entries_declared_config_only_are_rejected(tmp_path: Path) -> None:
    """外层声明与条目不符（把记忆包伪装成 v2）整包拒，不能靠运气少写一面。"""
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_memory(source, marker="SRC")
    archive = _rescoped(_export_scopes(source, (config_bundle.SCOPE_MEMORY,)), version=2, scopes=None)

    target = _write_workspace(tmp_path / "target")

    with pytest.raises(ValueError, match="declared scopes"):
        config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD)


def test_memory_notes_land_on_the_targets_own_configured_paths(tmp_path: Path) -> None:
    """包里只有逻辑名：目标机把 notes_dir 改到别处，正文要落去它自己算出来的那个目录。"""
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_memory(source, marker="SRC")
    archive = _export_scopes(source, (config_bundle.SCOPE_MEMORY,))

    target = _write_workspace(tmp_path / "target")
    mem = _seed_memory(target, marker="TGT")
    shutil.rmtree(mem / "notes")
    (mem / "private-notes").mkdir()
    manifest = target / "tools" / "memory_runtime"
    manifest.mkdir(parents=True)
    (manifest / "resource.yaml").write_text(
        "schema_version: 1\nkind: tool\nname: memory_runtime\nsettings:\n"
        "  document:\n    memory_file: memory/MEMORY.md\n    notes_dir: memory/private-notes\n",
        encoding="utf-8",
    )

    config_bundle.import_bundle(target, archive_path=archive, password=BUNDLE_PASSWORD,
                                scopes=(config_bundle.SCOPE_MEMORY,))

    assert (target / "memory" / "private-notes" / "note_alpha.md").read_text(encoding="utf-8").startswith("SRC")
    assert not (target / "memory" / "notes").exists()


def test_export_route_reports_scope_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)
    _seed_memory(workspace, marker="SRC")
    monkeypatch.chdir(workspace)
    client = _client()

    config_only = client.post(
        "/api/bootstrap/config-bundle/export",
        json={"password": BUNDLE_PASSWORD, "use_project_password": False},
    )
    with_memory = client.post(
        "/api/bootstrap/config-bundle/export",
        json={
            "password": BUNDLE_PASSWORD,
            "use_project_password": False,
            "include_config": False,
            "include_memory": True,
        },
    )

    assert config_only.json()["item"]["scopes"] == ["config"]
    assert config_only.json()["item"]["memory_entry_count"] == 0
    assert with_memory.json()["item"]["scopes"] == ["memory"]
    assert with_memory.json()["item"]["memory_entry_count"] == len(MEMORY_KEYS)
    assert with_memory.json()["item"]["config_entry_count"] == 0


def test_export_route_rejects_a_bundle_with_nothing_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _write_workspace(tmp_path / "source")
    _unlock(workspace)
    monkeypatch.chdir(workspace)

    empty = _client().post(
        "/api/bootstrap/config-bundle/export",
        json={"password": BUNDLE_PASSWORD, "include_config": False, "include_memory": False},
    )

    assert empty.status_code == 400
    assert empty.json()["detail"] == "bundle_scope_empty"


def test_import_route_applies_only_the_checked_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _write_workspace(tmp_path / "source")
    _unlock(source)
    _seed_memory(source, marker="SRC")
    archive = Path(config_bundle.export_bundle(
        source, password=BUNDLE_PASSWORD, use_project_password=False,
        scopes=(config_bundle.SCOPE_CONFIG, config_bundle.SCOPE_MEMORY),
    )["path"])

    target = _write_workspace(tmp_path / "target")
    (target / ".g3ku" / "config.json").write_text(
        json.dumps({"web": {"port": 19001}, "resources": {"statePath": ".g3ku/resources.state.json"}}),
        encoding="utf-8",
    )
    _seed_memory(target, marker="TGT", rows=7)
    monkeypatch.chdir(target)
    client = _client()

    with archive.open("rb") as handle:
        memory_only = client.post(
            "/api/bootstrap/config-bundle/import",
            data={"password": BUNDLE_PASSWORD, "include_config": "false", "include_memory": "true"},
            files={"file": ("bundle.g3kucb", handle, "application/octet-stream")},
        )

    assert memory_only.status_code == 200
    item = memory_only.json()["item"]
    assert item["scopes"] == ["memory"]
    assert item["memory_entry_count"] == len(MEMORY_KEYS)
    assert item["restart_required"] is True
    assert _memory_bodies(target) == _memory_bodies(source)
    assert json.loads((target / ".g3ku" / "config.json").read_text(encoding="utf-8"))["web"]["port"] == 19001

    with archive.open("rb") as handle:
        refused = client.post(
            "/api/bootstrap/config-bundle/import",
            data={"password": BUNDLE_PASSWORD, "include_config": "false", "include_memory": "false"},
            files={"file": ("bundle.g3kucb", handle, "application/octet-stream")},
        )
    assert refused.status_code == 400
    assert refused.json()["detail"] == "bundle_scope_empty"

