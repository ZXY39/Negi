from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

try:
    from langchain.agents.middleware import (
        AgentMiddleware,
        ModelRequest,
        ModelResponse,
        ToolCallRequest,
    )
    from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
except ModuleNotFoundError:  # pragma: no cover - optional dependency fallback
    AgentMiddleware = object  # type: ignore[assignment]
    ModelRequest = Any  # type: ignore[assignment]
    ModelResponse = Any  # type: ignore[assignment]
    ToolCallRequest = Any  # type: ignore[assignment]

    class AIMessage:  # type: ignore[no-redef]
        tool_calls: list[Any]

        def __init__(self, content: str = "", tool_calls: list[Any] | None = None):
            self.content = content
            self.tool_calls = list(tool_calls or [])

    class SystemMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = ""):
            self.content = content
            self.text = content

    class ToolMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = "", name: str = ""):
            self.content = content
            self.name = name

if TYPE_CHECKING:
    from g3ku.agent.loop import AgentLoop


@dataclass(slots=True)
class LoopRuntimeContext:
    """Run-scoped context injected into LangGraph runtime."""

    session_key: str | None
    channel: str | None
    chat_id: str | None
    message_id: str | None
    on_progress: Callable[..., Awaitable[None]] | None
    cancel_token: Any | None = None


class LoopRuntimeMiddleware(AgentMiddleware):
    """Internal middleware to align create_agent runtime behavior with g3ku UX."""

    def __init__(self, loop: "AgentLoop"):
        self._loop = loop

    async def awrap_model_call(
        self,
        request: ModelRequest[LoopRuntimeContext],
        handler,
    ) -> ModelResponse[Any]:
        runtime_context = getattr(request.runtime, "context", None)
        if self._loop.debug_trace:
            user_text = ""
            for message in reversed(request.messages):
                if getattr(message, "type", "") == "human":
                    user_text = self._loop._content_to_text(getattr(message, "content", None)) or ""
                    break
            tool_names = [str(getattr(tool, "name", "") or "") for tool in list(getattr(request, "tools", []) or [])]
            logger.info(
                "[debug:model:request] session={} channel={} chat={} messages={} tools={} user={}",
                getattr(runtime_context, "session_key", None),
                getattr(runtime_context, "channel", None),
                getattr(runtime_context, "chat_id", None),
                len(list(getattr(request, "messages", []) or [])),
                ",".join(name for name in tool_names if name) or "-",
                self._loop._preview(user_text),
            )
            if request.system_message is not None and request.system_message.text:
                logger.debug(
                    "[debug:model:system] {}",
                    self._loop._preview(request.system_message.text, max_chars=1200),
                )
        memory_block = ""
        if self._loop.memory_manager:
            try:
                user_text = ""
                for message in reversed(request.messages):
                    if getattr(message, "type", "") == "human":
                        user_text = self._loop._content_to_text(getattr(message, "content", None)) or ""
                        break
                if user_text:
                    memory_block = await self._loop.memory_manager.retrieve_block(
                        query=user_text,
                        channel=getattr(runtime_context, "channel", None),
                        chat_id=getattr(runtime_context, "chat_id", None),
                        session_key=getattr(runtime_context, "session_key", None),
                    )
            except Exception:
                logger.exception("Memory retrieval middleware failed")

        model_settings = dict(request.model_settings or {})
        model_settings.setdefault("temperature", self._loop.temperature)
        model_settings.setdefault("max_tokens", self._loop.max_tokens)
        if self._loop.reasoning_effort is not None:
            model_settings.setdefault("reasoning_effort", self._loop.reasoning_effort)

        patched = request.override(model_settings=model_settings)
        if memory_block:
            existing = request.system_message.text if request.system_message else ""
            merged = (existing + "\n\n" if existing else "") + memory_block
            patched = patched.override(system_message=SystemMessage(content=merged))
            if self._loop.debug_trace:
                logger.debug(
                    "[debug:model:memory_injected] chars={}",
                    len(str(memory_block)),
                )
        response = await handler(patched)

        on_progress = getattr(runtime_context, "on_progress", None)
        if not on_progress:
            return response

        ai_message = next(
            (
                message
                for message in reversed(response.result)
                if isinstance(message, AIMessage)
            ),
            None,
        )
        if self._loop.debug_trace:
            tool_count = len(ai_message.tool_calls) if (ai_message and getattr(ai_message, "tool_calls", None)) else 0
            content_preview = self._loop._preview(
                self._loop._strip_think(self._loop._content_to_text(getattr(ai_message, "content", None))) or ""
            )
            logger.info(
                "[debug:model:response] session={} channel={} chat={} tool_calls={} content={}",
                getattr(runtime_context, "session_key", None),
                getattr(runtime_context, "channel", None),
                getattr(runtime_context, "chat_id", None),
                tool_count,
                content_preview,
            )
        if ai_message is None or not ai_message.tool_calls:
            return response

        clean = self._loop._strip_think(self._loop._content_to_text(ai_message.content))
        if clean:
            await self._loop._emit_progress_event(on_progress, clean, event_kind="analysis")
        await self._loop._emit_progress_event(
            on_progress,
            self._loop._tool_hint(ai_message.tool_calls),
            tool_hint=True,
            event_kind="tool_plan",
        )
        return response

    async def awrap_tool_call(self, request: ToolCallRequest, handler) -> ToolMessage:
        return await self._loop.tool_bridge.wrap_tool_call(request, handler)


