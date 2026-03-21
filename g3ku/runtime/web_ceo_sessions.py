from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from g3ku.china_bridge.session_keys import build_session_key, parse_china_session_key
from g3ku.config.loader import load_config
from g3ku.runtime.memory_scope import DEFAULT_WEB_MEMORY_SCOPE, normalize_memory_scope
from g3ku.utils.helpers import ensure_dir, safe_filename

DEFAULT_CEO_SESSION_TITLE = "新会话"
WEB_CEO_STATE_FILE = Path(".g3ku") / "web-ceo-state.json"
WEB_CEO_UPLOAD_ROOT = Path(".g3ku") / "web-ceo-uploads"
WEB_CEO_INFLIGHT_ROOT = Path(".g3ku") / "web-ceo-inflight"
DEFAULT_TASK_MAX_DEPTH = 1
DEFAULT_TASK_HARD_MAX_DEPTH = 4
DEFAULT_FRONTDOOR_RAW_TAIL_TURNS = 4
FRONTDOOR_CONTEXT_VERSION = 1
FRONTDOOR_COMPACT_HISTORY_PREFIX = '[[G3KU_COMPACT_HISTORY_V1]]'
_FRONTDOOR_ROUTE_KINDS = {"direct_reply", "self_execute", "task_dispatch"}
_FRONTDOOR_SUMMARY_MAX_CHARS = 2_400
_FRONTDOOR_TURN_SUMMARY_MAX_CHARS = 240


def _normalize_frontdoor_route_kind(value: Any) -> str:
    route_kind = str(value or "").strip().lower()
    return route_kind if route_kind in _FRONTDOOR_ROUTE_KINDS else ""


def normalize_frontdoor_context(payload: Any, *, raw_tail_turns: int = DEFAULT_FRONTDOOR_RAW_TAIL_TURNS) -> dict[str, Any]:
    source = dict(payload or {}) if isinstance(payload, dict) else {}
    try:
        summary_turn_count = int(source.get("summary_turn_count", 0) or 0)
    except (TypeError, ValueError):
        summary_turn_count = 0
    try:
        normalized_tail_turns = int(source.get("raw_tail_turns", raw_tail_turns) or raw_tail_turns)
    except (TypeError, ValueError):
        normalized_tail_turns = int(raw_tail_turns)
    summary_text = str(source.get("summary_text") or "").strip()
    return {
        "version": FRONTDOOR_CONTEXT_VERSION,
        "summary_text": summary_text,
        "summary_turn_count": max(0, summary_turn_count),
        "last_route_kind": _normalize_frontdoor_route_kind(source.get("last_route_kind")),
        "last_updated_at": str(source.get("last_updated_at") or "").strip(),
        "raw_tail_turns": max(1, normalized_tail_turns),
    }


def _history_entry_from_message(message: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "role": str(message.get("role") or ""),
        "content": message.get("content", ""),
    }
    for key in ("tool_calls", "tool_call_id", "name"):
        if key in message:
            entry[key] = message[key]
    return entry


def _complete_transcript_turns(session: Any) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    current_user: dict[str, Any] | None = None
    for raw in list(getattr(session, "messages", []) or []):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role == "user":
            current_user = raw
            continue
        if role == "assistant" and current_user is not None:
            turns.append([current_user, raw])
            current_user = None
    return turns


def count_frontdoor_turns(session: Any) -> int:
    return len(_complete_transcript_turns(session))


def _render_frontdoor_summary(turns: list[list[dict[str, Any]]]) -> str:
    if not turns:
        return ""
    lines: list[str] = []
    used_chars = 0
    for turn_index, turn in enumerate(turns, start=1):
        user_text = summarize_preview_text(turn[0].get("content") or "", max_chars=_FRONTDOOR_TURN_SUMMARY_MAX_CHARS)
        assistant_text = summarize_preview_text(turn[1].get("content") or "", max_chars=_FRONTDOOR_TURN_SUMMARY_MAX_CHARS)
        if user_text and assistant_text:
            line = f"Earlier turn {turn_index}: user={user_text}; assistant={assistant_text}"
        elif user_text:
            line = f"Earlier turn {turn_index}: user={user_text}"
        elif assistant_text:
            line = f"Earlier turn {turn_index}: assistant={assistant_text}"
        else:
            continue
        next_chars = used_chars + len(line) + (1 if lines else 0)
        if next_chars > _FRONTDOOR_SUMMARY_MAX_CHARS:
            remaining = max(0, len(turns) - turn_index + 1)
            if remaining > 0:
                lines.append(f"... plus {remaining} earlier summarized turns.")
            break
        lines.append(line)
        used_chars = next_chars
    return "\n".join(lines).strip()


