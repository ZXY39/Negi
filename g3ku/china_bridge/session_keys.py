from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ParsedChinaSessionKey:
    channel: str
    account_id: str
    chat_type: str
    peer_id: str | None
    thread_id: str | None
    merged_dm: bool


def normalize_account_id(value: str | None) -> str:
    return str(value or "").strip() or "default"


def normalize_peer_kind(value: str | None) -> str:
    raw = str(value or "user").strip().lower()
    if raw in {"group", "chat", "channel"}:
        return "group"
    return "dm"


def _normalized_thread_id(thread_id: str | None) -> str | None:
    text = str(thread_id or "").strip()
    return text or None


def _normalized_peer_id(peer_id: str | None) -> str | None:
    text = str(peer_id or "").strip()
    return text or None


def build_session_key(
    *,
    channel: str,
    account_id: str | None,
    peer_kind: str,
    peer_id: str,
    thread_id: str | None = None,
) -> str:
    channel_value = str(channel or "").strip()
    account_value = normalize_account_id(account_id)
    scope = normalize_peer_kind(peer_kind)
    thread = _normalized_thread_id(thread_id)
    if scope == "dm":
        key = f"china:{channel_value}:{account_value}:dm"
        if thread:
            key = f"{key}:thread:{thread}"
        return key

    peer_value = _normalized_peer_id(peer_id) or "unknown"
    key = f"china:{channel_value}:{account_value}:group:{peer_value}"
    if thread:
        key = f"{key}:thread:{thread}"
    return key


def build_runtime_chat_id(
    *,
    account_id: str | None,
    peer_kind: str,
    peer_id: str,
    thread_id: str | None = None,
) -> str:
    account_value = normalize_account_id(account_id)
    scope = normalize_peer_kind(peer_kind)
    peer_value = _normalized_peer_id(peer_id) or "unknown"
    base = f"{account_value}:{scope}:{peer_value}"
    thread = _normalized_thread_id(thread_id)
    return f"{base}:thread:{thread}" if thread else base


def build_memory_chat_id(
    *,
    account_id: str | None,
    peer_kind: str,
    peer_id: str,
    thread_id: str | None = None,
) -> str:
    account_value = normalize_account_id(account_id)
    scope = normalize_peer_kind(peer_kind)
    thread = _normalized_thread_id(thread_id)
    if scope == "dm":
        base = f"{account_value}:dm"
        return f"{base}:thread:{thread}" if thread else base
    peer_value = _normalized_peer_id(peer_id) or "unknown"
    base = f"{account_value}:group:{peer_value}"
    return f"{base}:thread:{thread}" if thread else base


def build_chat_id(
    *,
    account_id: str | None,
    peer_kind: str,
    peer_id: str,
    thread_id: str | None = None,
) -> str:
    return build_runtime_chat_id(
        account_id=account_id,
        peer_kind=peer_kind,
        peer_id=peer_id,
        thread_id=thread_id,
    )


def parse_china_session_key(session_key: str | None) -> ParsedChinaSessionKey | None:
    raw = str(session_key or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) < 4 or parts[0] != "china":
        return None

    channel = parts[1].strip()
    account_id = normalize_account_id(parts[2])
    chat_type = parts[3].strip().lower()
    if not channel or chat_type not in {"dm", "group"}:
        return None

    remainder = parts[4:]
    peer_id: str | None = None
    thread_id: str | None = None
    merged_dm = False

    if chat_type == "dm":
        if not remainder:
            merged_dm = True
        elif remainder[0] == "thread":
            merged_dm = True
            thread_id = ":".join(remainder[1:]).strip() or None
        else:
            peer_id = remainder[0].strip() or None
            if len(remainder) >= 3 and remainder[1] == "thread":
                thread_id = ":".join(remainder[2:]).strip() or None
    else:
        if not remainder:
            return None
        peer_id = remainder[0].strip() or None
        if not peer_id:
            return None
        if len(remainder) >= 3 and remainder[1] == "thread":
            thread_id = ":".join(remainder[2:]).strip() or None

    return ParsedChinaSessionKey(
        channel=channel,
        account_id=account_id,
        chat_type=chat_type,
        peer_id=peer_id,
        thread_id=thread_id,
        merged_dm=merged_dm,
    )
