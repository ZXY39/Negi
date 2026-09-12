from __future__ import annotations

import contextvars
import inspect
import logging
from collections.abc import Sequence
from typing import Any

from mcp import types
from mcp.server.fastmcp import FastMCP
import mcp.server.fastmcp.server as fastmcp_server

from g3ku.agent.tools.base import Tool
from g3ku.resources.models import ToolResourceDescriptor
from g3ku.resources.tool_settings import resolve_universal_timeout_flag

logger = logging.getLogger(__name__)

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


def _with_universal_timeout_property(schema: dict[str, Any]) -> dict[str, Any]:
    properties = dict(schema.get("properties") or {})
    if "timeout" in properties:
        return schema
    return {
        **schema,
        "properties": {
            **properties,
            "timeout": {
                "type": "number",
                "description": (
                    "Optional maximum execution time in seconds for this call. "
                    "When omitted, the runtime default (600s) applies."
                ),
            },
        },
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


def _handler_dispatch_target(handler: Any) -> Any:
    """Resolve the callable that EmbeddedMCPTool._invoke will actually dispatch to.

    Mirrors the dispatch order in ``_build_tool_callable``: Tool.execute first,
    then any ``execute`` attribute, then the handler itself (``__call__``).
    """
    if isinstance(handler, Tool):
        return handler.execute
    if hasattr(handler, "execute"):
        return handler.execute
    if callable(handler):
        return handler
    return None


def _handler_parameter_info(handler: Any) -> tuple[frozenset[str], bool] | None:
    """Return (accepted keyword names, has **kwargs) for the handler dispatch target.

    None means the signature could not be resolved; callers must then pass
    arguments through unfiltered.
    """
    target = _handler_dispatch_target(handler)
    if target is None:
        return None
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    names = frozenset(
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
    )
    has_var_keyword = any(
        parameter.kind is parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )
    return names, has_var_keyword


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
        if self.self_enforced_timeout:
            # 自持工具消费统一 timeout 参数：FastMCP 按注册 schema 校验入参，
            # 必须把统一参数并入，否则 call_tool 会在进工具前把它拒掉。
            # 标志可能来自 handler 类属性，也可能来自清单 timeout_policy 声明。
            self._parameters = _with_universal_timeout_property(self._parameters)
        # schema/实现漂移防线：注册 schema 声明的参数未必被 handler 真实签名接受
        # （历史事故：resource.yaml 残留 timeout_ms，实现已改为 timeout，FastMCP
        # 按 schema 默认值给每次调用强注 timeout_ms，导致所有调用无差别报错）。
        # 构建期交叉校验告警 + 执行期过滤未接受参数，双保险。
        self._handler_param_info = _handler_parameter_info(handler)
        self._drift_warned_keys: set[str] = set()
        self._warn_schema_handler_drift()
        self._server = FastMCP(name=f"g3ku-{descriptor.name}")
        self._server.add_tool(
            self._build_tool_callable(),
            name=descriptor.name,
            description=descriptor.description or descriptor.name,
        )

    def _warn_schema_handler_drift(self) -> None:
        info = self._handler_param_info
        if info is None:
            return
        names, has_var_keyword = info
        if has_var_keyword:
            return
        declared = set((self._parameters or {}).get("properties") or {})
        drifted = sorted(declared - names)
        if drifted:
            logger.warning(
                "embedded tool %s: registration schema declares parameters %s that the handler "
                "signature does not accept (accepted: %s); such arguments will be dropped at "
                "execution time. Fix resource.yaml or the handler to remove the drift.",
                self.name,
                drifted,
                sorted(names),
            )

    def _filter_handler_arguments(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = self._handler_param_info
        if info is None:
            return payload
        names, has_var_keyword = info
        if has_var_keyword:
            return payload
        dropped = sorted(key for key in payload if key not in names)
        if not dropped:
            return payload
        warn_key = ",".join(dropped)
        if warn_key not in self._drift_warned_keys:
            self._drift_warned_keys.add(warn_key)
            logger.warning(
                "embedded tool %s: dropping arguments %s not accepted by the handler signature "
                "(schema/implementation drift); see resource.yaml and the tool handler.",
                self.name,
                dropped,
            )
        return {key: value for key, value in payload.items() if key in names}

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

    @property
    def self_enforced_timeout(self) -> bool:  # type: ignore[override]
        return resolve_universal_timeout_flag(
            self._handler,
            self._descriptor.metadata,
            handler_attr="self_enforced_timeout",
            policy_key="self_enforced",
        )

    @property
    def hide_universal_timeout_parameter(self) -> bool:  # type: ignore[override]
        return resolve_universal_timeout_flag(
            self._handler,
            self._descriptor.metadata,
            handler_attr="hide_universal_timeout_parameter",
            policy_key="hide_parameter",
        )

    @property
    def exempt_universal_timeout(self) -> bool:  # type: ignore[override]
        return resolve_universal_timeout_flag(
            self._handler,
            self._descriptor.metadata,
            handler_attr="exempt_universal_timeout",
            policy_key="exempt_universal",
        )

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
            payload = self._filter_handler_arguments(dict(kwargs))
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
