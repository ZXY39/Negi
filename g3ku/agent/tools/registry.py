"""Tool registry for dynamic tool management."""

from __future__ import annotations

import contextvars
import inspect
import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from g3ku.agent.tools.base import Tool
from g3ku.json_schema_utils import attach_raw_parameters_schema, build_args_schema_model
from g3ku.runtime.tool_watchdog import actor_role_allows_watchdog, run_tool_with_watchdog

_CONTROL_TOOL_NAMES = {"wait_tool_execution", "stop_tool_execution"}


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._dynamic_tools: dict[str, Tool] = {}
        self._langchain_tools: dict[str, BaseTool] = {}
        self._runtime_context: contextvars.ContextVar[dict[str, Any] | None] = (
            contextvars.ContextVar("tool_runtime_context", default=None)
        )

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def replace_dynamic_tools(self, tools: dict[str, Tool]) -> None:
        """Replace dynamically discovered resource-backed tools."""
        self._dynamic_tools = dict(tools)

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._dynamic_tools.pop(name, None)
        self._langchain_tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._dynamic_tools.get(name) or self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """Return registered native tools in registration order."""
        merged = dict(self._tools)
        merged.update(self._dynamic_tools)
        return list(merged.values())

    def register_langchain_tool(self, tool: BaseTool) -> None:
        """Register an already-built BaseTool (official LangChain tool)."""
        self._langchain_tools[tool.name] = tool

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._dynamic_tools or name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self.list_tools()]

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """Execute a tool by name with given parameters."""
        _hint = "\n\n[Analyze the error above and try a different approach.]"

        tool = self.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            # StructuredTool may include optional args as `None`; legacy schema validators
            # treat these as type mismatches, so drop unset values before validation/dispatch.
            normalized = {k: v for k, v in params.items() if v is not None}
            try:
                errors = tool.validate_params(normalized)
            except Exception as exc:
                await self._emit_progress(f"[tool:{name}] 参数校验异常: {exc}")
                return f"Error validating tool '{name}': {str(exc)}" + _hint
            if errors:
                await self._emit_progress(f"[tool:{name}] 参数校验失败: {'; '.join(errors)}")
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _hint

            runtime_context = self._runtime_context.get() or {}
            callback = runtime_context.get("on_progress")
            emit_lifecycle = bool(runtime_context.get("emit_lifecycle", False))

            if callback and emit_lifecycle:
                try:
                    await self._emit_runtime_event(
                        f"{name} started",
                        event_kind="tool_start",
                        event_data={"tool_name": name},
                    )
                except Exception:
                    pass

            result = await self._execute_tool_with_runtime(
                tool=tool,
                tool_name=name,
                params=normalized,
                runtime_context=runtime_context,
            )
            if isinstance(result, str) and result.startswith("Error"):
                await self._emit_progress(f"[tool:{name}] 执行失败: {result}")
                if callback and emit_lifecycle:
                    try:
                        await self._emit_runtime_event(
                            result,
                            event_kind="tool_error",
                            event_data={"tool_name": name},
                        )
                    except Exception:
                        pass
                return result + _hint
            if callback and emit_lifecycle:
                try:
                    await self._emit_runtime_event(
                        self._stringify_result(result),
                        event_kind="tool_result",
                        event_data={"tool_name": name},
                    )
                except Exception:
                    pass
            return result
        except Exception as e:
            await self._emit_progress(f"[tool:{name}] 执行异常: {e}")
            runtime_context = self._runtime_context.get() or {}
            callback = runtime_context.get("on_progress")
            if callback and runtime_context.get("emit_lifecycle"):
                try:
                    await self._emit_runtime_event(
                        str(e),
                        event_kind="tool_error",
                        event_data={"tool_name": name},
                    )
                except Exception:
                    pass
            return f"Error executing {name}: {str(e)}" + _hint

    def to_langchain_tools(self) -> list[BaseTool]:
        """Convert registered tools to official BaseTool instances."""
        tools: list[BaseTool] = []
        runtime_context = dict(self._runtime_context.get() or {})
        for tool in self.list_tools():
            args_schema = self._build_args_schema(tool)
            coroutine = self._build_tool_coroutine(tool.name, runtime_context_snapshot=runtime_context)
            tools.append(
                attach_raw_parameters_schema(
                    StructuredTool.from_function(
                        coroutine=coroutine,
                        name=tool.name,
                        description=tool.description,
                        args_schema=args_schema,
                        infer_schema=False,
                    ),
                    tool.parameters,
                )
            )
        tools.extend(self._langchain_tools.values())
        return tools

    def to_langchain_tools_filtered(self, allowed_names: list[str] | set[str]) -> list[BaseTool]:
        """Convert only the selected registered tools to official BaseTool instances."""
        visible = {str(name or '').strip() for name in (allowed_names or []) if str(name or '').strip()}
        tools: list[BaseTool] = []
        runtime_context = dict(self._runtime_context.get() or {})
        for tool in self.list_tools():
            if tool.name not in visible:
                continue
            args_schema = self._build_args_schema(tool)
            coroutine = self._build_tool_coroutine(tool.name, runtime_context_snapshot=runtime_context)
            tools.append(
                attach_raw_parameters_schema(
                    StructuredTool.from_function(
                        coroutine=coroutine,
                        name=tool.name,
                        description=tool.description,
                        args_schema=args_schema,
                        infer_schema=False,
                    ),
                    tool.parameters,
                )
            )
        for name, tool in self._langchain_tools.items():
            if name in visible:
                tools.append(tool)
        return tools

    def _build_tool_coroutine(self, tool_name: str, *, runtime_context_snapshot: dict[str, Any] | None = None):
        captured_runtime = dict(runtime_context_snapshot or {})

        async def _invoke(**kwargs: Any) -> Any:
            token: contextvars.Token | None = None
            if captured_runtime:
                current_runtime = self._runtime_context.get() or {}
                merged_runtime = dict(captured_runtime)
                merged_runtime.update({key: value for key, value in current_runtime.items() if value not in (None, "")})
                if merged_runtime != current_runtime:
                    token = self._runtime_context.set(merged_runtime)
            try:
                return await self.execute(tool_name, kwargs)
            finally:
                if token is not None:
                    self._runtime_context.reset(token)

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

    async def _execute_tool_with_runtime(
        self,
        *,
        tool: Tool,
        tool_name: str,
        params: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> Any:
        execute_kwargs = dict(params)
        runtime_param_name = self._runtime_context_parameter_name(tool)
        if runtime_context and runtime_param_name is not None:
            execute_kwargs[runtime_param_name] = runtime_context

        async def _invoke() -> Any:
            resource_manager = self._resolve_resource_manager(runtime_context)
            if resource_manager is not None and resource_manager.get_tool_descriptor(tool_name) is not None:
                with resource_manager.acquire_tool(tool_name):
                    return await tool.execute(**execute_kwargs)
            return await tool.execute(**execute_kwargs)

        execution_manager = self._resolve_tool_execution_manager(runtime_context)
        if not self._should_use_watchdog(
            tool_name=tool_name,
            runtime_context=runtime_context,
            execution_manager=execution_manager,
        ):
            return await _invoke()

        outcome = await run_tool_with_watchdog(
            _invoke(),
            tool_name=tool_name,
            arguments=params,
            runtime_context=runtime_context,
            manager=execution_manager,
            on_poll=(
                (lambda poll: self._emit_watchdog_progress(tool_name=tool_name, poll=poll))
                if runtime_context.get("on_progress")
                else None
            ),
        )
        return outcome.value

    def _should_use_watchdog(
        self,
        *,
        tool_name: str,
        runtime_context: dict[str, Any],
        execution_manager: Any,
    ) -> bool:
        if not runtime_context:
            return False
        if not actor_role_allows_watchdog(runtime_context):
            return False
        if runtime_context.get("skip_tool_registry_watchdog"):
            return False
        if tool_name in _CONTROL_TOOL_NAMES:
            return False
        if execution_manager is not None:
            return True
        if callable(runtime_context.get("tool_snapshot_supplier")):
            return True
        if runtime_context.get("cancel_token") is not None:
            return True
        return isinstance(runtime_context.get("tool_watchdog"), dict)

    async def _emit_watchdog_progress(self, *, tool_name: str, poll: dict[str, Any]) -> None:
        if not isinstance(poll, dict):
            return
        snapshot = poll.get("snapshot")
        summary_text = str(snapshot.get("summary_text") or "").strip() if isinstance(snapshot, dict) else ""
        elapsed = float(poll.get("elapsed_seconds") or 0.0)
        next_handoff = float(poll.get("next_handoff_in_seconds") or 0.0)
        text = f"{tool_name} 仍在处理中，已等待 {elapsed:.0f} 秒。"
        if summary_text:
            text = f"{text} 当前看到的阶段：{summary_text}"
        else:
            text = f"{text} 暂时还没有新的阶段快照。"
        if next_handoff > 0:
            text = f"{text} 如果还没完成，我会在约 {next_handoff:.0f} 秒后把新的运行快照交回给 agent。"
        await self._emit_runtime_event(
            text,
            event_kind="tool",
            event_data={"tool_name": tool_name, "watchdog": True},
        )

    async def _emit_runtime_event(
        self,
        content: str,
        *,
        event_kind: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> None:
        runtime_context = self._runtime_context.get() or {}
        callback = runtime_context.get("on_progress")
        if not callback:
            return
        try:
            result = callback(content, event_kind=event_kind, event_data=event_data)
        except TypeError:
            result = callback(content)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _resolve_tool_execution_manager(runtime_context: dict[str, Any]) -> Any:
        manager = runtime_context.get("tool_execution_manager")
        if manager is not None:
            return manager
        loop = runtime_context.get("loop")
        if loop is not None:
            return getattr(loop, "tool_execution_manager", None)
        return None

    @staticmethod
    def _resolve_resource_manager(runtime_context: dict[str, Any]) -> Any:
        manager = runtime_context.get("resource_manager")
        if manager is not None:
            return manager
        loop = runtime_context.get("loop")
        if loop is not None:
            return getattr(loop, "resource_manager", None)
        return None

    @staticmethod
    def _stringify_result(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    @staticmethod
    def _accepts_runtime_context(tool: Tool) -> bool:
        return ToolRegistry._runtime_context_parameter_name(tool) is not None

    @staticmethod
    def _runtime_context_parameter_name(tool: Tool) -> str | None:
        sig = inspect.signature(tool.execute)
        if "__g3ku_runtime" in sig.parameters:
            return "__g3ku_runtime"
        for name in sig.parameters:
            if str(name).endswith("__g3ku_runtime"):
                return str(name)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return "__g3ku_runtime"
        return None

    @staticmethod
    def _build_args_schema(tool: Tool):
        return build_args_schema_model(tool.name, tool.parameters)

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(dict.fromkeys([*self._tools.keys(), *self._dynamic_tools.keys(), *self._langchain_tools.keys()]))

    def __len__(self) -> int:
        return len(set(self._tools.keys()) | set(self._dynamic_tools.keys()) | set(self._langchain_tools.keys()))

    def __contains__(self, name: str) -> bool:
        return name in self._tools or name in self._dynamic_tools or name in self._langchain_tools

