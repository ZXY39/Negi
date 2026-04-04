from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.messages import SystemMessage

from ._ceo_support import CeoFrontDoorSupport
from .ceo_agent_middleware import CeoPromptAssemblyMiddleware, CeoToolExposureMiddleware
from .state_models import CeoPersistentState, CeoRuntimeContext


class CreateAgentCeoFrontDoorRunner(CeoFrontDoorSupport):
    def __init__(self, *, loop: Any) -> None:
        super().__init__(loop=loop)
        self._agent = None

    def build_prompt_context(self, *, state, runtime, tools) -> dict[str, str]:
        _ = runtime, tools
        summary_text = str(state.get("summary_text") or "").strip()
        system_overlay = "Use the existing CEO layered context rules."
        if summary_text:
            system_overlay = f"{system_overlay}\n\n{summary_text}"
        return {"system_overlay": system_overlay}

    def visible_langchain_tools(self, *, state, runtime) -> list[Any]:
        _ = state, runtime
        return []

    def _middleware(self) -> list[Any]:
        return [
            CeoPromptAssemblyMiddleware(runner=self),
            CeoToolExposureMiddleware(runner=self),
        ]

    def _get_agent(self):
        if self._agent is None:
            model_refs = self._resolve_ceo_model_refs()
            self._agent = create_agent(
                model=model_refs[0],
                tools=[],
                name="ceo_frontdoor",
                system_prompt=SystemMessage(content="You are the CEO frontdoor agent."),
                checkpointer=getattr(self._loop, "_checkpointer", None),
                store=getattr(self._loop, "_store", None),
                state_schema=CeoPersistentState,
                context_schema=CeoRuntimeContext,
                middleware=self._middleware(),
            )
        return self._agent
