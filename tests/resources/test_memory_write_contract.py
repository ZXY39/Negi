from __future__ import annotations

from types import SimpleNamespace

from g3ku.agent.tools.memory_delete import MemoryDeleteTool
from g3ku.agent.tools.memory_write import MemoryWriteTool
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps, _provider_visible_tool_contract


def test_memory_write_schema_exposes_content_only() -> None:
    tool = MemoryWriteTool(manager=object())

    assert list(tool.parameters["properties"].keys()) == ["content"]
    assert tool.parameters["required"] == ["content"]


def test_memory_delete_schema_exposes_id_or_ids() -> None:
    tool = MemoryDeleteTool(manager=object())

    assert set(tool.parameters["properties"].keys()) == {"id", "ids"}
    assert tool.parameters["anyOf"] == [{"required": ["id"]}, {"required": ["ids"]}]


def test_memory_delete_provider_visible_schema_strips_schema_combinators() -> None:
    tool = MemoryDeleteTool(manager=object())

    _description, schema = _provider_visible_tool_contract(tool)

    assert isinstance(schema, dict)
    assert set((schema.get("properties") or {}).keys()) == {"id", "ids"}
    assert "anyOf" not in schema
    assert "oneOf" not in schema
    assert "allOf" not in schema


def test_frontdoor_selected_tool_schemas_strip_provider_unsupported_combinators() -> None:
    runner = object.__new__(CeoFrontDoorRuntimeOps)
    runner._loop = SimpleNamespace(tools={"memory_delete": MemoryDeleteTool(manager=object())})

    tool_schemas = runner._selected_tool_schemas(["memory_delete"])

    assert len(tool_schemas) == 1
    parameters = dict((tool_schemas[0].get("function") or {}).get("parameters") or {})
    assert "anyOf" not in parameters
    assert "oneOf" not in parameters
    assert "allOf" not in parameters
