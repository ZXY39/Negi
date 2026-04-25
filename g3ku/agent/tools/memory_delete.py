"""Queued tool for natural-language long-term memory deletions."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool


class MemoryDeleteTool(Tool):
    """Queue durable memory deletion requests using natural-language descriptions."""

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_delete"

    @property
    def description(self) -> str:
        return (
            "Queue a durable long-term memory delete request using a natural-language memory description.\n"
            "Use this when the user explicitly asks the system to forget a remembered rule, preference, or fact."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Natural-language description of the remembered content that should be forgotten.",
                },
            },
            "required": ["content"],
        }

    @staticmethod
    def _normalize_content(params: dict[str, Any] | None) -> str:
        payload = dict(params or {})
        return str(payload.get("content") or "").strip()

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not self._normalize_content(params):
            errors.append("content must not be empty")
        return errors

    async def execute(
        self,
        content: str = "",
        **kwargs: Any,
    ) -> str:
        runtime_raw = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
        session_key = str(runtime.get("session_key") or "")
        normalized_content = self._normalize_content({"content": content})
        result = await self._manager.enqueue_delete_request(
            session_key=session_key,
            decision_source="user",
            payload_text=normalized_content,
            trigger_source="memory_delete_tool",
        )
        return json.dumps(result, ensure_ascii=False)
