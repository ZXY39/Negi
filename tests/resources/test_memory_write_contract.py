from __future__ import annotations

from g3ku.agent.tools.memory_write import MemoryWriteTool


def test_memory_write_schema_does_not_expose_scope() -> None:
    tool = MemoryWriteTool(manager=object())

    fact_schema = tool.parameters["properties"]["facts"]["items"]["properties"]
    required_fields = list(tool.parameters["properties"]["facts"]["items"]["required"])

    assert "scope" not in fact_schema
    assert "scope" not in required_fields
