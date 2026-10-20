from __future__ import annotations

import json
from typing import Any, Callable

from g3ku.agent.tools.base import Tool
from g3ku.runtime.tool_contract_block import attach_tool_contract_block
from g3ku.runtime.tool_error_guidance import format_name_group, no_load_needed_hint
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
    # 「只能加载本轮候选」而不列候选，模型只能猜一个名字再撞一次。
    group = format_name_group(f"本轮候选{label}：", candidates)
    return f"Error: 当前运行时{label}未包含 `{target}`，只能加载本轮候选{label}" + (f"；{group}" if group else "")


def _live_registered_tool_visible(
    service: Any,
    *,
    actor_role: str,
    session_id: str,
    tool_id: str,
) -> bool:
    """Fallback probe against the live governance registry.

    The per-round lists (`candidate_tool_names` / `rbac_visible_tool_names`) are
    built from the runtime frame captured at turn start. Tools installed or
    registered mid-turn (e.g. by addtools) never enter that snapshot, so the gate
    alone would reject them until the next turn. When the gate fails we re-check
    live visibility here; RBAC and repair-required semantics stay enforced by
    `load_tool_context_v2` downstream.
    """
    target = str(tool_id or "").strip()
    if not target:
        return False
    method = getattr(service, "list_visible_tools", None)
    if not callable(method):
        return False
    try:
        records = method(actor_role=actor_role, session_id=session_id)
    except Exception:
        return False
    for record in list(records or []):
        record_id = (
            record.get("tool_id")
            if isinstance(record, dict)
            else getattr(record, "tool_id", None)
        )
        if str(record_id or "").strip() == target:
            return True
    return False


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
    candidate_list = _normalized_runtime_names(payload.get("candidate_tool_names"))
    visible_list = _normalized_runtime_names(payload.get("rbac_visible_tool_names"))
    candidates = set(candidate_list)
    visible = set(visible_list)
    if target in candidates or target in visible:
        return ""
    if "rbac_visible_tool_names" not in payload:
        return _candidate_gate_error(
            runtime=runtime,
            field_name="candidate_tool_names",
            requested_id=target,
            label="工具",
        )
    # 常驻内置工具（exec 等）根本没有 toolskill，被拒时必须先答"它不用加载"；
    # 只说"加载不了"而不列名单，模型只能换个名字再撞一次。候选集与 RBAC 可见集是
    # 两份不同的名单（前者是本轮可提升的，后者是治理放行的），所以两级名单都要给。
    parts = [f"Error: 当前运行时未将 `{target}` 暴露为可加载工具"]
    no_load = no_load_needed_hint(requested=target, actor_role=_runtime_actor_role(runtime))
    parts.append(no_load or "只能加载本轮候选工具或本角色 RBAC 可见的工具")
    text = "；".join(parts)
    tail = "；".join(
        [
            item
            for item in (
                format_name_group("本轮候选工具：", [name for name in candidate_list if name not in visible]),
                format_name_group("RBAC 可见工具：", visible_list),
            )
            if item
        ]
    )
    return text + (f"。{tail}" if tail else "")


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
        # skill 候选闸门已拆除：skill 不水合、按 id 直读，可见性由 `load_skill_context_v2`
        # 按治理实时可见集强制（已删除 / 权限不允许 / 待修复三态回执）。回合初的
        # `candidate_skill_ids` 快照只会拿 stale 名单误拒 mid-turn 授权的 skill。
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
        return "Load full context for a currently visible tool, or search visible tool candidates by natural language using search_query."

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
        live_visible_via_fallback = False
        if tool_name:
            gate_error = _loadable_tool_gate_error(
                runtime=__g3ku_runtime,
                requested_id=tool_name,
            )
            if gate_error:
                live_visible_via_fallback = _live_registered_tool_visible(
                    service,
                    actor_role=_runtime_actor_role(__g3ku_runtime),
                    session_id=_runtime_session_key(__g3ku_runtime),
                    tool_id=tool_name,
                )
                if not live_visible_via_fallback:
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
                # 门槛被实时探针放行的名字不在回合初候选快照里；不带这份名单进投影，
                # 投影会按 stale 快照把 promotion 清成 not_in_this_turn_candidates，
                # 前门提升与节点提升都读不到 hydration_targets，load 就白做了。
                live_visible_tool_names=[tool_name] if live_visible_via_fallback else None,
            )
        # 成功的 load 结果把契约块写进 `content`（在场判据只认它）；失败/搜索态没有契约
        # 可交付，`attach_tool_contract_block` 原样返回载荷，信封形状不变。
        return json.dumps(attach_tool_contract_block(payload), ensure_ascii=False)
