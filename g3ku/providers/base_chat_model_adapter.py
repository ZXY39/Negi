"""Minimal BaseChatModel adapter for g3ku chat backends."""

from __future__ import annotations

import asyncio
import inspect
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


class G3kuChatModelAdapter(BaseChatModel):
    """Adapt g3ku chat backends to LangChain BaseChatModel."""

    chat_backend: Any
    default_model: str | None = None
    model_refs: list[str] | None = None
    default_temperature: float | None = None
    default_max_tokens: int | None = None
    default_reasoning_effort: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "g3ku-chat-model-adapter"

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
            "G3kuChatModelAdapter._generate cannot run inside an active event loop; use async APIs."
        )

    def _resolve_model_refs(self, kwargs: dict[str, Any]) -> list[str]:
        explicit_model = str(kwargs.get("model") or "").strip()
        if explicit_model:
            return [explicit_model]
        refs = [
            str(item or "").strip()
            for item in list(self.model_refs or [])
            if str(item or "").strip()
        ]
        if refs:
            return refs
        default_model = str(self.default_model or "").strip()
        return [default_model] if default_model else []

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
        resolved_model_refs = self._resolve_model_refs(kwargs)
        chat = getattr(self.chat_backend, "chat")
        signature = inspect.signature(chat)

        chat_kwargs: dict[str, Any] = {
            "messages": openai_messages,
            "tools": openai_tools,
            "tool_choice": normalized_tool_choice,
            "parallel_tool_calls": (
                bool(parallel_tool_calls) if isinstance(parallel_tool_calls, bool) else None
            ),
            "prompt_cache_key": (str(prompt_cache_key).strip() or None) if prompt_cache_key is not None else None,
        }
        if "model_refs" in signature.parameters:
            chat_kwargs["model_refs"] = resolved_model_refs
        elif "model" in signature.parameters:
            chat_kwargs["model"] = str(resolved_model_refs[0] if resolved_model_refs else "").strip() or None
        if raw_temperature is not None:
            chat_kwargs["temperature"] = float(raw_temperature)
        if raw_max_tokens is not None:
            chat_kwargs["max_tokens"] = int(raw_max_tokens)
        if reasoning_effort is not None:
            chat_kwargs["reasoning_effort"] = str(reasoning_effort)

        response = await chat(**chat_kwargs)

        additional_kwargs: dict[str, Any] = {}
        if getattr(response, "reasoning_content", None):
            additional_kwargs["reasoning_content"] = response.reasoning_content
        if getattr(response, "thinking_blocks", None):
            additional_kwargs["thinking_blocks"] = response.thinking_blocks

        tool_calls_payload: list[dict[str, Any]] = []
        for tc in list(getattr(response, "tool_calls", None) or []):
            args: Any = getattr(tc, "arguments", {})
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
                    "name": str(getattr(tc, "name", "") or ""),
                    "args": args,
                    "id": getattr(tc, "id", None),
                    "type": "tool_call",
                }
            )

        ai_message = AIMessage(
            content=getattr(response, "content", None) or "",
            tool_calls=tool_calls_payload,
            additional_kwargs=additional_kwargs,
            response_metadata={
                "finish_reason": getattr(response, "finish_reason", "stop"),
                "usage": getattr(response, "usage", {}),
                "error_text": getattr(response, "error_text", None),
            },
        )
        return ChatResult(
            generations=[ChatGeneration(message=ai_message)],
            llm_output={"usage": getattr(response, "usage", {})},
        )


__all__ = ["G3kuChatModelAdapter"]
