"""删除工具不再立即打断可复用前缀。

删除会把实例从资源快照里摘掉，而 `tools[]` 是按活实例现渲的。没有快照时，下一跳的工具表
就比上一跳少一条，provider 前缀从工具表那一截整段断裂——一次删除等于让身后全部正文重付。
权限收回不会断，因为实例还在、参数表由声明侧继续供着；删除现在走同一条待遇：删除前抄一份
`to_model_schema()`，渲染侧在名字没有活实例时取回同一份字节，直到重印点把名字移出清单。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from g3ku.resources.retired_schemas import (
    clear_tool_schemas,
    forget_tool_schema,
    lookup_tool_schema,
    record_tool_schema,
)
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from main.service.runtime_service import MainRuntimeService


def _schema(name: str, *, description: str = "") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or f"{name} tool",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "target path"}},
                "required": ["path"],
            },
        },
    }


class _FakeTool:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description or f"{name} tool"
        self.parameters = _schema(name, description=description)["function"]["parameters"]

    def to_model_schema(self) -> dict:
        return _schema(self.name, description=self.description)


def _frontdoor_runner(tools: dict[str, object]):
    class _Registry:
        def get(self, name: str):
            return tools.get(str(name or "").strip())

    return create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(tools=_Registry(), main_task_service=None)
    )


def test_retired_schema_roundtrip_and_forget():
    clear_tool_schemas()
    record_tool_schema("demo_tool", _schema("demo_tool"))
    assert lookup_tool_schema("demo_tool") == _schema("demo_tool")

    # 返回副本：调用方改它不会污染快照
    payload = lookup_tool_schema("demo_tool")
    payload["function"]["name"] = "mutated"
    assert lookup_tool_schema("demo_tool")["function"]["name"] == "demo_tool"

    forget_tool_schema("demo_tool")
    assert lookup_tool_schema("demo_tool") is None
    clear_tool_schemas()


def test_frontdoor_keeps_byte_identical_tool_schema_after_instance_is_gone():
    """删除前后渲染出的 `tools[]` 必须逐字节相同——这就是"不立即断"的判据。"""
    clear_tool_schemas()
    tools = {"exec": _FakeTool("exec"), "win_mcp": _FakeTool("win_mcp")}
    runner = _frontdoor_runner(tools)
    declared = ["exec", "win_mcp"]

    before = runner._selected_tool_schemas(declared)
    assert [item["function"]["name"] for item in before] == declared

    # 删除流程：实例还在时抄一份 schema，然后实例随资源快照消失
    record_tool_schema("win_mcp", tools["win_mcp"].to_model_schema())
    tools.pop("win_mcp")

    after = runner._selected_tool_schemas(declared)
    assert after == before
    clear_tool_schemas()


def test_frontdoor_drops_the_name_when_no_snapshot_exists():
    """没留过快照（例如资源被外部卸载）就只能让名字离开清单，绝不发猜出来的 schema。"""
    clear_tool_schemas()
    tools = {"exec": _FakeTool("exec")}
    runner = _frontdoor_runner(tools)

    schemas = runner._selected_tool_schemas(["exec", "never_seen"])
    assert [item["function"]["name"] for item in schemas] == ["exec"]


def test_deleted_tool_still_named_in_denied_tools_until_repaint():
    """删除与收回在尾块上是同一行：`tools[]` 里带着参数表、这一跳调不动。"""
    clear_tool_schemas()
    tools = {"exec": _FakeTool("exec"), "win_mcp": _FakeTool("win_mcp")}
    service = SimpleNamespace(list_effective_tool_names=lambda *, actor_role, session_id: ["exec"])
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=service,
            tools=SimpleNamespace(get=lambda name: tools.get(str(name or "").strip())),
        )
    )
    state = {"provider_tool_names": ["exec", "win_mcp"], "session_key": "web:shared"}

    assert runner._frontdoor_declared_denied_tool_names_for_state(state) == ["win_mcp"]

    record_tool_schema("win_mcp", tools["win_mcp"].to_model_schema())
    tools.pop("win_mcp")

    # 实例没了、快照还在：名字仍在 `tools[]` 里，所以仍然点名
    assert runner._frontdoor_declared_denied_tool_names_for_state(state) == ["win_mcp"]

    # 快照也没了：渲不出参数表，这一行不能再提它
    clear_tool_schemas()
    assert runner._frontdoor_declared_denied_tool_names_for_state(state) == []


def test_delete_records_schema_before_the_instance_disappears(tmp_path):
    """删除流程在 rmtree 之前抄 schema，且抄的是实例给出的原始形状。"""
    clear_tool_schemas()
    tool = _FakeTool("win_mcp")
    service = object.__new__(MainRuntimeService)
    service._resource_manager = SimpleNamespace(tool_instances=lambda: {"win_mcp": tool})

    service._remember_retired_tool_schema("win_mcp")

    assert lookup_tool_schema("win_mcp") == tool.to_model_schema()
    clear_tool_schemas()


def test_node_schema_renderer_uses_retired_schema(tmp_path):
    """节点道的 `declared_denied_tool_schemas` 同样要吃快照：两条车道发同一份字节。"""
    clear_tool_schemas()
    from main.runtime.react_loop import ReActToolLoop

    tool = _FakeTool("win_mcp")
    record_tool_schema("win_mcp", tool.to_model_schema())

    selection = {
        "declared_denied_tool_schemas": {"win_mcp": lookup_tool_schema("win_mcp")},
    }
    rendered = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=["win_mcp"],
        current_tools={},
        tool_schema_selection=selection,
    )
    assert rendered == [tool.to_model_schema()]
    clear_tool_schemas()


def test_delete_async_still_removes_directory(tmp_path):
    """删除的异步车道没有因为快照而改变磁盘行为。"""
    tool_dir = tmp_path / "demo_tool"
    (tool_dir / "main").mkdir(parents=True)
    (tool_dir / "main" / "tool.py").write_text("x = 1\n", encoding="utf-8")

    service = object.__new__(MainRuntimeService)
    service._prepare_tool_resource_delete = lambda _tool_id: {
        "target_id": "demo_tool",
        "root": tool_dir,
        "paths": [tool_dir],
        "skipped_paths": [],
        "before_state": {},
    }
    service._finish_tool_resource_delete = lambda prepared, deleted_paths, *, session_id: {
        "tool_id": "demo_tool",
        "path": str(tool_dir),
        "deleted_paths": list(deleted_paths),
        "skipped_paths": [],
        "resources": {"ok": True},
    }

    async def _sync_catalog_targets(*, tool_ids=None, skill_ids=None):
        return {"catalog_synced": False}

    service._sync_catalog_targets = _sync_catalog_targets

    item = asyncio.run(service.delete_tool_resource_async("demo_tool"))
    assert not tool_dir.exists()
    assert item["deleted_paths"] == [str(tool_dir)]
