"""Queued tool for explicit long-term memory writes."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool


class MemoryWriteTool(Tool):
    """Queue explicit durable memory write requests for the CEO agent."""

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Queue a durable long-term memory write request.\n"
            "Use this when the user explicitly asks the system to remember a stable rule, preference, identity detail, "
            "default, constraint, or project fact.\n"
            "Do not use this for temporary execution state or speculative guesses."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Raw memory candidate text to queue for the memory agent.",
                }
            },
            "required": ["content"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get("content") or "").strip():
            errors.append("content must not be empty")
        return errors

    async def execute(
        self,
        content: str,
        **kwargs: Any,
    ) -> str:
        runtime_raw = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
        session_key = str(runtime.get("session_key") or "")
        result = await self._manager.enqueue_write_request(
            session_key=session_key,
            decision_source="user",
            payload_text=str(content or "").strip(),
            trigger_source="memory_write_tool",
        )
        return json.dumps(result, ensure_ascii=False)
