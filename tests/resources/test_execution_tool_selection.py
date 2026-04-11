from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection


def _family(tool_id: str, *, description: str = "", executors: list[str] | None = None):
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
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
    )

    assert result.hydrated_tool_names[:3] == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
    ]


def test_selector_semantic_frontload_prefers_web_fetch_over_memory_search_for_web_research_query() -> None:
    result = build_execution_tool_selection(
        prompt="search the web for official source URLs and ranking pages",
        goal="collect public web sources for character rankings",
        core_requirement="use public web pages and source URLs for ranking research",
        visible_tool_families=[
            _family("memory", executors=["memory_search"]),
            _family("web_fetch"),
            _family("agent_browser"),
        ],
        visible_tool_names=[
            "memory_search",
            "web_fetch",
            "agent_browser",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        schema_size_by_executor={
            "memory_search": 100,
            "web_fetch": 100,
            "agent_browser": 100,
        },
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
    )

    assert result.hydrated_tool_names[:3] == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
    ]
    assert result.hydrated_tool_names.index("web_fetch") < result.hydrated_tool_names.index("memory_search")


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
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
    )

    assert result.lightweight_tool_ids == ["filesystem", "content_navigation"]
    assert result.hydrated_tool_names[:3] == [
        "submit_next_stage",
        "submit_final_result",
        "spawn_child_nodes",
    ]
    assert "filesystem" in result.hydrated_tool_names
    assert "content" in result.hydrated_tool_names


