from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "subsystems" / "china_channels_host" / "channel_registry.json"


@lru_cache(maxsize=1)
def _registry_payload() -> dict[str, Any]:
    payload = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid china channel registry payload: {_REGISTRY_PATH}")
    items = payload.get("channels")
    if not isinstance(items, list):
        raise ValueError(f"Invalid china channel registry channel list: {_REGISTRY_PATH}")
    return payload


def china_channel_registry_path() -> Path:
    return _REGISTRY_PATH


def list_china_channel_specs() -> list[dict[str, Any]]:
    items = _registry_payload().get("channels") or []
    return [dict(item) for item in items if isinstance(item, dict)]


def china_channel_ids() -> list[str]:
    return [str(item.get("id") or "").strip() for item in list_china_channel_specs() if str(item.get("id") or "").strip()]


def china_channel_id_set() -> set[str]:
    return set(china_channel_ids())


def china_channel_spec(channel_id: str) -> dict[str, Any]:
    normalized = normalize_china_channel_id(channel_id)
    for item in list_china_channel_specs():
        if str(item.get("id") or "").strip() == normalized:
            return dict(item)
    raise KeyError(f"Unknown china channel: {channel_id}")


def china_channel_attr(channel_id: str) -> str:
    spec = china_channel_spec(channel_id)
    return str(spec.get("python_attr") or "").strip() or normalize_china_channel_id(channel_id).replace("-", "_")


def china_channel_secret_fields(channel_id: str) -> list[str]:
    try:
        spec = china_channel_spec(channel_id)
    except KeyError:
        return []
    values = spec.get("secret_fields")
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def china_channel_template(channel_id: str) -> dict[str, Any]:
    spec = china_channel_spec(channel_id)
    payload = spec.get("template_json")
    return dict(payload) if isinstance(payload, dict) else {}


def china_channel_maintenance_status(channel_id: str) -> str:
    spec = china_channel_spec(channel_id)
    return str(spec.get("maintenance_status") or "active").strip().lower() or "active"


@lru_cache(maxsize=1)
def china_channel_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for item in list_china_channel_specs():
        channel_id = str(item.get("id") or "").strip()
        if not channel_id:
            continue
        candidates = {channel_id, channel_id.replace("-", "_")}
        for raw in item.get("legacy_keys") or []:
            text = str(raw or "").strip()
            if text:
                candidates.add(text)
                candidates.add(text.replace("-", "_"))
        for candidate in candidates:
            aliases[candidate.lower()] = channel_id
    return aliases


def normalize_china_channel_id(channel_id: str) -> str:
    raw = str(channel_id or "").strip().lower()
    normalized = china_channel_aliases().get(raw.replace("-", "_"), china_channel_aliases().get(raw))
    if normalized is None:
        raise KeyError(f"Unknown china channel: {channel_id}")
    return normalized


def clear_china_channel_registry_cache() -> None:
    _registry_payload.cache_clear()
    china_channel_aliases.cache_clear()


__all__ = [
    "china_channel_aliases",
    "china_channel_attr",
    "china_channel_id_set",
    "china_channel_ids",
    "china_channel_maintenance_status",
    "china_channel_registry_path",
    "china_channel_secret_fields",
    "china_channel_spec",
    "china_channel_template",
    "clear_china_channel_registry_cache",
    "list_china_channel_specs",
    "normalize_china_channel_id",
]
