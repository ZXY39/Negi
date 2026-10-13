from __future__ import annotations

import importlib
import json
from pathlib import Path

from g3ku.config.schema import MemoryToolsConfig


def _memory_tool_names() -> list[str]:
    visibility = importlib.import_module("g3ku.runtime.tool_visibility")
    return list(visibility.CEO_FIXED_BUILTIN_TOOL_NAMES)


def test_ceo_has_one_memory_entry_point_and_keeps_the_load_free_note_reader() -> None:
    assert "memory_update" in _memory_tool_names()
    assert "memory_note" in _memory_tool_names()
    assert "memory_write" not in _memory_tool_names()
    assert "memory_delete" not in _memory_tool_names()


def test_core_tools_default_roster_matches_the_tool_face() -> None:
    core = MemoryToolsConfig().assembly.core_tools

    assert "memory_update" in core
    assert "memory_note" in core
    assert "memory_write" not in core
    assert "memory_delete" not in core


def test_memory_tools_stay_exempt_from_the_active_stage_gate() -> None:
    ops = importlib.import_module("g3ku.runtime.frontdoor._ceo_runtime_ops")

    assert ops.FRONTDOOR_STAGELESS_MEMORY_TOOL_NAMES == frozenset({"memory_update", "memory_note"})


def test_memory_note_result_still_inlines_without_opening_a_window() -> None:
    navigation = importlib.import_module("g3ku.content.navigation")

    assert "tool_result:memory_note" in navigation._ALWAYS_INLINE_TOOL_RESULT_SOURCES


async def test_a_forget_request_enters_through_the_same_write_lane_as_a_remember_one(
    tmp_path: Path,
) -> None:
    """The collapsed entry point must not guess intent at enqueue time: a forget request
    is an ordinary `write` row, and the memory agent is the one that turns it into
    deletions."""
    module = importlib.import_module("g3ku.agent.memory_agent_runtime")
    tools = importlib.import_module("g3ku.agent.tools.memory_update")
    manager = module.MemoryManager(tmp_path, MemoryToolsConfig())
    tool = tools.MemoryUpdateTool(manager=manager)
    try:
        remember = json.loads(
            await tool.execute(
                content="以后回复先给结论",
                __g3ku_runtime={"session_key": "web:test"},
            )
        )
        forget = json.loads(
            await tool.execute(
                content="忘掉我喜欢简洁回答这条偏好",
                __g3ku_runtime={"session_key": "web:test"},
            )
        )

        assert remember["status"] == "queued"
        assert forget["status"] == "queued"
        rows = manager._read_queue_requests()
        assert [row.op for row in rows] == ["write", "write"]
        assert [row.trigger_source for row in rows] == ["memory_update_tool"] * 2
        assert rows[1].payload_text == "忘掉我喜欢简洁回答这条偏好"
    finally:
        manager.close()
