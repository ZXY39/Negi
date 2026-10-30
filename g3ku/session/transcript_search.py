"""Message-level search over persisted session transcripts.

The search reads the same rows ``Session.messages`` holds after the JSONL load:
one row per user/assistant turn, with internal prompt and UI-only marker rows
excluded by default.  Snippets are centered on the matched text instead of
being a head-truncated line preview, so long single-line messages remain
findable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

DEFAULT_LIMIT = 8
MAX_LIMIT = 20
MIN_SNIPPET_CHARS = 80
DEFAULT_SNIPPET_CHARS = 240
MAX_SNIPPET_CHARS = 800
MAX_KEYWORDS = 8
DEFAULT_OPEN_MESSAGE_CHARS = 4000
MAX_OPEN_MESSAGE_CHARS = 8000
CONVERSATION_ROLES = ("user", "assistant")


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _role(row: dict[str, Any]) -> str:
    return str(row.get("role") or "").strip().lower()


def _turn_id(row: dict[str, Any]) -> str:
    metadata = _metadata(row)
    return str(
        row.get("turn_id")
        or metadata.get("_transcript_turn_id")
        or metadata.get("turn_id")
        or ""
    ).strip()


def _attachment_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for raw in list(row.get("attachments") or []):
        if isinstance(raw, str):
            name = raw.strip()
        elif isinstance(raw, dict):
            name = ""
            for key in ("name", "filename", "display_name", "title"):
                candidate = str(raw.get(key) or "").strip()
                if candidate:
                    name = candidate
                    break
            if not name:
                path = str(raw.get("path") or "").strip()
                name = Path(path).name if path else ""
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return names


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                if block is not None:
                    parts.append(str(block))
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
                continue
            inner = block.get("content")
            if isinstance(inner, str) and inner.strip():
                parts.append(inner)
                continue
            if str(block.get("type") or "").strip().lower() in {"image", "image_url", "input_image"}:
                parts.append("[image]")
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content or "")


def _search_text(row: dict[str, Any]) -> str:
    text = _content_text(row.get("content"))
    attachments = _attachment_names(row)
    if not attachments:
        return text
    attachment_text = "[attachments: " + ", ".join(attachments) + "]"
    return f"{text}\n{attachment_text}".strip() if text.strip() else attachment_text


def _is_selected(
    row: Any,
    *,
    roles: Sequence[str],
    include_internal: bool,
    include_current_turn: bool,
    current_turn_id: str,
) -> bool:
    if not isinstance(row, dict):
        return False
    if str(row.get("_type") or "").strip().lower() == "metadata":
        return False
    if _role(row) not in roles:
        return False
    metadata = _metadata(row)
    if not include_internal:
        if str(metadata.get("internal_prompt_kind") or "").strip():
            return False
        if metadata.get("ui_visible") is False:
            return False
        if str(metadata.get("source") or "").strip().lower() == "follow_up_archive":
            return False
    if not include_current_turn and current_turn_id and _turn_id(row) == current_turn_id:
        return False
    return True


def _normalize_keywords(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        raise ValueError("keywords must be a non-empty array")
    terms: list[str] = []
    for raw in candidates:
        if not isinstance(raw, str):
            raise ValueError("keywords entries must be strings")
        term = raw.strip()
        if term and term not in terms:
            terms.append(term)
    if not terms:
        raise ValueError("keywords must contain at least one non-empty term")
    if len(terms) > MAX_KEYWORDS:
        raise ValueError(f"at most {MAX_KEYWORDS} keywords are allowed")
    return terms


def _normalize_roles(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return CONVERSATION_ROLES
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        raise ValueError("roles must be an array")
    roles: list[str] = []
    for raw in candidates:
        role = str(raw or "").strip().lower()
        if not role:
            continue
        if role not in CONVERSATION_ROLES:
            raise ValueError(f"roles must be one of {list(CONVERSATION_ROLES)}")
        if role not in roles:
            roles.append(role)
    if not roles:
        raise ValueError("roles must contain at least one role")
    return tuple(roles)


def _normalize_choice(value: Any, *, name: str, allowed: Sequence[str], default: str) -> str:
    normalized = str(value or "").strip().lower() or default
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of {list(allowed)}")
    return normalized


def _bounded_int(value: Any, *, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return max(minimum, min(number, maximum))


def _snippet(
    text: str,
    matched_terms: Sequence[str],
    patterns: dict[str, re.Pattern[str]],
    max_chars: int,
) -> str:
    positions: list[tuple[int, int]] = []
    for term in matched_terms:
        match = patterns[term].search(text)
        if match is not None:
            positions.append((match.start(), match.end()))
    if not positions:
        return re.sub(r"\s+", " ", text[:max_chars]).strip()
    span_start = min(start for start, _ in positions)
    span_end = max(end for _, end in positions)
    if span_end - span_start <= max_chars:
        padding = max_chars - (span_end - span_start)
        start = max(0, span_start - padding // 2)
        end = min(len(text), start + max_chars)
        if end - start < max_chars:
            start = max(0, end - max_chars)
    else:
        start = max(0, span_start - max_chars // 3)
        end = min(len(text), start + max_chars)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _build_hit(
    row: dict[str, Any],
    *,
    message_index: int,
    matched_terms: Sequence[str],
    patterns: dict[str, re.Pattern[str]],
    snippet_chars: int,
) -> dict[str, Any]:
    metadata = _metadata(row)
    hit: dict[str, Any] = {
        "message_index": message_index,
        "turn_id": _turn_id(row),
        "role": _role(row),
        "timestamp": str(row.get("timestamp") or ""),
        "snippet": _snippet(_search_text(row), matched_terms, patterns, snippet_chars),
        "matched_keywords": list(matched_terms),
    }
    state = str(metadata.get("_transcript_state") or "").strip().lower()
    if state and state != "completed":
        hit["transcript_state"] = state
    attachments = _attachment_names(row)
    if attachments:
        hit["attachment_names"] = attachments
    return hit


def search_messages(
    messages: Sequence[Any] | None,
    *,
    keywords: Any,
    match: str = "all",
    roles: Any = None,
    include_internal: bool = False,
    include_current_turn: bool = False,
    current_turn_id: str = "",
    limit: Any = DEFAULT_LIMIT,
    offset: Any = 0,
    order: str = "recent",
    snippet_chars: Any = DEFAULT_SNIPPET_CHARS,
) -> dict[str, Any]:
    """Search one session's messages and return bounded message-level hits."""

    terms = _normalize_keywords(keywords)
    normalized_match = _normalize_choice(
        match, name="match", allowed=("all", "any"), default="all"
    )
    normalized_order = _normalize_choice(
        order, name="order", allowed=("recent", "oldest"), default="recent"
    )
    normalized_roles = _normalize_roles(roles)
    normalized_limit = _bounded_int(
        limit, name="limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT
    )
    normalized_offset = _bounded_int(
        offset, name="offset", default=0, minimum=0, maximum=10**9
    )
    normalized_snippet = _bounded_int(
        snippet_chars,
        name="snippet_chars",
        default=DEFAULT_SNIPPET_CHARS,
        minimum=MIN_SNIPPET_CHARS,
        maximum=MAX_SNIPPET_CHARS,
    )
    patterns = {term: re.compile(re.escape(term), re.IGNORECASE) for term in terms}
    rows = list(messages or [])
    current_turn = str(current_turn_id or "").strip()
    matched: list[tuple[int, list[str]]] = []
    searched = 0
    for index, row in enumerate(rows):
        if not _is_selected(
            row,
            roles=normalized_roles,
            include_internal=bool(include_internal),
            include_current_turn=bool(include_current_turn),
            current_turn_id=current_turn,
        ):
            continue
        searched += 1
        text = _search_text(row)
        hit_terms = [term for term in terms if patterns[term].search(text)]
        if not hit_terms:
            continue
        if normalized_match == "all" and len(hit_terms) != len(terms):
            continue
        matched.append((index, hit_terms))
    if normalized_order == "recent":
        matched.reverse()
    total = len(matched)
    page = matched[normalized_offset : normalized_offset + normalized_limit]
    hits = [
        _build_hit(
            rows[index],
            message_index=index,
            matched_terms=hit_terms,
            patterns=patterns,
            snippet_chars=normalized_snippet,
        )
        for index, hit_terms in page
    ]
    has_more = normalized_offset + len(hits) < total
    payload: dict[str, Any] = {
        "ok": True,
        "action": "search",
        "keywords": terms,
        "match": normalized_match,
        "roles": list(normalized_roles),
        "order": normalized_order,
        "limit": normalized_limit,
        "offset": normalized_offset,
        "scanned_messages": len(rows),
        "searched_messages": searched,
        "total_matches": total,
        "hits": hits,
        "has_more": has_more,
        "next_offset": normalized_offset + len(hits) if has_more else None,
    }
    if not hits:
        payload["message"] = (
            "No matching messages. Matching is literal and case-insensitive; "
            "try fewer keywords or match='any'."
        )
    return payload


