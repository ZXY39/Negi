from __future__ import annotations

import contextvars
import inspect
from collections.abc import Sequence
from typing import Any

from mcp import types
from mcp.server.fastmcp import FastMCP

from g3ku.agent.tools.base import Tool
from g3ku.resources.models import ToolResourceDescriptor

_RUNTIME_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "g3ku_embedded_mcp_runtime",
    default=None,
)


def _schema_to_annotation(schema: dict[str, Any] | None) -> Any:
    schema = schema or {}
    schema_type = str(schema.get("type") or "").strip().lower()
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        return list[Any]
    if schema_type == "object":
        return dict[str, Any]
    return Any


def _normalize_parameters(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}
    return {
        **schema,
        "type": "object",
        "properties": dict(schema.get("properties") or {}),
        "required": list(schema.get("required") or []),
    }


def _build_signature(schema: dict[str, Any]) -> inspect.Signature:
    props = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    parameters: list[inspect.Parameter] = []
    for name, prop in props.items():
        prop_schema = prop if isinstance(prop, dict) else {}
        default = prop_schema.get("default", None)
        parameters.append(
            inspect.Parameter(
                str(name),
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect._empty if name in required else default,
                annotation=_schema_to_annotation(prop_schema),
            )
        )
    return inspect.Signature(parameters=parameters)


def _accepts_runtime_context(target: Any) -> bool:
    candidate = target.execute if hasattr(target, "execute") else target
    if not callable(candidate):
        return False
    try:
        signature = inspect.signature(candidate)
    except (TypeError, ValueError):
        return False
    if "__g3ku_runtime" in signature.parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())


def _render_mcp_result(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts: list[str] = []
        for block in value:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part).strip() or "(no output)"
    return value


class EmbeddedMCPTool(Tool):
    def __init__(self, descriptor: ToolResourceDescriptor, handler: Any):
        self._descriptor = descriptor
        self._handler = handler
        self._parameters = _normalize_parameters(descriptor.parameters)
        self._server = FastMCP(name=f"g3ku-{descriptor.name}")
        self._server.add_tool(
            self._build_tool_callable(),
            name=descriptor.name,
            description=descriptor.description or descriptor.name,
        )

    @property
    def name(self) -> str:
        return self._descriptor.name

    @property
    def description(self) -> str:
        return self._descriptor.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    def set_context(self, *args: Any, **kwargs: Any) -> Any:
        if hasattr(self._handler, "set_context"):
            return self._handler.set_context(*args, **kwargs)
        return None

    def close(self) -> Any:
        if hasattr(self._handler, "close"):
            return self._handler.close()
        return None

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        token = _RUNTIME_CONTEXT.set(dict(__g3ku_runtime or {}))
        try:
            result = await self._server.call_tool(self.name, arguments=kwargs)
            return _render_mcp_result(result)
        finally:
            _RUNTIME_CONTEXT.reset(token)

    def _build_tool_callable(self):
        async def _invoke(**kwargs: Any) -> Any:
            payload = dict(kwargs)
            runtime_context = _RUNTIME_CONTEXT.get() or {}
            if runtime_context and _accepts_runtime_context(self._handler):
                payload.setdefault("__g3ku_runtime", runtime_context)

            if isinstance(self._handler, Tool):
                return await self._handler.execute(**payload)

            if hasattr(self._handler, "execute"):
                result = self._handler.execute(**payload)
                if inspect.isawaitable(result):
                    return await result
                return result

            if callable(self._handler):
                result = self._handler(**payload)
                if inspect.isawaitable(result):
                    return await result
                return result

            raise TypeError(
                f"Unsupported embedded MCP handler for {self.name}: {type(self._handler).__name__}"
            )

        _invoke.__name__ = self.name.replace("-", "_")
        _invoke.__doc__ = self.description or self.name
        _invoke.__signature__ = _build_signature(self._parameters)
        return _invoke