def build_frontdoor_context(
    session: Any,
    *,
    raw_tail_turns: int = DEFAULT_FRONTDOOR_RAW_TAIL_TURNS,
    route_kind: str | None = None,
) -> dict[str, Any]:
    turns = _complete_transcript_turns(session)
    normalized_tail_turns = max(1, int(raw_tail_turns or DEFAULT_FRONTDOOR_RAW_TAIL_TURNS))
    summarized_turns = turns[:-normalized_tail_turns] if len(turns) > normalized_tail_turns else []
    metadata = dict(getattr(session, "metadata", {}) or {})
    existing = normalize_frontdoor_context(metadata.get("frontdoor_context"), raw_tail_turns=normalized_tail_turns)
    updated_at = getattr(session, "updated_at", None)
    last_updated_at = updated_at.isoformat() if isinstance(updated_at, datetime) else datetime.now().isoformat()
    return normalize_frontdoor_context(
        {
            "summary_text": _render_frontdoor_summary(summarized_turns),
            "summary_turn_count": len(summarized_turns),
            "last_route_kind": _normalize_frontdoor_route_kind(route_kind) or existing["last_route_kind"],
            "last_updated_at": last_updated_at,
            "raw_tail_turns": normalized_tail_turns,
        },
        raw_tail_turns=normalized_tail_turns,
    )


def resolve_frontdoor_context(
    session: Any,
    *,
    raw_tail_turns: int = DEFAULT_FRONTDOOR_RAW_TAIL_TURNS,
) -> tuple[dict[str, Any], str]:
    metadata = dict(getattr(session, "metadata", {}) or {})
    normalized_tail_turns = max(1, int(raw_tail_turns or DEFAULT_FRONTDOOR_RAW_TAIL_TURNS))
    stored = normalize_frontdoor_context(metadata.get("frontdoor_context"), raw_tail_turns=normalized_tail_turns)
    total_turns = count_frontdoor_turns(session)
    covered_turns = int(stored["summary_turn_count"]) + min(int(stored["raw_tail_turns"]), total_turns)
    if total_turns <= covered_turns:
        return stored, "metadata"
    return build_frontdoor_context(
        session,
        raw_tail_turns=normalized_tail_turns,
        route_kind=stored["last_route_kind"],
    ), "fallback"


def extract_frontdoor_recent_history(session: Any, *, raw_tail_turns: int = DEFAULT_FRONTDOOR_RAW_TAIL_TURNS) -> list[dict[str, Any]]:
    turns = _complete_transcript_turns(session)
    normalized_tail_turns = max(1, int(raw_tail_turns or DEFAULT_FRONTDOOR_RAW_TAIL_TURNS))
    tail_turns = turns[-normalized_tail_turns:]
    return [_history_entry_from_message(message) for turn in tail_turns for message in turn]


def build_frontdoor_compact_history_message(frontdoor_context: Any) -> dict[str, Any] | None:
    normalized = normalize_frontdoor_context(frontdoor_context)
    if not normalized["summary_text"] or int(normalized["summary_turn_count"]) <= 0:
        return None
    payload = {
        "kind": "frontdoor_context",
        "summary": normalized["summary_text"],
        "summary_turn_count": int(normalized["summary_turn_count"]),
        "last_route_kind": normalized["last_route_kind"],
        "raw_tail_turns": int(normalized["raw_tail_turns"]),
    }
    return {
        "role": "assistant",
        "content": f"{FRONTDOOR_COMPACT_HISTORY_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
    }


def workspace_path() -> Path:
    try:
        return Path(load_config().workspace_path).resolve()
    except Exception:
        return Path.cwd().resolve()


def new_web_ceo_session_id() -> str:
    return f"web:ceo-{uuid.uuid4().hex[:12]}"


def summarize_session_title(text: str, *, max_chars: int = 24) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return DEFAULT_CEO_SESSION_TITLE
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}…"


def summarize_preview_text(text: str, *, max_chars: int = 96) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}…"