def open_message(
    messages: Sequence[Any] | None,
    *,
    message_index: Any,
    expected_turn_id: str = "",
    include_internal: bool = False,
    include_current_turn: bool = False,
    current_turn_id: str = "",
    max_chars: Any = DEFAULT_OPEN_MESSAGE_CHARS,
) -> dict[str, Any]:
    """Return one full transcript message addressed by a search hit index."""

    try:
        index = int(message_index)
    except (TypeError, ValueError) as exc:
        raise ValueError("message_index must be an integer") from exc
    if index < 0:
        raise ValueError("message_index must be >= 0")
    rows = list(messages or [])
    if index >= len(rows):
        raise ValueError("message_index_out_of_range")
    row = rows[index]
    current_turn = str(current_turn_id or "").strip()
    if not _is_selected(
        row,
        roles=CONVERSATION_ROLES,
        include_internal=bool(include_internal),
        include_current_turn=bool(include_current_turn),
        current_turn_id=current_turn,
    ):
        if not include_current_turn and current_turn and isinstance(row, dict) and _turn_id(row) == current_turn:
            raise ValueError("current_turn_excluded")
        raise ValueError("message_not_searchable")
    assert isinstance(row, dict)
    turn_id = _turn_id(row)
    expected = str(expected_turn_id or "").strip()
    if expected and expected != turn_id:
        raise ValueError("stale_message_index")
    text = _content_text(row.get("content"))
    cap = _bounded_int(
        max_chars,
        name="max_chars",
        default=DEFAULT_OPEN_MESSAGE_CHARS,
        minimum=200,
        maximum=MAX_OPEN_MESSAGE_CHARS,
    )
    truncated = len(text) > cap
    payload: dict[str, Any] = {
        "ok": True,
        "action": "open",
        "message_index": index,
        "turn_id": turn_id,
        "role": _role(row),
        "timestamp": str(row.get("timestamp") or ""),
        "content": text[:cap],
        "truncated": truncated,
        "remaining_chars": max(len(text) - cap, 0),
    }
    metadata = _metadata(row)
    state = str(metadata.get("_transcript_state") or "").strip().lower()
    if state and state != "completed":
        payload["transcript_state"] = state
    attachments = list(row.get("attachments") or [])
    if attachments:
        payload["attachments"] = attachments
    return payload


__all__ = [
    "CONVERSATION_ROLES",
    "DEFAULT_LIMIT",
    "DEFAULT_OPEN_MESSAGE_CHARS",
    "MAX_KEYWORDS",
    "MAX_LIMIT",
    "MAX_OPEN_MESSAGE_CHARS",
    "MAX_SNIPPET_CHARS",
    "MIN_SNIPPET_CHARS",
    "open_message",
    "search_messages",
]
