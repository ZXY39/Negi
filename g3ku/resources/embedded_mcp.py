from __future__ import annotations

import contextvars
import inspect
from collections.abc import Sequence
from typing import Any

from mcp import types
from mcp.server.fastmcp import FastMCP
import mcp.server.fastmcp.server as fastmcp_server

from g3ku.agent.tools.base import Tool
from g3ku.resources.models import ToolResourceDescriptor

_RUNTIME_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "g3ku_embedded_mcp_runtime",
    default=None,
)
_FASTMCP_LOGGING_PATCHED = False


def _disable_fastmcp_root_logging() -> None:
    """Keep embedded FastMCP instances from mutating global root logging."""
    global _FASTMCP_LOGGING_PATCHED
    if _FASTMCP_LOGGING_PATCHED:
        return

    def _noop_configure_logging(_level: str = "INFO") -> None:
        return

    fastmcp_server.configure_logging = _noop_configure_logging
    _FASTMCP_LOGGING_PATCHED = True


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
    return _runtime_context_parameter_name(target) is not None


def _runtime_context_parameter_name(target: Any) -> str | None:
    candidate = target.execute if hasattr(target, "execute") else target
    if not callable(candidate):
        return None
    try:
        signature = inspect.signature(candidate)
    except (TypeError, ValueError):
        return None
    if "__g3ku_runtime" in signature.parameters:
        return "__g3ku_runtime"
    for name in signature.parameters:
        if str(name).endswith("__g3ku_runtime"):
            return str(name)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return "__g3ku_runtime"
    return None


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
        _disable_fastmcp_root_logging()
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
    def model_description(self) -> str:
        if isinstance(self._handler, Tool):
            runtime_description = str(getattr(self._handler, "model_description", "") or "").strip()
            if runtime_description:
                return runtime_description
        configured = str((self._descriptor.metadata or {}).get("model_description") or "").strip()
        return configured or self.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def model_parameters(self) -> dict[str, Any]:
        if isinstance(self._handler, Tool):
            runtime_parameters = getattr(self._handler, "model_parameters", None)
            if isinstance(runtime_parameters, dict):
                return _normalize_parameters(runtime_parameters)
        configured = (self._descriptor.metadata or {}).get("model_parameters")
        if isinstance(configured, dict):
            return _normalize_parameters(configured)
        return self.parameters

    def set_context(self, *args: Any, **kwargs: Any) -> Any:
        if hasattr(self._handler, "set_context"):
            return self._handler.set_context(*args, **kwargs)
        return None

    def close(self) -> Any:
        if hasattr(self._handler, "close"):
            return self._handler.close()
        return None

    async def execute(self, runtime_payload: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        runtime_context = kwargs.pop("__g3ku_runtime", None)
        if not isinstance(runtime_context, dict):
            runtime_context = runtime_payload if isinstance(runtime_payload, dict) else {}
        token = _RUNTIME_CONTEXT.set(dict(runtime_context or {}))
        try:
            result = await self._server.call_tool(self.name, arguments=kwargs)
            return _render_mcp_result(result)
        finally:
            _RUNTIME_CONTEXT.reset(token)

    def _build_tool_callable(self):
        async def _invoke(**kwargs: Any) -> Any:
            payload = dict(kwargs)
            runtime_context = _RUNTIME_CONTEXT.get() or {}
            runtime_param = _runtime_context_parameter_name(self._handler)
            if runtime_context and runtime_param:
                payload.setdefault(runtime_param, runtime_context)

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
