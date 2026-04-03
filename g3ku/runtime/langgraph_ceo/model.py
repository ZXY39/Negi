"""Narrow LangChain BaseChatModel adapter for the CEO LangGraph frontdoor."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict


def _as_message_dicts(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    converted = convert_to_openai_messages(messages, text_format="string", include_id=False)
    return [converted] if isinstance(converted, dict) else list(converted)


def _normalize_tool_choice(value: Any) -> str | dict[str, Any] | None:
    if value == "any":
        return "required"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value
    return None


class CeoChatModelAdapter(BaseChatModel):
    """Adapter from ConfigChatBackend-compatible interface to BaseChatModel."""

    chat_backend: Any
    model_refs: list[str]
    default_temperature: float | None = None
    default_max_tokens: int | None = None
    default_reasoning_effort: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "g3ku-ceo-frontdoor-model"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool | callable],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ):
        return self.bind(tools=list(tools), tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._agenerate(messages, **kwargs))
        raise RuntimeError(
            "CeoChatModelAdapter._generate cannot run inside an active event loop; use async APIs."
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager

        openai_messages = _as_message_dicts(messages)
        tools_arg = kwargs.get("tools") or []
        openai_tools = [convert_to_openai_tool(tool) for tool in tools_arg] or None
        requested_tool_choice = kwargs.get("tool_choice")
        normalized_tool_choice = _normalize_tool_choice(requested_tool_choice)
        parallel_tool_calls = kwargs.get("parallel_tool_calls")
        prompt_cache_key = kwargs.get("prompt_cache_key")
        raw_temperature = kwargs.get("temperature", self.default_temperature)
        raw_max_tokens = kwargs.get("max_tokens", self.default_max_tokens)
        reasoning_effort = kwargs.get("reasoning_effort", self.default_reasoning_effort)
        response = await self.chat_backend.chat(
            messages=openai_messages,
            tools=openai_tools,
            model_refs=list(self.model_refs or []),
            tool_choice=normalized_tool_choice,
            max_tokens=(int(raw_max_tokens) if raw_max_tokens is not None else None),
            temperature=(float(raw_temperature) if raw_temperature is not None else None),
            reasoning_effort=(str(reasoning_effort) if reasoning_effort is not None else None),
            parallel_tool_calls=(
                bool(parallel_tool_calls) if isinstance(parallel_tool_calls, bool) else None
            ),
            prompt_cache_key=(str(prompt_cache_key).strip() or None) if prompt_cache_key is not None else None,
        )

        additional_kwargs: dict[str, Any] = {}
        if response.reasoning_content:
            additional_kwargs["reasoning_content"] = response.reasoning_content
        if response.thinking_blocks:
            additional_kwargs["thinking_blocks"] = response.thinking_blocks

        tool_calls_payload: list[dict[str, Any]] = []
        for tc in list(response.tool_calls or []):
            args: Any = tc.arguments
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except json.JSONDecodeError:
                    parsed = {}
                args = parsed if isinstance(parsed, dict) else {}
            elif not isinstance(args, dict):
                args = {}
            tool_calls_payload.append(
                {
                    "name": str(tc.name or ""),
                    "args": args,
                    "id": tc.id or None,
                    "type": "tool_call",
                }
            )

        ai_message = AIMessage(
            content=response.content or "",
            tool_calls=tool_calls_payload,
            additional_kwargs=additional_kwargs,
            response_metadata={
                "finish_reason": response.finish_reason,
                "usage": response.usage,
                "error_text": response.error_text,
            },
        )
        return ChatResult(
            generations=[ChatGeneration(message=ai_message)],
            llm_output={"usage": response.usage},
        )
