from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool
from g3ku.session.transcript_search import open_message, search_messages

_MANIFEST = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "resource.yaml").read_text(encoding="utf-8")
)
_OWNER_SESSION_PREFIXES = ("web:", "internal:", "cli:")


def _runtime_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    value = kwargs.get("__g3ku_runtime")
    return value if isinstance(value, dict) else {}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(code: str, message: str = "") -> str:
    payload: dict[str, Any] = {"ok": False, "error": code}
    if message:
        payload["message"] = message
    return _json(payload)


def _bool_arg(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _resolve_trust(runtime: dict[str, Any], session_key: str) -> str:
    trust = str(runtime.get("trust_level") or "").strip().lower()
    if trust:
        return trust
    if session_key.startswith(_OWNER_SESSION_PREFIXES):
        return "owner"
    return "third_party"


class _SearchSessionHistoryTool(Tool):
    rerun_safe = True

    def __init__(self, loop: Any = None) -> None:
        self._loop = loop

    @property
    def name(self) -> str:
        return str(_MANIFEST.get("name") or "search_session_history")

    @property
    def description(self) -> str:
        return str(_MANIFEST.get("description") or "")

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(
            _MANIFEST.get("parameters")
            or {"type": "object", "properties": {}, "required": []}
        )

    async def execute(self, **kwargs: Any) -> str:
        runtime = _runtime_payload(kwargs)
        session_key = str(runtime.get("session_key") or "").strip()
        if not session_key:
            return _error("session_unavailable", "No active session is bound to this call.")
        if _resolve_trust(runtime, session_key) != "owner":
            return _error(
                "owner_only",
                "search_session_history is available only to owner sessions; "
                "third-party transcripts may contain owner history from before a trust downgrade.",
            )
        loop = runtime.get("loop") or self._loop
        sessions = getattr(loop, "sessions", None)
        if sessions is None:
            return _error(
                "session_manager_unavailable",
                "The session transcript store is not available in this runtime.",
            )
        try:
            session = sessions.get_or_create(session_key)
        except Exception as exc:
            return _error(
                "session_load_failed",
                f"Could not load the current session transcript: {type(exc).__name__}.",
            )
        messages = list(getattr(session, "messages", []) or [])
        current_turn_id = str(runtime.get("turn_id") or "").strip()
        include_internal = _bool_arg(kwargs.get("include_internal"))
        include_current_turn = _bool_arg(kwargs.get("include_current_turn"))
        action = str(kwargs.get("action") or "search").strip().lower()
        if action not in {"search", "open"}:
            return _error("invalid_arguments", "action must be search or open")
        try:
            if action == "open":
                message_index = kwargs.get("message_index")
                if message_index is None or message_index == "":
                    raise ValueError("message_index is required for action=open")
                payload = open_message(
                    messages,
                    message_index=message_index,
                    expected_turn_id=str(kwargs.get("expected_turn_id") or ""),
                    include_internal=include_internal,
                    include_current_turn=include_current_turn,
                    current_turn_id=current_turn_id,
                )
            else:
                keywords = kwargs.get("keywords")
                if keywords is None or keywords == "" or keywords == []:
                    raise ValueError("keywords is required for action=search")
                payload = search_messages(
                    messages,
                    keywords=keywords,
                    match=str(kwargs.get("match") or "all"),
                    roles=kwargs.get("roles"),
                    include_internal=include_internal,
                    include_current_turn=include_current_turn,
                    current_turn_id=current_turn_id,
                    limit=kwargs.get("limit"),
                    offset=kwargs.get("offset"),
                    order=str(kwargs.get("order") or "recent"),
                    snippet_chars=kwargs.get("snippet_chars"),
                )
        except ValueError as exc:
            text = str(exc).strip()
            code = text if re.fullmatch(r"[a-z0-9_]+", text) else "invalid_arguments"
            return _error(code, "" if code == text else text)
        payload["session_key"] = session_key
        return _json(payload)


def build(runtime: Any) -> _SearchSessionHistoryTool:
    loop = getattr(runtime, "loop", None)
    if loop is None:
        services = getattr(runtime, "services", None)
        loop = getattr(services, "loop", None)
    return _SearchSessionHistoryTool(loop=loop)
