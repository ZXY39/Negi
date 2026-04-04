from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.messages import SystemMessage


class CeoPromptAssemblyMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        prompt_context = self._runner.build_prompt_context(
            state=request.state,
            runtime=request.runtime,
            tools=request.tools,
        )
        current_blocks = list((request.system_message or SystemMessage(content="")).content_blocks)
        blocks = current_blocks + [{"type": "text", "text": prompt_context["system_overlay"]}]
        return handler(request.override(system_message=SystemMessage(content=blocks)))


class CeoToolExposureMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        tools = self._runner.visible_langchain_tools(state=request.state, runtime=request.runtime)
        return handler(request.override(tools=tools))
