from __future__ import annotations

import json
from pathlib import Path

from .exceptions import ConfigNotFoundError
from .models import NormalizedProviderConfig, StoredConfigSummary
from .secret_store import SecretStore


class EncryptedConfigRepository:
    def __init__(self, storage_root: str | Path, secret_store: SecretStore | None):
        self.storage_root = Path(storage_root)
        self.records_root = self.storage_root / "records"
        self.index_path = self.storage_root / "index.json"
        self.secret_store = secret_store
        self.records_root.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._write_index([])

    def _read_index(self) -> list[StoredConfigSummary]:
        if not self.index_path.exists():
            return []
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        entries = payload.get("configs", []) if isinstance(payload, dict) else []
        return [StoredConfigSummary.model_validate(entry) for entry in entries]

    def _write_index(self, entries: list[StoredConfigSummary]) -> None:
        payload = {
            "version": 1,
            "configs": [entry.model_dump(mode="json") for entry in entries],
        }
        self.index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _record_path(self, config_id: str) -> Path:
        return self.records_root / f"{config_id}.json"

    def _legacy_record_path(self, config_id: str) -> Path:
        return self.records_root / f"{config_id}.enc"

    def list_summaries(self) -> list[StoredConfigSummary]:
        return sorted(self._read_index(), key=lambda item: item.updated_at, reverse=True)

    def save(self, config: NormalizedProviderConfig, *, last_probe_status: str | None = None) -> StoredConfigSummary:
        self._record_path(config.config_id).write_text(
            config.model_dump_json(indent=2),
            encoding="utf-8",
        )
        legacy_path = self._legacy_record_path(config.config_id)
        if legacy_path.exists():
            legacy_path.unlink()
        summary = StoredConfigSummary(
            config_id=config.config_id,
            provider_id=config.provider_id,
            display_name=config.display_name,
            capability=config.capability,
            default_model=config.default_model,
            last_probe_status=last_probe_status,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )
        entries = [entry for entry in self._read_index() if entry.config_id != config.config_id]
        entries.append(summary)
        self._write_index(entries)
        return summary

    def get(self, config_id: str) -> NormalizedProviderConfig:
        record_path = self._record_path(config_id)
        if record_path.exists():
            return NormalizedProviderConfig.model_validate_json(record_path.read_text(encoding="utf-8"))
        legacy_path = self._legacy_record_path(config_id)
        if legacy_path.exists() and self.secret_store is not None:
            decrypted = self.secret_store.decrypt(legacy_path.read_bytes())
            return NormalizedProviderConfig.model_validate_json(decrypted)
        raise ConfigNotFoundError(f"Config not found: {config_id}")

    def delete(self, config_id: str) -> None:
        record_path = self._record_path(config_id)
        legacy_path = self._legacy_record_path(config_id)
        if not record_path.exists() and not legacy_path.exists():
            raise ConfigNotFoundError(f"Config not found: {config_id}")
        if record_path.exists():
            record_path.unlink()
        if legacy_path.exists():
            legacy_path.unlink()
        entries = [entry for entry in self._read_index() if entry.config_id != config_id]
        self._write_index(entries)
