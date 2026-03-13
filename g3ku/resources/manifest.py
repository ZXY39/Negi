from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


class ResourceManifestError(ValueError):
    pass


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ResourceManifestError(f"failed to read manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ResourceManifestError(f"manifest must be a mapping: {path}")
    data = copy.deepcopy(raw)
    schema_version = int(data.get("schema_version") or 1)
    if schema_version != 1:
        raise ResourceManifestError(f"unsupported schema_version {schema_version}: {path}")
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in {"skill", "tool"}:
        raise ResourceManifestError(f"manifest kind must be 'skill' or 'tool': {path}")
    name = str(data.get("name") or path.parent.name).strip()
    if not name:
        raise ResourceManifestError(f"manifest name missing: {path}")
    data["schema_version"] = schema_version
    data["kind"] = kind
    data["name"] = name
    data.setdefault("description", "")
    data.setdefault("requires", {})
    data.setdefault("exposure", {"agent": True, "main_runtime": True})
    return data
