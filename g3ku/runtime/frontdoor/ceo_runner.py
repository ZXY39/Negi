from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from loguru import logger

try:
    from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
    from langchain_core.messages import SystemMessage
except ModuleNotFoundError:  # pragma: no cover - optional dependency fallback
    AgentMiddleware = object  # type: ignore[assignment]
    ModelRequest = Any  # type: ignore[assignment]
    ModelResponse = Any  # type: ignore[assignment]

    class SystemMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = ""):
            self.content = content
            self.text = content

from g3ku.agent.chatmodel_utils import ensure_chat_model
from g3ku.providers.chatmodels import build_chat_model
from g3ku.runtime.ceo_async_task_guard import maybe_build_ceo_overlay
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.runtime.context import ContextAssemblyService
from g3ku.runtime.frontdoor.exposure_resolver import CeoExposureResolver
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from g3ku.runtime.project_environment import current_project_environment
from main.runtime.chat_backend import build_session_prompt_cache_key


class TemporaryCeoGuardOverlayMiddleware(AgentMiddleware):
    def __init__(self) -> None:
        self._iteration = 0

    async def awrap_model_call(self, request: ModelRequest[Any], handler) -> ModelResponse[Any]:
        self._iteration += 1
        overlay_text = maybe_build_ceo_overlay(iteration=self._iteration)
        if not overlay_text:
            return await handler(request)
        patched_messages = [SystemMessage(content=overlay_text), *list(getattr(request, 'messages', []) or [])]
        return await handler(request.override(messages=patched_messages))


