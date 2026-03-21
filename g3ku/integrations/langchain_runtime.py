"""LangChain 1.2+ runtime adapters for g3ku."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ToolMessage,
    convert_to_messages,
    convert_to_openai_messages,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from loguru import logger
from pydantic import ConfigDict

from g3ku.providers.base import LLMResponse


def _as_message_dicts(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """Convert LangChain messages to OpenAI-style message dicts."""
    converted = convert_to_openai_messages(messages, text_format="string", include_id=False)
    return [converted] if isinstance(converted, dict) else list(converted)


def _stringify(value: Any) -> str:
    """Render arbitrary tool results to text for tool messages."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _content_to_text(content: Any) -> str | None:
    """Normalize LangChain content shape into plain text."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return str(content)


def _normalize_tool_choice(value: Any) -> str | dict[str, Any] | None:
    """Normalize LangChain tool_choice values to provider-friendly values."""
    if value == "any":
        return "required"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value
    return None


def _parse_json_object(content: Any) -> dict[str, Any] | None:
    """Best-effort parse of JSON object content."""
    text = _content_to_text(content)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _should_promote_structured_content(
    *,
    tools_arg: Sequence[Any],
    openai_tools: list[dict[str, Any]],
    requested_tool_choice: Any,
    tool_calls_payload: list[dict[str, Any]],
) -> bool:
    """Detect with_structured_output calls that returned JSON text instead of tool calls."""
    if tool_calls_payload:
        return False
    if requested_tool_choice not in {"any", "required"}:
        return False
    if len(tools_arg) != 1 or len(openai_tools) != 1:
        return False
    tool = tools_arg[0]
    return isinstance(tool, dict) and isinstance(tool.get("title"), str) and bool(tool.get("title"))


@dataclass(slots=True)
class LegacyToolCall:
    """Minimal tool-call shape compatible with existing helper methods."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LegacyLLMResponseView:
    """Compatibility view over LangChain AI messages for legacy hooks."""

    content: str | None
    tool_calls: list[LegacyToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None
    thinking_blocks: list[dict[str, Any]] | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @classmethod
    def from_ai_message(cls, message: AIMessage) -> LegacyLLMResponseView:
        tool_calls: list[LegacyToolCall] = []
        for item in message.tool_calls or []:
            name = item.get("name")
            args = item.get("args")
            if not isinstance(name, str):
                continue
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(
                LegacyToolCall(
                    id=str(item.get("id") or ""),
                    name=name,
                    arguments=args,
                )
            )

        return cls(
            content=_content_to_text(message.content),
            tool_calls=tool_calls,
            finish_reason=str(message.response_metadata.get("finish_reason", "stop")),
            usage=dict(message.response_metadata.get("usage", {}) or {}),
            reasoning_content=message.additional_kwargs.get("reasoning_content"),
            thinking_blocks=message.additional_kwargs.get("thinking_blocks"),
        )

    @classmethod
    def from_any(cls, value: Any) -> LegacyLLMResponseView | None:
        """Coerce middleware outputs into a normalized response view."""
        if value is None:
            return None
        if isinstance(value, LegacyLLMResponseView):
            return value
        if isinstance(value, AIMessage):
            return cls.from_ai_message(value)
        if isinstance(value, LLMResponse):
            return cls(
                content=value.content,
                tool_calls=[
                    LegacyToolCall(
                        id=str(call.id),
                        name=call.name,
                        arguments=call.arguments if isinstance(call.arguments, dict) else {},
                    )
                    for call in value.tool_calls
                ],
                finish_reason=value.finish_reason,
                usage=dict(value.usage or {}),
                reasoning_content=value.reasoning_content,
                thinking_blocks=value.thinking_blocks,
            )

        content = getattr(value, "content", None)
        tool_calls_raw = getattr(value, "tool_calls", []) or []
        tool_calls: list[LegacyToolCall] = []
        for item in tool_calls_raw:
            if isinstance(item, LegacyToolCall):
                tool_calls.append(item)
                continue
            if isinstance(item, dict):
                name = item.get("name")
                args = item.get("arguments", item.get("args", {}))
                call_id = item.get("id", "")
            else:
                name = getattr(item, "name", None)
                args = getattr(item, "arguments", getattr(item, "args", {}))
                call_id = getattr(item, "id", "")
            if not isinstance(name, str):
                continue
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(
                LegacyToolCall(id=str(call_id or ""), name=name, arguments=args)
            )

        return cls(
            content=_content_to_text(content),
            tool_calls=tool_calls,
            finish_reason=str(getattr(value, "finish_reason", "stop")),
            usage=dict(getattr(value, "usage", {}) or {}),
            reasoning_content=getattr(value, "reasoning_content", None),
            thinking_blocks=getattr(value, "thinking_blocks", None),
        )

    def to_ai_message(self, template: AIMessage | None = None) -> AIMessage:
        """Convert response view back into LangChain AIMessage."""
        additional_kwargs: dict[str, Any] = {}
        if self.reasoning_content:
            additional_kwargs["reasoning_content"] = self.reasoning_content
        if self.thinking_blocks:
            additional_kwargs["thinking_blocks"] = self.thinking_blocks

        response_metadata: dict[str, Any] = {"finish_reason": self.finish_reason}
        if self.usage:
            response_metadata["usage"] = self.usage

        return AIMessage(
            content=self.content or "",
            tool_calls=[
                {
                    "name": call.name,
                    "args": call.arguments,
                    "id": call.id or None,
                    "type": "tool_call",
                }
                for call in self.tool_calls
            ],
            additional_kwargs=additional_kwargs or (template.additional_kwargs if template else {}),
            response_metadata=response_metadata,
            id=template.id if template else None,
        )


class ProviderChatModelAdapter(BaseChatModel):
    """Adapter from g3ku's async provider interface to BaseChatModel."""

    provider: Any
    default_model: str
    default_temperature: float
    default_max_tokens: int
    default_reasoning_effort: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "g3ku-provider-adapter"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool | callable],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ):
        """Enable tool binding for create_agent() compatibility."""
        return self.bind(tools=list(tools), tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        """Sync fallback for invoke(); async runtime should use _agenerate()."""
        del stop, run_manager
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._agenerate(messages, **kwargs))
        raise RuntimeError(
            "ProviderChatModelAdapter._generate cannot run inside active event loop; "
            "use async APIs."
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        """Forward calls to LLMProvider.chat and map result to ChatResult."""
        del stop, run_manager

        openai_messages = _as_message_dicts(messages)
        tools_arg = kwargs.get("tools") or []
        openai_tools = [
            convert_to_openai_tool(tool)
            for tool in tools_arg
        ] or None
        requested_tool_choice = kwargs.get("tool_choice")
        normalized_tool_choice = _normalize_tool_choice(requested_tool_choice)
        parallel_tool_calls = kwargs.get("parallel_tool_calls")
        prompt_cache_key = kwargs.get("prompt_cache_key")

        model = str(kwargs.get("model") or self.default_model)
        temperature = float(kwargs.get("temperature", self.default_temperature))
        max_tokens = int(kwargs.get("max_tokens", self.default_max_tokens))
        reasoning_effort = kwargs.get("reasoning_effort", self.default_reasoning_effort)
        if reasoning_effort is not None:
            reasoning_effort = str(reasoning_effort)

        response = await self.provider.chat(
            messages=openai_messages,
            tools=openai_tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            tool_choice=normalized_tool_choice,
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
        for tc in response.tool_calls:
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
                    "name": tc.name,
                    "args": args,
                    "id": tc.id or None,
                    "type": "tool_call",
                }
            )

        if openai_tools and _should_promote_structured_content(
            tools_arg=tools_arg,
            openai_tools=openai_tools,
            requested_tool_choice=requested_tool_choice,
            tool_calls_payload=tool_calls_payload,
        ):
            parsed = _parse_json_object(response.content)
            function_name = (
                ((openai_tools[0] or {}).get("function") or {}).get("name")
                if openai_tools
                else None
            )
            if parsed is not None and isinstance(function_name, str) and function_name:
                tool_calls_payload.append(
                    {
                        "name": function_name,
                        "args": parsed,
                        "id": "synthetic-structured-output",
                        "type": "tool_call",
                    }
                )
                logger.debug(
                    "Promoted JSON text content to synthetic tool_call for structured output: {}",
                    function_name,
                )

        ai_message = AIMessage(
            content=response.content or "",
            tool_calls=tool_calls_payload,
            additional_kwargs=additional_kwargs,
            response_metadata={
                "finish_reason": response.finish_reason,
                "usage": response.usage,
            },
        )

        return ChatResult(
            generations=[ChatGeneration(message=ai_message)],
            llm_output={"usage": response.usage},
        )


