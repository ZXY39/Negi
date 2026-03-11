"""Concrete runtime engine for g3ku agent execution."""

from __future__ import annotations

import asyncio
import inspect
import tempfile
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from g3ku.agent.tools.registry import ToolRegistry
from g3ku.bus.events import InboundMessage, OutboundMessage
from g3ku.legacy import direct_runtime as legacy_direct_runtime
from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.runtime.manager import SessionRuntimeManager


class AgentRuntimeEngine:
    """Lightweight source runtime engine used by the converged session runtime."""

    def __init__(
        self,
        *,
        bus,
        provider,
        workspace,
        model: str | None = None,
        provider_name: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config=None,
        memory_config=None,
        file_vault_config=None,
        capability_config=None,
        multi_agent_config=None,
        app_config: Any | None = None,
        cron_service=None,
        restrict_to_workspace: bool = False,
        session_manager=None,
        mcp_servers: dict | None = None,
        channels_config=None,
        picture_washing_config: dict[str, Any] | None = None,
        agent_browser_config: dict[str, Any] | None = None,
        context_builder_cls=None,
        memory_manager_cls=None,
        session_manager_cls=None,
        chat_model_factory=None,
        debug_mode: bool = False,
        middlewares: list[Any] | None = None,
    ) -> None:
        self.bus = bus
        self.provider = provider
        self.workspace = Path(workspace)
        self.model = str(model or '')
        self.provider_name = str(provider_name or '')
        self.max_iterations = int(max_iterations or 40)
        self.temperature = float(temperature or 0.1)
        self.max_tokens = int(max_tokens or 4096)
        self.memory_window = int(memory_window or 100)
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config
        self.memory_config = memory_config
        self.file_vault_config = file_vault_config
        self.capability_config = capability_config
        self.multi_agent_config = multi_agent_config
        self.app_config = app_config
        self.cron_service = cron_service
        self.restrict_to_workspace = bool(restrict_to_workspace)
        self.mcp_servers = dict(mcp_servers or {})
        self.channels_config = channels_config
        self.picture_washing_config = dict(picture_washing_config or {})
        self.agent_browser_config = dict(agent_browser_config or {})
        self.debug_mode = bool(debug_mode)
        self.debug_trace = bool(debug_mode)
        self.middlewares = list(middlewares or [])

        self._context_builder_cls = context_builder_cls
        self._memory_manager_cls = memory_manager_cls
        self._session_manager_cls = session_manager_cls
        self._chat_model_factory = chat_model_factory

        self.tools = ToolRegistry()
        self._active_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._checkpointer_lock = asyncio.Lock()
        self._session_notices: dict[str, list[dict[str, Any]]] = {}
        self._consolidating: set[str] = set()
        self._consolidation_locks: dict[str, asyncio.Lock] = {}
        self._consolidation_tasks: set[asyncio.Task[Any]] = set()
        self._commit_tasks: set[asyncio.Task[Any]] = set()
        self._checkpointer_enabled = False
        self._checkpointer_backend = 'disabled'
        self._checkpointer_path = None
        self._checkpointer = None
        self._checkpointer_cm = None
        self._store = None
        self._store_enabled = False
        self.memory_manager = None
        self.file_vault = None
        self.commit_service = None
        self.capability_registry = None
        self.capability_source_registry = None
        self.capability_index_registry = None
        self.capability_loader = None
        self.capability_installer = None
        self.capability_validator = None
        self.multi_agent_role_registry = None
        self.multi_agent_runner = None
        self.blackboard_store = None
        self.dynamic_subagent_session_store = None
        self.background_task_store = None
        self.dynamic_subagent_controller = None
        self.background_pool = None
        self.org_graph_service = None
        self.org_graph_monitor_service = None

        temp_root = self.workspace / '.g3ku' / 'tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root

        manager_cls = session_manager_cls or type(session_manager) if session_manager is not None else None
        if session_manager is not None:
            self.sessions = session_manager
        elif manager_cls is not None:
            self.sessions = manager_cls(self.workspace)
        else:
            from g3ku.session.manager import SessionManager

            self.sessions = SessionManager(self.workspace)

        if callable(chat_model_factory):
            self.model_client = chat_model_factory(
                provider,
                default_model=self.model,
                default_temperature=self.temperature,
                default_max_tokens=self.max_tokens,
                default_reasoning_effort=self.reasoning_effort,
            )
        else:
            self.model_client = None

        self._bootstrap = RuntimeBootstrapBridge(self)
        self._bootstrap.init_capability_runtime()
        self._bootstrap.init_file_vault()
        self._bootstrap.init_memory_runtime()
        self._bootstrap.init_commit_service()
        self._bootstrap.init_multi_agent_runtime()
        self._bootstrap.register_default_tools()

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _preview(value: Any, *, max_chars: int = 400) -> str:
        text = str(value or '').replace('\n', '\\n').strip()
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars]}...(truncated {len(text) - max_chars} chars)"

    def _use_rag_memory(self) -> bool:
        cfg = self.memory_config
        return bool(cfg and getattr(cfg, 'enabled', False) and str(getattr(cfg, 'mode', 'legacy')).lower() in {'rag', 'dual'})

    def _use_legacy_memory(self) -> bool:
        cfg = self.memory_config
        return bool(cfg and getattr(cfg, 'enabled', False) and str(getattr(cfg, 'mode', 'legacy')).lower() in {'legacy', 'dual'})

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        for name in ('message', 'cron'):
            tool = self.tools.get(name)
            if tool is None or not hasattr(tool, 'set_context'):
                continue
            if name == 'message':
                tool.set_context(channel, chat_id, message_id)
            else:
                tool.set_context(channel, chat_id)

    def _register_active_task(self, session_key: str, task: asyncio.Task[Any]) -> None:
        key = str(session_key or '').strip()
        if not key:
            return
        bucket = self._active_tasks.setdefault(key, set())
        bucket.add(task)

        def _cleanup(_task: asyncio.Task[Any]) -> None:
            tasks = self._active_tasks.get(key)
            if tasks is None:
                return
            tasks.discard(_task)
            if not tasks:
                self._active_tasks.pop(key, None)

        task.add_done_callback(_cleanup)

    async def cancel_session_tasks(self, session_key: str) -> int:
        key = str(session_key or '').strip()
        tasks = list(self._active_tasks.pop(key, set()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def _ensure_checkpointer_ready(self) -> None:
        return None

    async def _connect_mcp(self) -> None:
        return None

    async def close_mcp(self) -> None:
        return None

    def record_session_notice(self, session_key: str | None, *, source: str, level: str = 'warn', text: str, metadata: dict[str, Any] | None = None) -> None:
        key = str(session_key or '').strip()
        if not key:
            return
        self._session_notices.setdefault(key, []).append(
            {
                'source': str(source or 'runtime'),
                'level': str(level or 'warn'),
                'text': str(text or '').strip(),
                'metadata': dict(metadata or {}),
            }
        )

    def drain_session_notices(self, session_key: str | None) -> list[dict[str, Any]]:
        key = str(session_key or '').strip()
        if not key:
            return []
        return self._session_notices.pop(key, [])

    async def process_direct(
        self,
        content: str,
        session_key: str = 'cli:direct',
        channel: str = 'cli',
        chat_id: str = 'direct',
        on_progress=None,
    ) -> str:
        manager = SessionRuntimeManager(self)
        result = await manager.prompt(content, session_key=session_key, channel=channel, chat_id=chat_id)
        return str(result.output or '')

    async def _process_message(self, msg: InboundMessage, *, session_key: str, on_progress=None) -> OutboundMessage:
        output = await self.process_direct(
            content=msg.content,
            session_key=session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            on_progress=on_progress,
        )
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=output)

    def _transform_context(self, *, history_messages: list[Any], current_message: str, channel: str, chat_id: str, include_legacy_memory: bool = True, temp_dir: str | None = None):
        builder_cls = self._context_builder_cls
        if builder_cls is None:
            return list(history_messages)
        builder = builder_cls(self.workspace)
        from g3ku.runtime.message_adapter import agent_messages_to_dicts

        history = history_messages if history_messages and isinstance(history_messages[0], dict) else agent_messages_to_dicts(history_messages)
        return builder.build_messages(history, current_message, channel=channel, chat_id=chat_id, include_legacy_memory=include_legacy_memory, temp_dir=temp_dir)

    async def _run_agent_loop(self, messages, *, session_key: str, channel: str, chat_id: str, message_id: str | None = None):
        _ = messages, message_id
        manager = SessionRuntimeManager(self)
        result = await manager.prompt('', session_key=session_key, channel=channel, chat_id=chat_id)
        return str(result.output or ''), None, []

    def _save_turn(self, session, all_messages: list[dict[str, Any]], history_count: int, *, user_content_override: str | None = None) -> None:
        _ = history_count
        if user_content_override:
            session.add_message('user', user_content_override)
        for item in all_messages:
            if not isinstance(item, dict):
                continue
            session.add_message(str(item.get('role') or 'assistant'), str(item.get('content') or ''))

    def _save_checkpoint_turn_snapshot(self, session, *, user_content: str, assistant_content: str) -> None:
        session.add_message('user', str(user_content or ''))
        session.add_message('assistant', str(assistant_content or ''))

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        _ = session, archive_all
        return False

    def stop(self) -> None:
        return None


__all__ = ['AgentRuntimeEngine', 'legacy_direct_runtime']