def _has_visible_message_content(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(_has_visible_message_content(item) for item in content)
    if isinstance(content, dict):
        return any(_has_visible_message_content(value) for value in content.values())
    return content is not None


def latest_llm_output_at(session: Any) -> str:
    for item in reversed(list(getattr(session, "messages", []) or [])):
        if str(item.get("role") or "").strip().lower() != "assistant":
            continue
        if not _has_visible_message_content(item.get("content")):
            continue
        timestamp = str(item.get("timestamp") or "").strip()
        if timestamp:
            return timestamp
    return ""


def main_runtime_depth_limits() -> dict[str, int]:
    try:
        cfg = load_config()
        default_max_depth = int(getattr(getattr(cfg, "main_runtime", None), "default_max_depth", DEFAULT_TASK_MAX_DEPTH) or DEFAULT_TASK_MAX_DEPTH)
        hard_max_depth = int(getattr(getattr(cfg, "main_runtime", None), "hard_max_depth", DEFAULT_TASK_HARD_MAX_DEPTH) or DEFAULT_TASK_HARD_MAX_DEPTH)
    except Exception:
        default_max_depth = DEFAULT_TASK_MAX_DEPTH
        hard_max_depth = DEFAULT_TASK_HARD_MAX_DEPTH
    default_max_depth = max(0, default_max_depth)
    hard_max_depth = max(default_max_depth, hard_max_depth)
    return {
        "default_max_depth": default_max_depth,
        "hard_max_depth": hard_max_depth,
    }


def normalize_task_defaults(
    payload: Any,
    *,
    default_max_depth: int,
    hard_max_depth: int,
) -> dict[str, int]:
    source = payload if isinstance(payload, dict) else {}
    raw_depth = source.get("max_depth", source.get("maxDepth", default_max_depth))
    try:
        max_depth = int(raw_depth)
    except (TypeError, ValueError):
        max_depth = int(default_max_depth)
    max_depth = max(0, min(max_depth, int(hard_max_depth)))
    return {"max_depth": max_depth}


def normalize_ceo_metadata(metadata: Any, *, session_key: str) -> dict[str, Any]:
    payload = dict(metadata or {}) if isinstance(metadata, dict) else {}
    title = str(payload.get("title") or "").strip() or DEFAULT_CEO_SESSION_TITLE
    preview_text = summarize_preview_text(payload.get("last_preview_text") or payload.get("preview_text") or "")
    frontdoor_context = normalize_frontdoor_context(payload.get("frontdoor_context"))
    depth_limits = main_runtime_depth_limits()
    if str(session_key or "").startswith("web:"):
        memory_scope = normalize_memory_scope(
            payload.get("memory_scope"),
            fallback_channel=DEFAULT_WEB_MEMORY_SCOPE["channel"],
            fallback_chat_id=DEFAULT_WEB_MEMORY_SCOPE["chat_id"],
        )
    else:
        memory_scope = normalize_memory_scope(payload.get("memory_scope"), fallback_session_key=session_key)
    task_defaults = normalize_task_defaults(
        payload.get("task_defaults", payload.get("taskDefaults")),
        default_max_depth=depth_limits["default_max_depth"],
        hard_max_depth=depth_limits["hard_max_depth"],
    )
    return {
        **payload,
        "title": title,
        "last_preview_text": preview_text,
        "frontdoor_context": frontdoor_context,
        "memory_scope": memory_scope,
        "task_defaults": task_defaults,
    }


def ensure_ceo_session_metadata(session: Any) -> bool:
    normalized = normalize_ceo_metadata(getattr(session, "metadata", None), session_key=str(getattr(session, "key", "") or ""))
    current = getattr(session, "metadata", None)
    if current == normalized:
        return False
    session.metadata = normalized
    return True


def update_ceo_session_after_turn(
    session: Any,
    *,
    user_text: str,
    assistant_text: str,
    route_kind: str | None = None,
) -> bool:
    changed = ensure_ceo_session_metadata(session)
    metadata = dict(getattr(session, "metadata", {}) or {})
    if metadata.get("title") == DEFAULT_CEO_SESSION_TITLE and str(user_text or "").strip():
        next_title = summarize_session_title(user_text)
        if metadata.get("title") != next_title:
            metadata["title"] = next_title
            changed = True
    preview_source = str(assistant_text or "").strip() or str(user_text or "").strip()
    next_preview = summarize_preview_text(preview_source)
    if metadata.get("last_preview_text") != next_preview:
        metadata["last_preview_text"] = next_preview
        changed = True
    next_frontdoor_context = build_frontdoor_context(
        session,
        raw_tail_turns=DEFAULT_FRONTDOOR_RAW_TAIL_TURNS,
        route_kind=route_kind,
    )
    if metadata.get("frontdoor_context") != next_frontdoor_context:
        metadata["frontdoor_context"] = next_frontdoor_context
        changed = True
    if changed:
        session.metadata = metadata
    return changed


def upload_dir_for_session(session_id: str, *, create: bool = True) -> Path:
    safe_session = safe_filename(str(session_id or "web_shared").replace(":", "_")) or "web_shared"
    path = workspace_path() / WEB_CEO_UPLOAD_ROOT / safe_session
    return ensure_dir(path) if create else path


def inflight_snapshot_path_for_session(session_id: str, *, create: bool = True) -> Path:
    safe_session = safe_filename(str(session_id or "web_shared").replace(":", "_")) or "web_shared"
    root = workspace_path() / WEB_CEO_INFLIGHT_ROOT
    directory = ensure_dir(root) if create else root
    return directory / f"{safe_session}.json"


def read_inflight_turn_snapshot(session_id: str) -> dict[str, Any] | None:
    path = inflight_snapshot_path_for_session(session_id, create=False)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_inflight_turn_snapshot(session_id: str, snapshot: dict[str, Any] | None) -> None:
    key = str(session_id or "").strip()
    if not key:
        return
    path = inflight_snapshot_path_for_session(key)
    if not isinstance(snapshot, dict) or not snapshot:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_inflight_turn_snapshot(session_id: str) -> None:
    path = inflight_snapshot_path_for_session(session_id, create=False)
    path.unlink(missing_ok=True)


def build_session_summary(session: Any, *, is_active: bool, is_running: bool = False) -> dict[str, Any]:
    ensure_ceo_session_metadata(session)
    messages = list(getattr(session, "messages", []) or [])
    preview_text = str(session.metadata.get("last_preview_text") or "").strip()
    if not preview_text:
        for item in reversed(messages):
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                preview_text = summarize_preview_text(content)
                break
    created_at = getattr(session, "created_at", None)
    updated_at = getattr(session, "updated_at", None)
    last_llm_output = latest_llm_output_at(session)
    return {
        "session_id": str(getattr(session, "key", "") or ""),
        "title": str(session.metadata.get("title") or DEFAULT_CEO_SESSION_TITLE),
        "preview_text": preview_text,
        "message_count": len(messages),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at or ""),
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else str(updated_at or ""),
        "last_llm_output_at": last_llm_output,
        "is_active": bool(is_active),
        "is_running": bool(is_running),
        "task_defaults": dict(session.metadata.get("task_defaults") or {}),
        "session_family": "local",
        "session_origin": "web",
        "is_readonly": False,
        "can_rename": True,
        "can_delete": True,
    }


