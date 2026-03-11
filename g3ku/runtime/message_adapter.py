from __future__ import annotations

from typing import Any

from g3ku.core.messages import (
    AgentMessage,
    AssistantMessage,
    AttachmentReferenceMessage,
    ControlMessage,
    SystemNoteMessage,
    ToolResultMessage,
    UserInputMessage,
)


def _copy_content(value: Any) -> Any:
    if isinstance(value, list):
        return [dict(item) if isinstance(item, dict) else item for item in value]
    return value


def dict_to_agent_message(message: dict[str, Any]) -> AgentMessage:
    role = str(message.get("role") or "").strip().lower()
    timestamp = message.get("timestamp") if isinstance(message.get("timestamp"), str) else None
    content = _copy_content(message.get("content", ""))

    if role == "user":
        return UserInputMessage(content=content, timestamp=timestamp)
    if role == "assistant":
        return AssistantMessage(
            content=content,
            thinking=message.get("reasoning_content") if isinstance(message.get("reasoning_content"), str) else None,
            usage=dict(message.get("usage") or {}) if isinstance(message.get("usage"), dict) else {},
            tool_calls=[dict(item) for item in list(message.get("tool_calls") or []) if isinstance(item, dict)],
            timestamp=timestamp,
        )
    if role == "tool":
        text_content = content
        return ToolResultMessage(
            tool_name=str(message.get("name") or ""),
            tool_call_id=str(message.get("tool_call_id") or ""),
            content=text_content,
            is_error=isinstance(text_content, str) and text_content.startswith("Error"),
            timestamp=timestamp,
        )
    if role == "attachment":
        return AttachmentReferenceMessage(
            placeholder=str(message.get("placeholder") or ""),
            name=str(message.get("name") or ""),
            mime=str(message.get("mime") or ""),
            timestamp=timestamp,
        )
    if role == "control":
        return ControlMessage(
            action=str(message.get("action") or ""),
            content=str(content or ""),
            timestamp=timestamp,
        )
    return SystemNoteMessage(content=str(content or ""), timestamp=timestamp)


def agent_message_to_dict(message: AgentMessage) -> dict[str, Any]:
    if isinstance(message, UserInputMessage):
        payload = {"role": "user", "content": _copy_content(message.content)}
        if message.metadata:
            payload["metadata"] = dict(message.metadata)
        if message.attachments:
            payload["attachments"] = list(message.attachments)
        if message.timestamp:
            payload["timestamp"] = message.timestamp
        return payload
    if isinstance(message, AssistantMessage):
        payload = {"role": "assistant", "content": _copy_content(message.content)}
        if message.thinking:
            payload["reasoning_content"] = message.thinking
        if message.usage:
            payload["usage"] = dict(message.usage)
        if message.tool_calls:
            payload["tool_calls"] = [dict(item) for item in message.tool_calls]
        if message.timestamp:
            payload["timestamp"] = message.timestamp
        return payload
    if isinstance(message, ToolResultMessage):
        payload = {
            "role": "tool",
            "name": message.tool_name,
            "tool_call_id": message.tool_call_id,
            "content": _copy_content(message.content),
        }
        if message.timestamp:
            payload["timestamp"] = message.timestamp
        return payload
    if isinstance(message, AttachmentReferenceMessage):
        payload = {
            "role": "attachment",
            "placeholder": message.placeholder,
            "name": message.name,
            "mime": message.mime,
        }
        if message.timestamp:
            payload["timestamp"] = message.timestamp
        return payload
    if isinstance(message, ControlMessage):
        payload = {"role": "control", "action": message.action, "content": message.content}
        if message.timestamp:
            payload["timestamp"] = message.timestamp
        return payload
    payload = {"role": "system", "content": message.content}
    if message.timestamp:
        payload["timestamp"] = message.timestamp
    return payload


def dicts_to_agent_messages(messages: list[dict[str, Any]]) -> list[AgentMessage]:
    return [dict_to_agent_message(message) for message in messages]


def agent_messages_to_dicts(messages: list[AgentMessage]) -> list[dict[str, Any]]:
    return [agent_message_to_dict(message) for message in messages]

