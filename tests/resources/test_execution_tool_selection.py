from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection


def _tool_family_record(tool_id: str, *, description: str = "", executors: list[str] | None = None):
    return SimpleNamespace(
        tool_id=tool_id,
        display_name=tool_id,
        description=description or tool_id,
        primary_executor_name=(list(executors or [tool_id])[0] if list(executors or [tool_id]) else ""),
        metadata={"l0": description or tool_id, "l1": description or tool_id},
        actions=[
            SimpleNamespace(
                action_id="default",
                executor_names=list(executors or [tool_id]),
                agent_visible=True,
            )
        ],
    )


def test_selector_keeps_only_internal_protocol_and_fixed_builtin_tools_callable() -> None:
    result = build_execution_tool_selection(
        prompt="inspect repo and summarize",
        goal="inspect repo and summarize",
        core_requirement="inspect repo and summarize",
        visible_tool_families=[
            _tool_family_record("filesystem", executors=["filesystem_write"]),
            _tool_family_record("exec_runtime", executors=["exec"]),
        ],
        visible_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
            "exec",
            "filesystem_write",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
            "exec",
        ],
    )

    assert result.hydrated_tool_names == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
        "exec",
    ]
    assert result.candidate_tool_names == ["filesystem_write"]


def test_selector_does_not_promote_non_builtin_candidates_without_hydration() -> None:
    result = build_execution_tool_selection(
        prompt="write files and inspect memory",
        goal="write files and inspect memory",
        core_requirement="write files and inspect memory",
        visible_tool_families=[
            _tool_family_record("filesystem", executors=["filesystem_write", "filesystem_edit"]),
            _tool_family_record("memory", executors=["memory_search"]),
            _tool_family_record("web_fetch", executors=["web_fetch"]),
        ],
        visible_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
            "filesystem_write",
            "filesystem_edit",
            "memory_search",
            "web_fetch",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        top_k=1,
    )

    assert result.hydrated_tool_names == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
    ]
    assert result.candidate_tool_names == [
        "filesystem_write",
        "filesystem_edit",
        "memory_search",
        "web_fetch",
    ]
    assert result.trace["selected_executor_scores"] == []


def test_selector_promoted_tools_remain_concrete_and_do_not_consume_top_k_budget() -> None:
    result = build_execution_tool_selection(
        prompt="apply patch and inspect docs",
        goal="apply patch and inspect docs",
        core_requirement="apply patch and inspect docs",
        visible_tool_families=[
            _tool_family_record("filesystem", executors=["filesystem_propose_patch"]),
            _tool_family_record("content_navigation", executors=["content_open"]),
            _tool_family_record("memory", executors=["memory_search"]),
        ],
        visible_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
            "filesystem_propose_patch",
            "content_open",
            "memory_search",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        promoted_tool_names=["filesystem_propose_patch", "content_open"],
        top_k=1,
    )

    assert result.hydrated_tool_names == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
        "filesystem_propose_patch",
        "content_open",
    ]
    assert result.candidate_tool_names == ["memory_search"]
    assert result.trace["selected_promoted_tool_names"] == [
        "filesystem_propose_patch",
        "content_open",
    ]
    assert result.trace["selected_executor_scores"] == []


def test_selector_accepts_runtime_helper_dict_shape_families() -> None:
    result = build_execution_tool_selection(
        prompt="inspect repo",
        goal="inspect repo",
        core_requirement="inspect repo",
        visible_tool_families=[
            {
                "tool_id": "filesystem",
                "display_name": "filesystem",
                "description": "filesystem",
                "l0": "filesystem",
                "l1": "filesystem",
                "actions": [
                    {
                        "action_id": "write",
                        "executor_names": ["filesystem_write"],
                    }
                ],
            },
            {
                "tool_id": "exec_runtime",
                "display_name": "exec_runtime",
                "description": "exec_runtime",
                "l0": "exec_runtime",
                "l1": "exec_runtime",
                "actions": [
                    {
                        "action_id": "run",
                        "executor_names": ["exec"],
                    }
                ],
            },
        ],
        visible_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
            "exec",
            "filesystem_write",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
            "exec",
        ],
    )

    assert result.lightweight_tool_ids == ["filesystem", "exec_runtime"]
    assert result.hydrated_tool_names == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
        "exec",
    ]
    assert result.candidate_tool_names == ["filesystem_write"]
