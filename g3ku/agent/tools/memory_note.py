"""Tool for loading detailed memory notes by short ref."""

from __future__ import annotations

from typing import Any

from g3ku.agent.tools.base import Tool


class MemoryNoteTool(Tool):
    # 内部入队工具、瞬时完成：不向模型暴露统一 timeout_seconds 参数。
    hide_universal_timeout_parameter = True

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_note"

    @property
    def description(self) -> str:
        return (
            "Load the full body of one memory note. `ref` is the handle token alone - the block prints it after a 'note:' label, and you pass what follows the label (e.g. nb16a5), not the label. A real note name also resolves, without the '.md' suffix."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The handle token alone (what follows the block's 'note:' label), or a note name without the .md suffix.",
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
