from __future__ import annotations

import json
from typing import Any, Callable

from g3ku.agent.tools.base import Tool
from main.governance.tool_context import apply_runtime_tool_context_projection


def _runtime_session_key(runtime: dict[str, Any] | None) -> str:
    payload = runtime if isinstance(runtime, dict) else {}
    return str(payload.get("session_key") or "web:shared").strip() or "web:shared"


def _runtime_actor_role(runtime: dict[str, Any] | None) -> str:
    payload = runtime if isinstance(runtime, dict) else {}
    value = str(payload.get("actor_role") or "").strip().lower()
    return value or "ceo"


def _runtime_contract_enforced(runtime: dict[str, Any] | None) -> bool:
    payload = runtime if isinstance(runtime, dict) else {}
    return bool(payload.get("tool_contract_enforced"))


def _normalized_runtime_names(values: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_value in list(values or []):
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _candidate_gate_error(
    *,
    runtime: dict[str, Any] | None,
    field_name: str,
    requested_id: str,
    label: str,
) -> str:
    if not _runtime_contract_enforced(runtime):
        return ""
    payload = runtime if isinstance(runtime, dict) else {}
    raw_values = payload.get(field_name)
    if not isinstance(raw_values, list):
        return f"Error: 运行时工具合同损坏，缺失 `{field_name}`"
    candidates = _normalized_runtime_names(raw_values)
    target = str(requested_id or "").strip()
    if not target:
        return ""
    if target in set(candidates):
        return ""
    return f"Error: 当前运行时{label}未包含 `{target}`，只能加载本轮候选{label}"


def _loadable_tool_gate_error(
    *,
    runtime: dict[str, Any] | None,
    requested_id: str,
) -> str:
    if not _runtime_contract_enforced(runtime):
        return ""
    payload = runtime if isinstance(runtime, dict) else {}
    target = str(requested_id or "").strip()
    if not target:
        return ""
    candidates = set(_normalized_runtime_names(payload.get("candidate_tool_names")))
    visible = set(_normalized_runtime_names(payload.get("rbac_visible_tool_names")))
    if target in candidates or target in visible:
        return ""
    if "rbac_visible_tool_names" not in payload:
        return _candidate_gate_error(
            runtime=runtime,
            field_name="candidate_tool_names",
            requested_id=target,
            label="工具",
        )
    return (
        f"Error: 当前运行时未将 `{target}` 暴露为可加载工具；"
        "只能加载本轮候选工具或 RBAC 可见 surfaced tools"
    )


class _MainRuntimeTool(Tool):
    def __init__(self, service_getter: Callable[[], Any]):
        self._service_getter = service_getter

    async def _service(self):
        service = self._service_getter()
        await service.startup()
        return service


class LoadSkillContextTool(_MainRuntimeTool):
    @property
    def name(self) -> str:
        return "load_skill_context"

    @property
    def description(self) -> str:
        return "Load full context for a currently visible skill by exact skill_id. Do not use this tool to discover or search for skills."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string", "description": "The exact visible skill id to load."},
            },
            "required": ["skill_id"],
        }

    async def execute(
        self,
        skill_id: str,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        search_text = str(kwargs.get("search_query") or "").strip()
        if search_text:
            return json.dumps(
                {
                    "ok": False,
                    "error": "skill_search_not_allowed",
                    "message": "load_skill_context only loads a known visible skill by exact skill_id.",
                },
                ensure_ascii=False,
            )
        service = await self._service()
        skill_name = str(skill_id or "").strip()
        if not skill_name:
            return json.dumps({"ok": False, "error": "skill_id_required"}, ensure_ascii=False)
        gate_error = _candidate_gate_error(
            runtime=__g3ku_runtime,
            field_name="candidate_skill_ids",
            requested_id=skill_name,
            label="技能",
        )
        if gate_error:
            return gate_error
        if hasattr(service, "load_skill_context_v2"):
            kwargs_v2: dict[str, Any] = {
                "actor_role": _runtime_actor_role(__g3ku_runtime),
                "session_id": _runtime_session_key(__g3ku_runtime),
                "skill_id": skill_name,
            }
            payload = service.load_skill_context_v2(**kwargs_v2)
        else:
            kwargs_v1: dict[str, Any] = {
                "actor_role": _runtime_actor_role(__g3ku_runtime),
                "session_id": _runtime_session_key(__g3ku_runtime),
                "skill_id": skill_name,
            }
            payload = service.load_skill_context(**kwargs_v1)
        return json.dumps(payload, ensure_ascii=False)


class LoadToolContextTool(_MainRuntimeTool):
    @property
    def name(self) -> str:
        return "load_tool_context"

    @property
    def description(self) -> str:
        return "Load full context for a currently visible tool or registered external tool, or search visible tool candidates by natural language using search_query."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tool_id": {"type": "string", "description": "The tool id to load."},
                "search_query": {
                    "type": "string",
                    "description": "Optional natural-language query used to search visible tool candidates when tool_id is omitted.",
                },
                "limit": {"type": "integer", "description": "Optional candidate limit for search mode. Defaults to 5."},
            },
            "required": [],
        }

    async def execute(
        self,
        tool_id: str = "",
        search_query: str = "",
        limit: int | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        service = await self._service()
        tool_name = str(tool_id or "").strip()
        search_text = str(search_query or "")
        if tool_name:
            gate_error = _loadable_tool_gate_error(
                runtime=__g3ku_runtime,
                requested_id=tool_name,
            )
            if gate_error:
                return gate_error
        if hasattr(service, "load_tool_context_v2"):
            kwargs_v2: dict[str, Any] = {
                "actor_role": _runtime_actor_role(__g3ku_runtime),
                "session_id": _runtime_session_key(__g3ku_runtime),
                "tool_id": tool_name,
            }
            if search_text:
                kwargs_v2["search_query"] = search_text
                kwargs_v2["limit"] = limit
            payload = service.load_tool_context_v2(**kwargs_v2)
        else:
            kwargs_v1: dict[str, Any] = {
                "actor_role": _runtime_actor_role(__g3ku_runtime),
                "session_id": _runtime_session_key(__g3ku_runtime),
                "tool_id": tool_name,
            }
            if search_text:
                kwargs_v1["search_query"] = search_text
                kwargs_v1["limit"] = limit
            payload = service.load_tool_context(**kwargs_v1)
        if tool_name and isinstance(payload, dict) and bool(payload.get("ok")):
            payload = apply_runtime_tool_context_projection(
                payload,
                requested_tool_id=tool_name,
                runtime=__g3ku_runtime,
            )
        return json.dumps(payload, ensure_ascii=False)
