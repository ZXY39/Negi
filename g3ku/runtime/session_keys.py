from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from g3ku.runtime.frontdoor.tool_contract import strip_frontdoor_tool_contract_echo
from g3ku.runtime.stage_prompt_compaction import strip_stage_block_echo

"""Channel-agnostic session key rules.

Canonical home for session key construction/parsing and outbound text
sanitization. Migrated out of ``g3ku/china_bridge`` during the channel
communication rebuild (Step 1): the ``china:*`` key format is byte-for-byte
identical to the legacy implementation because persisted transcripts
(``sessions/china_*.jsonl``), continuity sidecars, and paused snapshots are
all indexed by it. The China channel subsystem was removed in rebuild Steps
4/5; ``china:*`` keys remain canonical so pre-existing transcripts stay
readable archives.

The ``ext:*`` namespace serves external bridge applications that consume the
External Agent API: ``ext:{bridge_id}:{hash}`` where the hash is derived from
the bridge-supplied ``external_key``; the authoritative external_key ↔
session_key mapping lives in the external session registry, never encoded in
the key itself.
"""

CHINA_SESSION_KEY_PREFIX = "china:"
EXTERNAL_SESSION_KEY_PREFIX = "ext:"
CHANNEL_SESSION_KEY_PREFIXES = (CHINA_SESSION_KEY_PREFIX, EXTERNAL_SESSION_KEY_PREFIX)

_EXTERNAL_BRIDGE_ID_RE = re.compile(r"[^a-z0-9_-]+")


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


def is_channel_session_key(session_key: str | None) -> bool:
    """True for session keys owned by channel transports (``china:*`` legacy
    subsystem sessions and ``ext:*`` external bridge sessions). These keys get
    the same web-UI read-only and catalog grouping semantics."""
    raw = str(session_key or "").strip()
    return raw.startswith(CHANNEL_SESSION_KEY_PREFIXES)


def normalize_bridge_id(value: str | None) -> str:
    normalized = _EXTERNAL_BRIDGE_ID_RE.sub("-", str(value or "").strip().lower()).strip("-")
    return normalized or "bridge"


def build_external_session_key(*, bridge_id: str, external_key: str, digest_length: int = 16) -> str:
    """Build the stable runtime session key for an external bridge session.

    The key embeds only a hash of the bridge-supplied ``external_key`` so it
    stays filename-safe regardless of the platform's identifier shape; the
    authoritative mapping between ``external_key`` and this key is owned by
    the external session registry. ``digest_length`` exists purely as a
    collision-escape hatch for the registry.
    """
    bridge = normalize_bridge_id(bridge_id)
    bounded = max(8, min(40, int(digest_length or 16)))
    digest = hashlib.sha1(str(external_key or "").encode("utf-8")).hexdigest()[:bounded]
    return f"{EXTERNAL_SESSION_KEY_PREFIX}{bridge}:{digest}"


SESSION_EVENTS_MARKER = "[SESSION EVENTS]"


def sanitize_channel_outbound_text(text: str) -> str:
    """Remove internal-only artifacts from channel-bound reply text.

    Models occasionally echo internal context blocks verbatim. This truncates
    everything from a ``[SESSION EVENTS]`` marker onward and strips runtime
    tool-contract / stage-compaction block echoes. The result is stripped; an
    empty result means the whole message was internal-only and must not be
    delivered.
    """
    cleaned = strip_frontdoor_tool_contract_echo(text)
    cleaned = strip_stage_block_echo(cleaned)
    marker_index = cleaned.find(SESSION_EVENTS_MARKER)
    if marker_index >= 0:
        cleaned = cleaned[:marker_index]
    return cleaned.strip()