CHINA_SESSION_CHANNEL_SPECS = (
    {"attr": "qqbot", "channel_id": "qqbot", "label": "QQ Bot"},
    {"attr": "dingtalk", "channel_id": "dingtalk", "label": "DingTalk"},
    {"attr": "wecom", "channel_id": "wecom", "label": "企业微信"},
    {"attr": "wecom_app", "channel_id": "wecom-app", "label": "企业微信应用"},
    {"attr": "feishu_china", "channel_id": "feishu-china", "label": "飞书"},
)

CHINA_CHANNEL_LABELS = {
    spec["channel_id"]: spec["label"]
    for spec in CHINA_SESSION_CHANNEL_SPECS
}


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], {}, False)


def _channel_session_kind(parsed) -> str:
    if parsed is None:
        return "dm"
    return "thread" if parsed.thread_id else parsed.chat_type


def _channel_label(channel_id: str) -> str:
    return CHINA_CHANNEL_LABELS.get(str(channel_id or "").strip(), str(channel_id or "渠道").strip() or "渠道")


def _channel_title(parsed) -> str:
    label = _channel_label(parsed.channel)
    if parsed.thread_id:
        return f"{label} · {parsed.account_id} · Thread · {str(parsed.thread_id).strip()[:24]}"
    if parsed.chat_type == "group":
        peer = str(parsed.peer_id or "group").strip()[:24]
        return f"{label} · {parsed.account_id} · Group · {peer}"
    return f"{label} · {parsed.account_id} · DM"


