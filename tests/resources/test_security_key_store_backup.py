"""The key store gets its own rolling backup, an evidence trail, and no silent deletion."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _backup_mod():
    return importlib.import_module("g3ku.security.key_store_backup")


def _bootstrap_mod():
    return importlib.import_module("g3ku.security.bootstrap")


def _seed_store(workspace: Path, *, with_master_key: bool = True) -> Path:
    records = workspace / ".g3ku" / "llm-config" / "records"
    records.mkdir(parents=True, exist_ok=True)
    (records / "cfg1.json").write_text("{}", encoding="utf-8")
    summary = {
        "config_id": "cfg1",
        "provider_id": "openai",
        "display_name": "cfg1",
        "default_model": "gpt",
        "created_at": "2026-04-17T09:00:00+00:00",
        "updated_at": "2026-04-17T09:00:00+00:00",
    }
    (workspace / ".g3ku" / "llm-config" / "index.json").write_text(
        json.dumps({"version": 1, "configs": [summary]}, indent=2),
        encoding="utf-8",
    )
    realms = workspace / ".g3ku" / "secret-realms"
    realms.mkdir(parents=True, exist_ok=True)
    (realms / "default.enc").write_text("overlay-blob", encoding="utf-8")
    master_key = workspace / ".g3ku" / "llm-config" / "master.key"
    if with_master_key:
        master_key.write_text(json.dumps({"version": 2}), encoding="utf-8")
    (workspace / ".g3ku" / "llm-config" / "auto-unlock.key").write_text("k\n", encoding="utf-8")
    return master_key


def test_snapshot_copies_key_files_and_skips_an_unchanged_store(tmp_path: Path) -> None:
    module = _backup_mod()
    _seed_store(tmp_path)

    first = module.snapshot_key_store(tmp_path, reason="test")
    assert first is not None
    copied = sorted(path.name for path in first.rglob("*") if path.is_file())
    assert copied == ["auto-unlock.key", "cfg1.json", "default.enc", "index.json", "master.key"]

    again = module.snapshot_key_store(tmp_path, reason="test2")
    assert again == first, "an unchanged store must not consume a generation"
    forced = module.snapshot_key_store(tmp_path, reason="explicit", force=True)
    assert forced != first


def test_unconfigured_store_never_consumes_a_generation(tmp_path: Path) -> None:
    module = _backup_mod()
    (tmp_path / ".g3ku" / "llm-config").mkdir(parents=True)
    (tmp_path / ".g3ku" / "llm-config" / "index.json").write_text(
        json.dumps({"version": 1, "configs": []}), encoding="utf-8"
    )

    assert module.snapshot_key_store(tmp_path, reason="boot") is None
    assert module.list_backups(tmp_path) == []
    assert module.audit_key_store_continuity(tmp_path, reason="boot")["state"] == "unconfigured"


def test_audit_reports_loss_and_does_not_snapshot_the_damaged_state(tmp_path: Path) -> None:
    module = _backup_mod()
    master_key = _seed_store(tmp_path)
    module.snapshot_key_store(tmp_path, reason="boot")

    master_key.unlink()
    (tmp_path / ".g3ku" / "llm-config" / "records" / "cfg1.json").unlink()
    (tmp_path / ".g3ku" / "secret-realms" / "default.enc").unlink()

    finding = module.audit_key_store_continuity(tmp_path, reason="boot")

    assert finding["state"] == "lost"
    assert finding["missing"] == [".g3ku/llm-config/master.key"]
    assert int(finding["backups"]) == 1, "the wiped state must not push the good copy out"
    assert Path(finding["newest_backup"]).is_dir()
    events = [str(item.get("event") or "") for item in module.read_events(tmp_path)]
    assert "key_store_missing_material" in events


def test_restore_is_dry_by_default_and_refuses_to_overwrite_a_live_key(tmp_path: Path) -> None:
    module = _backup_mod()
    master_key = _seed_store(tmp_path)
    generation = module.snapshot_key_store(tmp_path, reason="boot")

    master_key.unlink()
    planned = module.restore_from_backup(tmp_path, generation)
    assert planned["dry_run"] is True
    assert not master_key.exists()

    applied = module.restore_from_backup(tmp_path, generation, dry_run=False)
    assert master_key.exists()
    assert len(applied["files"]) == 5

    with pytest.raises(ValueError, match="refusing to overwrite"):
        module.restore_from_backup(tmp_path, generation, dry_run=False)


def test_bootstrap_status_surfaces_a_lost_key_store(tmp_path: Path) -> None:
    bootstrap = _bootstrap_mod()
    service = bootstrap.BootstrapSecurityService(workspace=tmp_path)
    service.setup_initial_realm(password="alpha-pass")

    status = service.status()
    assert status["key_store"]["state"] == "ok"
    assert int(status["key_store"]["backups"]) >= 1

    (tmp_path / ".g3ku" / "llm-config" / "master.key").unlink()
    finding = service.audit_key_store(reason="test")
    assert finding["state"] == "lost"
    assert service.status()["key_store"]["state"] == "lost"


def test_legacy_migration_refuses_to_delete_without_a_backup_copy(tmp_path: Path, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    bootstrap = _bootstrap_mod()
    module = _backup_mod()
    _seed_store(tmp_path)
    # The legacy shape this migration exists for: master.key holding a bare key, not an envelope.
    (tmp_path / ".g3ku" / "llm-config" / "master.key").write_text(
        Fernet.generate_key().decode("ascii"), encoding="utf-8"
    )
    service = bootstrap.BootstrapSecurityService(workspace=tmp_path)
    assert service.legacy_detected() is True

    def _copy_silently_does_nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bootstrap.shutil, "copytree", _copy_silently_does_nothing)

    with pytest.raises(ValueError, match="backup copy of the key store is missing"):
        service._migrate_legacy_state()

    assert (tmp_path / ".g3ku" / "llm-config" / "master.key").exists()
    assert module.snapshot_key_store(tmp_path, reason="still-kept") is not None