def test_selector_multi_per_family_exposes_multiple_related_executors() -> None:
    result = build_execution_tool_selection(
        prompt="open content and search content for official source pages",
        goal="inspect and search content",
        core_requirement="collect source pages from public web content",
        visible_tool_families=[
            {
                "tool_id": "content_navigation",
                "display_name": "content_navigation",
                "description": "content navigation",
                "l0": "content navigation",
                "l1": "content navigation",
                "actions": [
                    {
                        "action_id": "describe",
                        "executor_names": ["content_describe", "content_search", "content_open"],
                    }
                ],
            }
        ],
        visible_tool_names=[
            "content_describe",
            "content_search",
            "content_open",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
    )

    assert "content_describe" in result.hydrated_tool_names
    assert "content_search" in result.hydrated_tool_names
    assert "content_open" in result.hydrated_tool_names


def test_selector_prefers_split_filesystem_and_content_executors() -> None:
    result = build_execution_tool_selection(
        prompt="inspect project files and read docs",
        goal="inspect project files and read docs",
        core_requirement="inspect project files and read docs",
        visible_tool_families=[
            SimpleNamespace(
                tool_id="filesystem",
                display_name="filesystem",
                description="filesystem",
                primary_executor_name="filesystem_describe",
                metadata={"l0": "filesystem", "l1": "filesystem"},
                actions=[
                    SimpleNamespace(action_id="legacy", executor_names=["filesystem"], agent_visible=True),
                    SimpleNamespace(
                        action_id="describe",
                        executor_names=["filesystem_describe", "filesystem_search", "filesystem_open"],
                        agent_visible=True,
                    ),
                ],
            ),
            SimpleNamespace(
                tool_id="content_navigation",
                display_name="content_navigation",
                description="content_navigation",
                primary_executor_name="content_describe",
                metadata={"l0": "content_navigation", "l1": "content_navigation"},
                actions=[
                    SimpleNamespace(action_id="legacy", executor_names=["content"], agent_visible=True),
                    SimpleNamespace(
                        action_id="describe",
                        executor_names=["content_describe", "content_search", "content_open"],
                        agent_visible=True,
                    ),
                ],
            ),
        ],
        visible_tool_names=[
            "filesystem",
            "filesystem_describe",
            "filesystem_search",
            "filesystem_open",
            "content",
            "content_describe",
            "content_search",
            "content_open",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
    )

    assert "filesystem" not in result.hydrated_tool_names
    assert "content" not in result.hydrated_tool_names
    assert "filesystem_describe" in result.hydrated_tool_names
    assert "filesystem_search" in result.hydrated_tool_names
    assert "filesystem_open" in result.hydrated_tool_names
    assert "content_describe" in result.hydrated_tool_names
    assert "content_search" in result.hydrated_tool_names
    assert "content_open" in result.hydrated_tool_names


def test_selector_replay_promotions_prefer_split_over_legacy_monoliths() -> None:
    result = build_execution_tool_selection(
        prompt="inspect project files and read docs",
        goal="inspect project files and read docs",
        core_requirement="inspect project files and read docs",
        visible_tool_families=[
            SimpleNamespace(
                tool_id="filesystem",
                display_name="filesystem",
                description="filesystem",
                primary_executor_name="filesystem_describe",
                metadata={"l0": "filesystem", "l1": "filesystem"},
                actions=[
                    SimpleNamespace(action_id="legacy", executor_names=["filesystem"], agent_visible=True),
                    SimpleNamespace(
                        action_id="describe",
                        executor_names=["filesystem_describe", "filesystem_search", "filesystem_open"],
                        agent_visible=True,
                    ),
                ],
            ),
            SimpleNamespace(
                tool_id="content_navigation",
                display_name="content_navigation",
                description="content_navigation",
                primary_executor_name="content_describe",
                metadata={"l0": "content_navigation", "l1": "content_navigation"},
                actions=[
                    SimpleNamespace(action_id="legacy", executor_names=["content"], agent_visible=True),
                    SimpleNamespace(
                        action_id="describe",
                        executor_names=["content_describe", "content_search", "content_open"],
                        agent_visible=True,
                    ),
                ],
            ),
        ],
        visible_tool_names=[
            "filesystem",
            "filesystem_describe",
            "filesystem_search",
            "filesystem_open",
            "content",
            "content_describe",
            "content_search",
            "content_open",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        promoted_tool_names=["filesystem", "content"],
    )

    assert result.trace["selected_promoted_tool_names"] == ["filesystem_describe", "content_describe"]
    assert "content_describe" in result.hydrated_tool_names
    assert "filesystem_search" in result.hydrated_tool_names
    assert "content_search" in result.hydrated_tool_names
    assert "filesystem" not in result.hydrated_tool_names
    assert "content" not in result.hydrated_tool_names


def test_selector_promoted_tools_are_not_skipped_without_budget() -> None:
    result = build_execution_tool_selection(
        prompt="inspect project files and read docs",
        goal="inspect project files and read docs",
        core_requirement="inspect project files and read docs",
        visible_tool_families=[
            _family("filesystem", executors=["filesystem_describe"]),
            _family("content_navigation", executors=["content_describe"]),
        ],
        visible_tool_names=[
            "filesystem_describe",
            "content_describe",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        promoted_tool_names=["filesystem_describe", "content_describe"],
    )

    assert result.trace["selected_promoted_tool_names"] == [
        "filesystem_describe",
        "content_describe",
    ]
    assert "filesystem_describe" in result.hydrated_tool_names
    assert "content_describe" in result.hydrated_tool_names


def test_selector_promoted_tools_do_not_consume_top_k_budget() -> None:
    result = build_execution_tool_selection(
        prompt="inspect project files and read docs",
        goal="inspect project files and read docs",
        core_requirement="inspect project files and read docs",
        visible_tool_families=[
            _family("filesystem", executors=["filesystem_open"]),
            _family("content_navigation", executors=["content_open"]),
            _family("memory", executors=["memory_search"]),
            _family("web_fetch", executors=["web_fetch"]),
        ],
        visible_tool_names=[
            "filesystem_open",
            "content_open",
            "memory_search",
            "web_fetch",
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        always_callable_tool_names=[
            "submit_next_stage",
            "submit_final_result",
            "spawn_child_nodes",
        ],
        promoted_tool_names=["filesystem_open", "content_open"],
        top_k=1,
    )

    assert result.trace["selected_promoted_tool_names"] == ["filesystem_open", "content_open"]
    assert "filesystem_open" in result.hydrated_tool_names
    assert "content_open" in result.hydrated_tool_names
    assert len(result.trace["selected_executor_scores"]) == 1
