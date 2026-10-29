"""Session trust levels for channel traffic.

Contract owner: ``docs/architecture/sandbox-execution-plane.md`` §信任分级.

The runtime has exactly one notion of "who is speaking to this session":
``owner`` (the operator's own local surface, or a whitelisted openid on a
direct channel) and ``third_party`` (anyone else). Multi-party chats are third
party **wholesale** — the QQ group payload carries no nickname and its
``member_openid`` cannot be matched against a c2c ``user_openid``, so "which
member of the group is the operator" is undecidable at the protocol layer.

Two rules matter for callers:

* ``effective trust = min(session trust, turn trust)``: trust only ever moves
  down. A session that has seen a stranger stays third party even when a later
  turn claims to be the owner.
* Fail closed: an external session the registry cannot classify is third party.
  A missing or empty sender is never treated as the operator.

Reads are pure in-memory registry lookups (the registry is a process
singleton); the trust of a stored turn is never re-derived from disk.
"""

from __future__ import annotations

from typing import Any, Iterable

TRUST_OWNER = "owner"
TRUST_THIRD_PARTY_DM = "third_party_dm"
TRUST_THIRD_PARTY_GROUP = "third_party_group"

ALL_TRUST_LEVELS: tuple[str, ...] = (
    TRUST_OWNER,
    TRUST_THIRD_PARTY_DM,
    TRUST_THIRD_PARTY_GROUP,
)

# 数值越大越受限；`min(会话, 回合)` 就是取更大的那个数。
_TRUST_RANK: dict[str, int] = {
    TRUST_OWNER: 0,
    TRUST_THIRD_PARTY_DM: 1,
    TRUST_THIRD_PARTY_GROUP: 2,
}

# MCP 网关每轮自报的发送者身份。管理面「当操作员」开关写的就是这个 id：把它加进
# ``externalApi.tokens.<id>.trustedSenders`` 后，该桥的会话按 owner 处理（档位、记忆、
# 人设、任务四扇门同源）。常量放这里而不是网关包里，是为了让 admin 路由引用它时
# 不把 httpx 客户端拖进导入图。
GATEWAY_SENDER_ID = "negi-mcp"

# ``/api/v1`` 接受的 ``chat_type`` 白名单。guild/guild_dm 是 QQ 频道（多人/私信）
# 的既有说法，与 group/dm 同义分档。
CHAT_TYPE_GROUP = "group"
CHAT_TYPE_DM = "dm"
CHAT_TYPE_GUILD = "guild"
CHAT_TYPE_GUILD_DM = "guild_dm"
CHAT_TYPES: frozenset[str] = frozenset(
    {CHAT_TYPE_GROUP, CHAT_TYPE_DM, CHAT_TYPE_GUILD, CHAT_TYPE_GUILD_DM}
)
MULTI_PARTY_CHAT_TYPES: frozenset[str] = frozenset({CHAT_TYPE_GROUP, CHAT_TYPE_GUILD})

# 桥在 ``external_key`` 里自报的聊天形状。服务端不接受 ``trust`` 自报，但这两个
# 前缀是既有契约（`g3ku/qq_official/messages.py`），用作 ``chat_type`` 缺失时的回退。
_GROUP_EXTERNAL_KEY_PREFIXES = ("qq:group:", "qq:guild:")
_DM_EXTERNAL_KEY_PREFIXES = ("qq:c2c:", "qq:guilddm:")


def normalize_chat_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in CHAT_TYPES else ""


def chat_type_from_external_key(external_key: Any) -> str:
    key = str(external_key or "").strip()
    if key.startswith(_GROUP_EXTERNAL_KEY_PREFIXES):
        return CHAT_TYPE_GROUP
    if key.startswith(_DM_EXTERNAL_KEY_PREFIXES):
        return CHAT_TYPE_DM
    return ""


def is_multi_party_chat(*, chat_type: Any = "", external_key: Any = "") -> bool:
    normalized = normalize_chat_type(chat_type)
    if normalized:
        return normalized in MULTI_PARTY_CHAT_TYPES
    return chat_type_from_external_key(external_key) in MULTI_PARTY_CHAT_TYPES


