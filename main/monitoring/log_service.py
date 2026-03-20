from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable

from g3ku.content import ContentNavigationService
from g3ku.content.navigation import INLINE_CHAR_LIMIT, INLINE_LINE_LIMIT
from main.models import NodeOutputEntry, NodeRecord, TaskRecord, normalize_final_acceptance_metadata
from main.monitoring.file_store import TaskFileStore
from main.monitoring.projection_service import TaskProjectionService
from main.protocol import build_envelope, now_iso
from main.token_usage import aggregate_node_token_usage, build_token_usage_from_attempts, merge_token_usage_by_model, merge_token_usage_records
from main.monitoring.tree_builder import TaskTreeBuilder


def _single_line_text(value: Any, *, max_chars: int = 120) -> str:
    text = ' '.join(str(value or '').split())
    if len(text) <= max_chars:
        return text
    return f'{text[: max_chars - 3].rstrip()}...'


class TaskLogService:
    def __init__(self, *, store, file_store: TaskFileStore, tree_builder: TaskTreeBuilder, registry=None, content_store: ContentNavigationService | None = None):
        self._store = store
        self._file_store = file_store
        self._tree_builder = tree_builder
        self._registry = registry
        self._content_store = content_store
        self._snapshot_payload_builder = None
        self._task_terminal_listeners: list[Callable[[TaskRecord], None]] = []
        self._task_locks: dict[str, threading.RLock] = {}
        self._task_locks_guard = threading.Lock()
        self._projection_service = TaskProjectionService(store=store, tree_builder=tree_builder)

    def set_snapshot_payload_builder(self, builder) -> None:
        self._snapshot_payload_builder = builder

    def ensure_task_projection(self, task_id: str) -> None:
        self._projection_service.ensure_task_projection(task_id)

    def add_task_terminal_listener(self, listener: Callable[[TaskRecord], None]) -> None:
        if callable(listener):
            self._task_terminal_listeners.append(listener)

    def _task_lock(self, task_id: str) -> threading.RLock:
        key = str(task_id or '').strip()
        with self._task_locks_guard:
            lock = self._task_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._task_locks[key] = lock
            return lock

    @staticmethod
    def _default_frame(*, node_id: str = '', depth: int = 0, node_kind: str = 'execution', phase: str = '') -> dict[str, Any]:
        return {
            'node_id': str(node_id or '').strip(),
            'depth': int(depth or 0),
            'node_kind': str(node_kind or 'execution').strip() or 'execution',
            'phase': str(phase or '').strip(),
            'messages': [],
            'pending_tool_calls': [],
            'pending_child_specs': [],
            'partial_child_results': [],
            'tool_calls': [],
            'child_pipelines': [],
            'last_error': '',
        }

    @classmethod
    def _frame_has_active_children(cls, frame: dict[str, Any]) -> bool:
        for item in list(frame.get('child_pipelines') or []):
            status = str((item or {}).get('status') or '').strip().lower()
            if status in {'queued', 'running'}:
                return True
        return False

    @classmethod
    def _frame_has_active_tools(cls, frame: dict[str, Any]) -> bool:
        for item in list(frame.get('tool_calls') or []):
            status = str((item or {}).get('status') or '').strip().lower()
            if status in {'queued', 'running'}:
                return True
        return False

    @classmethod
    def _frame_index_payload(cls, frames: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
        active_ids = sorted({str(item.get('node_id') or '') for item in frames if str(item.get('node_id') or '').strip()})
        runnable_ids = sorted(
            {
                str(item.get('node_id') or '')
                for item in frames
                if (
                    str(item.get('phase') or '') in {'before_model', 'waiting_tool_results', 'after_model'}
                    or cls._frame_has_active_tools(item)
                )
                and str(item.get('node_id') or '').strip()
            }
        )
        waiting_ids = sorted(
            {
                str(item.get('node_id') or '')
                for item in frames
                if (
                    str(item.get('phase') or '') in {'waiting_children', 'waiting_acceptance'}
                    or cls._frame_has_active_children(item)
                )
                and str(item.get('node_id') or '').strip()
            }
        )
        return active_ids, runnable_ids, waiting_ids

    def initialize_task(self, task: TaskRecord, root: NodeRecord) -> tuple[TaskRecord, NodeRecord]:
        with self._task_lock(task.task_id):
            paths = self._file_store.paths_for_task(task.task_id)
            task = task.model_copy(
                update={
                    **paths,
                    'is_unread': True,
                    'brief_text': _single_line_text(task.user_request),
                    'updated_at': now_iso(),
                }
            )
            root = root.model_copy(update={'updated_at': now_iso()})
            self._store.upsert_task(task)
            self._store.upsert_node(root)
            self.update_runtime_state(
                task.task_id,
                root_node_id=task.root_node_id,
                paused=False,
                active_node_ids=[root.node_id],
                runnable_node_ids=[root.node_id],
                waiting_node_ids=[],
                frames=[self._default_frame(node_id=root.node_id, depth=root.depth, node_kind=root.node_kind, phase='before_model')],
            )
            self.refresh_task_view(task.task_id, mark_unread=True)
            return task, root

    def create_node(self, task_id: str, node: NodeRecord) -> NodeRecord:
        with self._task_lock(task_id):
            self._store.upsert_node(node)
            self.refresh_task_view(task_id, mark_unread=True)
            task = self._store.get_task(task_id)
            if task is not None:
                self._append_task_event(
                    task=task,
                    event_type='task.node.updated',
                    data={'task_id': task_id, 'node_id': node.node_id},
                )
            return node

    def update_node_input(self, task_id: str, node_id: str, content: str) -> NodeRecord | None:
        with self._task_lock(task_id):
            text, ref = self._summarize_node_input(task_id=task_id, node_id=node_id, content=content)
            updated = self._store.update_node(
                node_id,
                lambda record: record.model_copy(update={'input': text, 'input_ref': ref, 'updated_at': now_iso()}),
            )
            self.refresh_task_view(task_id, mark_unread=True)
            task = self._store.get_task(task_id)
            if updated is not None and task is not None:
                self._append_task_event(
                    task=task,
                    event_type='task.node.updated',
                    data={'task_id': task_id, 'node_id': node_id},
                )
            return updated

    def append_node_output(
        self,
        task_id: str,
        node_id: str,
        *,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        usage_attempts: list[Any] | None = None,
        model_messages: list[dict[str, Any]] | None = None,
    ) -> NodeRecord | None:
        with self._task_lock(task_id):
            current = self._store.get_node(node_id)
            call_index = len(list(getattr(current, 'output', []) or [])) + 1 if current is not None else 1
            tracked_usage = bool(getattr(getattr(current, 'token_usage', None), 'tracked', False))
            delta_usage = None
            delta_usage_by_model: list[Any] = []
            if usage_attempts is not None and tracked_usage:
                delta_usage, delta_usage_by_model = build_token_usage_from_attempts(usage_attempts, tracked=True)

            text, ref = self._summarize_content(
                content,
                task_id=task_id,
                node_id=node_id,
                display_name=f'node-output:{node_id}:{call_index}',
                source_kind='node_output',
            )

            def _mutate(record: NodeRecord) -> NodeRecord:
                output = list(record.output)
                output.append(
                    NodeOutputEntry(
                        seq=len(output) + 1,
                        content=text,
                        content_ref=ref,
                        tool_calls=list(tool_calls or []),
                        created_at=now_iso(),
                    )
                )
                update: dict[str, Any] = {'output': output, 'updated_at': now_iso()}
                if delta_usage is not None and bool(getattr(record.token_usage, 'tracked', False)):
                    update['token_usage'] = merge_token_usage_records([record.token_usage, delta_usage], tracked=True)
                    update['token_usage_by_model'] = merge_token_usage_by_model(
                        [*list(record.token_usage_by_model or []), *delta_usage_by_model],
                        tracked=True,
                    )
                return record.model_copy(update=update)

            updated = self._store.update_node(node_id, _mutate)
            self.refresh_task_view(task_id, mark_unread=True)
            task = self._store.get_task(task_id)
            if updated is not None and task is not None:
                self._append_task_event(
                    task=task,
                    event_type='task.node.updated',
                    data={'task_id': task_id, 'node_id': node_id},
                )
                if delta_usage is not None:
                    self._append_task_event(
                        task=task,
                        event_type='task.model.call',
                        data=self._model_call_payload(
                            task_id=task_id,
                            node_id=node_id,
                            call_index=call_index,
                            model_messages=model_messages,
                            tool_calls=tool_calls,
                            delta_usage=delta_usage,
                            delta_usage_by_model=delta_usage_by_model,
                        ),
                    )
            return updated

    def update_node_check_result(self, task_id: str, node_id: str, check_result: str) -> NodeRecord | None:
        with self._task_lock(task_id):
            text, ref = self._summarize_content(
                check_result,
                task_id=task_id,
                node_id=node_id,
                display_name=f'check-result:{node_id}',
                source_kind='node_check_result',
            )
            updated = self._store.update_node(
                node_id,
                lambda record: record.model_copy(update={'check_result': text, 'check_result_ref': ref, 'updated_at': now_iso()}),
            )
            self.refresh_task_view(task_id, mark_unread=True)
            task = self._store.get_task(task_id)
            if updated is not None and task is not None:
                self._append_task_event(
                    task=task,
                    event_type='task.node.updated',
                    data={'task_id': task_id, 'node_id': node_id},
                )
            return updated

    def update_node_status(
        self,
        task_id: str,
        node_id: str,
        *,
        status: str,
        final_output: str = '',
        failure_reason: str = '',
    ) -> NodeRecord | None:
        with self._task_lock(task_id):
            final_text, final_ref = self._summarize_content(
                final_output,
                task_id=task_id,
                node_id=node_id,
                display_name=f'final-output:{node_id}',
                source_kind='node_final_output',
            )
            failure_text, _failure_ref = self._summarize_content(
                failure_reason,
                task_id=task_id,
                node_id=node_id,
                display_name=f'failure-output:{node_id}',
                source_kind='node_failure_output',
            )
            updated = self._store.update_node(
                node_id,
                lambda record: record.model_copy(
                    update={
                        'status': str(status or record.status),
                        'final_output': final_text or record.final_output,
                        'final_output_ref': final_ref or record.final_output_ref,
                        'failure_reason': failure_text or record.failure_reason,
                        'finished_at': now_iso() if status in {'success', 'failed'} else record.finished_at,
                        'updated_at': now_iso(),
                    }
                ),
            )
            self.refresh_task_view(task_id, mark_unread=True)
            task = self._store.get_task(task_id)
            if updated is not None and task is not None:
                self._append_task_event(
                    task=task,
                    event_type='task.node.updated',
                    data={'task_id': task_id, 'node_id': node_id},
                )
            return updated

    def mark_task_read(self, task_id: str) -> TaskRecord | None:
        with self._task_lock(task_id):
            return self._store.update_task(
                task_id,
                lambda task: task.model_copy(update={'is_unread': False, 'updated_at': now_iso()}),
            )

    def request_cancel(self, task_id: str) -> TaskRecord | None:
        return self.update_task_control(task_id, cancel_requested=True)

    def update_task_control(
        self,
        task_id: str,
        *,
        cancel_requested: bool | None = None,
        pause_requested: bool | None = None,
        is_paused: bool | None = None,
    ) -> TaskRecord | None:
        with self._task_lock(task_id):
            def _mutate(task: TaskRecord) -> TaskRecord:
                update: dict[str, Any] = {'updated_at': now_iso()}
                if cancel_requested is not None:
                    update['cancel_requested'] = bool(cancel_requested)
                if pause_requested is not None:
                    update['pause_requested'] = bool(pause_requested)
                if is_paused is not None:
                    update['is_paused'] = bool(is_paused)
                return task.model_copy(update=update)

            updated = self._store.update_task(task_id, _mutate)
            if updated is None:
                return None
            self.update_runtime_state(
                task_id,
                paused=bool(updated.is_paused),
                pause_requested=bool(updated.pause_requested),
                cancel_requested=bool(updated.cancel_requested),
            )
            return self.refresh_task_view(task_id, mark_unread=False) or updated

    def update_task_metadata(self, task_id: str, metadata_mutator: Callable[[dict[str, Any]], dict[str, Any]], *, mark_unread: bool = True) -> TaskRecord | None:
        with self._task_lock(task_id):
            def _mutate(task: TaskRecord) -> TaskRecord:
                metadata = metadata_mutator(dict(task.metadata or {}))
                if not isinstance(metadata, dict):
                    raise TypeError('task metadata mutator must return a dict')
                return task.model_copy(update={'metadata': metadata, 'updated_at': now_iso()})

            updated = self._store.update_task(task_id, _mutate)
            if updated is None:
                return None
            return self.refresh_task_view(task_id, mark_unread=mark_unread) or updated

    def _summarize_content(
        self,
        value: Any,
        *,
        task_id: str,
        node_id: str | None,
        display_name: str,
        source_kind: str,
        force: bool = False,
    ) -> tuple[str, str]:
        store = self._content_store
        if store is None:
            return str(value or ''), ''
        return store.summarize_for_storage(
            value,
            runtime={'task_id': task_id, 'node_id': node_id},
            display_name=display_name,
            source_kind=source_kind,
            force=force,
        )

    def _summarize_node_input(self, *, task_id: str, node_id: str, content: str) -> tuple[str, str]:
        text = str(content or '')
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return text, ''
        return self._summarize_content(
            text,
            task_id=task_id,
            node_id=node_id,
            display_name=f'node-input:{node_id}',
            source_kind='node_input',
        )

    @staticmethod
    def _model_call_payload(
        *,
        task_id: str,
        node_id: str,
        call_index: int,
        model_messages: list[dict[str, Any]] | None,
        tool_calls: list[dict[str, Any]] | None,
        delta_usage,
        delta_usage_by_model: list[Any],
    ) -> dict[str, Any]:
        message_list = list(model_messages or [])
        try:
            prepared_payload = json.dumps(message_list, ensure_ascii=False, default=str)
        except Exception:
            prepared_payload = str(message_list)
        return {
            'task_id': task_id,
            'node_id': node_id,
            'call_index': int(call_index or 0),
            'prepared_message_count': len(message_list),
            'prepared_message_chars': len(prepared_payload),
            'response_tool_call_count': len(list(tool_calls or [])),
            'delta_usage': delta_usage.model_dump(mode='json'),
            'delta_usage_by_model': [item.model_dump(mode='json') for item in list(delta_usage_by_model or [])],
        }

    def _next_output_seq(self, node_id: str) -> int:
        record = self._store.get_node(node_id)
        if record is None:
            return 1
        return len(list(record.output or [])) + 1

    def set_pause_state(self, task_id: str, *, pause_requested: bool | None = None, is_paused: bool | None = None) -> TaskRecord | None:
        return self.update_task_control(task_id, pause_requested=pause_requested, is_paused=is_paused)

    def update_node_metadata(self, node_id: str, metadata_mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> NodeRecord | None:
        record = self._store.get_node(node_id)
        if record is None:
            return None
        with self._task_lock(record.task_id):
            def _mutate(current: NodeRecord) -> NodeRecord:
                metadata = metadata_mutator(dict(current.metadata or {}))
                if not isinstance(metadata, dict):
                    raise TypeError('node metadata mutator must return a dict')
                return current.model_copy(update={'metadata': metadata, 'updated_at': now_iso()})

            updated = self._store.update_node(node_id, _mutate)
            task = self._store.get_task(record.task_id)
            if updated is not None and task is not None:
                self._append_task_event(
                    task=task,
                    event_type='task.node.updated',
                    data={'task_id': record.task_id, 'node_id': node_id},
                )
            return updated

    def ensure_node_output_externalized(self, task_id: str, node_id: str) -> NodeRecord | None:
        with self._task_lock(task_id):
            record = self._store.get_node(node_id)
            if record is None:
                return None
            final_output = str(record.final_output or '')
            if not final_output or str(record.final_output_ref or '').strip():
                return record
            if len(final_output) <= INLINE_CHAR_LIMIT and len(final_output.splitlines()) <= INLINE_LINE_LIMIT:
                return record
            text, ref = self._summarize_content(
                final_output,
                task_id=task_id,
                node_id=node_id,
                display_name=f'final-output:{node_id}',
                source_kind='node_final_output',
                force=True,
            )
            if not ref:
                return record
            updated = self._store.update_node(
                node_id,
                lambda current: current.model_copy(
                    update={
                        'final_output': text or current.final_output,
                        'final_output_ref': ref or current.final_output_ref,
                        'updated_at': now_iso(),
                    }
                ),
            )
            if updated is not None:
                self.refresh_task_view(task_id, mark_unread=True)
            return updated or record

    def ensure_node_result_payload_externalized(self, task_id: str, node_id: str) -> NodeRecord | None:
        with self._task_lock(task_id):
            record = self._store.get_node(node_id)
            if record is None:
                return None
            metadata = dict(record.metadata or {})
            payload = metadata.get('result_payload')
            if not isinstance(payload, dict):
                return record
            if str(metadata.get('result_payload_ref') or '').strip():
                return record
            text, ref = self._summarize_content(
                json.dumps(payload, ensure_ascii=False, indent=2),
                task_id=task_id,
                node_id=node_id,
                display_name=f'result-payload:{node_id}',
                source_kind='node_result_payload',
                force=True,
            )
            if not ref:
                return record

            def _mutate(current: NodeRecord) -> NodeRecord:
                next_metadata = dict(current.metadata or {})
                next_metadata['result_payload'] = payload
                next_metadata['result_payload_ref'] = ref
                next_metadata['result_payload_summary'] = text
                return current.model_copy(update={'metadata': next_metadata, 'updated_at': now_iso()})

            updated = self._store.update_node(node_id, _mutate)
            if updated is not None:
                self.refresh_task_view(task_id, mark_unread=True)
            return updated or record

    def mark_task_failed(self, task_id: str, *, reason: str) -> TaskRecord | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            if task is None:
                return None
            root_node = self._store.get_node(task.root_node_id)
            text = str(reason or 'task failed').strip() or 'task failed'
            if root_node is not None:
                self.update_node_status(task_id, root_node.node_id, status='failed', final_output=text, failure_reason=text)
                return self._store.get_task(task_id)

            updated = self._store.update_task(
                task_id,
                lambda record: record.model_copy(
                    update={
                        'status': 'failed',
                        'is_unread': True,
                        'final_output': text,
                        'failure_reason': text,
                        'updated_at': now_iso(),
                        'finished_at': now_iso(),
                    }
                ),
            )
            if updated is None:
                return None
            self.update_runtime_state(
                task_id,
                paused=bool(updated.is_paused),
                pause_requested=bool(updated.pause_requested),
                cancel_requested=bool(updated.cancel_requested),
            )
            self._notify_task_terminal(updated, previous_status=str(task.status or ''))
            return updated

    def update_runtime_state(self, task_id: str, *, publish_snapshot: bool = False, **payload: Any) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            current = self._store.get_runtime_state(task.task_id) or {
                'task_id': task.task_id,
                'root_node_id': task.root_node_id,
                'updated_at': now_iso(),
                'paused': bool(task.is_paused),
                'pause_requested': bool(task.pause_requested),
                'cancel_requested': bool(task.cancel_requested),
                'active_node_ids': [],
                'runnable_node_ids': [],
                'waiting_node_ids': [],
                'frames': [],
            }
            current.update({key: copy.deepcopy(value) for key, value in payload.items()})
            current['task_id'] = task.task_id
            current['root_node_id'] = task.root_node_id
            current['updated_at'] = now_iso()
            current['paused'] = bool(current.get('paused', task.is_paused))
            current['pause_requested'] = bool(current.get('pause_requested', task.pause_requested))
            current['cancel_requested'] = bool(current.get('cancel_requested', task.cancel_requested))
            current = self._sanitize_runtime_state(current)
            self._store.upsert_runtime_state(
                task_id=task.task_id,
                session_id=task.session_id,
                updated_at=str(current['updated_at'] or now_iso()),
                payload=current,
            )
            self._projection_service.sync_runtime_state(task.task_id, task=task, runtime_state=current)
            if publish_snapshot:
                self._append_task_event(
                    task=task,
                    event_type='task.runtime.updated',
                    data={
                        'task_id': task.task_id,
                        'runtime_summary': self._runtime_summary_payload(task.task_id, runtime_state=current),
                    },
                )
            return current

    def read_runtime_state(self, task_id: str) -> dict[str, Any] | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        return self._store.get_runtime_state(task_id)

    def upsert_frame(self, task_id: str, frame: dict[str, Any], *, publish_snapshot: bool = False) -> dict[str, Any]:
        with self._task_lock(task_id):
            state = self.read_runtime_state(task_id) or {}
            frames = [item for item in list(state.get('frames') or []) if str(item.get('node_id') or '') != str(frame.get('node_id') or '')]
            frames.append(copy.deepcopy(frame))
            active_ids, runnable_ids, waiting_ids = self._frame_index_payload(frames)
            return self.update_runtime_state(
                task_id,
                frames=frames,
                active_node_ids=active_ids,
                runnable_node_ids=runnable_ids,
                waiting_node_ids=waiting_ids,
                publish_snapshot=publish_snapshot,
            )

    def update_frame(
        self,
        task_id: str,
        node_id: str,
        frame_mutator: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        publish_snapshot: bool = False,
    ) -> dict[str, Any]:
        with self._task_lock(task_id):
            state = self.read_runtime_state(task_id) or {}
            frames = list(state.get('frames') or [])
            target = None
            remaining: list[dict[str, Any]] = []
            for item in frames:
                if str(item.get('node_id') or '') == str(node_id or '') and target is None:
                    target = copy.deepcopy(item)
                    continue
                remaining.append(copy.deepcopy(item))
            mutated = frame_mutator(target or self._default_frame(node_id=node_id))
            if not isinstance(mutated, dict):
                raise TypeError('frame mutator must return a dict')
            remaining.append(copy.deepcopy(mutated))
            active_ids, runnable_ids, waiting_ids = self._frame_index_payload(remaining)
            return self.update_runtime_state(
                task_id,
                frames=remaining,
                active_node_ids=active_ids,
                runnable_node_ids=runnable_ids,
                waiting_node_ids=waiting_ids,
                publish_snapshot=publish_snapshot,
            )

    def remove_frame(self, task_id: str, node_id: str, *, publish_snapshot: bool = False) -> dict[str, Any]:
        with self._task_lock(task_id):
            state = self.read_runtime_state(task_id) or {}
            frames = [item for item in list(state.get('frames') or []) if str(item.get('node_id') or '') != str(node_id or '')]
            active_ids, runnable_ids, waiting_ids = self._frame_index_payload(frames)
            return self.update_runtime_state(
                task_id,
                frames=frames,
                active_node_ids=active_ids,
                runnable_node_ids=runnable_ids,
                waiting_node_ids=waiting_ids,
                publish_snapshot=publish_snapshot,
            )

    def refresh_task_view(self, task_id: str, *, mark_unread: bool) -> TaskRecord | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            if task is None:
                return None
            previous_status = str(task.status or '').strip().lower()
            nodes = self._store.list_nodes(task_id)
            root = self._tree_builder.build_tree(task, nodes)
            tree_text = self._tree_builder.render_tree_text(root)
            root_node = self._store.get_node(task.root_node_id)
            final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
            next_status = self._derive_task_status(root_node=root_node, task_status=task.status, final_acceptance=final_acceptance)
            output_fields = self._task_output_fields(
                task=task,
                root_node=root_node,
                next_status=next_status,
                final_acceptance=final_acceptance,
            )
            brief_text = self._brief_text(
                task=task,
                root_node=root_node,
                next_status=next_status,
                final_acceptance=final_acceptance,
                output_fields=output_fields,
            )
            task_token_usage, _task_token_usage_by_model = aggregate_node_token_usage(nodes, tracked=bool(getattr(task.token_usage, 'tracked', False)))
            updated = task.model_copy(
                update={
                    'status': next_status,
                    'brief_text': brief_text,
                    'is_unread': True if mark_unread else task.is_unread,
                    'updated_at': now_iso(),
                    'final_output': str(output_fields.get('final_output') or ''),
                    'final_output_ref': str(output_fields.get('final_output_ref') or ''),
                    'failure_reason': str(output_fields.get('failure_reason') or ''),
                    'finished_at': now_iso() if next_status in {'success', 'failed'} and not task.finished_at else task.finished_at,
                    'token_usage': task_token_usage,
                }
            )
            self._store.upsert_task(updated)
            runtime_state = self.read_runtime_state(task_id)
            if runtime_state is not None:
                runtime_state['updated_at'] = now_iso()
                runtime_state['paused'] = bool(updated.is_paused)
                runtime_state['pause_requested'] = bool(updated.pause_requested)
                runtime_state['cancel_requested'] = bool(updated.cancel_requested)
                self._store.upsert_runtime_state(
                    task_id=updated.task_id,
                    session_id=updated.session_id,
                    updated_at=str(runtime_state['updated_at'] or now_iso()),
                    payload=runtime_state,
                )
            self._projection_service.sync_task(
                updated.task_id,
                task=updated,
                nodes=nodes,
                runtime_state=runtime_state,
            )
            self._publish_snapshot(updated.task_id, task=updated, nodes=nodes, root=root, tree_text=tree_text, publish_summary=True)
            self._notify_task_terminal(updated, previous_status=previous_status)
            return updated

    def bootstrap_missing_files(self, task_id: str) -> TaskRecord | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            if task is None:
                return None
            self.refresh_task_view(task_id, mark_unread=False)
            if self._store.get_runtime_state(task.task_id) is None:
                self.update_runtime_state(task.task_id, paused=bool(task.is_paused), active_node_ids=[], runnable_node_ids=[], waiting_node_ids=[], frames=[])
            return self._store.get_task(task_id)

    def _brief_text(
        self,
        *,
        task: TaskRecord,
        root_node: NodeRecord | None,
        next_status: str,
        final_acceptance,
        output_fields: dict[str, str],
    ) -> str:
        acceptance_failed = bool(
            getattr(final_acceptance, 'required', False)
            and str(getattr(final_acceptance, 'status', '') or '').strip().lower() == 'failed'
        )
        if acceptance_failed:
            failure_reason = str(output_fields.get('failure_reason') or '').strip()
            execution_output = str(((task.metadata or {}).get('final_execution_output') if isinstance(task.metadata, dict) else '') or '').strip()
            if execution_output:
                return _single_line_text(f'Acceptance failed: {failure_reason} | Execution deliverable: {execution_output}')
            if failure_reason:
                return _single_line_text(failure_reason)
        if root_node is not None:
            if str(root_node.check_result or '').strip():
                return _single_line_text(root_node.check_result)
            if str(root_node.final_output or '').strip():
                return _single_line_text(root_node.final_output)
            if str(root_node.failure_reason or '').strip():
                return _single_line_text(root_node.failure_reason)
            if root_node.output:
                last = str(root_node.output[-1].content or '').strip()
                if last:
                    return _single_line_text(last)
        if str(output_fields.get('final_output') or '').strip():
            return _single_line_text(output_fields['final_output'])
        if str(output_fields.get('failure_reason') or '').strip():
            return _single_line_text(output_fields['failure_reason'])
        if next_status == 'failed' and str(task.failure_reason or '').strip():
            return _single_line_text(task.failure_reason)
        return _single_line_text(task.user_request)

    @staticmethod
    def _task_output_fields(*, task: TaskRecord, root_node: NodeRecord | None, next_status: str, final_acceptance) -> dict[str, str]:
        if root_node is None:
            return {
                'final_output': str(task.final_output or ''),
                'final_output_ref': str(task.final_output_ref or ''),
                'failure_reason': str(task.failure_reason or ''),
            }

        metadata = dict(task.metadata or {})
        acceptance_failed = bool(
            getattr(final_acceptance, 'required', False)
            and str(getattr(final_acceptance, 'status', '') or '').strip().lower() == 'failed'
        )
        root_final_output = str(root_node.final_output or '').strip()
        root_final_output_ref = str(root_node.final_output_ref or '').strip()
        root_failure_reason = str(root_node.failure_reason or '').strip()
        root_check_result = str(root_node.check_result or '').strip()
        execution_output = str(metadata.get('final_execution_output') or '').strip()

        if acceptance_failed:
            failure_reason = root_failure_reason or root_check_result or str(task.failure_reason or '').strip()
            deliverable = execution_output or root_final_output
            return {
                'final_output': TaskLogService._dual_channel_output(deliverable, failure_reason) if deliverable else failure_reason,
                'final_output_ref': '',
                'failure_reason': failure_reason,
            }

        if str(root_node.status or '').strip().lower() == 'success':
            return {
                'final_output': root_final_output,
                'final_output_ref': root_final_output_ref,
                'failure_reason': '',
            }

        if next_status == 'failed':
            return {
                'final_output': root_final_output or str(task.final_output or ''),
                'final_output_ref': root_final_output_ref if root_final_output else str(task.final_output_ref or ''),
                'failure_reason': root_failure_reason or root_check_result or str(task.failure_reason or '').strip(),
            }

        return {
            'final_output': str(task.final_output or ''),
            'final_output_ref': str(task.final_output_ref or ''),
            'failure_reason': str(task.failure_reason or ''),
        }

    @staticmethod
    def _dual_channel_output(execution_output: str, failure_reason: str) -> str:
        execution_text = str(execution_output or '').strip()
        failure_text = str(failure_reason or '').strip()
        return (
            f'Execution Deliverable:\n{execution_text or "(empty)"}\n\n'
            f'Acceptance Failure:\n{failure_text or "(empty)"}'
        )

    @staticmethod
    def _derive_task_status(*, root_node: NodeRecord | None, task_status: str, final_acceptance) -> str:
        if root_node is None:
            return task_status
        root_status = str(root_node.status or task_status)
        if root_status != 'success':
            return root_status
        if not bool(getattr(final_acceptance, 'required', False)):
            return root_status
        acceptance_status = str(getattr(final_acceptance, 'status', 'pending') or 'pending').strip().lower()
        if acceptance_status == 'passed':
            return 'success'
        if acceptance_status == 'failed':
            return 'failed'
        return 'in_progress'

    def _require_task(self, task_id: str) -> TaskRecord:
        task = self._store.get_task(task_id)
        if task is None:
            raise ValueError(f'task not found: {task_id}')
        return task

    def _publish_snapshot(
        self,
        task_id: str,
        *,
        task: TaskRecord | None = None,
        nodes: list[NodeRecord] | None = None,
        root=None,
        tree_text: str | None = None,
        publish_summary: bool = False,
    ) -> None:
        current_task = task or self._store.get_task(task_id)
        if current_task is None:
            return
        current_nodes = list(nodes or self._store.list_nodes(task_id))
        current_root = root if root is not None else self._tree_builder.build_tree(current_task, current_nodes)
        self._append_task_event(
            task=current_task,
            event_type='task.summary.updated',
            data={'task': self._task_summary_payload(current_task)},
        )
        self._append_task_event(
            task=current_task,
            event_type='task.tree.updated',
            data={
                'task_id': current_task.task_id,
                'tree_root': self._compact_tree_payload(current_root),
                'default_selected_node_id': self._default_selected_node_id(current_root),
            },
        )
        if str(current_task.status or '').strip().lower() in {'success', 'failed'}:
            self._append_task_event(
                task=current_task,
                event_type='task.terminal',
                data={'task': self._task_summary_payload(current_task)},
            )
        if self._registry is not None:
            payload = self._snapshot_payload_builder(current_task.task_id) if callable(self._snapshot_payload_builder) else None
            if payload is not None:
                self._registry.publish_global_task(
                    current_task.task_id,
                    build_envelope(
                        channel='task',
                        session_id=current_task.session_id,
                        task_id=current_task.task_id,
                        seq=self._registry.next_global_task_seq(current_task.task_id),
                        type='task.snapshot.internal',
                        data=payload,
                    ),
                )
        if publish_summary:
            self._append_task_event(
                task=current_task,
                event_type='task.list.patch',
                data={'task': self._task_summary_payload(current_task)},
            )

    def _append_task_event(self, *, task: TaskRecord, event_type: str, data: dict[str, Any]) -> None:
        self._store.append_task_event(
            task_id=task.task_id,
            session_id=task.session_id,
            event_type=event_type,
            created_at=now_iso(),
            payload=data,
        )

    @staticmethod
    def _task_summary_payload(task: TaskRecord) -> dict[str, Any]:
        return {
            'task_id': task.task_id,
            'session_id': task.session_id,
            'title': task.title,
            'brief': task.brief_text,
            'status': task.status,
            'is_unread': bool(task.is_unread),
            'is_paused': bool(task.is_paused),
            'created_at': task.created_at,
            'updated_at': task.updated_at,
            'max_depth': int(task.max_depth or 0),
            'token_usage': task.token_usage.model_dump(mode='json'),
        }

    def _runtime_summary_payload(self, task_id: str, *, runtime_state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = runtime_state if isinstance(runtime_state, dict) else (self.read_runtime_state(task_id) or {})
        return {
            'active_node_ids': [str(item) for item in list(state.get('active_node_ids') or []) if str(item or '').strip()],
            'runnable_node_ids': [str(item) for item in list(state.get('runnable_node_ids') or []) if str(item or '').strip()],
            'waiting_node_ids': [str(item) for item in list(state.get('waiting_node_ids') or []) if str(item or '').strip()],
            'frames': [dict(item) for item in list(state.get('frames') or []) if isinstance(item, dict)],
        }

    def _compact_tree_payload(self, root) -> dict[str, Any] | None:
        if root is None:
            return None
        return {
            'node_id': root.node_id,
            'parent_node_id': root.parent_node_id,
            'depth': int(root.depth or 0),
            'node_kind': 'execution',
            'status': root.status,
            'title': root.title,
            'updated_at': root.updated_at,
            'spawn_rounds': [
                {
                    'round_id': round_item.round_id,
                    'round_index': int(round_item.round_index or 0),
                    'label': round_item.label,
                    'is_latest': bool(round_item.is_latest),
                    'created_at': round_item.created_at,
                    'child_node_ids': list(round_item.child_node_ids or []),
                    'source': round_item.source,
                    'total_children': int(round_item.total_children or 0),
                    'completed_children': int(round_item.completed_children or 0),
                    'running_children': int(round_item.running_children or 0),
                    'failed_children': int(round_item.failed_children or 0),
                    'children': [self._compact_tree_payload(child) for child in list(round_item.children or [])],
                }
                for round_item in list(root.spawn_rounds or [])
            ],
            'default_round_id': str(root.default_round_id or ''),
            'children': [self._compact_tree_payload(child) for child in list(root.children or [])],
        }

    @staticmethod
    def _default_selected_node_id(root) -> str:
        return str(getattr(root, 'node_id', '') or '')

    @classmethod
    def _sanitize_runtime_state(cls, payload: dict[str, Any]) -> dict[str, Any]:
        state = dict(payload or {})
        state['frames'] = [cls._sanitize_runtime_frame(frame) for frame in list(state.get('frames') or []) if isinstance(frame, dict)]
        return state

    @staticmethod
    def _sanitize_runtime_frame(frame: dict[str, Any]) -> dict[str, Any]:
        payload = dict(frame or {})
        return {
            'node_id': str(payload.get('node_id') or '').strip(),
            'depth': int(payload.get('depth') or 0),
            'node_kind': str(payload.get('node_kind') or 'execution'),
            'phase': str(payload.get('phase') or ''),
            'tool_calls': [dict(item) for item in list(payload.get('tool_calls') or []) if isinstance(item, dict)],
            'child_pipelines': [dict(item) for item in list(payload.get('child_pipelines') or []) if isinstance(item, dict)],
        }

    def _notify_task_terminal(self, task: TaskRecord, *, previous_status: str) -> None:
        next_status = str(getattr(task, 'status', '') or '').strip().lower()
        prev_status = str(previous_status or '').strip().lower()
        if next_status not in {'success', 'failed'} or prev_status in {'success', 'failed'}:
            return
        for listener in list(self._task_terminal_listeners):
            try:
                listener(task)
            except Exception:
                continue