def _channel_preview_text(session: Any, *, fallback_text: str = "") -> str:
    messages = list(getattr(session, "messages", []) or [])
    for item in reversed(messages):
        content = item.get("content") if isinstance(item, dict) else ""
        preview = summarize_preview_text(content or "")
        if preview:
            return preview
    return str(fallback_text or "").strip()


def _session_time_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "").strip()


def _session_created_at(session: Any) -> str:
    return _session_time_value(getattr(session, "created_at", None))


def _session_updated_at(session: Any) -> str:
    updated_at = _session_time_value(getattr(session, "updated_at", None))
    if updated_at:
        return updated_at
    messages = list(getattr(session, "messages", []) or [])
    for item in reversed(messages):
        if isinstance(item, dict) and str(item.get("timestamp") or "").strip():
            return str(item.get("timestamp") or "").strip()
    return ""


def _session_last_assistant_at(session: Any) -> str:
    return latest_llm_output_at(session)


def _canonical_china_session_id(parsed) -> str:
    if parsed.chat_type == "group":
        return build_session_key(
            channel=parsed.channel,
            account_id=parsed.account_id,
            peer_kind="group",
            peer_id=str(parsed.peer_id or ""),
            thread_id=parsed.thread_id,
        )
    return build_session_key(
        channel=parsed.channel,
        account_id=parsed.account_id,
        peer_kind="user",
        peer_id=str(parsed.peer_id or ""),
        thread_id=parsed.thread_id,
    )


def _top_level_channel_payload(channel_cfg: Any) -> dict[str, Any]:
    if channel_cfg is None:
        return {}
    if hasattr(channel_cfg, "model_dump"):
        data = channel_cfg.model_dump(by_alias=True, exclude_none=True)
        return data if isinstance(data, dict) else {}
    if isinstance(channel_cfg, dict):
        return dict(channel_cfg)
    return {}


def _has_base_channel_account(channel_cfg: Any) -> bool:
    payload = _top_level_channel_payload(channel_cfg)
    ignore = {"enabled", "name", "defaultAccount", "default_account", "accounts"}
    return any(key not in ignore and _non_empty(value) for key, value in payload.items())


def _iter_enabled_channel_accounts() -> list[dict[str, str]]:
    cfg = load_config()
    rows: list[dict[str, str]] = []
    channels_cfg = getattr(getattr(cfg, "china_bridge", None), "channels", None)
    if channels_cfg is None:
        return rows
    for spec in CHINA_SESSION_CHANNEL_SPECS:
        channel_cfg = getattr(channels_cfg, spec["attr"], None)
        payload = _top_level_channel_payload(channel_cfg)
        if not bool(payload.get("enabled")):
            continue
        accounts = payload.get("accounts") if isinstance(payload.get("accounts"), dict) else {}
        seen_accounts: set[str] = set()
        if _has_base_channel_account(channel_cfg):
            rows.append(
                {
                    "channel_id": spec["channel_id"],
                    "label": spec["label"],
                    "account_id": "default",
                }
            )
            seen_accounts.add("default")
        for account_id, account_payload in sorted(accounts.items()):
            if not isinstance(account_payload, dict):
                continue
            normalized_account_id = str(account_id or "").strip() or "default"
            if normalized_account_id in seen_accounts:
                continue
            if account_payload.get("enabled") is False:
                continue
            rows.append(
                {
                    "channel_id": spec["channel_id"],
                    "label": spec["label"],
                    "account_id": normalized_account_id,
                }
            )
            seen_accounts.add(normalized_account_id)
        if not rows or rows[-1]["channel_id"] != spec["channel_id"]:
            if not accounts:
                rows.append(
                    {
                        "channel_id": spec["channel_id"],
                        "label": spec["label"],
                        "account_id": "default",
                    }
                )
    return rows


