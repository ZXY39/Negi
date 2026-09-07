"""Pure mapping helpers for the QQ official adapter.

No botpy import, no I/O: everything here translates between QQ-side
identifiers and the External Agent API's ``external_key`` namespace, so it is
fully unit-testable.

external_key shapes (bridge-owned, opaque to g3ku):
- 群 @机器人 / 群消息: ``qq:group:<group_openid>``
- 单聊(好友):          ``qq:c2c:<user_openid>``
- 频道:                ``qq:guild:<guild_id>:<channel_id>``
- 频道私信:            ``qq:guilddm:<guild_id>:<user_openid>``
"""

from __future__ import annotations

from typing import Any

QQ_BRIDGE_ID = "qq-official"


def external_key_for_group(group_openid: Any) -> str:
    return f"qq:group:{str(group_openid or '').strip()}"


def external_key_for_c2c(user_openid: Any) -> str:
    return f"qq:c2c:{str(user_openid or '').strip()}"


def external_key_for_guild(guild_id: Any, channel_id: Any) -> str:
    return f"qq:guild:{str(guild_id or '').strip()}:{str(channel_id or '').strip()}"


def external_key_for_guild_dm(guild_id: Any, user_id: Any) -> str:
    return f"qq:guilddm:{str(guild_id or '').strip()}:{str(user_id or '').strip()}"


def idempotency_key_for(event_id: Any) -> str:
    raw = str(event_id or "").strip()
    return f"qq-{raw}" if raw else ""


def parse_external_key(external_key: str) -> tuple[str, dict[str, str]]:
    """Inverse mapping: external_key -> (kind, target) for reply delivery.

    kind is one of ``group`` / ``c2c`` / ``guild`` / ``guilddm`` / ``unknown``
    and target carries the fields the botpy post_* call needs.
    """
    parts = str(external_key or "").strip().split(":")
    if len(parts) >= 3 and parts[0] == "qq":
        kind = parts[1]
        if kind == "group" and len(parts) == 3:
            return "group", {"group_openid": parts[2]}
        if kind == "c2c" and len(parts) == 3:
            return "c2c", {"user_openid": parts[2]}
        if kind == "guild" and len(parts) == 4:
            return "guild", {"guild_id": parts[2], "channel_id": parts[3]}
        if kind == "guilddm" and len(parts) == 4:
            return "guilddm", {"guild_id": parts[2], "user_id": parts[3]}
    return "unknown", {}


# Event types the bridge consumes from ``/api/v1/sessions/{id}/events``.
REPLY_FINAL_EVENT = "reply.final"
REPLY_DELTA_EVENT = "reply.delta"
OUTBOUND_EVENT = "outbound.created"


def is_deliverable_event(event_type: str) -> bool:
    return event_type in (REPLY_FINAL_EVENT, OUTBOUND_EVENT)