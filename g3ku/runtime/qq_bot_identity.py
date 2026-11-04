"""Per-bridge QQ bot identity reported by the in-process official adapter.

The bridge is the only component that can call ``/users/@me``; the runtime needs
the nickname for the QQ identity notice. This store is intentionally tiny and
separate from external session entries: identity is per account (bridge_id), not
per conversation.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

from g3ku.deployment.data_root import data_work_path

_LOCK = threading.RLock()


def _path():
    return data_work_path("external-sessions", "qq-bot-identity.json")


def _read() -> dict[str, Any]:
    try:
        payload = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    bots = payload.get("bots")
    if not isinstance(bots, dict):
        bots = {}
    return {"version": 1, "bots": bots}


def _write(payload: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def set_qq_bot_identity(bridge_id: str, *, bot_id: str = "", nickname: str = "") -> dict[str, Any]:
    key = str(bridge_id or "").strip()
    if not key:
        return {}
    entry = {
        "bot_id": str(bot_id or "").strip()[:128],
        "nickname": " ".join(str(nickname or "").split())[:128],
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with _LOCK:
        payload = _read()
        payload["bots"][key] = entry
        _write(payload)
    return dict(entry)


def get_qq_bot_identity(bridge_id: str) -> dict[str, Any]:
    key = str(bridge_id or "").strip()
    if not key:
        return {}
    with _LOCK:
        bots = _read()["bots"]
    entry = bots.get(key)
    return dict(entry) if isinstance(entry, dict) else {}


def clear_qq_bot_identity_store() -> None:
    with _LOCK:
        try:
            _path().unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


__all__ = [
    "clear_qq_bot_identity_store",
    "get_qq_bot_identity",
    "set_qq_bot_identity",
]
