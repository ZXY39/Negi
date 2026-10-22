from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

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


def test_memory_note_resolves_the_token_the_resident_block_shows(tmp_path: Path) -> None:
    """The handle is the only token the model ever sees, so it must resolve on its own."""
    module = importlib.import_module("g3ku.agent.memory_agent_runtime")
    markdown_memory = importlib.import_module("g3ku.agent.markdown_memory")
    manager = module.MemoryManager(tmp_path, MemoryToolsConfig())
    try:
        note_path = manager.notes_dir / "note_ports.md"
        note_path.write_text("端口正文\n", encoding="utf-8")
        manager._memory_repo.create_memory(
            memory_body="改完端口要重启服务",
            minimal_memory="端口->重启",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
            note_ref="note_ports",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:01+08:00")

        snapshot = manager.snapshot_text()
        handle = markdown_memory.note_handle("note_ports")
        assert f"(note: {handle})" in snapshot
        assert "note_ports" not in snapshot

        assert manager.load_note(handle) == "端口正文\n"
        # The real file name still resolves: the writing lanes name notes by it.
        assert manager.load_note("note_ports") == "端口正文\n"
        with pytest.raises(FileNotFoundError):
            manager.load_note("n00000")

        # 读侧回实名给管理面：预览拿句柄进来，编辑要按实名提交
        assert manager.resolve_note_ref(handle) == "note_ports"
        assert manager.resolve_note_ref("note_ports") == "note_ports"
        assert manager.resolve_note_ref("n00000") == "n00000"
        assert manager.resolve_note_ref("") == ""
    finally:
        manager.close()


def test_a_note_ref_copied_straight_off_the_block_resolves(tmp_path: Path) -> None:
    """块上那行印的是 `note:<句柄>`：照抄整行的人不该被判失败——容错在读侧，不赌措辞。"""
    markdown_memory = importlib.import_module("g3ku.agent.markdown_memory")
    module = importlib.import_module("g3ku.agent.memory_agent_runtime")
    manager = module.MemoryManager(tmp_path, MemoryToolsConfig())
    try:
        notes_dir = tmp_path / "memory" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        (notes_dir / "note_ports.md").write_text("端口正文" + chr(10), encoding="utf-8")
        manager._memory_repo.create_memory(
            memory_body="改完端口要重启服务",
            minimal_memory="端口->重启",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
            note_ref="note_ports",
        )
        handle = markdown_memory.note_handle("note_ports")
        plain = manager.load_note(handle)
        assert manager.load_note("note:" + handle) == plain
        assert manager.load_note("(note: " + handle + ")") == plain
        assert manager.load_note("note_ports.md") == plain
        assert manager.load_note("note_ports" + chr(10)) == plain
        with pytest.raises(FileNotFoundError) as raised:
            manager.load_note("note:zzzzzz")
        assert "without the 'note:'" in str(raised.value)
    finally:
        manager.close()


def test_the_note_tool_describes_the_argument_without_its_label() -> None:
    """上一版的描述写的是\"照块里显示的原样传\"，实盘 3/3 都连 `note:` 一起传进来。"""
    tools = importlib.import_module("g3ku.agent.tools.memory_note")
    description = tools.MemoryNoteTool(manager=None).description
    assert "not the label" in description
    assert "exactly as shown" not in description
    assert "without the '.md' suffix" in description
