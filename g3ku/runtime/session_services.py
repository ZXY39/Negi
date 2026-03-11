from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.agent.context import ContextBuilder
from g3ku.agent.memory import MemoryStore
from g3ku.core.messages import AssistantMessage, ToolResultMessage, UserInputMessage
from g3ku.runtime.message_adapter import agent_message_to_dict, dicts_to_agent_messages


class SessionTranscriptService:
    """Session transcript persistence and history sanitization."""

    def __init__(self, *, tool_result_max_chars: int = 500):
        self.tool_result_max_chars = int(tool_result_max_chars)

    def save_turn(
        self,
        session,
        messages: list[dict[str, Any]],
        skip: int,
        *,
        user_content_override: str | None = None,
    ) -> None:
        user_override_consumed = False
        for message in dicts_to_agent_messages(list(messages[skip:])):
            if isinstance(message, AssistantMessage) and not message.content and not message.tool_calls:
                continue

            if isinstance(message, ToolResultMessage):
                content = message.content
                if isinstance(content, str) and len(content) > self.tool_result_max_chars:
                    message = ToolResultMessage(
                        tool_name=message.tool_name,
                        tool_call_id=message.tool_call_id,
                        content=content[: self.tool_result_max_chars] + "\n... (truncated)",
                        is_error=message.is_error,
                        timestamp=message.timestamp,
                    )
                elif isinstance(content, list):
                    message = ToolResultMessage(
                        tool_name=message.tool_name,
                        tool_call_id=message.tool_call_id,
                        content=self.sanitize_tool_content_for_history(content),
                        is_error=message.is_error,
                        timestamp=message.timestamp,
                    )

            if isinstance(message, UserInputMessage):
                content = message.content
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    continue
                if not user_override_consumed and isinstance(user_content_override, str):
                    content = user_content_override
                    user_override_consumed = True
                elif isinstance(content, list):
                    content = [
                        {"type": "text", "text": "[image]"}
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "image_url"
                            and isinstance(block.get("image_url"), dict)
                            and str(block.get("image_url", {}).get("url", "")).startswith("data:image/")
                        )
                        else (dict(block) if isinstance(block, dict) else block)
                        for block in content
                    ]
                message = UserInputMessage(
                    content=content,
                    attachments=list(message.attachments),
                    metadata=dict(message.metadata),
                    timestamp=message.timestamp,
                )

            entry = agent_message_to_dict(message)
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if entry.get("role") == "user":
                session.last_user_turn_at = entry["timestamp"]
                session.commit_turn_counter = int(getattr(session, "commit_turn_counter", 0) or 0) + 1
        session.updated_at = datetime.now()

    def sanitize_tool_content_for_history(self, content: list[Any]) -> list[dict[str, str]] | str:
        sanitized: list[dict[str, str]] = []
        for item in content:
            if isinstance(item, str):
                text = item[: self.tool_result_max_chars]
                if text:
                    sanitized.append({"type": "text", "text": text})
                continue
            if not isinstance(item, dict):
                text = str(item)[: self.tool_result_max_chars]
                if text:
                    sanitized.append({"type": "text", "text": text})
                continue

            item_type = str(item.get("type") or "").strip().lower()
            if item_type in {"text", "input_text", "output_text"}:
                text = item.get("text", item.get("content", ""))
                if isinstance(text, str) and text:
                    sanitized.append({"type": "text", "text": text[: self.tool_result_max_chars]})
                continue
            if item_type in {"image_url", "input_image"}:
                sanitized.append({"type": "text", "text": "[image omitted from history]"})
                continue
            if item_type in {"file", "input_file"}:
                file_obj = item.get("file") if isinstance(item.get("file"), dict) else item
                filename = ""
                if isinstance(file_obj, dict):
                    filename = str(file_obj.get("filename") or item.get("filename") or "").strip()
                label = f"[file omitted from history: {filename}]" if filename else "[file omitted from history]"
                sanitized.append({"type": "text", "text": label})
                continue

            text = item.get("text", item.get("content", ""))
            if isinstance(text, str) and text:
                sanitized.append({"type": "text", "text": text[: self.tool_result_max_chars]})

        return sanitized or "[multimodal tool output omitted]"

    @staticmethod
    def save_checkpoint_turn_snapshot(
        session,
        *,
        user_content: str,
        assistant_content: str,
    ) -> None:
        session.add_message("user", str(user_content or ""))
        session.add_message("assistant", str(assistant_content or ""))


class SessionMemoryConsolidationService:
    """Memory consolidation service for session transcripts."""

    def __init__(self, *, workspace: Path, model_client: Any, model: str, memory_window: int):
        self.workspace = Path(workspace)
        self.model_client = model_client
        self.model = model
        self.memory_window = int(memory_window)

    async def consolidate(self, session, *, archive_all: bool = False) -> bool:
        return await MemoryStore(self.workspace).consolidate(
            session,
            self.model_client,
            self.model,
            archive_all=archive_all,
            memory_window=self.memory_window,
        )