class ModelExecutionBridge:
    """Shared model middleware bridge for runtime integrations."""

    def __init__(self, loop: "AgentLoop"):
        self._loop = loop

    async def emit_progress_event(
        self,
        on_progress,
        content: str,
        *,
        tool_hint: bool = False,
        deep_progress: bool = False,
        event_kind: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> None:
        if not on_progress:
            return
        try:
            result = on_progress(
                content,
                tool_hint=tool_hint,
                deep_progress=deep_progress,
                event_kind=event_kind,
                event_data=event_data,
            )
        except TypeError:
            result = on_progress(content, tool_hint=tool_hint, deep_progress=deep_progress)
        if inspect.isawaitable(result):
            await result

    def build_runtime_context(
        self,
        *,
        session_key: str | None,
        channel: str | None,
        chat_id: str | None,
        message_id: str | None,
        on_progress: Callable[..., Awaitable[None]] | None,
        cancel_token: Any | None = None,
    ) -> LoopRuntimeContext:
        return LoopRuntimeContext(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            on_progress=on_progress,
            cancel_token=cancel_token,
        )

    def runtime_middlewares(self) -> list[Any]:
        return [*self._loop.middlewares, LoopRuntimeMiddleware(self._loop)]

    async def apply_before_middlewares(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None,
        iteration: int,
        session_key: str | None,
    ) -> dict[str, Any]:
        req: dict[str, Any] = {
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
        }
        for mw in self._loop.middlewares:
            hook = getattr(mw, "before_llm", None)
            if not hook:
                continue
            try:
                update = await self._loop._maybe_await(
                    hook(
                        messages=req["messages"],
                        model=req["model"],
                        temperature=req["temperature"],
                        max_tokens=req["max_tokens"],
                        reasoning_effort=req["reasoning_effort"],
                        iteration=iteration,
                        session_key=session_key,
                    )
                )
            except Exception:
                logger.exception("Middleware before_llm failed: {}", type(mw).__name__)
                continue

            if update is None:
                continue
            if not isinstance(update, dict):
                logger.warning(
                    "Middleware before_llm must return dict|None, got {} from {}",
                    type(update).__name__,
                    type(mw).__name__,
                )
                continue

            for key in ("messages", "model", "temperature", "max_tokens", "reasoning_effort"):
                if key in update and update[key] is not None:
                    req[key] = update[key]

        return req

    async def apply_after_middlewares(
        self,
        *,
        response: Any,
        iteration: int,
        session_key: str | None,
    ) -> Any:
        current = response
        for mw in self._loop.middlewares:
            hook = getattr(mw, "after_llm", None)
            if not hook:
                continue
            try:
                maybe_new = await self._loop._maybe_await(
                    hook(
                        response=current,
                        iteration=iteration,
                        session_key=session_key,
                    )
                )
            except Exception:
                logger.exception("Middleware after_llm failed: {}", type(mw).__name__)
                continue
            if maybe_new is not None:
                current = maybe_new
        return current