def normalize_trust(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _TRUST_RANK else ""


def more_restrictive(current: Any, incoming: Any) -> str:
    """``min`` over the severity axis; an empty side means "no opinion".

    两边都空才 fail-closed 到 third-party-dm —— 空值表示"还没判过"，不是"更宽松"，
    否则会话第一次见到 owner 就会被自己的空记录降级成第三方。
    """
    left = normalize_trust(current)
    right = normalize_trust(incoming)
    if not left:
        return right or TRUST_THIRD_PARTY_DM
    if not right:
        return left
    return left if _TRUST_RANK[left] >= _TRUST_RANK[right] else right


def is_trusted_sender(
    sender_id: Any,
    *,
    owner_openids: Iterable[Any] = (),
    trusted_senders: Iterable[Any] = (),
) -> bool:
    """空 sender 永不等于操作员：白名单里不含空串，比较也就不会意外命中。"""
    candidate = str(sender_id or "").strip()
    if not candidate:
        return False
    return candidate in _normalized_set(owner_openids) or candidate in _normalized_set(trusted_senders)


def _normalized_set(values: Iterable[Any]) -> frozenset[str]:
    return frozenset(
        str(item or "").strip() for item in values or () if str(item or "").strip()
    )


def resolve_turn_trust(
    *,
    chat_type: Any = "",
    external_key: Any = "",
    sender_id: Any = "",
    owner_openids: Iterable[Any] = (),
    trusted_senders: Iterable[Any] = (),
) -> str:
    """Trust of one inbound channel message.

    Multi-party chats short-circuit to ``third_party_group`` regardless of the
    sender: in a group the operator is one of many listeners, and a whitelisted
    openid must not upgrade the whole group session.
    """
    if is_multi_party_chat(chat_type=chat_type, external_key=external_key):
        return TRUST_THIRD_PARTY_GROUP
    if is_trusted_sender(
        sender_id, owner_openids=owner_openids, trusted_senders=trusted_senders
    ):
        return TRUST_OWNER
    return TRUST_THIRD_PARTY_DM


def session_trust(session_key: Any) -> str:
    """Effective (sticky) trust for a runtime session key.

    Local sessions (``web:`` / ``internal:`` / CLI) are owner. External bridge
    sessions read the registry entry, which records the most restrictive turn
    trust ever observed. Unregistered or unclassifiable external keys — and
    legacy ``china:`` keys, whose channel subsystem no longer exists — are
    third party.
    """
    raw = str(session_key or "").strip()
    if not raw:
        return TRUST_THIRD_PARTY_DM
    # 惰性导入：`session_keys` 顶层会拉起 `frontdoor.tool_contract`（进而拉起 main），
    # 本模块要被记忆门/投影这类热路径 import，不能在模块级把它拖进来。
    from g3ku.runtime.session_keys import is_channel_session_key, parse_china_session_key

    if not is_channel_session_key(raw):
        return TRUST_OWNER

    entry = _registry_entry(raw)
    if entry is not None:
        recorded = normalize_trust(getattr(entry, "trust", ""))
        if recorded:
            return recorded
        if is_multi_party_chat(
            chat_type=getattr(entry, "chat_type", ""),
            external_key=getattr(entry, "external_key", ""),
        ):
            return TRUST_THIRD_PARTY_GROUP
        return TRUST_THIRD_PARTY_DM

    parsed = parse_china_session_key(raw)
    if parsed is not None and parsed.chat_type == "group":
        return TRUST_THIRD_PARTY_GROUP
    return TRUST_THIRD_PARTY_DM


def is_owner_session(session_key: Any) -> bool:
    return session_trust(session_key) == TRUST_OWNER


def is_third_party_session(session_key: Any) -> bool:
    return session_trust(session_key) != TRUST_OWNER


def _registry_entry(session_key: str) -> Any:
    # 惰性导入：`external_sessions` 会经 `g3ku.config.live_runtime` 拉起配置层，
    # 而本模块要被记忆门/投影这些热路径 import，模块级导入会扩大启动图。
    try:
        from g3ku.runtime.external_sessions import get_external_session_registry
    except Exception:  # noqa: BLE001 - 拿不到注册表就按第三方兜底
        return None
    try:
        return get_external_session_registry().get_by_session_key(session_key)
    except Exception:  # noqa: BLE001
        return None


def note_inbound_session_shape(
    session_key: str,
    *,
    chat_type: Any = "",
    external_key: Any = "",
    sender_id: Any = "",
    owner_openids: Iterable[Any] = (),
    trusted_senders: Iterable[Any] = (),
) -> str:
    """Record an inbound message's shape on the registry entry; return its trust.

    Sticky by design: the entry keeps the most restrictive trust ever observed,
    so one turn claiming owner cannot lift an earlier third-party verdict. The
    write only happens when something actually changes, so the steady state is
    one dict lookup per message.
    """
    resolved = resolve_turn_trust(
        chat_type=chat_type,
        external_key=external_key,
        sender_id=sender_id,
        owner_openids=owner_openids,
        trusted_senders=trusted_senders,
    )
    try:
        from g3ku.runtime.external_sessions import get_external_session_registry

        get_external_session_registry().note_inbound(
            session_key,
            chat_type=normalize_chat_type(chat_type)
            or chat_type_from_external_key(external_key),
            trust=resolved,
        )
    except Exception:  # noqa: BLE001 - 记不住不阻塞入站，读取侧仍按第三方兜底
        pass
    return resolved


__all__ = [
    "ALL_TRUST_LEVELS",
    "CHAT_TYPES",
    "CHAT_TYPE_DM",
    "CHAT_TYPE_GROUP",
    "CHAT_TYPE_GUILD",
    "CHAT_TYPE_GUILD_DM",
    "GATEWAY_SENDER_ID",
    "MULTI_PARTY_CHAT_TYPES",
    "TRUST_OWNER",
    "TRUST_THIRD_PARTY_DM",
    "TRUST_THIRD_PARTY_GROUP",
    "chat_type_from_external_key",
    "is_multi_party_chat",
    "is_owner_session",
    "is_third_party_session",
    "is_trusted_sender",
    "more_restrictive",
    "normalize_chat_type",
    "normalize_trust",
    "note_inbound_session_shape",
    "resolve_turn_trust",
    "session_trust",
]
