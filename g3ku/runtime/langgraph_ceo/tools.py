"""LangChain tool wrappers for the CEO LangGraph frontdoor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import ConfigDict, Field, create_model

from g3ku.agent.tools.base import Tool

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(slots=True)
class VisibleToolBundle:
    native_tools: dict[str, Tool]
    langchain_tools: list[BaseTool]
    langchain_tool_map: dict[str, BaseTool]


def _build_args_schema(tool: Tool):
    schema = tool.parameters or {}
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()

    fields: dict[str, tuple[Any, Any]] = {}
    for key, prop in props.items():
        description = prop.get("description") if isinstance(prop, dict) else None
        default = ... if key in required else None
        if description:
            fields[key] = (Any, Field(default=default, description=description))
        else:
            fields[key] = (Any, default)

    model_name = "".join(part.capitalize() for part in tool.name.split("_")) + "Args"
    return create_model(model_name, __config__=ConfigDict(extra="allow"), **fields)


def _build_langchain_tool(tool: Tool, executor: ToolExecutor) -> BaseTool:
    async def _invoke(**kwargs: Any) -> Any:
        return await executor(tool.name, kwargs)

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=tool.name,
        description=tool.description,
        args_schema=_build_args_schema(tool),
        infer_schema=False,
    )


def build_visible_tool_bundle(
    *,
    tools: dict[str, Tool],
    executor: ToolExecutor,
) -> VisibleToolBundle:
    native_tools = dict(tools or {})
    langchain_tool_map = {
        name: _build_langchain_tool(tool, executor)
        for name, tool in native_tools.items()
    }
    return VisibleToolBundle(
        native_tools=native_tools,
        langchain_tools=list(langchain_tool_map.values()),
        langchain_tool_map=dict(langchain_tool_map),
    )
