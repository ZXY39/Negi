"""Queued tool for id-based long-term memory deletions."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool


class MemoryDeleteTool(Tool):
    """Queue durable memory deletion requests using visible memory ids."""

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_delete"

    @property
    def description(self) -> str:
        return (
            "Queue a durable long-term memory delete request using ids visible in the current memory snapshot.\n"
            "Use this when the user explicitly asks the system to forget a remembered rule, preference, or fact."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "A single memory id to remove from the current MEMORY snapshot.",
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "A list of memory ids to remove from the current MEMORY snapshot.",
                },
            },
            "anyOf": [
                {"required": ["id"]},
                {"required": ["ids"]},
            ],
        }

    @staticmethod
    def _normalize_ids(params: dict[str, Any] | None) -> list[str]:
        payload = dict(params or {})
        direct_id = str(payload.get("id") or "").strip()
        if direct_id:
            return [direct_id]
        normalized_ids: list[str] = []
        for raw in list(payload.get("ids") or []):
            item = str(raw or "").strip()
            if item:
                normalized_ids.append(item)
        return normalized_ids

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not self._normalize_ids(params):
            errors.append("id or ids must not be empty")
        return errors

    async def execute(
        self,
        id: str = "",
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime_raw = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
        session_key = str(runtime.get("session_key") or "")
        normalized_ids = self._normalize_ids({"id": id, "ids": ids})
        result = await self._manager.enqueue_delete_request(
            session_key=session_key,
            decision_source="user",
            payload_text=json.dumps(normalized_ids, ensure_ascii=False),
            trigger_source="memory_delete_tool",
        )
        return json.dumps(result, ensure_ascii=False)
