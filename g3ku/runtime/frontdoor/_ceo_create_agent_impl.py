from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.messages import SystemMessage
from langgraph.types import Command

from ._ceo_support import CeoFrontDoorSupport
from .ceo_agent_middleware import CeoPromptAssemblyMiddleware, CeoToolExposureMiddleware
from .state_models import (
    CeoFrontdoorInterrupted,
    CeoPendingInterrupt,
    CeoPersistentState,
    CeoRuntimeContext,
    initial_persistent_state,
)


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

    async def _ensure_ready(self) -> None:
        ensure_ready = getattr(self._loop, "_ensure_checkpointer_ready", None)
        if callable(ensure_ready):
            result = ensure_ready()
            if hasattr(result, "__await__"):
                await result

    @staticmethod
    def _thread_config(session_key: str) -> dict[str, object]:
        return {"configurable": {"thread_id": str(session_key or "").strip()}}

    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        await self._ensure_ready()
        session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        payload = initial_persistent_state(
            user_input={
                "content": getattr(user_input, "content", ""),
                "metadata": dict(getattr(user_input, "metadata", {}) or {}),
            }
        )
        payload["agent_runtime"] = "create_agent"
        graph_output = await self._get_agent().ainvoke(
            payload,
            config=self._thread_config(session_key),
            context=CeoRuntimeContext(
                loop=self._loop,
                session=session,
                session_key=session_key,
                on_progress=on_progress,
            ),
            version="v2",
        )
        interrupts = [
            CeoPendingInterrupt(
                interrupt_id=str(getattr(item, "id", "") or ""),
                value=getattr(item, "value", None),
            )
            for item in list(getattr(graph_output, "interrupts", ()) or ())
        ]
        values = dict(getattr(graph_output, "value", graph_output) or {})
        if interrupts:
            raise CeoFrontdoorInterrupted(interrupts=interrupts, values=values)
        return str(values.get("final_output") or "")

    async def resume_turn(self, *, session, resume_value, on_progress=None) -> str:
        await self._ensure_ready()
        session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        graph_output = await self._get_agent().ainvoke(
            Command(resume=resume_value),
            config=self._thread_config(session_key),
            context=CeoRuntimeContext(
                loop=self._loop,
                session=session,
                session_key=session_key,
                on_progress=on_progress,
            ),
            version="v2",
        )
        values = dict(getattr(graph_output, "value", graph_output) or {})
        return str(values.get("final_output") or "")
