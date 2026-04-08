"""Structured tool for precise long-term memory deletions."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool


class MemoryDeleteTool(Tool):
    """Delete structured memory facts by fact_id and/or canonical_key."""

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_delete"

    @property
    def description(self) -> str:
        return (
            "Delete structured long-term memory facts precisely by fact_id and/or canonical_key.\n"
            "MUST CALL: when the user explicitly asks to forget/remove a specific remembered fact and you have "
            "its fact_id or canonical_key from memory_search/memory_runtime output.\n"
            "DO NOT CALL: to bulk-delete or guess targets; this tool is precise deletion only."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fact_ids": {
                    "type": "array",
                    "description": "Structured fact ids to delete (exact match).",
                    "items": {"type": "string"},
                },
                "canonical_keys": {
                    "type": "array",
                    "description": "Structured canonical keys to delete (exact match).",
                    "items": {"type": "string"},
                },
            },
            "required": [],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        fact_ids = (params or {}).get("fact_ids")
        canonical_keys = (params or {}).get("canonical_keys")

        fact_ids_list = list(fact_ids or []) if isinstance(fact_ids, list) else []
        canonical_keys_list = list(canonical_keys or []) if isinstance(canonical_keys, list) else []

        fact_ids_list = [str(item).strip() for item in fact_ids_list if str(item).strip()]
        canonical_keys_list = [str(item).strip() for item in canonical_keys_list if str(item).strip()]

        if not fact_ids_list and not canonical_keys_list:
            errors.append("must provide at least one fact_id or canonical_key")
        return errors

    async def execute(
        self,
        fact_ids: list[str] | None = None,
        canonical_keys: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime_raw = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
        session_key = str(runtime.get("session_key") or "")
        channel = runtime.get("memory_channel", runtime.get("channel"))
        chat_id = runtime.get("memory_chat_id", runtime.get("chat_id"))

        if (not channel or not chat_id) and session_key and ":" in session_key:
            ch, cid = session_key.split(":", 1)
            channel = channel or ch
            chat_id = chat_id or cid

        result = await self._manager.delete_structured_memory_facts(
            session_key=session_key,
            channel=str(channel or "unknown"),
            chat_id=str(chat_id or "unknown"),
            fact_ids=list(fact_ids or []),
            canonical_keys=list(canonical_keys or []),
        )
        return json.dumps(result, ensure_ascii=False)

