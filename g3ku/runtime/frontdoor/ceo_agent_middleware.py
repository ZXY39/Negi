from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.messages import SystemMessage
from main.runtime.chat_backend import build_prompt_cache_diagnostics, build_session_prompt_cache_key


def _stable_messages_from_state(state: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in list((state or {}).get("messages") or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if not role:
            continue
        messages.append(
            {
                "role": role,
                "content": item.get("content", ""),
            }
        )
    return messages


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

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        prompt_context = self._runner.build_prompt_context(
            state=state,
            runtime=runtime,
            tools=self._runner.visible_langchain_tools(state=state, runtime=runtime),
        )
        stable_messages = _stable_messages_from_state(state)
        tool_schemas = [
            schema
            for schema in (
                _tool_schema(tool)
                for tool in self._runner.visible_langchain_tools(state=state, runtime=runtime)
            )
            if schema is not None
        ]
        model_refs = list(self._runner._resolve_ceo_model_refs() or [])
        provider_model = str(model_refs[0] if model_refs else "").strip()
        session_key = str(getattr(getattr(runtime, "context", None), "session_key", "") or "").strip()
        overlay_text = str(prompt_context.get("system_overlay") or "").strip()
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
        return {
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_diagnostics": prompt_cache_diagnostics,
        }

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        prompt_context = self._runner.build_prompt_context(
            state=request.state,
            runtime=request.runtime,
            tools=request.tools,
        )
        current_blocks = list((request.system_message or SystemMessage(content="")).content_blocks)
        blocks = current_blocks + [{"type": "text", "text": prompt_context["system_overlay"]}]
        return handler(request.override(system_message=SystemMessage(content=blocks)))


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
