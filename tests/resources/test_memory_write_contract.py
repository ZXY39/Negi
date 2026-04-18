from __future__ import annotations

from g3ku.agent.tools.memory_delete import MemoryDeleteTool
from g3ku.agent.tools.memory_write import MemoryWriteTool


def test_memory_write_schema_exposes_content_only() -> None:
    tool = MemoryWriteTool(manager=object())

    assert list(tool.parameters["properties"].keys()) == ["content"]
    assert tool.parameters["required"] == ["content"]


def test_memory_delete_schema_exposes_id_or_ids() -> None:
    tool = MemoryDeleteTool(manager=object())

    assert set(tool.parameters["properties"].keys()) == {"id", "ids"}
    assert tool.parameters["anyOf"] == [{"required": ["id"]}, {"required": ["ids"]}]
