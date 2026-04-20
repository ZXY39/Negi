from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime import web_ceo_sessions
from g3ku.session.manager import SessionManager


_PARAMETER_GUIDANCE_TEMPLATE = (
    '请先调用 load_tool_context(tool_id="{tool_name}") 查看该工具的详细说明、参数契约和示例后，再重新使用该工具。'
)


class _UnionTypeTool(Tool):
    @property
    def name(self) -> str:
        return "union_type_tool"

    @property
    def description(self) -> str:
        return "tool with union-typed parameter"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {
                    "type": ["object", "string"],
                    "properties": {
                        "mode": {"type": "string"},
                    },
                    "required": ["mode"],
                }
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _BrokenValidatorTool(Tool):
    @property
    def name(self) -> str:
        return "broken_validator_tool"

    @property
    def description(self) -> str:
        return "tool whose validator crashes"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        raise AssertionError("execute should not be reached when validation crashes")

    def validate_params(self, params: dict[str, object]) -> list[str]:
        _ = params
        raise TypeError("unhashable type: 'list'")


class _NestedArrayObjectTool(Tool):
    @property
    def name(self) -> str:
        return "nested_array_object_tool"

    @property
    def description(self) -> str:
        return "tool with array<object> parameter"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                        },
                        "required": ["kind"],
                    },
                }
            },
            "required": ["items"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _InvalidParameterTool(Tool):
    @property
    def name(self) -> str:
        return "invalid_parameter_tool"

    @property
    def description(self) -> str:
        return "tool with invalid parameter payload"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        raise AssertionError("execute should not be reached when validation fails")


class _ExecuteValueErrorTool(Tool):
    @property
    def name(self) -> str:
        return "execute_value_error_tool"

    @property
    def description(self) -> str:
        return "tool that raises ValueError at execution time"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        _ = kwargs
        raise ValueError("value must be a canonical memory ref")


class _ExecuteTypeErrorTool(Tool):
    @property
    def name(self) -> str:
        return "execute_type_error_tool"

    @property
    def description(self) -> str:
        return "tool that raises TypeError at execution time"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        _ = kwargs
        raise TypeError("value must be a string scalar")


class _ExecuteRuntimeErrorTool(Tool):
    @property
    def name(self) -> str:
        return "execute_runtime_error_tool"

    @property
    def description(self) -> str:
        return "tool that raises RuntimeError at execution time"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        _ = kwargs
        raise RuntimeError("runtime execution failed")


def test_tool_validate_params_accepts_union_type_object_and_string() -> None:
    tool = _UnionTypeTool()

    assert tool.validate_params({"value": {"mode": "focus"}}) == []
    assert tool.validate_params({"value": "focus"}) == []

    errors = tool.validate_params({"value": 1})

    assert "value should be object" in errors
    assert "value should be string" in errors


@pytest.mark.asyncio
async def test_tool_registry_execute_degrades_validator_crash_to_error() -> None:
    registry = ToolRegistry()
    registry.register(_BrokenValidatorTool())

    result = await registry.execute("broken_validator_tool", {"value": "demo"})

    assert result.startswith("Error validating tool 'broken_validator_tool':")
    assert "unhashable type: 'list'" in result
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="broken_validator_tool") in result


@pytest.mark.asyncio
async def test_tool_registry_execute_appends_loader_guidance_for_invalid_parameters() -> None:
    registry = ToolRegistry()
    registry.register(_InvalidParameterTool())

    result = await registry.execute("invalid_parameter_tool", {})

    assert result.startswith("Error: Invalid parameters for tool 'invalid_parameter_tool':")
    assert "missing required value" in result
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="invalid_parameter_tool") in result


@pytest.mark.asyncio
async def test_tool_registry_execute_appends_loader_guidance_for_value_error() -> None:
    registry = ToolRegistry()
    registry.register(_ExecuteValueErrorTool())

    result = await registry.execute("execute_value_error_tool", {"value": "demo"})

    assert result.startswith("Error executing execute_value_error_tool: value must be a canonical memory ref")
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="execute_value_error_tool") in result


@pytest.mark.asyncio
async def test_tool_registry_execute_appends_loader_guidance_for_type_error() -> None:
    registry = ToolRegistry()
    registry.register(_ExecuteTypeErrorTool())

    result = await registry.execute("execute_type_error_tool", {"value": "demo"})

    assert result.startswith("Error executing execute_type_error_tool: value must be a string scalar")
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="execute_type_error_tool") in result


@pytest.mark.asyncio
async def test_tool_registry_execute_keeps_runtime_error_without_loader_guidance() -> None:
    registry = ToolRegistry()
    registry.register(_ExecuteRuntimeErrorTool())

    result = await registry.execute("execute_runtime_error_tool", {"value": "demo"})

    assert result.startswith("Error executing execute_runtime_error_tool: runtime execution failed")
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="execute_runtime_error_tool") not in result


@pytest.mark.asyncio
async def test_tool_registry_langchain_tool_normalizes_nested_array_object_arguments() -> None:
    registry = ToolRegistry()
    registry.register(_NestedArrayObjectTool())

    langchain_tool = registry.to_langchain_tools_filtered(["nested_array_object_tool"])[0]
    result = await langchain_tool.ainvoke({"items": [{"kind": "memory_write"}]})

    assert result == {"items": [{"kind": "memory_write"}]}


def test_build_last_task_memory_ignores_unverified_assistant_dispatch_claim(tmp_path) -> None:
    session = SessionManager(tmp_path).get_or_create("web:shared")
    session.add_message(
        "assistant",
        "Claude Code Haha 项目分析任务已在后台成功续跑。新任务 ID: `task:fake-123`。",
    )

    memory = web_ceo_sessions.build_last_task_memory(session)

    assert memory["task_ids"] == []


def test_build_last_task_memory_keeps_verified_assistant_task_metadata(tmp_path) -> None:
    session = SessionManager(tmp_path).get_or_create("web:shared")
    session.add_message(
        "assistant",
        "已开始处理，这个需求已转为异步任务。",
        metadata={"task_ids": ["task:real-123"], "source": "transcript"},
    )

    memory = web_ceo_sessions.build_last_task_memory(session)

    assert memory["task_ids"] == ["task:real-123"]