class CeoFrontDoorRunner:
    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._resolver = CeoExposureResolver(loop=loop)
        self._prompt_builder = CeoPromptBuilder(loop=loop)
        self._assembly = ContextAssemblyService(loop=loop, prompt_builder=self._prompt_builder)

    @staticmethod
    def _content_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        parts.append(text)
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"image_url", "input_image"}:
                    parts.append("[image omitted]")
                    continue
                if item_type in {"file", "input_file"}:
                    filename = str(item.get("filename") or item.get("name") or "").strip()
                    parts.append(f"[file omitted: {filename}]" if filename else "[file omitted]")
                    continue
                text = item.get("text", item.get("content", ""))
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts).strip()
        return str(value or "")

    @staticmethod
    def _model_content(value: Any) -> Any:
        return value if isinstance(value, list) else str(value or '')

    @classmethod
    def _extract_visible_output(cls, messages: list[Any]) -> str:
        for message in reversed(list(messages or [])):
            message_type = str(getattr(message, 'type', '') or '').strip().lower()
            if message_type != 'ai':
                continue
            text = cls._content_text(getattr(message, 'content', ''))
            if text.strip():
                return text.strip()
        return ''

    @staticmethod
    def _empty_reply_fallback(query_text: str) -> str:
        snippet = " ".join(str(query_text or "").split()).strip()
        if len(snippet) > 32:
            snippet = f"{snippet[:29].rstrip()}..."
        if snippet:
            return f"我这边这次没有生成可展示的回复。请直接再发一次“{snippet}”，我会继续处理。"
        return "我这边这次没有生成可展示的回复。请直接再发一次你的请求，我会继续处理。"

    @staticmethod
    def _provider_model_ref(model_client: Any, model_chain: list[str]) -> str:
        default_model = str(getattr(model_client, 'default_model', '') or '').strip()
        if default_model:
            return default_model
        return str(model_chain[0] if model_chain else '').strip()

    @staticmethod
    def _session_task_defaults(session_record: Any) -> dict[str, Any]:
        metadata = getattr(session_record, 'metadata', None)
        if not isinstance(metadata, dict):
            return {}
        payload = metadata.get('task_defaults', metadata.get('taskDefaults'))
        if not isinstance(payload, dict):
            return {}
        max_depth = payload.get('max_depth', payload.get('maxDepth'))
        if max_depth in (None, ''):
            return dict(payload)
        return {
            **dict(payload),
            'max_depth': max_depth,
        }

    def _resolve_ceo_model_client(self) -> tuple[Any, list[str]]:
        refresh_loop_runtime_config(self._loop, force=False, reason="ceo_model_client")
        app_config = getattr(self._loop, 'app_config', None)
        ceo_refs: list[str] = []
        if app_config is not None:
            ceo_refs = [
                str(ref or '').strip()
                for ref in app_config.get_role_model_keys('ceo')
                if str(ref or '').strip()
            ]
        if ceo_refs:
            cache_key = tuple(ceo_refs)
            cached_key = getattr(self._loop, '_ceo_model_chain_cache_key', None)
            cached_client = getattr(self._loop, '_ceo_model_client_cache', None)
            if cached_client is not None and cached_key == cache_key:
                return cached_client, ceo_refs

            client = build_chat_model(app_config, role='ceo')
            self._loop._ceo_model_chain_cache_key = cache_key
            self._loop._ceo_model_client_cache = client
            return client, ceo_refs

        client = getattr(self._loop, 'model_client', None)
        if client is not None:
            default_ref = f"{getattr(self._loop, 'provider_name', '')}:{getattr(self._loop, 'model', '')}".strip(':')
            return client, ([default_ref] if default_ref else [])

        client = ensure_chat_model(
            self._loop.provider,
            default_model=str(getattr(self._loop, 'model', '') or ''),
            default_temperature=float(getattr(self._loop, 'temperature', 0.2) or 0.2),
            default_max_tokens=int(getattr(self._loop, 'max_tokens', 4096) or 4096),
            default_reasoning_effort=getattr(self._loop, 'reasoning_effort', None),
        )
        default_ref = f"{getattr(self._loop, 'provider_name', '')}:{getattr(self._loop, 'model', '')}".strip(':')
        return client, ([default_ref] if default_ref else [])

    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        await self._loop._ensure_checkpointer_ready()
        query_text = self._content_text(getattr(user_input, 'content', ''))
        metadata = dict(getattr(user_input, 'metadata', None) or {})
        heartbeat_internal = bool(metadata.get('heartbeat_internal'))
        persisted_history: list[dict[str, Any]] = []
        runtime_session = self._loop.sessions.get_or_create(session.state.session_key)
        persisted_session = None
        if getattr(self._loop, '_checkpointer', None) is None:
            persisted_session = runtime_session
        main_service = getattr(self._loop, 'main_task_service', None)
        if main_service is not None:
            await main_service.startup()
        memory_channel = getattr(session, '_memory_channel', getattr(session, '_channel', 'cli'))
        memory_chat_id = getattr(session, '_memory_chat_id', getattr(session, '_chat_id', session.state.session_key))
        for name in ('message', 'cron'):
            tool = self._loop.tools.get(name)
            if tool is not None and hasattr(tool, 'set_context'):
                tool.set_context(getattr(session, '_channel', 'cli'), getattr(session, '_chat_id', session.state.session_key), None) if name == 'message' else tool.set_context(getattr(session, '_channel', 'cli'), getattr(session, '_chat_id', session.state.session_key))
        message_tool = self._loop.tools.get('message')
        if message_tool is not None and hasattr(message_tool, 'start_turn'):
            message_tool.start_turn()
        exposure = await self._resolver.resolve_for_actor(actor_role='ceo', session_id=session.state.session_key)
        assembly = await self._assembly.build_for_ceo(
            session=session,
            query_text=query_text,
            exposure=exposure,
            persisted_session=persisted_session,
        )
        system_prompt = assembly.system_prompt
        persisted_history = list(assembly.recent_history or [])
        tool_names = list(assembly.tool_names or list(exposure.get('tool_names') or []))
        config: dict[str, Any] = {
            'recursion_limit': max(8, int(getattr(self._loop, 'max_iterations', 12) or 12) * 2 + 4),
            'configurable': {'thread_id': session.state.session_key},
        }
        messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
            *persisted_history,
            {'role': 'user', 'content': self._model_content(getattr(user_input, 'content', ''))},
        ]
        project_environment = current_project_environment(workspace_root=getattr(self._loop, 'workspace', None))
        session_task_defaults = self._session_task_defaults(runtime_session)
        runtime_context = {
            'on_progress': on_progress,
            'emit_lifecycle': True,
            'actor_role': 'ceo',
            'session_key': session.state.session_key,
            'channel': getattr(session, '_channel', 'cli'),
            'chat_id': getattr(session, '_chat_id', session.state.session_key),
            'memory_channel': memory_channel,
            'memory_chat_id': memory_chat_id,
            'cancel_token': getattr(session, '_active_cancel_token', None),
            'tool_snapshot_supplier': getattr(session, 'inflight_turn_snapshot', None),
            'temp_dir': str(getattr(self._loop, 'temp_dir', '') or ''),
            'loop': self._loop,
            'task_defaults': session_task_defaults,
            'project_python': str(project_environment.get('project_python') or ''),
            'project_python_dir': str(project_environment.get('project_python_dir') or ''),
            'project_scripts_dir': str(project_environment.get('project_scripts_dir') or ''),
            'project_path_entries': list(project_environment.get('project_path_entries') or []),
            'project_virtual_env': str(project_environment.get('project_virtual_env') or ''),
            'project_python_hint': str(project_environment.get('project_python_hint') or ''),
        }
        setattr(session, '_last_route_kind', 'direct_reply')
        token = self._loop.tools.push_runtime_context(runtime_context)
        try:
            tools = self._loop.tools.to_langchain_tools_filtered(tool_names)
            model_client, model_chain = self._resolve_ceo_model_client()
            provider_model = self._provider_model_ref(model_client, model_chain)
            stable_prompt_cache_key = build_session_prompt_cache_key(
                session_key=str(getattr(session.state, 'session_key', '') or ''),
                provider_model=provider_model,
                scope='ceo_frontdoor',
            )
            bound_model_client = model_client.bind(prompt_cache_key=stable_prompt_cache_key)
            agent = create_agent(
                model=bound_model_client,
                tools=tools,
                middleware=[TemporaryCeoGuardOverlayMiddleware()],
                checkpointer=getattr(self._loop, '_checkpointer', None),
                store=getattr(self._loop, '_store', None),
                name='g3ku_ceo_frontdoor',
            )
            result = await agent.ainvoke({'messages': messages}, config=config)
        finally:
            self._loop.tools.pop_runtime_context(token)
        result_messages = list(result.get('messages') or [])
        output = self._extract_visible_output(result_messages)
        used_tools = [message.name for message in result_messages if getattr(message, 'name', None)]
        route_kind = 'direct_reply'
        if 'create_async_task' in used_tools:
            route_kind = 'task_dispatch'
        elif used_tools:
            route_kind = 'self_execute'
        if not output and not heartbeat_internal:
            logger.warning(
                'ceo frontdoor produced empty visible output; session_key={} used_tools={}',
                str(getattr(session.state, 'session_key', '') or ''),
                used_tools,
            )
            output = self._empty_reply_fallback(query_text)
        setattr(session, '_last_route_kind', route_kind)
        return output
