from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.session_events import SessionHeartbeatEvent, SessionHeartbeatEventQueue
from g3ku.heartbeat.session_wake import SessionHeartbeatWakeQueue
from g3ku.runtime.web_ceo_sessions import normalize_ceo_metadata, update_ceo_session_after_turn
from main.models import TaskRecord
from main.protocol import build_envelope

HEARTBEAT_OK = 'HEARTBEAT_OK'


class WebSessionHeartbeatService:
    def __init__(
        self,
        *,
        workspace: Path | str,
        agent: Any,
        runtime_manager: Any,
        main_task_service: Any,
        session_manager: Any,
    ) -> None:
        self._workspace = Path(workspace)
        self._agent = agent
        self._runtime_manager = runtime_manager
        self._main_task_service = main_task_service
        self._session_manager = session_manager
        self._events = SessionHeartbeatEventQueue()
        self._wake = SessionHeartbeatWakeQueue(handler=self._run_session)
        self._started = False
        self._start_lock = asyncio.Lock()
        self._prompt_tasks: dict[str, asyncio.Task[Any]] = {}

    @property
    def heartbeat_file(self) -> Path:
        return self._workspace / 'HEARTBEAT.md'

    async def start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            self._started = True
            for session_id in self._events.session_ids():
                self._wake.request(session_id)

    async def stop(self) -> None:
        self._started = False
        await self._wake.close()
        prompt_tasks = list(self._prompt_tasks.values())
        self._prompt_tasks.clear()
        for task in prompt_tasks:
            task.cancel()
        if prompt_tasks:
            await asyncio.gather(*prompt_tasks, return_exceptions=True)
        self._events.clear_all()

    def enqueue_task_terminal(self, task: TaskRecord) -> None:
        record = task if isinstance(task, TaskRecord) else None
        if record is None:
            return
        session_id = str(getattr(record, 'session_id', '') or '').strip()
        task_id = str(getattr(record, 'task_id', '') or '').strip()
        status = str(getattr(record, 'status', '') or '').strip().lower()
        if not session_id or not task_id or status not in {'success', 'failed'}:
            return
        event = self._events.enqueue(
            session_id=session_id,
            source='main_runtime',
            reason='task_terminal',
            dedupe_key=f'task-terminal:{task_id}:{status}',
            payload={
                'task_id': task_id,
                'session_id': session_id,
                'title': str(getattr(record, 'title', '') or task_id).strip() or task_id,
                'status': status,
                'brief_text': str(getattr(record, 'brief_text', '') or '').strip(),
                'failure_reason': str(getattr(record, 'failure_reason', '') or '').strip(),
                'finished_at': str(getattr(record, 'finished_at', '') or '').strip(),
            },
        )
        if event is None:
            return
        if self._started:
            self._wake.request(session_id)

    def clear_session(self, session_id: str) -> None:
        key = str(session_id or '').strip()
        if not key:
            return
        self._events.clear_session(key)
        self._wake.clear_session(key)
        task = self._prompt_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    def _session_exists(self, session_id: str) -> bool:
        get_path = getattr(self._session_manager, 'get_path', None)
        if not callable(get_path):
            return False
        try:
            return bool(get_path(session_id).exists())
        except Exception:
            return False

    def _read_heartbeat_text(self) -> str:
        try:
            content = self.heartbeat_file.read_text(encoding='utf-8')
        except FileNotFoundError:
            return ''
        except Exception:
            logger.debug('heartbeat file read skipped')
            return ''
        return str(content or '').strip()

    def _build_prompt(self, events: list[SessionHeartbeatEvent]) -> str:
        lines = [
            '这是一次后台心跳，不要解释内部机制。',
            f'如果不需要提醒用户，严格只回复 {HEARTBEAT_OK}。',
            '如果需要提醒，直接输出你要对当前会话用户说的话。',
        ]
        heartbeat_text = self._read_heartbeat_text()
        if heartbeat_text:
            lines.extend(['', heartbeat_text])
        lines.append('')
        lines.append('[SESSION EVENTS]')
        for event in events:
            payload = dict(event.payload or {})
            title = str(payload.get('title') or payload.get('task_id') or '任务').strip() or '任务'
            task_id = str(payload.get('task_id') or '').strip()
            status = str(payload.get('status') or '').strip().lower() or 'unknown'
            summary = str(payload.get('brief_text') or payload.get('failure_reason') or '').strip() or '无摘要'
            lines.append(f'- 任务 {title} ({task_id}) 已结束')
            lines.append(f'  状态: {status}')
            lines.append(f'  摘要: {summary}')
        return '\n'.join(lines).strip()

    def _serialize_tool_event(self, event: AgentEvent) -> dict[str, Any] | None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        tool_name = str(payload.get('tool_name') or 'tool').strip() or 'tool'
        text = str(payload.get('text') or '').strip()
        is_error = bool(payload.get('is_error'))
        if event.type == 'tool_execution_start':
            status = 'running'
        elif event.type == 'tool_execution_end':
            status = 'error' if is_error else 'success'
        else:
            return None
        return {
            'status': status,
            'tool_name': tool_name,
            'text': text,
            'timestamp': event.timestamp,
            'tool_call_id': str(payload.get('tool_call_id') or ''),
            'is_error': is_error,
            'source': 'heartbeat',
        }

    def _publish_ceo(self, session_id: str, event_type: str, data: dict[str, Any] | None = None) -> None:
        registry = getattr(self._main_task_service, 'registry', None)
        if registry is None:
            return
        registry.publish_ceo(
            session_id,
            build_envelope(
                channel='ceo',
                session_id=session_id,
                seq=registry.next_ceo_seq(session_id),
                type=event_type,
                data=data or {},
            ),
        )

    def _persist_assistant_reply(self, session_id: str, *, text: str, task_ids: list[str]) -> None:
        if not self._session_exists(session_id):
            return
        session = self._session_manager.get_or_create(session_id)
        session.add_message(
            'assistant',
            text,
            metadata={
                'source': 'heartbeat',
                'reason': 'task_terminal',
                'task_ids': list(task_ids),
            },
        )
        update_ceo_session_after_turn(session, user_text='', assistant_text=text)
        self._session_manager.save(session)

    async def _run_session(self, session_id: str) -> float | None:
        key = str(session_id or '').strip()
        if not self._started or not key:
            return None
        if not self._events.has_events(key):
            return None
        if not self._session_exists(key):
            self.clear_session(key)
            return None

        persisted_session = self._session_manager.get_or_create(key)
        normalized_metadata = normalize_ceo_metadata(getattr(persisted_session, 'metadata', None), session_key=key)
        if normalized_metadata != getattr(persisted_session, 'metadata', None):
            persisted_session.metadata = normalized_metadata
            self._session_manager.save(persisted_session)

        memory_scope = dict(normalized_metadata.get('memory_scope') or {})
        if ':' in key:
            channel, chat_id = key.split(':', 1)
        else:
            channel, chat_id = 'web', key
        session = self._runtime_manager.get_or_create(
            session_key=key,
            channel=channel or 'web',
            chat_id=chat_id or 'shared',
            memory_channel=str(memory_scope.get('channel') or 'web'),
            memory_chat_id=str(memory_scope.get('chat_id') or 'shared'),
        )
        state = getattr(session, 'state', None)
        status = str(getattr(state, 'status', '') or '').strip().lower()
        if bool(getattr(state, 'is_running', False)) or status == 'running':
            return 1.0

        events = self._events.peek(key)
        if not events:
            return None
        user_input = UserInputMessage(
            content=self._build_prompt(events),
            metadata={
                'heartbeat_internal': True,
                'heartbeat_reason': 'task_terminal',
                'heartbeat_task_ids': [str((event.payload or {}).get('task_id') or '').strip() for event in events],
            },
        )

        async def _relay(event: AgentEvent) -> None:
            if event.type == 'state_snapshot':
                state_payload = dict((event.payload or {}).get('state') or {})
                self._publish_ceo(key, 'ceo.state', {'state': state_payload, 'source': 'heartbeat'})
                return
            serialized = self._serialize_tool_event(event)
            if serialized is not None:
                self._publish_ceo(key, 'ceo.agent.tool', serialized)

        unsubscribe = session.subscribe(_relay)
        prompt_task = asyncio.create_task(session.prompt(user_input, persist_transcript=False))
        self._prompt_tasks[key] = prompt_task
        register_task = getattr(self._agent, '_register_active_task', None)
        if callable(register_task):
            register_task(key, prompt_task)
        try:
            result = await prompt_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('heartbeat session run failed for {}', key)
            self._publish_ceo(key, 'ceo.turn.discard', {'source': 'heartbeat'})
            return 10.0
        finally:
            unsubscribe()
            current = self._prompt_tasks.get(key)
            if current is prompt_task:
                self._prompt_tasks.pop(key, None)

        output = str(getattr(result, 'output', '') or '').strip()
        event_ids = {event.event_id for event in events}
        self._events.pop_many(key, event_ids=event_ids)
        if not self._session_exists(key):
            self.clear_session(key)
            return None
        if not output or output == HEARTBEAT_OK:
            self._publish_ceo(key, 'ceo.turn.discard', {'source': 'heartbeat'})
            return 0.25 if self._events.has_events(key) else None

        task_ids = [
            str((event.payload or {}).get('task_id') or '').strip()
            for event in events
            if str((event.payload or {}).get('task_id') or '').strip()
        ]
        self._persist_assistant_reply(key, text=output, task_ids=task_ids)
        self._publish_ceo(key, 'ceo.reply.final', {'text': output, 'source': 'heartbeat'})
        return 0.25 if self._events.has_events(key) else None
