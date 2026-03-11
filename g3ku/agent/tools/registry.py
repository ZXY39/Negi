"""Tool registry for dynamic tool management."""

from __future__ import annotations

import contextvars
import inspect
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import ConfigDict, Field, create_model

from g3ku.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._langchain_tools: dict[str, BaseTool] = {}
        self._runtime_context: contextvars.ContextVar[dict[str, Any] | None] = (
            contextvars.ContextVar("tool_runtime_context", default=None)
        )

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._langchain_tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """Return registered native tools in registration order."""
        return list(self._tools.values())

    def register_langchain_tool(self, tool: BaseTool) -> None:
        """Register an already-built BaseTool (official LangChain tool)."""
        self._langchain_tools[tool.name] = tool

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """Execute a tool by name with given parameters."""
        _hint = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            # StructuredTool may include optional args as `None`; legacy schema validators
            # treat these as type mismatches, so drop unset values before validation/dispatch.
            normalized = {k: v for k, v in params.items() if v is not None}
            errors = tool.validate_params(normalized)
            if errors:
                await self._emit_progress(f"[tool:{name}] 鍙傛暟鏍￠獙澶辫触: {'; '.join(errors)}")
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _hint

            execute_kwargs = dict(normalized)
            runtime_context = self._runtime_context.get() or {}
            if runtime_context and self._accepts_runtime_context(tool):
                execute_kwargs["__g3ku_runtime"] = runtime_context

            result = await tool.execute(**execute_kwargs)
            if isinstance(result, str) and result.startswith("Error"):
                await self._emit_progress(f"[tool:{name}] 鎵ц澶辫触: {result}")
                return result + _hint
            return result
        except Exception as e:
            await self._emit_progress(f"[tool:{name}] 鎵ц寮傚父: {e}")
            return f"Error executing {name}: {str(e)}" + _hint

    def to_langchain_tools(self) -> list[BaseTool]:
        """Convert registered tools to official BaseTool instances."""
        tools: list[BaseTool] = []
        for tool in self._tools.values():
            args_schema = self._build_args_schema(tool)
            coroutine = self._build_tool_coroutine(tool.name)
            tools.append(
                StructuredTool.from_function(
                    coroutine=coroutine,
                    name=tool.name,
                    description=tool.description,
                    args_schema=args_schema,
                    infer_schema=False,
                )
            )
        tools.extend(self._langchain_tools.values())
        return tools

    def _build_tool_coroutine(self, tool_name: str):
        async def _invoke(**kwargs: Any) -> Any:
            return await self.execute(tool_name, kwargs)

        return _invoke

    def push_runtime_context(self, context: dict[str, Any]) -> contextvars.Token:
        """Push per-call runtime context (e.g. progress callback) into current context."""
        return self._runtime_context.set(context)

    def pop_runtime_context(self, token: contextvars.Token) -> None:
        """Restore previous runtime context."""
        self._runtime_context.reset(token)

    async def _emit_progress(self, content: str) -> None:
        runtime_context = self._runtime_context.get() or {}
        callback = runtime_context.get("on_progress")
        if not callback:
            return
        try:
            await callback(content)
        except Exception:
            # Progress emission is best-effort and should not affect tool execution.
            pass

    def get_runtime_context(self) -> dict[str, Any]:
        """Return current tool runtime context."""
        return self._runtime_context.get() or {}

    @staticmethod
    def _accepts_runtime_context(tool: Tool) -> bool:
        sig = inspect.signature(tool.execute)
        if "__g3ku_runtime" in sig.parameters:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    @staticmethod
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

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys()) + list(self._langchain_tools.keys())

    def __len__(self) -> int:
        return len(self._tools) + len(self._langchain_tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools or name in self._langchain_tools