def _channel_session_summary_from_entry(
    *,
    session_id: str,
    parsed,
    is_active: bool,
    is_running: bool,
    preview_text: str,
    message_count: int,
    created_at: str,
    updated_at: str,
    last_llm_output_at: str,
    is_virtual: bool,
) -> dict[str, Any]:
    kind = _channel_session_kind(parsed)
    return {
        "session_id": session_id,
        "title": _channel_title(parsed),
        "preview_text": preview_text,
        "message_count": message_count,
        "created_at": created_at,
        "updated_at": updated_at,
        "last_llm_output_at": last_llm_output_at,
        "is_active": bool(is_active),
        "is_running": bool(is_running),
        "session_family": "channel",
        "session_origin": "china",
        "is_readonly": True,
        "can_rename": False,
        "can_delete": False,
        "channel_id": parsed.channel,
        "account_id": parsed.account_id,
        "chat_type": kind,
        "peer_id": parsed.peer_id,
        "thread_id": parsed.thread_id,
        "is_virtual": bool(is_virtual),
    }


def list_local_ceo_sessions(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    changed_keys: list[str] = []
    for item in session_manager.list_sessions():
        key = str(item.get("key") or "").strip()
        if not key.startswith("web:"):
            continue
        session = session_manager.get_or_create(key)
        if ensure_ceo_session_metadata(session):
            changed_keys.append(key)
        is_running = False
        if callable(is_running_resolver):
            try:
                is_running = bool(is_running_resolver(key))
            except Exception:
                is_running = False
        rows.append(build_session_summary(session, is_active=key == active_session_id, is_running=is_running))
    for key in changed_keys:
        session_manager.save(session_manager.get_or_create(key))
    rows.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("session_id") or "")), reverse=True)
    return rows


def list_channel_ceo_sessions(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}

    for account in _iter_enabled_channel_accounts():
        session_id = f"china:{account['channel_id']}:{account['account_id']}:dm"
        parsed = parse_china_session_key(session_id)
        if parsed is None:
            continue
        summaries[session_id] = _channel_session_summary_from_entry(
            session_id=session_id,
            parsed=parsed,
            is_active=session_id == active_session_id,
            is_running=bool(callable(is_running_resolver) and is_running_resolver(session_id)),
            preview_text="等待该渠道的私聊消息",
            message_count=0,
            created_at="",
            updated_at="",
            last_llm_output_at="",
            is_virtual=True,
        )

    for item in session_manager.list_sessions():
        key = str(item.get("key") or "").strip()
        if not key.startswith("china:"):
            continue
        parsed = parse_china_session_key(key)
        if parsed is None:
            continue
        session = session_manager.get_or_create(key)
        canonical_id = _canonical_china_session_id(parsed)
        canonical_parsed = parse_china_session_key(canonical_id) or parsed
        summary = _channel_session_summary_from_entry(
            session_id=canonical_id,
            parsed=canonical_parsed,
            is_active=canonical_id == active_session_id,
            is_running=bool(callable(is_running_resolver) and is_running_resolver(canonical_id)),
            preview_text=_channel_preview_text(session),
            message_count=len(list(getattr(session, "messages", []) or [])),
            created_at=_session_created_at(session),
            updated_at=_session_updated_at(session),
            last_llm_output_at=_session_last_assistant_at(session),
            is_virtual=False,
        )
        existing = summaries.get(canonical_id)
        if existing is None:
            summaries[canonical_id] = summary
            continue
        existing_updated_at = str(existing.get("updated_at") or existing.get("last_llm_output_at") or "")
        summary_updated_at = str(summary.get("updated_at") or summary.get("last_llm_output_at") or "")
        merged = dict(existing)
        merged["message_count"] = int(existing.get("message_count") or 0) + int(summary.get("message_count") or 0)
        merged["is_virtual"] = bool(existing.get("is_virtual")) and bool(summary.get("is_virtual"))
        merged["is_running"] = bool(existing.get("is_running")) or bool(summary.get("is_running"))
        if summary_updated_at >= existing_updated_at:
            for key_name in ("preview_text", "created_at", "updated_at", "last_llm_output_at"):
                merged[key_name] = summary.get(key_name) or merged.get(key_name)
        summaries[canonical_id] = merged

    rows = list(summaries.values())
    rows = [
        item
        for item in rows
        if item.get("chat_type") == "dm"
        or int(item.get("message_count") or 0) > 0
        or not bool(item.get("is_virtual"))
    ]
    rows.sort(
        key=lambda item: (
            0 if str(item.get("chat_type") or "") == "dm" else 1,
            str(item.get("last_llm_output_at") or item.get("updated_at") or item.get("created_at") or ""),
            str(item.get("session_id") or ""),
        ),
        reverse=True,
    )
    return rows


