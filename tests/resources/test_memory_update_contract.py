from __future__ import annotations

import json
from types import SimpleNamespace

from g3ku.agent.tools.memory_update import MemoryUpdateTool
from g3ku.runtime.frontdoor._ceo_runtime_ops import (
    CeoFrontDoorRuntimeOps,
    _provider_visible_tool_contract,
)


def test_memory_update_schema_exposes_content_only() -> None:
    tool = MemoryUpdateTool(manager=object())

    assert tool.name == "memory_update"
    assert list(tool.parameters["properties"].keys()) == ["content"]
    assert tool.parameters["required"] == ["content"]


def test_memory_update_provider_visible_schema_uses_content_only() -> None:
    tool = MemoryUpdateTool(manager=object())

    _description, schema = _provider_visible_tool_contract(tool)

    assert isinstance(schema, dict)
    assert list((schema.get("properties") or {}).keys()) == ["content"]
    assert schema.get("required") == ["content"]


def test_frontdoor_selected_tool_schemas_strip_provider_unsupported_combinators() -> None:
    runner = object.__new__(CeoFrontDoorRuntimeOps)
    runner._loop = SimpleNamespace(tools={"memory_update": MemoryUpdateTool(manager=object())})

    tool_schemas = runner._selected_tool_schemas(["memory_update"])

    assert len(tool_schemas) == 1
    parameters = dict((tool_schemas[0].get("function") or {}).get("parameters") or {})
    assert "anyOf" not in parameters
    assert "oneOf" not in parameters
    assert "allOf" not in parameters
    assert list((parameters.get("properties") or {}).keys()) == ["content"]


async def test_memory_update_enqueues_through_the_write_lane() -> None:
    """The CEO has one entry point: a forget request is a request like any other, and
    the memory agent - not this tool - decides that it becomes a deletion."""
    calls = []

    class _FakeManager:
        async def enqueue_write_request(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "request_id": "req_1", "status": "queued"}

    tool = MemoryUpdateTool(manager=_FakeManager())

    payload = json.loads(
        await tool.execute(
            content="忘掉我喜欢简洁回答这条偏好",
            __g3ku_runtime={"session_key": "cli:demo"},
        )
    )

    assert payload["status"] == "queued"
    assert calls[0]["trigger_source"] == "memory_update_tool"
    assert calls[0]["decision_source"] == "user"
    assert calls[0]["payload_text"] == "忘掉我喜欢简洁回答这条偏好"


def test_memory_update_refuses_blank_content() -> None:
    tool = MemoryUpdateTool(manager=object())

    assert tool.validate_params({"content": "   "}) == ["content must not be empty"]
