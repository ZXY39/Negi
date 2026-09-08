from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.main_runtime import LoadSkillContextTool

RUNTIME_CONTRACT = {
    "tool_contract_enforced": True,
    "candidate_skill_ids": ["already-visible-skill"],
    "actor_role": "execution",
    "session_key": "web:shared",
}


def _skill_record(skill_id: str, *, available: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        skill_id=skill_id,
        description=f"{skill_id} description",
        available=available,
        enabled=True,
        metadata={"warnings": [] if available else ["missing required bins"], "errors": []},
    )


class _StubService:
    def __init__(self, *, visible_records: list[SimpleNamespace], payload: dict) -> None:
        self._visible_records = list(visible_records)
        self._payload = payload
        self.startup_calls = 0
        self.v2_calls: list[dict] = []

    async def startup(self) -> None:
        self.startup_calls += 1

    def list_contract_visible_skill_resources(self, *, actor_role: str, session_id: str):
        assert actor_role == "execution"
        assert session_id == "web:shared"
        return list(self._visible_records)

    def load_skill_context_v2(self, *, actor_role: str, session_id: str, skill_id: str) -> dict:
        self.v2_calls.append(
            {"actor_role": actor_role, "session_id": session_id, "skill_id": skill_id}
        )
        return dict(self._payload)


def _make_tool(service: _StubService) -> LoadSkillContextTool:
    return LoadSkillContextTool(lambda: service)


def _runtime_kwargs(runtime: dict | None) -> dict:
    """Mimic react_loop runtime injection: the `__g3ku_runtime` parameter is
    name-mangled inside the tool class, so resolve the real parameter name."""
    tool = LoadSkillContextTool(lambda: None)
    for name in inspect.signature(tool.execute).parameters:
        if name == "__g3ku_runtime" or name.endswith("__g3ku_runtime"):
            return {name: runtime}
    return {"__g3ku_runtime": runtime}


@pytest.mark.asyncio
async def test_gate_rejects_skill_missing_from_snapshot_and_live_registry():
    service = _StubService(visible_records=[], payload={"ok": True})
    tool = _make_tool(service)

    result = await tool.execute(skill_id="mid-run-installed", **_runtime_kwargs(dict(RUNTIME_CONTRACT)))

    assert "当前运行时技能未包含" in result
    assert "mid-run-installed" in result
    assert service.v2_calls == []


@pytest.mark.asyncio
async def test_gate_falls_back_to_live_registry_for_mid_run_installed_skill():
    payload = {"ok": True, "skill_id": "mid-run-installed", "content": "# body"}
    service = _StubService(
        visible_records=[_skill_record("mid-run-installed")],
        payload=payload,
    )
    tool = _make_tool(service)

    result = await tool.execute(skill_id="mid-run-installed", **_runtime_kwargs(dict(RUNTIME_CONTRACT)))

    assert json.loads(result) == payload
    assert service.v2_calls == [
        {
            "actor_role": "execution",
            "session_id": "web:shared",
            "skill_id": "mid-run-installed",
        }
    ]


@pytest.mark.asyncio
async def test_live_fallback_surfaces_repair_required_payload():
    repair_payload = {
        "ok": False,
        "error": "skill_repair_required",
        "skill_id": "mid-run-installed",
        "warnings": ["missing required bins"],
        "next_actions": ["Use `exec` and `filesystem_*` tools to repair the skill files or dependencies."],
    }
    service = _StubService(
        visible_records=[_skill_record("mid-run-installed", available=False)],
        payload=repair_payload,
    )
    tool = _make_tool(service)

    result = await tool.execute(skill_id="mid-run-installed", **_runtime_kwargs(dict(RUNTIME_CONTRACT)))

    assert json.loads(result) == repair_payload


@pytest.mark.asyncio
async def test_candidate_snapshot_hit_skips_live_probe():
    payload = {"ok": True, "skill_id": "already-visible-skill", "content": "# body"}
    service = _StubService(visible_records=[], payload=payload)
    tool = _make_tool(service)

    result = await tool.execute(
        skill_id="already-visible-skill", **_runtime_kwargs(dict(RUNTIME_CONTRACT))
    )

    assert json.loads(result) == payload
    assert service.v2_calls and service.v2_calls[0]["skill_id"] == "already-visible-skill"


@pytest.mark.asyncio
async def test_gate_error_preserved_when_contract_not_enforced_path_unused():
    # Without an enforced contract the gate never triggers; load proceeds.
    service = _StubService(visible_records=[], payload={"ok": True, "skill_id": "any"})
    tool = _make_tool(service)

    result = await tool.execute(
        skill_id="any", **_runtime_kwargs({"tool_contract_enforced": False})
    )

    assert json.loads(result) == {"ok": True, "skill_id": "any"}
