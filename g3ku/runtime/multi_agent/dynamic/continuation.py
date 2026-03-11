from __future__ import annotations

from g3ku.runtime.multi_agent.dynamic.controller import DynamicSubagentController


class ContinuationService:
    def __init__(self, controller: DynamicSubagentController) -> None:
        self._controller = controller

    async def continue_session(self, **kwargs):
        return await self._controller.continue_session(**kwargs)

