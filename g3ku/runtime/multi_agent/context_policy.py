from __future__ import annotations

from typing import Any

from g3ku.core.messages import UserInputMessage
from g3ku.core.state import AgentState
from g3ku.runtime.multi_agent.state import CompiledAgentRole


def build_role_context(
    *,
    session_state: AgentState,
    user_input: UserInputMessage,
    role: CompiledAgentRole,
) -> list[dict[str, Any]]:
    policy = dict(role.context_policy or {})
    include = set(policy.get("include") or ["user_request"])
    max_messages = int(policy.get("max_messages", 4) or 4)
    allow_parent_history = bool(policy.get("allow_parent_history", False))
    blocks: list[dict[str, Any]] = []

    if "user_request" in include:
        blocks.append({"kind": "user_request", "role": "user", "content": _content_text(user_input.content)})

    if "referenced_files" in include:
        refs = _collect_referenced_files(user_input.metadata)
        if refs:
            blocks.append({"kind": "referenced_files", "content": "\n".join(refs), "items": refs})

    if "selected_memory" in include:
        memory_items = _collect_selected_memory(user_input.metadata)
        if memory_items:
            blocks.append({"kind": "selected_memory", "content": "\n".join(memory_items), "items": memory_items})

    if allow_parent_history:
        recent_messages = session_state.messages[-max_messages:] if max_messages > 0 else []
        for message in recent_messages:
            role_name = str(getattr(message, "role", "note") or "note")
            content = _content_text(getattr(message, "content", ""))
            if not content:
                continue
            blocks.append({"kind": "parent_history", "role": role_name, "content": content})

    return blocks


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                for key in ("content", "text", "placeholder", "name"):
                    raw = item.get(key)
                    if isinstance(raw, str) and raw.strip():
                        parts.append(raw.strip())
                        break
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "\n".join(parts)
    return str(value or "").strip()


def _collect_referenced_files(metadata: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("referenced_files", "uploaded_placeholders"):
        value = metadata.get(key)
        if isinstance(value, list):
            refs.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, str) and value.strip():
            refs.append(value.strip())
    return refs


def _collect_selected_memory(metadata: dict[str, Any]) -> list[str]:
    value = metadata.get("selected_memory")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []

