from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection


def _family(tool_id: str, *, description: str = "", executors: list[str] | None = None):
    return SimpleNamespace(
        tool_id=tool_id,
        display_name=tool_id,
        description=description or tool_id,
        metadata={"l0": description or tool_id, "l1": description or tool_id},
        actions=[
            SimpleNamespace(
                action_id="default",
                executor_names=list(executors or [tool_id]),
                agent_visible=True,
            )
        ],
    )


def test_selector_keeps_internal_protocol_tools_always_callable() -> None:
    result = build_execution_tool_selection(
        prompt="search frontend skills",
        goal="search frontend skills",
        core_requirement="search frontend skills",
        visible_tool_families=[
            _family("filesystem"),
            _family("content_navigation", executors=["content"]),
        ],
        visible_tool_names=[
            "filesystem",
            "content",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        schema_size_by_executor={
            "filesystem": 2900,
            "content": 1700,
            "submit_next_stage": 1473,
            "submit_final_result": 1547,
            "spawn_child_nodes": 1630,
        },
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        max_schema_chars=8000,
    )

    assert result.hydrated_tool_names[:3] == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
    ]


def test_selector_does_not_hydrate_all_visible_tools_when_budget_would_overflow() -> None:
    result = build_execution_tool_selection(
        prompt="search frontend skills",
        goal="search frontend skills",
        core_requirement="search frontend skills",
        visible_tool_families=[
            _family("filesystem"),
            _family("content_navigation", executors=["content"]),
            _family("memory", executors=["memory_write"]),
        ],
        visible_tool_names=[
            "filesystem",
            "content",
            "memory_write",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        schema_size_by_executor={
            "filesystem": 2900,
            "content": 1700,
            "memory_write": 2265,
            "submit_next_stage": 1473,
            "submit_final_result": 1547,
            "spawn_child_nodes": 1630,
        },
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        max_schema_chars=8000,
    )

    assert "memory_write" not in result.hydrated_tool_names
    assert result.schema_chars <= 8000


def test_selector_accepts_runtime_helper_dict_shape_families() -> None:
    result = build_execution_tool_selection(
        prompt="search frontend skills",
        goal="search frontend skills",
        core_requirement="search frontend skills",
        visible_tool_families=[
            {
                "tool_id": "filesystem",
                "display_name": "filesystem",
                "description": "filesystem",
                "l0": "filesystem",
                "l1": "filesystem",
                "actions": [
                    {
                        "action_id": "default",
                        "executor_names": ["filesystem"],
                    }
                ],
            },
            {
                "tool_id": "content_navigation",
                "display_name": "content_navigation",
                "description": "content_navigation",
                "l0": "content_navigation",
                "l1": "content_navigation",
                "actions": [
                    {
                        "action_id": "default",
                        "executor_names": ["content"],
                    }
                ],
            },
        ],
        visible_tool_names=[
            "filesystem",
            "content",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        schema_size_by_executor={
            "filesystem": 2900,
            "content": 1700,
            "submit_next_stage": 1473,
            "submit_final_result": 1547,
            "spawn_child_nodes": 1630,
        },
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        max_schema_chars=8000,
    )

    assert result.lightweight_tool_ids == ["filesystem", "content_navigation"]
    assert result.hydrated_tool_names == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
        "filesystem",
    ]


def test_selector_zero_schema_budget_disables_optional_hydration() -> None:
    result = build_execution_tool_selection(
        prompt="search frontend skills",
        goal="search frontend skills",
        core_requirement="search frontend skills",
        visible_tool_families=[
            _family("filesystem"),
            _family("content_navigation", executors=["content"]),
        ],
        visible_tool_names=[
            "filesystem",
            "content",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        schema_size_by_executor={
            "filesystem": 10,
            "content": 10,
            "submit_next_stage": 1,
            "submit_final_result": 1,
            "spawn_child_nodes": 1,
        },
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        max_schema_chars=0,
    )

    assert result.hydrated_tool_names == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
    ]
    assert result.schema_chars == 3
    assert result.trace["budget"] == 0
