from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from g3ku.capabilities.models import CapabilityRecord


class CapabilityStateStore:
    """Persist capability enable/disable and install metadata."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "capabilities": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"schema_version": 1, "capabilities": {}}

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_record(self, name: str) -> CapabilityRecord | None:
        raw = (self._data.get("capabilities") or {}).get(name)
        if not isinstance(raw, dict):
            return None
        return CapabilityRecord(
            enabled=bool(raw.get("enabled", True)),
            version=raw.get("version"),
            install_hash=raw.get("install_hash"),
            last_validated_at=raw.get("last_validated_at"),
            compatibility=dict(raw.get("compatibility") or {}),
            source=dict(raw.get("source") or {}),
            installed_path=raw.get("installed_path"),
        )

    def upsert(self, name: str, record: CapabilityRecord) -> None:
        self._data.setdefault("capabilities", {})[name] = asdict(record)
        self.save()

    def set_enabled(self, name: str, enabled: bool) -> CapabilityRecord:
        record = self.get_record(name) or CapabilityRecord()
        record.enabled = bool(enabled)
        self.upsert(name, record)
        return record

    def remove(self, name: str) -> None:
        capabilities = self._data.setdefault("capabilities", {})
        if name in capabilities:
            capabilities.pop(name, None)
            self.save()

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self._data))