def build_ceo_session_catalog(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    local_items = list_local_ceo_sessions(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=is_running_resolver,
    )
    channel_items = list_channel_ceo_sessions(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=is_running_resolver,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for item in channel_items:
        channel_id = str(item.get("channel_id") or "").strip()
        if not channel_id:
            continue
        bucket = grouped.get(channel_id)
        if bucket is None:
            bucket = {
                "channel_id": channel_id,
                "label": _channel_label(channel_id),
                "items": [],
            }
            grouped[channel_id] = bucket
        bucket["items"].append(item)
    channel_groups = [grouped[key] for key in [spec["channel_id"] for spec in CHINA_SESSION_CHANNEL_SPECS] if key in grouped]
    channel_ids = {str(item.get("session_id") or "") for item in channel_items}
    active_family = "channel" if str(active_session_id or "") in channel_ids else "local"
    return {
        "items": local_items,
        "channel_groups": channel_groups,
        "active_session_id": str(active_session_id or "").strip(),
        "active_session_family": active_family,
        "_channel_items": channel_items,
    }


def find_ceo_session_catalog_item(catalog: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    target = str(session_id or "").strip()
    if not target:
        return None
    for item in list(catalog.get("items") or []):
        if str(item.get("session_id") or "").strip() == target:
            return item
    for item in list(catalog.get("_channel_items") or []):
        if str(item.get("session_id") or "").strip() == target:
            return item
    return None


class WebCeoStateStore:
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace or workspace_path()).resolve()
        self.path = ensure_dir(self.workspace / ".g3ku") / "web-ceo-state.json"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def write(self, *, active_session_id: str | None) -> dict[str, Any]:
        payload = {
            "active_session_id": str(active_session_id or "").strip(),
            "updated_at": datetime.now().isoformat(),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def get_active_session_id(self) -> str:
        return str(self.read().get("active_session_id") or "").strip()

    def set_active_session_id(self, session_id: str | None) -> dict[str, Any]:
        return self.write(active_session_id=session_id)


def create_web_ceo_session(session_manager: Any, *, session_id: str | None = None, title: str | None = None) -> Any:
    key = str(session_id or "").strip() or new_web_ceo_session_id()
    session = session_manager.get_or_create(key)
    ensure_ceo_session_metadata(session)
    next_title = str(title or "").strip()
    if next_title:
        session.metadata["title"] = next_title
    session.updated_at = datetime.now()
    session_manager.save(session)
    return session


def delete_web_ceo_session_artifacts(*, session_manager: Any, session_id: str) -> None:
    path = session_manager.get_path(session_id)
    if path.exists():
        path.unlink()
    session_manager.invalidate(session_id)
    clear_inflight_turn_snapshot(session_id)
    upload_dir = upload_dir_for_session(session_id, create=False)
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)


def list_web_ceo_sessions(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    return list_local_ceo_sessions(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=is_running_resolver,
    )


def resolve_active_ceo_session_id(session_manager: Any, state_store: WebCeoStateStore) -> str:
    requested = state_store.get_active_session_id()
    catalog = build_ceo_session_catalog(session_manager, active_session_id=requested)
    if find_ceo_session_catalog_item(catalog, requested) is not None:
        return requested
    local_items = list(catalog.get("items") or [])
    if local_items:
        fallback = str(local_items[0].get("session_id") or "").strip()
        state_store.set_active_session_id(fallback)
        return fallback
    created = create_web_ceo_session(session_manager)
    state_store.set_active_session_id(created.key)
    return str(created.key)


def ensure_active_web_ceo_session(session_manager: Any, state_store: WebCeoStateStore) -> str:
    active_session_id = state_store.get_active_session_id()
    available_ids = [
        str(item.get("key") or "").strip()
        for item in session_manager.list_sessions()
        if str(item.get("key") or "").strip().startswith("web:")
    ]
    if active_session_id and active_session_id in available_ids:
        return active_session_id
    if available_ids:
        fallback = available_ids[0]
        state_store.set_active_session_id(fallback)
        return fallback
    created = create_web_ceo_session(session_manager)
    state_store.set_active_session_id(created.key)
    return str(created.key)
