"""Structured memory search tool with grouped and unified views."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool


class MemorySearchTool(Tool):
    """Search long-term memory and return grouped + unified ranked view."""

    def __init__(self, *, manager: Any, default_limit: int = 8):
        self._manager = manager
        self._default_limit = max(1, int(default_limit or 8))

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search long-term memory and return structured results grouped by context type "
            "(memory/resource/skill) plus a unified ranked view.\n"
            "STRUCTURED MEMORY: memory hits for structured facts include fact_id/canonical_key/category/"
            "observed_at/expires_at fields when available; use these identifiers before memory_delete.\n"
            "MUST CALL: when the answer depends on prior user/project facts not fully present in current turn, "
            "including remembered preferences, previous decisions, constraints, unresolved action items, "
            "or references like 'as discussed before'/'remember'.\n"
            "AVOID CALL: for pure single-turn transformations (rewrite/translate/format), general world knowledge, "
            "simple greetings, or when current-turn content already contains all required facts.\n"
            "CALL LIMIT: normally call at most once per user turn; call again only if tool outputs introduce "
            "new entities that require a second targeted lookup.\n"
            "QUERY WRITING: include concrete entities and intent (person/project/date/decision), "
            "avoid vague queries like 'help me' or 'that thing'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Concrete lookup query with entities/intent. Avoid vague text.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of unified results.",
                    "minimum": 1,
                    "maximum": 20,
                },
                "context_type": {
                    "type": "string",
                    "enum": ["memory", "resource", "skill"],
                    "description": "Optional: restrict search to one context type.",
                },
                "include_l2": {
                    "type": "boolean",
                    "description": "Include L2 preview snippets when available.",
                },
                "session": {
                    "type": "string",
                    "description": "Optional session key override, e.g. cli:direct.",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        limit: int | None = None,
        context_type: str | None = None,
        include_l2: bool = False,
        session: str | None = None,
        **kwargs: Any,
    ) -> str:
        runtime_raw = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
        session_key = str(session or runtime.get("session_key") or "")
        channel = runtime.get("memory_channel", runtime.get("channel"))
        chat_id = runtime.get("memory_chat_id", runtime.get("chat_id"))

        if (not channel or not chat_id) and session_key and ":" in session_key:
            ch, cid = session_key.split(":", 1)
            channel = channel or ch
            chat_id = chat_id or cid

        result = await self._manager.search_tool_view(
            query=query,
            channel=str(channel or "unknown"),
            chat_id=str(chat_id or "unknown"),
            session_key=session_key or None,
            limit=max(1, min(int(limit or self._default_limit), 20)),
            context_type=context_type if context_type in {"memory", "resource", "skill"} else None,
            include_l2=bool(include_l2),
        )
        return json.dumps(result, ensure_ascii=False)

