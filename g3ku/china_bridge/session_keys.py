"""Compatibility shim.

Canonical implementation lives at ``g3ku.runtime.session_keys`` (channel
communication rebuild, Step 1). This shim keeps legacy imports
(``g3ku.china_bridge.session_keys``) working and is removed together with the
china_channels subsystem (rebuild Step 4).
"""

from __future__ import annotations

from g3ku.runtime.session_keys import (  # noqa: F401
    CHINA_SESSION_KEY_PREFIX,
    CHANNEL_SESSION_KEY_PREFIXES,
    EXTERNAL_SESSION_KEY_PREFIX,
    ParsedChinaSessionKey,
    build_chat_id,
    build_external_session_key,
    build_memory_chat_id,
    build_runtime_chat_id,
    build_session_key,
    is_channel_session_key,
    normalize_account_id,
    normalize_bridge_id,
    normalize_peer_kind,
    parse_china_session_key,
)

__all__ = [
    "CHINA_SESSION_KEY_PREFIX",
    "CHANNEL_SESSION_KEY_PREFIXES",
    "EXTERNAL_SESSION_KEY_PREFIX",
    "ParsedChinaSessionKey",
    "build_chat_id",
    "build_external_session_key",
    "build_memory_chat_id",
    "build_runtime_chat_id",
    "build_session_key",
    "is_channel_session_key",
    "normalize_account_id",
    "normalize_bridge_id",
    "normalize_peer_kind",
    "parse_china_session_key",
]
