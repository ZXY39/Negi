from __future__ import annotations

from typing import Any

from .capability_snapshot import build_capability_snapshot


class CeoExposureResolver:
    def __init__(self, *, loop) -> None:
        self._loop = loop

    async def resolve_for_actor(self, *, actor_role: str, session_id: str) -> dict[str, Any]:
        service = getattr(self._loop, 'main_task_service', None)
        if service is None:
            capability_snapshot = build_capability_snapshot(
                visible_skills=[],
                visible_tools=[],
                visible_tool_names=[],
            )
            return {
                'tool_names': [],
                'skills': [],
                'tools': [],
                'capability_snapshot': capability_snapshot,
            }
        await service.startup()
        visible_tools = service.list_visible_tools(actor_role=actor_role, session_id=session_id)
        skill_supplier = getattr(service, 'list_contract_visible_skill_resources', None)
        if callable(skill_supplier):
            visible_skills = skill_supplier(actor_role=actor_role, session_id=session_id)
        else:
            visible_skills = service.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)
        allowed_tool_names = {
            str(getattr(record, 'tool_id', '') or '').strip()
            for record in list(visible_tools or [])
            if str(getattr(record, 'tool_id', '') or '').strip()
        }
        registered = set(getattr(self._loop.tools, 'tool_names', []) or [])
        tool_names = sorted(registered & allowed_tool_names)
        capability_snapshot = build_capability_snapshot(
            visible_skills=list(visible_skills or []),
            visible_tools=list(visible_tools or []),
            visible_tool_names=tool_names,
        )
        return {
            'tool_names': tool_names,
            'skills': visible_skills,
            'tools': visible_tools,
            'capability_snapshot': capability_snapshot,
        }
