"""Tool for loading detailed memory notes by short ref."""

from __future__ import annotations

from typing import Any

from g3ku.agent.tools.base import Tool


class MemoryNoteTool(Tool):
    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_note"

    @property
    def description(self) -> str:
        return "Load a detailed memory note from memory/notes by short ref."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Short note ref such as note_a1b2.",
                }
            },
            "required": ["ref"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get("ref") or "").strip():
            errors.append("ref must not be empty")
        return errors

    async def execute(self, ref: str, **kwargs: Any) -> str:
        _ = kwargs
        return self._manager.load_note(str(ref or "").strip())
