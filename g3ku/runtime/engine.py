"""Concrete runtime engine for g3ku agent execution."""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.agent.tools.registry import ToolRegistry
from g3ku.bus.events import InboundMessage, OutboundMessage
from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.runtime.cancellation import ToolCancellationToken
from g3ku.runtime.frontdoor.inline_tool_reminder import CeoToolReminderService, InlineToolExecutionRegistry
from g3ku.runtime.manager import SessionRuntimeManager
from g3ku.runtime.tool_watchdog import ToolExecutionManager


class AgentRuntimeEngine:
    """Lightweight source runtime engine used by the converged session runtime."""
    _UNSET = object()

    def __init__(
        self,
        *,
        bus,
        provider,
        workspace,
        model: str | None = None,
        provider_name: str | None = None,
        max_iterations: int | None | object = _UNSET,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        resource_config=None,
        multi_agent_config=None,
        app_config: Any | None = None,
        cron_service=None,
        session_manager=None,
        channels_config=None,
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
        self.max_iterations = self._normalize_optional_limit(max_iterations, default=40)
        self.temperature = float(temperature or 0.1)
        self.max_tokens = int(max_tokens or 4096)
        self.memory_window = int(memory_window or 100)
        self.reasoning_effort = reasoning_effort
        self.resource_config = resource_config
        self.multi_agent_config = multi_agent_config
        self.app_config = app_config
        self.cron_service = cron_service
        self.channels_config = channels_config
        self.debug_mode = bool(debug_mode)
        self.debug_trace = bool(debug_mode)
        raw_prompt_trace = str(os.getenv("G3KU_PROMPT_TRACE", "")).strip().lower()
        self.prompt_trace = self.debug_trace or raw_prompt_trace in {"1", "true", "yes", "on", "debug", "log"}
        self.middlewares = list(middlewares or [])

        self._memory_manager_cls = memory_manager_cls
        self._session_manager_cls = session_manager_cls
        self._chat_model_factory = chat_model_factory

        self.tools = ToolRegistry()
        self.tool_execution_manager = ToolExecutionManager()
        self.inline_tool_execution_registry = InlineToolExecutionRegistry()
        self.ceo_tool_reminder_service = CeoToolReminderService(
            loop=self,
            registry=self.inline_tool_execution_registry,
        )
        self._active_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._session_cancellation_tokens: dict[str, set[ToolCancellationToken]] = {}
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
        self._memory_runtime_settings = None
        self._internal_tool_settings_fingerprints: dict[str, str] = {}
        self.memory_manager = None
        self.commit_service = None
        self.resource_manager = None
        self.multi_agent_role_registry = None
        self.multi_agent_runner = None
        self.blackboard_store = None
        self.dynamic_subagent_session_store = None
        self.background_task_store = None
        self.dynamic_subagent_controller = None
        self.background_pool = None
        self.main_task_service = None
        self._runtime_closed = False

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
        self._bootstrap.init_multi_agent_runtime()
        self._bootstrap.register_default_tools()

    @classmethod
    def _normalize_optional_limit(cls, value: int | None | object, *, default: int | None) -> int | None:
        if value is cls._UNSET:
            value = default
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return max(0, int(value))

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
        cfg = getattr(self, '_memory_runtime_settings', None)
        return bool(cfg and getattr(cfg, 'enabled', False) and str(getattr(cfg, 'mode', 'legacy')).lower() in {'rag', 'dual'})

    def _use_legacy_memory(self) -> bool:
        cfg = getattr(self, '_memory_runtime_settings', None)
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

    def create_session_cancellation_token(self, session_key: str) -> ToolCancellationToken:
        key = str(session_key or "").strip()
        token = ToolCancellationToken(session_key=key)
        if not key:
            return token
        bucket = self._session_cancellation_tokens.setdefault(key, set())
        bucket.add(token)
        return token

    def release_session_cancellation_token(self, session_key: str, token: ToolCancellationToken | None) -> None:
        key = str(session_key or "").strip()
        if not key or token is None:
            return
        tokens = self._session_cancellation_tokens.get(key)
        if tokens is None:
            return
        tokens.discard(token)
        if not tokens:
            self._session_cancellation_tokens.pop(key, None)

    def signal_session_cancellation(self, session_key: str, *, reason: str = "user_cancelled") -> int:
        key = str(session_key or "").strip()
        tokens = list(self._session_cancellation_tokens.get(key, set()))
        for token in tokens:
            try:
                token.cancel(reason=reason)
            except Exception:
                continue
        return len(tokens)

    async def cancel_session_tasks(self, session_key: str) -> int:
        key = str(session_key or '').strip()
        self.signal_session_cancellation(key, reason="user_cancelled")
        tasks = list(self._active_tasks.pop(key, set()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    @staticmethod
    def _sqlite_checkpointer_is_active(checkpointer: Any) -> bool:
        connection = getattr(checkpointer, "conn", None)
        if connection is None:
            return True
        try:
            _ = getattr(connection, "_conn")
        except ValueError as exc:
            if "no active connection" in str(exc).strip().lower():
                return False
            raise
        return True

    async def _reset_checkpointer_handles(self) -> None:
        checkpointer = getattr(self, "_checkpointer", None)
        checkpointer_cm = getattr(self, "_checkpointer_cm", None)
        if checkpointer is not None or checkpointer_cm is not None:
            logger.info(
                "Closing stale SQLite checkpointer handles "
                "(checkpointer_id={}, checkpointer_cm_id={})",
                "" if checkpointer is None else str(id(checkpointer)),
                "" if checkpointer_cm is None else str(id(checkpointer_cm)),
            )
        if checkpointer is not None and hasattr(checkpointer, "close"):
            try:
                maybe = checkpointer.close()
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                logger.debug("Stale checkpointer close skipped during rebuild")
        if checkpointer_cm is not None and hasattr(checkpointer_cm, "__aexit__"):
            try:
                maybe = checkpointer_cm.__aexit__(None, None, None)
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                logger.debug("Stale checkpointer context close skipped during rebuild")
        self._checkpointer = None
        self._checkpointer_cm = None

    async def _ensure_checkpointer_ready(self) -> None:
        if not self._checkpointer_enabled:
            return None
        backend = str(getattr(self, '_checkpointer_backend', 'disabled') or 'disabled').lower()
        if backend != 'sqlite' or not self._checkpointer_path:
            return None

        async with self._checkpointer_lock:
            if not self._checkpointer_enabled:
                return None
            if self._checkpointer is not None:
                if self._sqlite_checkpointer_is_active(self._checkpointer):
                    return None
                logger.warning(
                    'SQLite checkpointer connection inactive; rebuilding at {}',
                    self._checkpointer_path,
                )
                await self._reset_checkpointer_handles()
            try:
                from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                cp_path = Path(self._checkpointer_path)
                cp_path.parent.mkdir(parents=True, exist_ok=True)
                self._checkpointer_cm = AsyncSqliteSaver.from_conn_string(str(cp_path))
                self._checkpointer = await self._checkpointer_cm.__aenter__()
                setup = getattr(self._checkpointer, 'setup', None)
                if setup is not None:
                    maybe = setup()
                    if inspect.isawaitable(maybe):
                        await maybe
                logger.info('SQLite checkpointer ready at {}', cp_path)
            except Exception as exc:
                logger.warning(
                    'SQLite checkpointer bootstrap failed; fallback to session-file history: {}',
                    exc,
                )
                cm = self._checkpointer_cm
                self._checkpointer = None
                self._checkpointer_cm = None
                self._checkpointer_path = None
                self._checkpointer_backend = 'disabled'
                self._checkpointer_enabled = False
                if cm is not None and hasattr(cm, '__aexit__'):
                    try:
                        maybe = cm.__aexit__(None, None, None)
                        if inspect.isawaitable(maybe):
                            await maybe
                    except Exception:
                        logger.debug('Checkpointer cleanup skipped after bootstrap failure')
        return None

    async def _connect_mcp(self) -> None:
        return None

    async def close_mcp(self) -> None:
        if self._runtime_closed:
            return None
        self._runtime_closed = True
        active_task_sessions = sorted(
            str(key or "").strip()
            for key in getattr(self, "_active_tasks", {}).keys()
            if str(key or "").strip()
        ) if isinstance(getattr(self, "_active_tasks", None), dict) else []
        checkpointer = getattr(self, "_checkpointer", None)
        is_active = getattr(self, "_sqlite_checkpointer_is_active", None)
        checkpointer_active = "unknown"
        if callable(is_active) and checkpointer is not None:
            try:
                checkpointer_active = str(bool(is_active(checkpointer)))
            except Exception:
                checkpointer_active = "error"
        if active_task_sessions:
            logger.warning(
                "Closing runtime while active sessions still exist "
                "(active_task_sessions={}, checkpointer_id={}, checkpointer_active={})",
                ",".join(active_task_sessions),
                "" if checkpointer is None else str(id(checkpointer)),
                checkpointer_active,
            )
        else:
            logger.info(
                "Closing runtime with no active sessions "
                "(active_task_sessions={}, checkpointer_id={}, checkpointer_active={})",
                "",
                "" if checkpointer is None else str(id(checkpointer)),
                checkpointer_active,
            )

        for task_set in (self._consolidation_tasks, self._commit_tasks):
            tasks = list(task_set)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            task_set.clear()

        pool = getattr(self, 'background_pool', None)
        if pool is not None and hasattr(pool, 'close'):
            try:
                await pool.close()
            except Exception:
                logger.debug('Background pool close skipped during runtime shutdown')

        main_task_service = getattr(self, 'main_task_service', None)
        if main_task_service is not None:
            try:
                await main_task_service.close()
            except Exception:
                logger.debug('main task service close skipped during runtime shutdown')

        memory_manager = getattr(self, 'memory_manager', None)
        if memory_manager is not None:
            try:
                memory_manager.close()
            except Exception:
                logger.debug('Memory manager close skipped during runtime shutdown')

        checkpointer = getattr(self, '_checkpointer', None)
        if checkpointer is not None and hasattr(checkpointer, 'close'):
            try:
                maybe = checkpointer.close()
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                logger.debug('Checkpointer close skipped during runtime shutdown')
        checkpointer_cm = getattr(self, '_checkpointer_cm', None)
        if checkpointer_cm is not None and hasattr(checkpointer_cm, '__aexit__'):
            try:
                maybe = checkpointer_cm.__aexit__(None, None, None)
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                logger.debug('Checkpointer context close skipped during runtime shutdown')
        self._checkpointer = None
        self._checkpointer_cm = None
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

    def stop(self) -> None:
        return None


__all__ = ['AgentRuntimeEngine']

