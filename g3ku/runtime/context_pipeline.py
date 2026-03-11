from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from g3ku.agent.context import ContextBuilder
from g3ku.core.messages import AgentMessage
from g3ku.runtime.message_adapter import agent_messages_to_dicts


@dataclass(slots=True)
class ContextTransformRequest:
    current_message: str = ""
    history: list[dict[str, Any]] | None = None
    history_messages: list[AgentMessage] | None = None
    skill_names: list[str] | None = None
    media: list[str] | None = None
    channel: str | None = None
    chat_id: str | None = None
    interleaved_content: list[dict[str, Any]] | None = None
    include_legacy_memory: bool = True
    retrieved_memory: str | None = None
    temp_dir: str | None = None


class SessionContextPipeline:
    """Explicit two-stage context pipeline for runtime message construction."""

    def __init__(self, builder: ContextBuilder):
        self._builder = builder

    def transform_context(self, request: ContextTransformRequest) -> list[dict[str, Any]]:
        history = list(request.history or [])
        if request.history_messages is not None:
            history = agent_messages_to_dicts(list(request.history_messages))
        return self._builder.build_messages(
            history=history,
            current_message=request.current_message,
            skill_names=request.skill_names,
            media=request.media,
            channel=request.channel,
            chat_id=request.chat_id,
            interleaved_content=request.interleaved_content,
            include_legacy_memory=request.include_legacy_memory,
            retrieved_memory=request.retrieved_memory,
            temp_dir=request.temp_dir,
        )

    @staticmethod
    def convert_to_llm_messages(messages: list[dict[str, Any]]) -> list[Any]:
        from langchain_core.messages import convert_to_messages

        return list(convert_to_messages(messages))

