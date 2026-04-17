"""Queued tool for visible-text long-term memory deletions."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool


class MemoryDeleteTool(Tool):
    """Queue durable memory deletion requests using visible memory text."""

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_delete"

    @property
    def description(self) -> str:
        return (
            "Queue a durable long-term memory delete request using text visible in the current memory snapshot.\n"
            "Use this when the user explicitly asks the system to forget a remembered rule, preference, or fact."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_text": {
                    "type": "string",
                    "description": "The memory text block or summary line to remove from the current MEMORY snapshot.",
                }
            },
            "required": ["target_text"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get("target_text") or "").strip():
            errors.append("target_text must not be empty")
        return errors

    async def execute(
        self,
        target_text: str,
        **kwargs: Any,
    ) -> str:
        runtime_raw = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
        session_key = str(runtime.get("session_key") or "")
        result = await self._manager.enqueue_delete_request(
            session_key=session_key,
            decision_source="user",
            payload_text=str(target_text or "").strip(),
            trigger_source="memory_delete_tool",
        )
        return json.dumps(result, ensure_ascii=False)
