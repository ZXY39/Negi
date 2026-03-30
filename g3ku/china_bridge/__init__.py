from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "CHINA_CHANNELS",
    "ChinaBridgeClient",
    "ChinaBridgeSupervisor",
    "ChinaBridgeTransport",
    "build_chat_id",
    "build_session_key",
]


def __getattr__(name: str) -> Any:
    if name in {"build_chat_id", "build_session_key"}:
        return getattr(import_module("g3ku.china_bridge.session_keys"), name)
    if name in {"CHINA_CHANNELS", "ChinaBridgeTransport"}:
        return getattr(import_module("g3ku.china_bridge.transport"), name)
    if name == "ChinaBridgeClient":
        return getattr(import_module("g3ku.china_bridge.client"), name)
    if name == "ChinaBridgeSupervisor":
        return getattr(import_module("g3ku.china_bridge.supervisor"), name)
    raise AttributeError(name)
