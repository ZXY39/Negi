from __future__ import annotations

from typing import Any

from langchain.agents import create_agent

from g3ku.agent.chatmodel_utils import ensure_chat_model
from g3ku.integrations.langchain_runtime import extract_final_response
from g3ku.runtime.frontdoor.exposure_resolver import CeoExposureResolver
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder


class CeoFrontDoorRunner:
    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._resolver = CeoExposureResolver(loop=loop)
        self._prompt_builder = CeoPromptBuilder(loop=loop)

    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        await self._loop._ensure_checkpointer_ready()
        org_service = getattr(self._loop, 'org_graph_service', None)
        if org_service is not None:
            await org_service.startup()
        for name in ('message', 'cron'):
            tool = self._loop.tools.get(name)
            if tool is not None and hasattr(tool, 'set_context'):
                tool.set_context(getattr(session, '_channel', 'cli'), getattr(session, '_chat_id', session.state.session_key), None) if name == 'message' else tool.set_context(getattr(session, '_channel', 'cli'), getattr(session, '_chat_id', session.state.session_key))
        message_tool = self._loop.tools.get('message')
        if message_tool is not None and hasattr(message_tool, 'start_turn'):
            message_tool.start_turn()
        exposure = await self._resolver.resolve_for_actor(actor_role='ceo', session_id=session.state.session_key)
        system_prompt = self._prompt_builder.build(skills=list(exposure.get('skills') or []))
        tool_names = list(exposure.get('tool_names') or [])
        tools = self._loop.tools.to_langchain_tools_filtered(tool_names)
        model_client = getattr(self._loop, 'model_client', None)
        if model_client is None:
            model_client = ensure_chat_model(
                self._loop.provider,
                default_model=str(getattr(self._loop, 'model', '') or ''),
                default_temperature=float(getattr(self._loop, 'temperature', 0.2) or 0.2),
                default_max_tokens=int(getattr(self._loop, 'max_tokens', 4096) or 4096),
                default_reasoning_effort=getattr(self._loop, 'reasoning_effort', None),
            )
        agent = create_agent(
            model=model_client,
            tools=tools,
            checkpointer=getattr(self._loop, '_checkpointer', None),
            store=getattr(self._loop, '_store', None),
            name='g3ku_ceo_frontdoor',
        )
        config: dict[str, Any] = {
            'recursion_limit': max(8, int(getattr(self._loop, 'max_iterations', 12) or 12) * 2 + 4),
            'configurable': {'thread_id': session.state.session_key},
        }
        messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': str(user_input.content or '')},
        ]
        token = self._loop.tools.push_runtime_context(
            {
                'on_progress': on_progress,
                'emit_lifecycle': True,
                'session_key': session.state.session_key,
                'channel': getattr(session, '_channel', 'cli'),
                'chat_id': getattr(session, '_chat_id', session.state.session_key),
                'temp_dir': str(getattr(self._loop, 'temp_dir', '') or ''),
                'loop': self._loop,
            }
        )
        try:
            result = await agent.ainvoke({'messages': messages}, config=config)
        finally:
            self._loop.tools.pop_runtime_context(token)
        result_messages = list(result.get('messages') or [])
        final = extract_final_response(result_messages)
        output = final.content if final and final.content else ''
        used_tools = [message.name for message in result_messages if getattr(message, 'name', None)]
        route_kind = 'direct_reply'
        if 'orggraph_create_project' in used_tools:
            route_kind = 'project_dispatch'
        elif used_tools:
            route_kind = 'self_execute'
        setattr(session, '_last_route_kind', route_kind)
        return output
