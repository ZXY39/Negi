from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.main_runtime import LoadToolContextTool

RUNTIME_CONTRACT = {
    "tool_contract_enforced": True,
    "candidate_tool_names": ["already-visible-tool"],
    "rbac_visible_tool_names": ["load_tool_context", "already-visible-tool"],
    "actor_role": "ceo",
    "session_key": "web:shared",
}


def _tool_record(tool_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        tool_id=tool_id,
        display_name=tool_id,
        description=f"{tool_id} description",
    )


class _StubService:
    def __init__(
        self,
        *,
        live_visible: list[SimpleNamespace],
        payload: dict,
    ) -> None:
        self._live_visible = list(live_visible)
        self._payload = payload
        self.startup_calls = 0
        self.visible_calls: list[dict] = []
        self.v2_calls: list[dict] = []

    async def startup(self) -> None:
        self.startup_calls += 1

    def list_visible_tools(self, *, actor_role: str, session_id: str):
        self.visible_calls.append({"actor_role": actor_role, "session_id": session_id})
        return list(self._live_visible)

    def load_tool_context_v2(self, *, actor_role: str, session_id: str, tool_id: str, **_: object) -> dict:
        self.v2_calls.append(
            {"actor_role": actor_role, "session_id": session_id, "tool_id": tool_id}
        )
        return dict(self._payload)


def _make_tool(service: _StubService) -> LoadToolContextTool:
    return LoadToolContextTool(lambda: service)


def _runtime_kwargs(runtime: dict | None) -> dict:
    """Mimic react_loop runtime injection: the `__g3ku_runtime` parameter is
    name-mangled inside the tool class, so resolve the real parameter name."""
    tool = LoadToolContextTool(lambda: None)
    for name in inspect.signature(tool.execute).parameters:
        if name == "__g3ku_runtime" or name.endswith("__g3ku_runtime"):
            return {name: runtime}
    return {"__g3ku_runtime": runtime}


LOAD_PAYLOAD = {
    "ok": True,
    "tool_id": "mid-turn-tool",
    "content": "# mid-turn-tool",
    "callable": True,
    "available": True,
    "hydration_targets": ["mid-turn-tool"],
}


@pytest.mark.asyncio
async def test_gate_rejects_tool_missing_from_snapshot_and_live_registry():
    service = _StubService(live_visible=[], payload=LOAD_PAYLOAD)
    tool = _make_tool(service)

    result = await tool.execute(tool_id="mid-turn-tool", **_runtime_kwargs(dict(RUNTIME_CONTRACT)))

    assert "当前运行时未将" in result
    assert "mid-turn-tool" in result
    assert service.v2_calls == []


@pytest.mark.asyncio
async def test_gate_falls_back_to_live_registry_for_mid_turn_registered_tool():
    """回合中途注册进 registry 的工具不在回合起点快照里；实时探针放行后 load 照常走
    服务端（服务端 v2 自己按治理实时可见再校验一次），投影不得把实时放行的名字按
    stale 快照清成 not_in_this_turn_candidates。"""

    payload = dict(LOAD_PAYLOAD)
    service = _StubService(
        live_visible=[_tool_record("mid-turn-tool")],
        payload=payload,
    )
    tool = _make_tool(service)

    result = await tool.execute(tool_id="mid-turn-tool", **_runtime_kwargs(dict(RUNTIME_CONTRACT)))

    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["promotion"] == "promoted_next_turn"
    assert parsed["hydration_targets"] == ["mid-turn-tool"]
    assert parsed["will_be_hydrated_next_turn"] is True
    assert service.v2_calls and service.v2_calls[0]["tool_id"] == "mid-turn-tool"


@pytest.mark.asyncio
async def test_snapshot_hit_skips_live_probe():
    payload = {"ok": True, "tool_id": "already-visible-tool", "content": "# body", "hydration_targets": ["already-visible-tool"]}
    service = _StubService(live_visible=[], payload=payload)
    tool = _make_tool(service)

    result = await tool.execute(
        tool_id="already-visible-tool", **_runtime_kwargs(dict(RUNTIME_CONTRACT))
    )

    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["promotion"] == "promoted_next_turn"
    assert service.visible_calls == []


@pytest.mark.asyncio
async def test_gate_error_preserved_when_contract_not_enforced():
    # Without an enforced contract the gate never triggers; load proceeds.
    service = _StubService(live_visible=[], payload={"ok": True, "tool_id": "any"})
    tool = _make_tool(service)

    result = await tool.execute(
        tool_id="any", **_runtime_kwargs({"tool_contract_enforced": False})
    )

    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["tool_id"] == "any"
    assert service.visible_calls == []
