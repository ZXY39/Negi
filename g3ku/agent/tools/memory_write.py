"""Structured tool for explicit long-term memory writes."""

from __future__ import annotations

import json
import re
from typing import Any

from g3ku.agent.tools.base import Tool

_ALLOWED_KINDS = {
    "profile",
    "preference",
    "constraint",
    "default",
    "avoidance",
    "workflow",
    "project_fact",
    "other",
}
_KEY_PATTERN = re.compile(r"^[a-z0-9_]+$")


class MemoryWriteTool(Tool):
    """Write explicit permanent memory items for the CEO agent."""

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Write explicit long-term memory immediately and make it searchable in future memory retrieval.\n"
            "MUST CALL: when the user explicitly asks the system to remember a stable identity, preference, "
            "constraint, default, avoidance rule, workflow rule, or durable project fact.\n"
            "DO NOT CALL: for temporary task status, speculative inferences, short-lived context, or unconfirmed facts.\n"
            "WRITE RULE: normalize the memory into durable reusable statements and stable keys before saving."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "One to five normalized permanent memory items to save immediately.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": sorted(_ALLOWED_KINDS),
                                "description": "Memory item category.",
                            },
                            "key": {
                                "type": "string",
                                "description": "Stable snake_case key used for deterministic replacement.",
                            },
                            "value": {
                                "type": "string",
                                "description": "Canonical value for the memory item.",
                            },
                            "statement": {
                                "type": "string",
                                "description": "Human-readable normalized statement that will be indexed for retrieval.",
                            },
                            "source_excerpt": {
                                "type": "string",
                                "description": "Short excerpt from the current user turn supporting this memory.",
                            },
                        },
                        "required": ["kind", "key", "value", "statement", "source_excerpt"],
                    },
                }
            },
            "required": ["items"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        items = (params or {}).get("items")
        if not isinstance(items, list):
            return errors
        if not 1 <= len(items) <= 5:
            errors.append("items must contain between 1 and 5 entries")
            return errors
        seen_keys: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            key = str(item.get("key") or "").strip()
            value = str(item.get("value") or "").strip()
            statement = str(item.get("statement") or "").strip()
            source_excerpt = str(item.get("source_excerpt") or "").strip()
            if kind not in _ALLOWED_KINDS:
                errors.append(f"items[{index}].kind must be one of {sorted(_ALLOWED_KINDS)}")
            if not key:
                errors.append(f"items[{index}].key must not be empty")
            elif not _KEY_PATTERN.fullmatch(key):
                errors.append(f"items[{index}].key must use lowercase snake_case")
            elif key in seen_keys:
                errors.append(f"items[{index}].key must be unique within one call")
            else:
                seen_keys.add(key)
            if not value:
                errors.append(f"items[{index}].value must not be empty")
            if not statement:
                errors.append(f"items[{index}].statement must not be empty")
            if not source_excerpt:
                errors.append(f"items[{index}].source_excerpt must not be empty")
        return errors

    async def execute(
        self,
        items: list[dict[str, Any]],
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

        result = await self._manager.write_explicit_memory_items(
            session_key=session_key,
            channel=str(channel or "unknown"),
            chat_id=str(chat_id or "unknown"),
            items=list(items or []),
        )
        return json.dumps(result, ensure_ascii=False, default=str)
