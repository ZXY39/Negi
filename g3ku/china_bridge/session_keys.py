from __future__ import annotations


def normalize_account_id(value: str | None) -> str:
    return str(value or "").strip() or "default"


def normalize_peer_kind(value: str | None) -> str:
    raw = str(value or "user").strip().lower()
    if raw in {"group", "chat", "channel"}:
        return "group"
    return "dm"


def build_session_key(
    *,
    channel: str,
    account_id: str | None,
    peer_kind: str,
    peer_id: str,
    thread_id: str | None = None,
) -> str:
    scope = normalize_peer_kind(peer_kind)
    key = f"china:{str(channel or '').strip()}:{normalize_account_id(account_id)}:{scope}:{str(peer_id or '').strip()}"
    thread = str(thread_id or "").strip()
    if thread:
        key = f"{key}:thread:{thread}"
    return key


def build_chat_id(
    *,
    account_id: str | None,
    peer_kind: str,
    peer_id: str,
    thread_id: str | None = None,
) -> str:
    base = f"{normalize_account_id(account_id)}:{normalize_peer_kind(peer_kind)}:{str(peer_id or '').strip()}"
    thread = str(thread_id or "").strip()
    return f"{base}:thread:{thread}" if thread else base