class LegacyMiddlewareBridge(AgentMiddleware):
    """Bridge g3ku legacy middleware hooks onto AgentMiddleware API."""

    def __init__(self, loop: Any):
        self._loop = loop

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler,
    ) -> ModelResponse[Any]:
        runtime_context = getattr(request.runtime, "context", None)
        session_key = getattr(runtime_context, "session_key", None)
        on_progress = getattr(runtime_context, "on_progress", None)

        iteration = 1
        if runtime_context is not None:
            iteration = int(getattr(runtime_context, "iteration", 0)) + 1
            setattr(runtime_context, "iteration", iteration)

        chain_messages = list(request.messages)
        if request.system_message is not None:
            chain_messages.insert(0, request.system_message)
        openai_messages = _as_message_dicts(chain_messages)

        model_settings = dict(request.model_settings or {})
        model_name = str(model_settings.get("model") or getattr(runtime_context, "model", ""))
        temperature = float(
            model_settings.get("temperature", getattr(runtime_context, "temperature", 0.1))
        )
        max_tokens = int(
            model_settings.get("max_tokens", getattr(runtime_context, "max_tokens", 4096))
        )
        reasoning_effort = model_settings.get(
            "reasoning_effort",
            getattr(runtime_context, "reasoning_effort", None),
        )
        if reasoning_effort is not None:
            reasoning_effort = str(reasoning_effort)

        llm_req = await self._loop._apply_before_llm_middlewares(
            messages=openai_messages,
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            iteration=iteration,
            session_key=session_key,
        )

        patched_request = request.override(
            messages=convert_to_messages(llm_req["messages"]),
            system_message=None,
            model_settings={
                **model_settings,
                "model": llm_req["model"],
                "temperature": llm_req["temperature"],
                "max_tokens": llm_req["max_tokens"],
                "reasoning_effort": llm_req["reasoning_effort"],
            },
        )

        model_response = await handler(patched_request)
        if not hasattr(model_response, "result"):
            return model_response

        response_messages = list(model_response.result)
        ai_index = next(
            (
                index
                for index in range(len(response_messages) - 1, -1, -1)
                if isinstance(response_messages[index], AIMessage)
            ),
            None,
        )
        if ai_index is None:
            return model_response

        ai_message = response_messages[ai_index]
        legacy_response = LegacyLLMResponseView.from_ai_message(ai_message)
        after_value = await self._loop._apply_after_llm_middlewares(
            response=legacy_response,
            iteration=iteration,
            session_key=session_key,
        )
        coerced = LegacyLLMResponseView.from_any(after_value) if after_value is not None else None
        if coerced is not None:
            legacy_response = coerced

        response_messages[ai_index] = legacy_response.to_ai_message(template=ai_message)
        model_response = ModelResponse(
            result=response_messages,
            structured_response=getattr(model_response, "structured_response", None),
        )

        if on_progress and legacy_response.has_tool_calls:
            clean = self._loop._strip_think(legacy_response.content)
            if clean:
                await on_progress(clean)
            await on_progress(self._loop._tool_hint(legacy_response.tool_calls), tool_hint=True)

        return model_response

    async def awrap_tool_call(self, request, handler) -> ToolMessage:
        """Execute tools through ToolRegistry while preserving legacy hooks."""
        del handler

        runtime_context = getattr(request.runtime, "context", None)
        lock = getattr(runtime_context, "tool_lock", None)
        if lock is not None:
            async with lock:
                return await self._execute_tool_call(request, runtime_context)
        return await self._execute_tool_call(request, runtime_context)

    async def _execute_tool_call(self, request, runtime_context: Any) -> ToolMessage:
        tool_call = dict(request.tool_call or {})
        raw_name = tool_call.get("name")
        raw_args = tool_call.get("args")

        name = raw_name if isinstance(raw_name, str) else ""
        arguments = raw_args if isinstance(raw_args, dict) else {}
        tool_call_id = str(tool_call.get("id") or getattr(request.runtime, "tool_call_id", "") or "tool")
        return await self._loop.tool_bridge.execute_named_tool(
            name=name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            runtime_context=runtime_context,
            emit_progress=False,
        )


def extract_tools_used(messages: Sequence[BaseMessage]) -> list[str]:
    """Extract tool names in execution order from LangChain message history."""
    used: list[str] = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.name:
            used.append(message.name)
    return used


def extract_final_response(messages: Sequence[BaseMessage]) -> LegacyLLMResponseView | None:
    """Return the last AI message as a legacy-compatible response view."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return LegacyLLMResponseView.from_ai_message(message)
    return None


def to_openai_message_dicts(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """Convert output messages back to g3ku's stored dict format."""
    message_dicts = _as_message_dicts(messages)
    for index, message in enumerate(messages):
        if not isinstance(message, AIMessage):
            continue
        if index >= len(message_dicts):
            break
        extra = message.additional_kwargs or {}
        if extra.get("reasoning_content"):
            message_dicts[index]["reasoning_content"] = extra["reasoning_content"]
        if extra.get("thinking_blocks"):
            message_dicts[index]["thinking_blocks"] = extra["thinking_blocks"]
    return message_dicts



