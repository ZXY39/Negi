from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.messages import SystemMessage
from langgraph.types import Command

from main.runtime.chat_backend import build_prompt_cache_diagnostics, build_session_prompt_cache_key


def _message_role(value: Any) -> str:
    if isinstance(value, dict):
        role = str(value.get("role") or "").strip().lower()
    else:
        role = str(getattr(value, "type", "") or getattr(value, "role", "")).strip().lower()
    if role == "human":
        return "user"
    if role == "ai":
        return "assistant"
    return role


def _message_content(value: Any) -> Any:
    if isinstance(value, dict):
        content = value.get("content", "")
    else:
        content = getattr(value, "content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text", item.get("content", ""))
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts).strip()
    return content


def _stable_messages_from_request(*, system_message: Any, messages: list[Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if system_message is not None:
        result.append(
            {
                "role": "system",
                "content": _message_content(system_message),
            }
        )
    for item in list(messages or []):
        role = _message_role(item)
        if not role:
            continue
        result.append(
            {
                "role": role,
                "content": _message_content(item),
            }
        )
    return result


def _tool_schema(tool: Any) -> dict[str, Any] | None:
    if isinstance(tool, dict):
        return dict(tool)
    name = str(getattr(tool, "name", "") or "").strip()
    if not name:
        return None
    args_schema = getattr(tool, "args_schema", None)
    parameters = {}
    if args_schema is not None and hasattr(args_schema, "model_json_schema"):
        parameters = dict(args_schema.model_json_schema() or {})
    return {
        "name": name,
        "description": str(getattr(tool, "description", "") or ""),
        "parameters": parameters,
    }


class CeoPromptAssemblyMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def _prepare_request_and_update(self, request: ModelRequest) -> tuple[ModelRequest, dict[str, Any]]:
        prompt_context = self._runner.build_prompt_context(
            state=request.state,
            runtime=request.runtime,
            tools=request.tools,
        )
        current_blocks = list((request.system_message or SystemMessage(content="")).content_blocks)
        overlay_text = str(prompt_context.get("system_overlay") or "").strip()
        blocks = current_blocks + [{"type": "text", "text": overlay_text}]
        system_message = SystemMessage(content=blocks)
        tool_schemas = [
            schema
            for schema in (
                _tool_schema(tool)
                for tool in list(request.tools or [])
            )
            if schema is not None
        ]
        model_refs = list(self._runner._resolve_ceo_model_refs() or [])
        provider_model = str(model_refs[0] if model_refs else "").strip()
        session_key = str(getattr(getattr(request.runtime, "context", None), "session_key", "") or "").strip()
        stable_messages = _stable_messages_from_request(
            system_message=system_message,
            messages=list(request.messages or []),
        )
        prompt_cache_key = build_session_prompt_cache_key(
            session_key=session_key,
            provider_model=provider_model,
            scope="ceo_frontdoor",
            stable_messages=stable_messages,
            tool_schemas=tool_schemas,
        )
        prompt_cache_diagnostics = build_prompt_cache_diagnostics(
            stable_messages=stable_messages,
            tool_schemas=tool_schemas,
            provider_model=provider_model,
            scope="ceo_frontdoor",
            prompt_cache_key=prompt_cache_key,
            overlay_text=overlay_text,
            overlay_section_count=len([section for section in overlay_text.split("\n\n") if section.strip()]),
        )
        return request.override(system_message=system_message), {
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_diagnostics": prompt_cache_diagnostics,
        }

    @staticmethod
    def _wrap_response(response: ModelResponse | ExtendedModelResponse, update: dict[str, Any]) -> ExtendedModelResponse:
        if isinstance(response, ExtendedModelResponse):
            existing_update = dict(getattr(getattr(response, "command", None), "update", {}) or {})
            return ExtendedModelResponse(
                model_response=response.model_response,
                command=Command(update={**existing_update, **update}),
            )
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update=update),
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        updated_request, update = self._prepare_request_and_update(request)
        response = handler(updated_request)
        return self._wrap_response(response, update)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        updated_request, update = self._prepare_request_and_update(request)
        response = await handler(updated_request)
        return self._wrap_response(response, update)


class CeoToolExposureMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        tools = self._runner.visible_langchain_tools(state=request.state, runtime=request.runtime)
        return handler(request.override(tools=tools))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        tools = self._runner.visible_langchain_tools(state=request.state, runtime=request.runtime)
        return await handler(request.override(tools=tools))
