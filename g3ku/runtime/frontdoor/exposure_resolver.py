from __future__ import annotations

from typing import Any


class CeoExposureResolver:
    def __init__(self, *, loop) -> None:
        self._loop = loop

    async def resolve_for_actor(self, *, actor_role: str, session_id: str) -> dict[str, Any]:
        service = getattr(self._loop, 'main_task_service', None)
        if service is None:
            return {'tool_names': [], 'skills': []}
        await service.startup()
        visible_families = service.list_visible_tool_families(actor_role=actor_role, session_id=session_id)
        visible_skills = service.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)
        allowed_tool_names = {
            executor_name
            for family in visible_families
            for action in family.actions
            for executor_name in (action.executor_names or [])
        }
        registered = set(getattr(self._loop.tools, 'tool_names', []) or [])
        return {
            'tool_names': sorted(registered & allowed_tool_names),
            'skills': visible_skills,
        }
