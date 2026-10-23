from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection


def _tool_record(tool_id: str, *, description: str = ""):
    return SimpleNamespace(
        tool_id=tool_id,
        display_name=tool_id,
        description=description or tool_id,
        metadata={"l0": description or tool_id, "l1": description or tool_id},
    )


def test_selector_keeps_only_internal_protocol_and_fixed_builtin_tools_callable() -> None:
    result = build_execution_tool_selection(
        prompt="inspect repo and summarize",
        goal="inspect repo and summarize",
        core_requirement="inspect repo and summarize",
        visible_tool_items=[
            _tool_record("filesystem_write"),
            _tool_record("exec"),
        ],
        visible_tool_names=[
            "filesystem_stat",
            "submit_final_result",
            "spawn_child_nodes",
            "exec",
            "filesystem_write",
        ],
        always_callable_tool_names=[
            "filesystem_stat",
            "submit_final_result",
            "spawn_child_nodes",
            "exec",
        ],
    )

    assert result.callable_tool_names == [
        "filesystem_stat",
        "submit_final_result",
        "spawn_child_nodes",
        "exec",
    ]
    assert result.candidate_tool_names == ["filesystem_write"]


def test_selector_keeps_contract_absent_tools_out_of_callable() -> None:
    result = build_execution_tool_selection(
        prompt="write files and inspect memory",
        goal="write files and inspect memory",
        core_requirement="write files and inspect memory",
        visible_tool_items=[
            _tool_record("filesystem_write"),
            _tool_record("filesystem_edit"),
            _tool_record("memory_note"),
            _tool_record("web_fetch"),
        ],
        visible_tool_names=[
            "filesystem_stat",
            "submit_final_result",
            "spawn_child_nodes",
            "filesystem_write",
            "filesystem_edit",
            "memory_note",
            "web_fetch",
        ],
        always_callable_tool_names=[
            "filesystem_stat",
            "submit_final_result",
            "spawn_child_nodes",
        ],
    )

    assert result.callable_tool_names == [
        "filesystem_stat",
        "submit_final_result",
        "spawn_child_nodes",
    ]
    assert result.candidate_tool_names == [
        "filesystem_write",
        "filesystem_edit",
        "memory_note",
        "web_fetch",
    ]


def test_selector_contract_present_tools_are_callable() -> None:
    result = build_execution_tool_selection(
        prompt="apply patch and inspect docs",
        goal="apply patch and inspect docs",
        core_requirement="apply patch and inspect docs",
        visible_tool_items=[
            _tool_record("filesystem_propose_patch"),
            _tool_record("content_open"),
            _tool_record("memory_note"),
        ],
        visible_tool_names=[
            "filesystem_stat",
            "submit_final_result",
            "spawn_child_nodes",
            "filesystem_propose_patch",
            "content_open",
            "memory_note",
        ],
        always_callable_tool_names=[
            "filesystem_stat",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        contract_present_tool_names=["filesystem_propose_patch", "content_open"],
    )

    assert result.callable_tool_names == [
        "filesystem_stat",
        "submit_final_result",
        "spawn_child_nodes",
        "filesystem_propose_patch",
        "content_open",
    ]
    assert result.candidate_tool_names == ["memory_note"]
    assert result.trace["selected_contract_present_tool_names"] == [
        "filesystem_propose_patch",
        "content_open",
    ]


def test_selector_accepts_runtime_helper_dict_shape_tools() -> None:
    result = build_execution_tool_selection(
        prompt="inspect repo",
        goal="inspect repo",
        core_requirement="inspect repo",
        visible_tool_items=[
            {
                "tool_id": "filesystem_write",
                "display_name": "filesystem_write",
                "description": "filesystem_write",
                "l0": "filesystem_write",
                "l1": "filesystem_write",
            },
            {
                "tool_id": "exec",
                "display_name": "exec",
                "description": "exec",
                "l0": "exec",
                "l1": "exec",
            },
        ],
        visible_tool_names=[
            "filesystem_stat",
            "submit_final_result",
            "spawn_child_nodes",
            "exec",
            "filesystem_write",
        ],
        always_callable_tool_names=[
            "filesystem_stat",
            "submit_final_result",
            "spawn_child_nodes",
            "exec",
        ],
    )

    assert result.lightweight_tool_ids == ["filesystem_write", "exec"]
    assert result.callable_tool_names == [
        "filesystem_stat",
        "submit_final_result",
        "spawn_child_nodes",
        "exec",
    ]
    assert result.candidate_tool_names == ["filesystem_write"]
