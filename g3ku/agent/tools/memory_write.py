"""Structured tool for explicit long-term memory writes."""

from __future__ import annotations

import json
from typing import Any, get_args

from g3ku.agent.structured_memory import StructuredCategory, TimeSemantics
from g3ku.agent.tools.base import Tool

_ALLOWED_CATEGORIES = set(get_args(StructuredCategory))
_ALLOWED_TIME_SEMANTICS = set(get_args(TimeSemantics))


def _normalize_memory_fact_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    trimmed = value.strip()
    if not trimmed or trimmed[0] not in {"{", "["}:
        return value

    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        return value

    if isinstance(parsed, (dict, list)):
        return parsed
    return value


class MemoryWriteTool(Tool):
    """Upsert explicit structured memory facts for the CEO agent."""

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Upsert structured long-term memory facts for future retrieval.\n"
            "MUST CALL: when the user explicitly asks the system to remember a stable identity, preference, "
            "constraint, workflow rule, default setting, relationship, current state, or historical fact.\n"
            "DO NOT CALL: for temporary task status, speculative inferences, short-lived context, or unconfirmed facts.\n"
            "WRITE RULE: send normalized structured facts with category, scope, entity, attribute, value, and time semantics."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "description": "One to five structured memory facts to upsert immediately.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "enum": sorted(_ALLOWED_CATEGORIES),
                                "description": "Structured fact category.",
                            },
                            "scope": {
                                "type": "string",
                                "description": "Namespace scope for the fact (e.g. global, session, project).",
                            },
                            "entity": {
                                "type": "string",
                                "description": "Entity the fact is about (e.g. user, project:g3ku).",
                            },
                            "attribute": {
                                "type": "string",
                                "description": "Attribute name for the entity (stable key within the entity).",
                            },
                            "value": {
                                "type": ["string", "number", "boolean", "object", "array", "null"],
                                "description": "Canonical value for the fact (string/number/bool/object/array).",
                            },
                            "observed_at": {
                                "type": "string",
                                "description": "ISO8601 timestamp when the fact was observed (or recorded).",
                            },
                            "time_semantics": {
                                "type": "string",
                                "enum": sorted(_ALLOWED_TIME_SEMANTICS),
                                "description": "How the fact should be interpreted over time.",
                            },
                            "source_excerpt": {
                                "type": "string",
                                "description": "Short excerpt from the current user turn supporting this fact.",
                            },
                            "qualifier": {
                                "type": "object",
                                "description": "Optional qualifier metadata (e.g. project context, constraints).",
                            },
                            "expires_at": {
                                "type": ["string", "null"],
                                "description": "Optional ISO8601 expiry timestamp (null for no expiry).",
                            },
                        },
                        "required": [
                            "category",
                            "scope",
                            "entity",
                            "attribute",
                            "value",
                            "observed_at",
                            "time_semantics",
                            "source_excerpt",
                        ],
                    },
                }
            },
            "required": ["facts"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        facts = (params or {}).get("facts")
        if not isinstance(facts, list):
            return errors
        if not 1 <= len(facts) <= 5:
            errors.append("facts must contain between 1 and 5 entries")
            return errors
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                continue
            category = str(fact.get("category") or "").strip()
            scope = str(fact.get("scope") or "").strip()
            entity = str(fact.get("entity") or "").strip()
            attribute = str(fact.get("attribute") or "").strip()
            observed_at = str(fact.get("observed_at") or "").strip()
            time_semantics = str(fact.get("time_semantics") or "").strip()
            source_excerpt = str(fact.get("source_excerpt") or "").strip()

            if category not in _ALLOWED_CATEGORIES:
                errors.append(f"facts[{index}].category must be one of {sorted(_ALLOWED_CATEGORIES)}")
            if not scope:
                errors.append(f"facts[{index}].scope must not be empty")
            if not entity:
                errors.append(f"facts[{index}].entity must not be empty")
            if not attribute:
                errors.append(f"facts[{index}].attribute must not be empty")
            if "value" not in fact:
                errors.append(f"facts[{index}].value is required")
            else:
                value = fact.get("value")
                if isinstance(value, str) and not value.strip():
                    errors.append(f"facts[{index}].value must not be empty")
            if not observed_at:
                errors.append(f"facts[{index}].observed_at must not be empty")
            if time_semantics not in _ALLOWED_TIME_SEMANTICS:
                errors.append(f"facts[{index}].time_semantics must be one of {sorted(_ALLOWED_TIME_SEMANTICS)}")
            if not source_excerpt:
                errors.append(f"facts[{index}].source_excerpt must not be empty")

            qualifier = fact.get("qualifier")
            if qualifier is not None and not isinstance(qualifier, dict):
                errors.append(f"facts[{index}].qualifier must be an object when provided")
            expires_at = fact.get("expires_at")
            if expires_at is not None and not isinstance(expires_at, str):
                errors.append(f"facts[{index}].expires_at must be a string or null when provided")
        return errors

    async def execute(
        self,
        facts: list[dict[str, Any]],
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

        normalized_facts: list[dict[str, Any]] = []
        for fact in list(facts or []):
            if not isinstance(fact, dict):
                normalized_facts.append(fact)
                continue
            normalized_fact = dict(fact)
            if "value" in normalized_fact:
                normalized_fact["value"] = _normalize_memory_fact_value(normalized_fact.get("value"))
            normalized_facts.append(normalized_fact)

        result = await self._manager.upsert_structured_memory_facts(
            session_key=session_key,
            channel=str(channel or "unknown"),
            chat_id=str(chat_id or "unknown"),
            facts=normalized_facts,
        )
        return json.dumps(result, ensure_ascii=False)
