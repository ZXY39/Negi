from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime import web_ceo_sessions
from g3ku.session.manager import SessionManager


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
