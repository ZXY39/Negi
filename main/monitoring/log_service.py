from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from g3ku.content import ContentNavigationService
from g3ku.content.navigation import INLINE_CHAR_LIMIT, INLINE_LINE_LIMIT
from main.ids import new_stage_id, new_stage_round_id
from main.models import (
    ExecutionStageRecord,
    ExecutionStageRound,
    ExecutionStageState,
    NodeOutputEntry,
    NodeRecord,
    TaskRecord,
    normalize_execution_stage_metadata,
    normalize_final_acceptance_metadata,
)
from main.runtime.stage_budget import (
    STAGE_TOOL_NAME,
    response_tool_calls_count_against_stage_budget,
    tool_call_counts_against_stage_budget,
)
from main.monitoring.file_store import TaskFileStore
from main.monitoring.execution_trace import build_execution_trace
from main.monitoring.models import (
    TaskProjectionNodeDetailRecord,
    TaskProjectionNodeRecord,
    TaskProjectionRoundRecord,
    TaskProjectionRuntimeFrameRecord,
)
from main.monitoring.task_event_writer import TaskEventWriter
from main.monitoring.task_projector import TaskProjector
from main.protocol import build_envelope, now_iso
from main.token_usage import aggregate_node_token_usage, build_token_usage_from_attempts, merge_token_usage_by_model, merge_token_usage_records


def _single_line_text(value: Any, *, max_chars: int = 120) -> str:
    text = ' '.join(str(value or '').split())
    if len(text) <= max_chars:
        return text
    return f'{text[: max_chars - 3].rstrip()}...'


def _precise_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='microseconds')


_EXECUTION_STAGE_METADATA_KEY = 'execution_stages'
_EXECUTION_STAGE_TOOL_NAME = STAGE_TOOL_NAME
_EXECUTION_STAGE_MODE_SELF = '自主执行'
_EXECUTION_STAGE_MODE_WITH_CHILDREN = '包含派生'
_EXECUTION_STAGE_STATUS_ACTIVE = '进行中'
_EXECUTION_STAGE_STATUS_COMPLETED = '完成'
_EXECUTION_STAGE_STATUS_FAILED = '失败'
_NON_BUDGET_EXECUTION_TOOLS = {
    _EXECUTION_STAGE_TOOL_NAME,
    'spawn_child_nodes',
    'wait_tool_execution',
    'stop_tool_execution',
}

class TaskLogService:
    def __init__(self, *, store, file_store: TaskFileStore, registry=None, content_store: ContentNavigationService | None = None):
        self._store = store
        self._file_store = file_store
        self._registry = registry
        self._content_store = content_store
        self._event_writer = TaskEventWriter(store=store)
        self._projector = TaskProjector(store=store)
        self._live_snapshot_publishers: list[Callable[[TaskRecord, dict[str, Any], bool], None]] = []
        self._task_terminal_listeners: list[Callable[[TaskRecord], None]] = []
        self._task_visible_output_listeners: list[Callable[[str, str], None]] = []
        self._task_locks: dict[str, threading.RLock] = {}
        self._task_locks_guard = threading.Lock()

    def add_live_snapshot_publisher(self, publisher: Callable[[TaskRecord, dict[str, Any], bool], None]) -> None:
        if callable(publisher):
            self._live_snapshot_publishers.append(publisher)

    def add_task_terminal_listener(self, listener: Callable[[TaskRecord], None]) -> None:
        if callable(listener):
            self._task_terminal_listeners.append(listener)

    def add_task_visible_output_listener(self, listener: Callable[[str, str], None]) -> None:
        if callable(listener):
            self._task_visible_output_listeners.append(listener)

    def append_task_event(
        self,
        *,
        task_id: str | None,
        session_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> int:
        return self._event_writer.append_task_event(
            task_id=task_id,
            session_id=session_id,
            event_type=event_type,
            data=data,
        )

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
            'stage_mode': '',
            'stage_status': '',
            'stage_goal': '',
            'stage_total_steps': 0,
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

    @staticmethod
    def _is_terminal_status(status: Any) -> bool:
        return str(status or '').strip().lower() in {'success', 'failed'}

    @classmethod
    def _coerce_terminal_runtime_state(cls, payload: dict[str, Any]) -> dict[str, Any]:
        state = dict(payload or {})
        state['active_node_ids'] = []
        state['runnable_node_ids'] = []
        state['waiting_node_ids'] = []
        state['frames'] = []
        return cls._sanitize_runtime_state(state)

    @staticmethod
    def _default_runtime_meta(*, last_visible_output_at: str = '') -> dict[str, Any]:
        return {
            'updated_at': now_iso(),
            'last_visible_output_at': str(last_visible_output_at or '').strip(),
            'last_stall_notice_bucket_minutes': 0,
        }

    def initialize_task(self, task: TaskRecord, root: NodeRecord) -> tuple[TaskRecord, NodeRecord]:
        with self._task_lock(task.task_id):
            task = task.model_copy(
                update={
                    'is_unread': True,
                    'brief_text': _single_line_text(task.user_request),
                    'updated_at': now_iso(),
                }
            )
            root = root.model_copy(update={'updated_at': now_iso()})
            self._store.upsert_task(task)
            self._store.upsert_node(root)
            self._sync_node_read_models_locked(root)
            self.update_task_runtime_meta(
                task.task_id,
                last_visible_output_at=_precise_now_iso(),
                last_stall_notice_bucket_minutes=0,
            )
            self.upsert_frame(
                task.task_id,
                self._default_frame(node_id=root.node_id, depth=root.depth, node_kind=root.node_kind, phase='before_model'),
                publish_snapshot=False,
            )
            self.refresh_task_view(task.task_id, mark_unread=True)
            return task, root

    def create_node(self, task_id: str, node: NodeRecord) -> NodeRecord:
        with self._task_lock(task_id):
            self._store.upsert_node(node)
            self._sync_node_read_models_locked(node)
            self._sync_task_node_rounds_locked(node)
            task = self._store.get_task(task_id)
            if task is not None:
                self._publish_task_node_patch_locked(task=task, node=node)
            self.refresh_task_view(task_id, mark_unread=True)
            return node

    def update_node_input(self, task_id: str, node_id: str, content: str) -> NodeRecord | None:
        with self._task_lock(task_id):
            text, ref = self._summarize_node_input(task_id=task_id, node_id=node_id, content=content)
            updated = self._store.update_node(
                node_id,
                lambda record: record.model_copy(update={'input': text, 'input_ref': ref, 'updated_at': now_iso()}),
            )
            task = self._store.get_task(task_id)
            if updated is not None and task is not None:
                self._sync_node_read_models_locked(updated)
                self._publish_task_node_patch_locked(task=task, node=updated)
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
        request_messages: list[dict[str, Any]] | None = None,
        prompt_cache_key: str | None = None,
        request_message_count: int | None = None,
        request_message_chars: int | None = None,
    ) -> NodeRecord | None:
        with self._task_lock(task_id):
            current = self._store.get_node(node_id)
            call_index = len(list(getattr(current, 'output', []) or [])) + 1 if current is not None else 1
            changed_at = now_iso()
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
                        created_at=changed_at,
                    )
                )
                update: dict[str, Any] = {'output': output, 'updated_at': changed_at}
                if delta_usage is not None and bool(getattr(record.token_usage, 'tracked', False)):
                    update['token_usage'] = merge_token_usage_records([record.token_usage, delta_usage], tracked=True)
                    update['token_usage_by_model'] = merge_token_usage_by_model(
                        [*list(record.token_usage_by_model or []), *delta_usage_by_model],
                        tracked=True,
                    )
                return record.model_copy(update=update)

            updated = self._store.update_node(node_id, _mutate)
            task = self._store.get_task(task_id)
            if updated is not None and task is not None:
                if delta_usage is not None and bool(getattr(task.token_usage, 'tracked', False)):
                    task = self._store.update_task(
                        task_id,
                        lambda current: current.model_copy(
                            update={
                                'token_usage': merge_token_usage_records([current.token_usage, delta_usage], tracked=True),
                                'updated_at': changed_at,
                            }
                        ),
                    ) or task
                self._sync_node_read_models_locked(updated)
                self._publish_task_node_patch_locked(task=task, node=updated)
                if delta_usage is not None:
                    model_call_payload = self._model_call_payload(
                        task_id=task_id,
                        node_id=node_id,
                        call_index=call_index,
                        model_messages=model_messages,
                        request_messages=request_messages,
                        prompt_cache_key=prompt_cache_key,
                        tool_calls=tool_calls,
                        delta_usage=delta_usage,
                        delta_usage_by_model=delta_usage_by_model,
                        request_message_count=request_message_count,
                        request_message_chars=request_message_chars,
                    )
                    self._event_writer.append_task_model_call(
                        task_id=task_id,
                        node_id=node_id,
                        created_at=changed_at,
                        payload=model_call_payload,
                    )
                    self._append_task_event(task=task, event_type='task.model.call', data=model_call_payload)
                    self._dispatch_live_event_locked(task=task, event_type='task.model.call', data=model_call_payload)
                self._notify_task_visible_output(task_id, occurred_at=_precise_now_iso())
            self.refresh_task_view(task_id, mark_unread=True)
            return updated

    def update_node_check_result(self, task_id: str, node_id: str, check_result: str) -> NodeRecord | None:
        with self._task_lock(task_id):
            changed_at = now_iso()
            text, ref = self._summarize_content(
                check_result,
                task_id=task_id,
                node_id=node_id,
                display_name=f'check-result:{node_id}',
                source_kind='node_check_result',
            )
            updated = self._store.update_node(
                node_id,
                lambda record: record.model_copy(update={'check_result': text, 'check_result_ref': ref, 'updated_at': changed_at}),
            )
            task = self._store.get_task(task_id)
            if updated is not None and task is not None:
                self._sync_node_read_models_locked(updated)
                self._publish_task_node_patch_locked(task=task, node=updated)
                self._notify_task_visible_output(task_id, occurred_at=_precise_now_iso())
            self.refresh_task_view(task_id, mark_unread=True)
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
            changed_at = now_iso()
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
                        'finished_at': changed_at if status in {'success', 'failed'} else record.finished_at,
                        'updated_at': changed_at,
                    }
                ),
            )
            propagated_node_ids: list[str] = []
            if updated is not None:
                propagated_node_ids = self._sync_acceptance_terminal_status(
                    task_id=task_id,
                    acceptance_node=updated,
                )
            task = self._store.get_task(task_id)
            if updated is not None and task is not None:
                self._sync_node_read_models_locked(updated)
                self._publish_task_node_patch_locked(task=task, node=updated)
                for propagated_node_id in propagated_node_ids:
                    if not str(propagated_node_id or '').strip() or propagated_node_id == node_id:
                        continue
                    propagated = self._store.get_node(propagated_node_id)
                    if propagated is not None:
                        self._sync_node_read_models_locked(propagated)
                        self._publish_task_node_patch_locked(task=task, node=propagated)
                self._notify_task_visible_output(task_id, occurred_at=_precise_now_iso())
            self.refresh_task_view(task_id, mark_unread=True)
            return updated

    @staticmethod
    def _acceptance_failure_text(node: NodeRecord) -> str:
        for candidate in (node.failure_reason, node.final_output, node.check_result):
            text = str(candidate or '').strip()
            if text:
                return text
        return 'acceptance failed'

    def _sync_acceptance_terminal_status(self, *, task_id: str, acceptance_node: NodeRecord) -> list[str]:
        normalized_kind = str(getattr(acceptance_node, 'node_kind', '') or '').strip().lower()
        normalized_status = str(getattr(acceptance_node, 'status', '') or '').strip().lower()
        if normalized_kind != 'acceptance' or normalized_status not in {'success', 'failed'}:
            return []

        propagated_node_ids: list[str] = []
        accepted_node_id = str(
            ((acceptance_node.metadata or {}).get('accepted_node_id') if isinstance(acceptance_node.metadata, dict) else '')
            or acceptance_node.parent_node_id
            or ''
        ).strip()
        accepted_node = self._store.get_node(accepted_node_id) if accepted_node_id else None

        if accepted_node is not None:
            acceptance_text = self._acceptance_failure_text(acceptance_node)
            check_text, check_ref = self._summarize_content(
                acceptance_text,
                task_id=task_id,
                node_id=accepted_node.node_id,
                display_name=f'check-result:{accepted_node.node_id}',
                source_kind='node_check_result',
            )

            def _mutate(record: NodeRecord) -> NodeRecord:
                update: dict[str, Any] = {
                    'updated_at': now_iso(),
                }
                if check_text:
                    update['check_result'] = check_text
                    update['check_result_ref'] = check_ref or record.check_result_ref
                return record.model_copy(update=update)

            normalized_parent_kind = str(getattr(accepted_node, 'node_kind', '') or '').strip().lower()
            if normalized_parent_kind == 'execution':
                updated_parent = self._store.update_node(accepted_node.node_id, _mutate)
                if updated_parent is not None:
                    accepted_node = updated_parent
                    propagated_node_ids.append(updated_parent.node_id)

        self._sync_final_acceptance_state(
            task_id=task_id,
            acceptance_node=acceptance_node,
            accepted_node=accepted_node,
            status='passed' if normalized_status == 'success' else 'failed',
        )
        return propagated_node_ids

    def _sync_final_acceptance_state(
        self,
        *,
        task_id: str,
        acceptance_node: NodeRecord,
        accepted_node: NodeRecord | None,
        status: str,
    ) -> None:
        task = self._store.get_task(task_id)
        if task is None:
            return

        metadata = dict(task.metadata or {})
        acceptance_metadata = dict(acceptance_node.metadata or {}) if isinstance(acceptance_node.metadata, dict) else {}
        current = normalize_final_acceptance_metadata(metadata.get('final_acceptance'))
        is_final_acceptance = bool(acceptance_metadata.get('final_acceptance')) or (
            str(current.node_id or '').strip() == str(acceptance_node.node_id or '').strip()
        )
        if not is_final_acceptance:
            return

        next_final_acceptance = current.model_dump(mode='json')
        next_final_acceptance['required'] = bool(current.required or acceptance_metadata.get('final_acceptance'))
        next_final_acceptance['node_id'] = str(acceptance_node.node_id or '').strip()
        next_final_acceptance['status'] = str(status or current.status or 'pending').strip().lower() or 'pending'
        metadata['final_acceptance'] = next_final_acceptance

        execution_output = str(getattr(accepted_node, 'final_output', '') or '').strip() if accepted_node is not None else ''
        if next_final_acceptance['status'] == 'failed' and execution_output:
            metadata['final_execution_output'] = execution_output
        else:
            metadata.pop('final_execution_output', None)

        self._store.update_task(
            task_id,
            lambda record: record.model_copy(
                update={
                    'metadata': metadata,
                    'updated_at': now_iso(),
                }
            ),
        )

    def mark_task_read(self, task_id: str) -> TaskRecord | None:
        with self._task_lock(task_id):
            updated = self._store.update_task(
                task_id,
                lambda task: task.model_copy(update={'is_unread': False, 'updated_at': now_iso()}),
            )
            if updated is not None:
                self._publish_task_summary_patch_locked(task=updated)
            return updated

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
        request_messages: list[dict[str, Any]] | None,
        prompt_cache_key: str | None,
        tool_calls: list[dict[str, Any]] | None,
        delta_usage,
        delta_usage_by_model: list[Any],
        request_message_count: int | None,
        request_message_chars: int | None,
    ) -> dict[str, Any]:
        message_list = list(model_messages or [])
        request_list = list(request_messages or message_list)
        try:
            prepared_payload = json.dumps(request_list, ensure_ascii=False, default=str)
        except Exception:
            prepared_payload = str(request_list)
        if request_message_count is None or request_message_chars is None:
            prepared_message_count = len(request_list)
            prepared_message_chars = len(prepared_payload)
        else:
            prepared_message_count = max(0, int(request_message_count or 0))
            prepared_message_chars = max(0, int(request_message_chars or 0))
        try:
            model_payload = json.dumps(message_list, ensure_ascii=False, default=str)
        except Exception:
            model_payload = str(message_list)
        return {
            'task_id': task_id,
            'node_id': node_id,
            'call_index': int(call_index or 0),
            'model_message_count': len(message_list),
            'model_message_chars': len(model_payload),
            'prepared_message_count': prepared_message_count,
            'prepared_message_chars': prepared_message_chars,
            'response_tool_call_count': len(list(tool_calls or [])),
            'prompt_cache_key_present': bool(str(prompt_cache_key or '').strip()),
            'prompt_cache_key_hash': TaskLogService._short_hash(prompt_cache_key),
            'request_overlay_applied': request_list != message_list,
            'model_message_hash': TaskLogService._short_hash(model_payload),
            'prepared_message_hash': TaskLogService._short_hash(prepared_payload),
            'model_prefix_hash': TaskLogService._message_prefix_hash(message_list),
            'prepared_prefix_hash': TaskLogService._message_prefix_hash(request_list),
            'delta_usage': delta_usage.model_dump(mode='json'),
            'delta_usage_by_model': [item.model_dump(mode='json') for item in list(delta_usage_by_model or [])],
        }

    @staticmethod
    def _short_hash(value: Any) -> str:
        text = str(value or '').strip()
        if not text:
            return ''
        return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]

    @classmethod
    def _message_prefix_hash(cls, messages: list[dict[str, Any]] | None, *, prefix_count: int = 2) -> str:
        prefix = list(messages or [])[: max(0, int(prefix_count or 0))]
        try:
            payload = json.dumps(prefix, ensure_ascii=False, default=str)
        except Exception:
            payload = str(prefix)
        return cls._short_hash(payload)

    def _next_output_seq(self, node_id: str) -> int:
        record = self._store.get_node(node_id)
        if record is None:
            return 1
        return len(list(record.output or [])) + 1

    def set_pause_state(self, task_id: str, *, pause_requested: bool | None = None, is_paused: bool | None = None) -> TaskRecord | None:
        return self.update_task_control(task_id, pause_requested=pause_requested, is_paused=is_paused)

    @staticmethod
    def _execution_stage_state(node: NodeRecord | None) -> ExecutionStageState:
        payload = (node.metadata or {}).get(_EXECUTION_STAGE_METADATA_KEY) if node is not None and isinstance(node.metadata, dict) else {}
        return normalize_execution_stage_metadata(payload)

    @classmethod
    def _tool_call_counts_against_stage_budget(cls, tool_call: dict[str, Any]) -> bool:
        return tool_call_counts_against_stage_budget(tool_call, extra_non_budget_tools=_NON_BUDGET_EXECUTION_TOOLS)

    @staticmethod
    def _active_execution_stage(state: ExecutionStageState) -> ExecutionStageRecord | None:
        active_stage_id = str(state.active_stage_id or '').strip()
        if not active_stage_id:
            return None
        for stage in list(state.stages or []):
            if str(stage.stage_id or '').strip() == active_stage_id:
                return stage
        return None

    @staticmethod
    def _execution_stage_frame_payload(state: ExecutionStageState) -> dict[str, Any]:
        active = TaskLogService._active_execution_stage(state)
        if active is None:
            return {
                'stage_mode': '',
                'stage_status': '',
                'stage_goal': '',
                'stage_total_steps': 0,
            }
        return {
            'stage_mode': str(active.mode or ''),
            'stage_status': str(active.status or ''),
            'stage_goal': str(active.stage_goal or ''),
            'stage_total_steps': int(active.tool_round_budget or 0),
        }

    def execution_stage_gate_snapshot(self, task_id: str, node_id: str) -> dict[str, Any]:
        with self._task_lock(task_id):
            node = self._store.get_node(node_id)
            state = self._execution_stage_state(node)
            active = self._active_execution_stage(state)
            completed_stages = [
                {
                    'stage_index': int(stage.stage_index or 0),
                    'mode': str(stage.mode or ''),
                    'status': str(stage.status or ''),
                    'stage_goal': str(stage.stage_goal or ''),
                    'tool_round_budget': int(stage.tool_round_budget or 0),
                    'tool_rounds_used': int(stage.tool_rounds_used or 0),
                }
                for stage in list(state.stages or [])
                if str(stage.status or '') != _EXECUTION_STAGE_STATUS_ACTIVE
            ]
            return {
                'has_active_stage': active is not None,
                'transition_required': bool(state.transition_required),
                'active_stage': active.model_dump(mode='json') if active is not None else None,
                'completed_stages': completed_stages,
            }

    def execution_stage_prompt_payload(self, task_id: str, node_id: str) -> dict[str, Any]:
        snapshot = self.execution_stage_gate_snapshot(task_id, node_id)
        active = snapshot.get('active_stage') if isinstance(snapshot, dict) else None
        completed_stages: list[dict[str, Any]] = []
        record = self._store.get_node(node_id)
        state = self._execution_stage_state(record)
        for stage in list(state.stages or []):
            if str(stage.status or '') == _EXECUTION_STAGE_STATUS_ACTIVE:
                continue
            completed_stages.append(
                {
                    'stage_index': int(stage.stage_index or 0),
                    'mode': str(stage.mode or ''),
                    'status': str(stage.status or ''),
                    'stage_goal': str(stage.stage_goal or ''),
                    'tool_round_budget': int(stage.tool_round_budget or 0),
                    'tool_rounds_used': int(stage.tool_rounds_used or 0),
                }
            )
        return {
            'has_active_stage': bool(snapshot.get('has_active_stage')) if isinstance(snapshot, dict) else False,
            'transition_required': bool(snapshot.get('transition_required')) if isinstance(snapshot, dict) else False,
            'active_stage': dict(active or {}) if isinstance(active, dict) else None,
            'completed_stages': completed_stages,
        }

    def _persist_execution_stage_state_locked(
        self,
        *,
        task: TaskRecord,
        node_id: str,
        state: ExecutionStageState,
    ) -> NodeRecord | None:
        payload = state.model_dump(mode='json')
        updated = self._store.update_node(
            node_id,
            lambda current: current.model_copy(
                update={
                    'metadata': {**dict(current.metadata or {}), _EXECUTION_STAGE_METADATA_KEY: payload},
                    'updated_at': now_iso(),
                }
            ),
        )
        if updated is not None:
            self._sync_node_read_models_locked(updated)
            self._publish_task_node_patch_locked(task=task, node=updated)
        return updated

    def _sync_execution_stage_frame_locked(self, *, task_id: str, node_id: str, state: ExecutionStageState) -> None:
        payload = self._execution_stage_frame_payload(state)
        self.update_frame(
            task_id,
            node_id,
            lambda frame: {
                **(frame or self._default_frame(node_id=node_id)),
                **payload,
            },
            publish_snapshot=True,
        )

    def submit_next_stage(self, task_id: str, node_id: str, *, stage_goal: str, tool_round_budget: int) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            node = self._store.get_node(node_id)
            if node is None:
                raise ValueError(f'node not found: {node_id}')
            normalized_goal = str(stage_goal or '').strip()
            normalized_budget = int(tool_round_budget or 0)
            if not normalized_goal:
                raise ValueError('stage_goal must not be empty')
            if normalized_budget < 1 or normalized_budget > 10:
                raise ValueError('tool_round_budget must be between 1 and 10')
            state = self._execution_stage_state(node)
            now = now_iso()
            stages: list[ExecutionStageRecord] = []
            for stage in list(state.stages or []):
                current = stage
                if str(stage.stage_id or '').strip() == str(state.active_stage_id or '').strip() and str(stage.status or '') == _EXECUTION_STAGE_STATUS_ACTIVE:
                    current = stage.model_copy(update={'status': _EXECUTION_STAGE_STATUS_COMPLETED, 'finished_at': now})
                stages.append(current)
            next_stage = ExecutionStageRecord(
                stage_id=new_stage_id(),
                stage_index=len(stages) + 1,
                mode=_EXECUTION_STAGE_MODE_SELF,
                status=_EXECUTION_STAGE_STATUS_ACTIVE,
                stage_goal=normalized_goal,
                tool_round_budget=normalized_budget,
                tool_rounds_used=0,
                created_at=now,
                finished_at='',
                rounds=[],
            )
            next_state = ExecutionStageState(
                active_stage_id=next_stage.stage_id,
                transition_required=False,
                stages=[*stages, next_stage],
            )
            self._persist_execution_stage_state_locked(task=task, node_id=node_id, state=next_state)
            self._sync_execution_stage_frame_locked(task_id=task_id, node_id=node_id, state=next_state)
            self.refresh_task_view(task_id, mark_unread=True)
            return next_stage.model_dump(mode='json')

    def record_execution_stage_round(
        self,
        task_id: str,
        node_id: str,
        *,
        tool_calls: list[dict[str, Any]],
        created_at: str,
    ) -> dict[str, Any] | None:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            node = self._store.get_node(node_id)
            if node is None:
                return None
            state = self._execution_stage_state(node)
            active = self._active_execution_stage(state)
            if active is None or bool(state.transition_required):
                return None
            visible_calls = [
                item for item in list(tool_calls or [])
                if str(item.get('name') or '').strip() and str(item.get('name') or '').strip() != _EXECUTION_STAGE_TOOL_NAME
            ]
            if not visible_calls:
                return None
            tool_names = [str(item.get('name') or '').strip() for item in visible_calls if str(item.get('name') or '').strip()]
            counts_budget = response_tool_calls_count_against_stage_budget(
                visible_calls,
                extra_non_budget_tools=_NON_BUDGET_EXECUTION_TOOLS,
            )
            next_round = ExecutionStageRound(
                round_id=new_stage_round_id(),
                round_index=len(list(active.rounds or [])) + 1,
                created_at=str(created_at or now_iso()),
                tool_call_ids=[str(item.get('id') or '').strip() for item in visible_calls if str(item.get('id') or '').strip()],
                tool_names=tool_names,
                budget_counted=counts_budget,
            )
            stages: list[ExecutionStageRecord] = []
            for stage in list(state.stages or []):
                current = stage
                if str(stage.stage_id or '').strip() == str(active.stage_id or '').strip():
                    next_used = int(stage.tool_rounds_used or 0) + (1 if counts_budget else 0)
                    if int(stage.tool_round_budget or 0) > 0:
                        next_used = min(next_used, int(stage.tool_round_budget or 0))
                    current = stage.model_copy(
                        update={
                            'tool_rounds_used': next_used,
                            'rounds': [*list(stage.rounds or []), next_round],
                        }
                    )
                stages.append(current)
            latest_active = next((item for item in stages if str(item.stage_id or '').strip() == str(active.stage_id or '').strip()), None)
            next_state = ExecutionStageState(
                active_stage_id=str(state.active_stage_id or '').strip(),
                transition_required=bool(
                    latest_active is not None
                    and int(latest_active.tool_round_budget or 0) > 0
                    and int(latest_active.tool_rounds_used or 0) >= int(latest_active.tool_round_budget or 0)
                ),
                stages=stages,
            )
            self._persist_execution_stage_state_locked(task=task, node_id=node_id, state=next_state)
            self._sync_execution_stage_frame_locked(task_id=task_id, node_id=node_id, state=next_state)
            self.refresh_task_view(task_id, mark_unread=True)
            return next_round.model_dump(mode='json')

    def mark_execution_stage_contains_spawn(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            node = self._store.get_node(node_id)
            if node is None:
                return None
            state = self._execution_stage_state(node)
            active = self._active_execution_stage(state)
            if active is None or str(active.mode or '') == _EXECUTION_STAGE_MODE_WITH_CHILDREN:
                return active.model_dump(mode='json') if active is not None else None
            stages: list[ExecutionStageRecord] = []
            for stage in list(state.stages or []):
                current = stage
                if str(stage.stage_id or '').strip() == str(active.stage_id or '').strip():
                    current = stage.model_copy(update={'mode': _EXECUTION_STAGE_MODE_WITH_CHILDREN})
                stages.append(current)
            next_state = ExecutionStageState(
                active_stage_id=str(state.active_stage_id or '').strip(),
                transition_required=bool(state.transition_required),
                stages=stages,
            )
            self._persist_execution_stage_state_locked(task=task, node_id=node_id, state=next_state)
            self._sync_execution_stage_frame_locked(task_id=task_id, node_id=node_id, state=next_state)
            self.refresh_task_view(task_id, mark_unread=True)
            current = next((item for item in stages if str(item.stage_id or '').strip() == str(active.stage_id or '').strip()), None)
            return current.model_dump(mode='json') if current is not None else None

    def finalize_execution_stage(self, task_id: str, node_id: str, *, status: str) -> dict[str, Any] | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            node = self._store.get_node(node_id)
            if task is None or node is None:
                return None
            state = self._execution_stage_state(node)
            active = self._active_execution_stage(state)
            if active is None:
                return None
            final_status = _EXECUTION_STAGE_STATUS_COMPLETED if str(status or '').strip().lower() == 'success' else _EXECUTION_STAGE_STATUS_FAILED
            now = now_iso()
            stages: list[ExecutionStageRecord] = []
            for stage in list(state.stages or []):
                current = stage
                if str(stage.stage_id or '').strip() == str(active.stage_id or '').strip():
                    current = stage.model_copy(update={'status': final_status, 'finished_at': now})
                stages.append(current)
            next_state = ExecutionStageState(
                active_stage_id='',
                transition_required=False,
                stages=stages,
            )
            self._persist_execution_stage_state_locked(task=task, node_id=node_id, state=next_state)
            self._sync_execution_stage_frame_locked(task_id=task_id, node_id=node_id, state=next_state)
            self.refresh_task_view(task_id, mark_unread=True)
            current = next((item for item in stages if str(item.stage_id or '').strip() == str(active.stage_id or '').strip()), None)
            return current.model_dump(mode='json') if current is not None else None

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
                self._sync_node_read_models_locked(updated)
                self._sync_task_node_rounds_locked(updated)
                self._publish_task_node_patch_locked(task=task, node=updated)
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
                task = self._store.get_task(task_id)
                self._sync_node_read_models_locked(updated)
                if task is not None:
                    self._publish_task_node_patch_locked(task=task, node=updated)
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
                task = self._store.get_task(task_id)
                self._sync_node_read_models_locked(updated)
                if task is not None:
                    self._publish_task_node_patch_locked(task=task, node=updated)
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
            self.replace_runtime_frames(task_id, frames=[], active_node_ids=[], runnable_node_ids=[], waiting_node_ids=[])
            self._notify_task_terminal(updated, previous_status=str(task.status or ''))
            return updated

    def update_task_runtime_meta(self, task_id: str, **payload: Any) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            current = dict(self._store.get_task_runtime_meta(task.task_id) or self._default_runtime_meta())
            if 'last_visible_output_at' in payload:
                current['last_visible_output_at'] = str(payload.get('last_visible_output_at') or '').strip()
            if 'last_stall_notice_bucket_minutes' in payload:
                try:
                    current['last_stall_notice_bucket_minutes'] = max(0, int(payload.get('last_stall_notice_bucket_minutes') or 0))
                except (TypeError, ValueError):
                    current['last_stall_notice_bucket_minutes'] = 0
            current['updated_at'] = now_iso()
            self._store.upsert_task_runtime_meta(
                task_id=task.task_id,
                updated_at=str(current.get('updated_at') or now_iso()),
                payload=current,
            )
            return self.read_task_runtime_meta(task.task_id) or current

    def read_task_runtime_meta(self, task_id: str) -> dict[str, Any] | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        current = dict(self._store.get_task_runtime_meta(task_id) or self._default_runtime_meta())
        current['task_id'] = task.task_id
        current.setdefault('updated_at', now_iso())
        current.setdefault('last_visible_output_at', '')
        try:
            current['last_stall_notice_bucket_minutes'] = max(0, int(current.get('last_stall_notice_bucket_minutes') or 0))
        except (TypeError, ValueError):
            current['last_stall_notice_bucket_minutes'] = 0
        return current

    def replace_runtime_frames(
        self,
        task_id: str,
        *,
        frames: list[dict[str, Any]],
        active_node_ids: list[str] | set[str] | tuple[str, ...] | None = None,
        runnable_node_ids: list[str] | set[str] | tuple[str, ...] | None = None,
        waiting_node_ids: list[str] | set[str] | tuple[str, ...] | None = None,
        publish_snapshot: bool = False,
    ) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            if self._is_terminal_status(task.status):
                self._store.replace_task_runtime_frames(task.task_id, [])
                return self.read_runtime_state(task_id) or {}
            provided_frames = {
                str(item.get('node_id') or '').strip(): self._sanitize_runtime_frame(item)
                for item in list(frames or [])
                if isinstance(item, dict) and str(item.get('node_id') or '').strip()
            }
            active_ids = {
                str(item or '').strip()
                for item in list(active_node_ids or [])
                if str(item or '').strip()
            }
            runnable_ids = {
                str(item or '').strip()
                for item in list(runnable_node_ids or [])
                if str(item or '').strip()
            }
            waiting_ids = {
                str(item or '').strip()
                for item in list(waiting_node_ids or [])
                if str(item or '').strip()
            }
            frame_records: list[TaskProjectionRuntimeFrameRecord] = []
            for node_id in sorted(set(provided_frames) | active_ids | runnable_ids | waiting_ids):
                frame_payload = provided_frames.get(node_id) or self._default_frame(node_id=node_id)
                record = self._runtime_frame_record(task=task, frame=frame_payload)
                frame_records.append(
                    record.model_copy(
                        update={
                            'active': node_id in active_ids,
                            'runnable': node_id in runnable_ids,
                            'waiting': node_id in waiting_ids,
                            'updated_at': now_iso(),
                        }
                    )
                )
            self._projector.replace_runtime_frames(task.task_id, frame_records)
            if publish_snapshot:
                self._publish_task_live_patch_locked(task=task)
            return self.read_runtime_state(task_id) or {}

    def read_runtime_state(self, task_id: str) -> dict[str, Any] | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        meta = self.read_task_runtime_meta(task_id) or self._default_runtime_meta()
        frame_records = list(self._store.list_task_runtime_frames(task_id) or [])
        frames = [self._hydrate_runtime_frame_record(record) for record in frame_records]
        return {
            'task_id': task.task_id,
            'root_node_id': task.root_node_id,
            'updated_at': str(meta.get('updated_at') or now_iso()),
            'paused': bool(task.is_paused),
            'pause_requested': bool(task.pause_requested),
            'cancel_requested': bool(task.cancel_requested),
            'last_visible_output_at': str(meta.get('last_visible_output_at') or '').strip(),
            'last_stall_notice_bucket_minutes': max(0, int(meta.get('last_stall_notice_bucket_minutes') or 0)),
            'frames': frames,
            'active_node_ids': [record.node_id for record in frame_records if bool(record.active)],
            'runnable_node_ids': [record.node_id for record in frame_records if bool(record.runnable)],
            'waiting_node_ids': [record.node_id for record in frame_records if bool(record.waiting)],
        }

    def read_runtime_frame(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        record = self._store.get_task_runtime_frame(task_id, node_id)
        return self._hydrate_runtime_frame_record(record) if record is not None else None

    def upsert_frame(self, task_id: str, frame: dict[str, Any], *, publish_snapshot: bool = False) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            if self._is_terminal_status(task.status):
                self._store.replace_task_runtime_frames(task.task_id, [])
                return self.read_runtime_state(task_id) or {}
            sanitized = self._sanitize_runtime_frame(frame)
            record = self._runtime_frame_record(task=task, frame=sanitized)
            self._store.upsert_task_runtime_frame(record)
            if publish_snapshot:
                self._publish_task_live_patch_locked(task=task, frame=record)
            return self.read_runtime_state(task_id) or {}

    def update_frame(
        self,
        task_id: str,
        node_id: str,
        frame_mutator: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        publish_snapshot: bool = False,
    ) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            if self._is_terminal_status(task.status):
                self._store.replace_task_runtime_frames(task.task_id, [])
                return self.read_runtime_state(task_id) or {}
            current = self._store.get_task_runtime_frame(task_id, node_id)
            target = self._hydrate_runtime_frame_record(current) if current is not None else self._default_frame(node_id=node_id)
            mutated = frame_mutator(copy.deepcopy(target))
            if not isinstance(mutated, dict):
                raise TypeError('frame mutator must return a dict')
            record = self._runtime_frame_record(task=task, frame=self._sanitize_runtime_frame(mutated))
            self._store.upsert_task_runtime_frame(record)
            if publish_snapshot:
                self._publish_task_live_patch_locked(task=task, frame=record)
            return self.read_runtime_state(task_id) or {}

    def remove_frame(self, task_id: str, node_id: str, *, publish_snapshot: bool = False) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            self._store.delete_task_runtime_frame(task_id, node_id)
            if publish_snapshot:
                self._publish_task_live_patch_locked(task=task, removed_node_id=node_id)
            return self.read_runtime_state(task_id) or {}

    def refresh_task_view(self, task_id: str, *, mark_unread: bool) -> TaskRecord | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            if task is None:
                return None
            previous_status = str(task.status or '').strip().lower()
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
            terminal_transition = self._is_terminal_status(next_status) and not self._is_terminal_status(previous_status)
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
                }
            )
            self._store.upsert_task(updated)
            if self._is_terminal_status(next_status):
                self._store.replace_task_runtime_frames(updated.task_id, [])
                self.update_task_runtime_meta(updated.task_id)
            self._publish_task_summary_patch_locked(task=updated)
            if terminal_transition:
                self._publish_task_terminal_locked(task=updated)
            self._notify_task_terminal(updated, previous_status=previous_status)
            return updated

    def sync_task_read_models(self, task_id: str) -> TaskRecord | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            if task is None:
                return None
            for node in list(self._store.list_nodes(task_id) or []):
                self._sync_node_read_models_locked(node)
                self._sync_task_node_rounds_locked(node)
            self.refresh_task_view(task_id, mark_unread=False)
            if self._store.get_task_runtime_meta(task.task_id) is None:
                self.update_task_runtime_meta(task.task_id, last_stall_notice_bucket_minutes=0)
            return self._store.get_task(task_id)

    def _sync_node_read_models_locked(self, node: NodeRecord) -> None:
        self._projector.sync_node(
            self._task_projection_node_record(node),
            self._task_projection_node_detail_record(node),
        )

    def _sync_task_node_rounds_locked(self, node: NodeRecord) -> None:
        if node is None:
            return
        self._projector.sync_rounds_for_parent(
            node.task_id,
            node.node_id,
            self._task_projection_round_records(node),
        )

    def _task_projection_node_record(self, node: NodeRecord) -> TaskProjectionNodeRecord:
        rounds = self._task_projection_round_records(node)
        default_round_id = str(rounds[-1].round_id or '') if rounds else ''
        return TaskProjectionNodeRecord(
            node_id=node.node_id,
            task_id=node.task_id,
            parent_node_id=node.parent_node_id,
            root_node_id=node.root_node_id,
            depth=int(node.depth or 0),
            node_kind=str(node.node_kind or 'execution'),
            status=node.status,
            title=node.goal or node.node_id,
            updated_at=str(node.updated_at or ''),
            default_round_id=default_round_id,
            selected_round_id=default_round_id,
            round_options_count=len(rounds),
            sort_key=f'{str(node.created_at or "")}:{str(node.node_id or "")}',
            payload={
                'node_id': node.node_id,
                'parent_node_id': node.parent_node_id,
                'depth': int(node.depth or 0),
                'node_kind': str(node.node_kind or 'execution'),
                'status': node.status,
                'title': node.goal or node.node_id,
                'updated_at': str(node.updated_at or ''),
                'default_round_id': default_round_id,
                'selected_round_id': default_round_id,
                'round_options_count': len(rounds),
            },
        )

    def _task_projection_node_detail_record(self, node: NodeRecord) -> TaskProjectionNodeDetailRecord:
        prompt_summary = _single_line_text(node.prompt or node.goal or '', max_chars=400)
        return TaskProjectionNodeDetailRecord(
            node_id=node.node_id,
            task_id=node.task_id,
            updated_at=str(node.updated_at or ''),
            input_text=str(node.input or ''),
            input_ref=str(node.input_ref or ''),
            output_text=self._node_output_text(node),
            output_ref=self._node_output_ref(node),
            check_result=str(node.check_result or ''),
            check_result_ref=str(node.check_result_ref or ''),
            final_output=str(node.final_output or ''),
            final_output_ref=str(node.final_output_ref or ''),
            failure_reason=str(node.failure_reason or ''),
            prompt_summary=prompt_summary,
            execution_trace_ref='',
            payload={
                'node_id': node.node_id,
                'task_id': node.task_id,
                'parent_node_id': node.parent_node_id,
                'depth': int(node.depth or 0),
                'node_kind': str(node.node_kind or 'execution'),
                'status': node.status,
                'goal': str(node.goal or ''),
                'prompt_summary': prompt_summary,
                'input_text': str(node.input or ''),
                'input_ref': str(node.input_ref or ''),
                'output_text': self._node_output_text(node),
                'output_ref': self._node_output_ref(node),
                'check_result': str(node.check_result or ''),
                'check_result_ref': str(node.check_result_ref or ''),
                'final_output': str(node.final_output or ''),
                'final_output_ref': str(node.final_output_ref or ''),
                'failure_reason': str(node.failure_reason or ''),
                'updated_at': str(node.updated_at or ''),
                'execution_trace': build_execution_trace(node),
                'token_usage': node.token_usage.model_dump(mode='json'),
                'token_usage_by_model': [item.model_dump(mode='json') for item in list(node.token_usage_by_model or [])],
            },
        )

    def _task_projection_round_records(self, node: NodeRecord) -> list[TaskProjectionRoundRecord]:
        payload = (node.metadata or {}).get('spawn_operations') if isinstance(node.metadata, dict) else {}
        if not isinstance(payload, dict):
            return []
        records: list[TaskProjectionRoundRecord] = []
        for index, (round_id, operation) in enumerate(payload.items(), start=1):
            if not isinstance(operation, dict):
                continue
            entries = [item for item in list(operation.get('entries') or []) if isinstance(item, dict)]
            child_node_ids = [
                str(item.get('child_node_id') or '').strip()
                for item in entries
                if str(item.get('child_node_id') or '').strip()
            ]
            total_children = max(len(child_node_ids), len(entries))
            completed_children = sum(1 for item in entries if str(item.get('status') or '').strip().lower() == 'success')
            failed_children = sum(1 for item in entries if str(item.get('status') or '').strip().lower() == 'error')
            running_children = sum(
                1 for item in entries if str(item.get('status') or '').strip().lower() in {'queued', 'running'}
            )
            records.append(
                TaskProjectionRoundRecord(
                    task_id=node.task_id,
                    parent_node_id=node.node_id,
                    round_id=str(round_id or '').strip() or f'round:{index}',
                    round_index=index,
                    label=f'Round {index}',
                    is_latest=index == len(payload),
                    created_at=str(
                        operation.get('created_at')
                        or next((item.get('started_at') for item in entries if str(item.get('started_at') or '').strip()), '')
                        or str(node.updated_at or '')
                    ),
                    source='explicit',
                    total_children=total_children,
                    completed_children=completed_children,
                    running_children=running_children,
                    failed_children=failed_children,
                    child_node_ids=child_node_ids,
                )
            )
        return records

    def _runtime_frame_record(self, *, task: TaskRecord, frame: dict[str, Any]) -> TaskProjectionRuntimeFrameRecord:
        payload = dict(frame or {})
        node_id = str(payload.get('node_id') or '').strip()
        messages = [dict(item) for item in list(payload.get('messages') or []) if isinstance(item, dict)]
        if messages:
            serialized = json.dumps(messages, ensure_ascii=False, indent=2)
            _summary, ref = self._summarize_content(
                serialized,
                task_id=task.task_id,
                node_id=node_id,
                display_name=f'runtime-frame-messages:{node_id}',
                source_kind='task_runtime_messages',
                force=True,
            )
            if ref:
                payload['messages_ref'] = ref
                payload['messages_count'] = len(messages)
        payload.pop('messages', None)
        next_frame = {
            **self._default_frame(node_id=node_id, depth=int(payload.get('depth') or 0), node_kind=str(payload.get('node_kind') or 'execution'), phase=str(payload.get('phase') or '')),
            **payload,
        }
        runnable = bool(
            str(next_frame.get('phase') or '') in {'before_model', 'waiting_tool_results', 'after_model'}
            or self._frame_has_active_tools(next_frame)
        )
        waiting = bool(
            str(next_frame.get('phase') or '') in {'waiting_children', 'waiting_acceptance'}
            or self._frame_has_active_children(next_frame)
        )
        return TaskProjectionRuntimeFrameRecord(
            task_id=task.task_id,
            node_id=node_id,
            depth=int(next_frame.get('depth') or 0),
            node_kind=str(next_frame.get('node_kind') or 'execution'),
            phase=str(next_frame.get('phase') or ''),
            active=bool(node_id),
            runnable=runnable,
            waiting=waiting,
            updated_at=now_iso(),
            payload={
                'node_id': node_id,
                'depth': int(next_frame.get('depth') or 0),
                'node_kind': str(next_frame.get('node_kind') or 'execution'),
                'phase': str(next_frame.get('phase') or ''),
                'stage_mode': str(next_frame.get('stage_mode') or ''),
                'stage_status': str(next_frame.get('stage_status') or ''),
                'stage_goal': str(next_frame.get('stage_goal') or ''),
                'stage_total_steps': int(next_frame.get('stage_total_steps') or 0),
                'pending_tool_calls': [dict(item) for item in list(next_frame.get('pending_tool_calls') or []) if isinstance(item, dict)],
                'pending_child_specs': [dict(item) for item in list(next_frame.get('pending_child_specs') or []) if isinstance(item, dict)],
                'partial_child_results': [dict(item) for item in list(next_frame.get('partial_child_results') or []) if isinstance(item, dict)],
                'tool_calls': [dict(item) for item in list(next_frame.get('tool_calls') or []) if isinstance(item, dict)],
                'child_pipelines': [dict(item) for item in list(next_frame.get('child_pipelines') or []) if isinstance(item, dict)],
                'last_error': str(next_frame.get('last_error') or ''),
                'messages_ref': str(next_frame.get('messages_ref') or ''),
                'messages_count': int(next_frame.get('messages_count') or 0),
            },
        )

    def _hydrate_runtime_frame_record(self, record: TaskProjectionRuntimeFrameRecord) -> dict[str, Any]:
        payload = dict(record.payload or {})
        messages: list[dict[str, Any]] = []
        ref = str(payload.get('messages_ref') or '').strip()
        if ref:
            text = self._resolve_content_ref(ref)
            if text:
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = []
                if isinstance(parsed, list):
                    messages = [item for item in parsed if isinstance(item, dict)]
        return {
            'node_id': record.node_id,
            'depth': int(record.depth or 0),
            'node_kind': str(record.node_kind or 'execution'),
            'phase': str(record.phase or ''),
            'stage_mode': str(payload.get('stage_mode') or ''),
            'stage_status': str(payload.get('stage_status') or ''),
            'stage_goal': str(payload.get('stage_goal') or ''),
            'stage_total_steps': int(payload.get('stage_total_steps') or 0),
            'messages': messages,
            'pending_tool_calls': [dict(item) for item in list(payload.get('pending_tool_calls') or []) if isinstance(item, dict)],
            'pending_child_specs': [dict(item) for item in list(payload.get('pending_child_specs') or []) if isinstance(item, dict)],
            'partial_child_results': [dict(item) for item in list(payload.get('partial_child_results') or []) if isinstance(item, dict)],
            'tool_calls': [dict(item) for item in list(payload.get('tool_calls') or []) if isinstance(item, dict)],
            'child_pipelines': [dict(item) for item in list(payload.get('child_pipelines') or []) if isinstance(item, dict)],
            'last_error': str(payload.get('last_error') or ''),
        }

    def _resolve_content_ref(self, ref: str) -> str:
        store = self._content_store
        resolver = getattr(store, '_resolve', None) if store is not None else None
        if not callable(resolver):
            return ''
        try:
            text, _handle = resolver(ref=ref, path=None)
        except Exception:
            return ''
        return str(text or '')

    def _publish_task_summary_patch_locked(self, *, task: TaskRecord) -> None:
        payload = {'task': self._task_summary_payload(task)}
        self._append_task_event(task=task, event_type='task.summary.patch', data=payload)
        self._dispatch_live_event_locked(task=task, event_type='task.summary.patch', data=payload)

    def _publish_task_node_patch_locked(self, *, task: TaskRecord, node: NodeRecord) -> None:
        payload = {
            'node': {
                'node_id': node.node_id,
                'parent_node_id': node.parent_node_id,
                'depth': int(node.depth or 0),
                'node_kind': str(node.node_kind or 'execution'),
                'status': str(node.status or 'in_progress'),
                'title': str(node.goal or node.node_id),
                'updated_at': str(node.updated_at or ''),
            }
        }
        self._append_task_event(task=task, event_type='task.node.patch', data=payload)
        self._dispatch_live_event_locked(task=task, event_type='task.node.patch', data=payload)

    def _publish_task_live_patch_locked(
        self,
        *,
        task: TaskRecord,
        frame: TaskProjectionRuntimeFrameRecord | None = None,
        removed_node_id: str = '',
    ) -> None:
        payload = {
            'task_id': task.task_id,
            'runtime_summary': self._runtime_summary_payload(task.task_id),
            'frame': self._public_runtime_frame(self._hydrate_runtime_frame_record(frame)) if frame is not None else None,
            'removed_node_id': str(removed_node_id or '').strip(),
        }
        self._append_task_event(task=task, event_type='task.live.patch', data=payload)
        self._dispatch_live_event_locked(task=task, event_type='task.live.patch', data=payload)

    def _publish_task_terminal_locked(self, *, task: TaskRecord) -> None:
        payload = {'task': self._task_summary_payload(task)}
        self._append_task_event(task=task, event_type='task.terminal', data=payload)
        self._dispatch_live_event_locked(task=task, event_type='task.terminal', data=payload)

    def _dispatch_live_event_locked(self, *, task: TaskRecord, event_type: str, data: dict[str, Any]) -> None:
        if self._live_snapshot_publishers:
            for publisher in list(self._live_snapshot_publishers):
                try:
                    publisher(
                        task,
                        {
                            'event_type': str(event_type or '').strip(),
                            'session_id': str(task.session_id or 'web:shared').strip() or 'web:shared',
                            'task_id': task.task_id,
                            'data': dict(data or {}),
                        },
                        False,
                    )
                except Exception:
                    continue
            return
        if self._registry is None:
            return
        session_id = str(task.session_id or 'web:shared').strip() or 'web:shared'
        if event_type == 'task.summary.patch':
            for target_session_id in {session_id, 'all'}:
                self._registry.publish_task_list(
                    target_session_id,
                    build_envelope(
                        channel='task',
                        session_id=session_id,
                        task_id=task.task_id,
                        seq=self._registry.next_task_list_seq(target_session_id),
                        type=event_type,
                        data=dict(data or {}),
                    ),
                )
        if event_type == 'task.model.call':
            self._registry.publish_global_task(
                task.task_id,
                build_envelope(
                    channel='task',
                    session_id=session_id,
                    task_id=task.task_id,
                    seq=self._registry.next_global_task_seq(task.task_id),
                    type=event_type,
                    data=dict(data or {}),
                ),
            )
            return
        self._registry.publish_global_task(
            task.task_id,
            build_envelope(
                channel='task',
                session_id=session_id,
                task_id=task.task_id,
                seq=self._registry.next_global_task_seq(task.task_id),
                type=event_type,
                data=dict(data or {}),
            ),
        )

    @staticmethod
    def _node_output_text(node: NodeRecord) -> str:
        final_output = str(node.final_output or '').strip()
        if final_output:
            return final_output
        for entry in reversed(list(node.output or [])):
            content = str(entry.content or '').strip()
            if content:
                return content
        failure_reason = str(node.failure_reason or '').strip()
        if failure_reason:
            return failure_reason
        return ''

    @staticmethod
    def _node_output_ref(node: NodeRecord) -> str:
        final_ref = str(getattr(node, 'final_output_ref', '') or '').strip()
        if final_ref:
            return final_ref
        for entry in reversed(list(node.output or [])):
            content_ref = str(getattr(entry, 'content_ref', '') or '').strip()
            if content_ref:
                return content_ref
        return ''

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

    def _append_task_event(self, *, task: TaskRecord, event_type: str, data: dict[str, Any]) -> None:
        self._event_writer.append_task_event(
            task_id=task.task_id,
            session_id=task.session_id,
            event_type=event_type,
            data=data,
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
        if isinstance(runtime_state, dict):
            state = runtime_state
        else:
            frame_records = list(self._store.list_task_runtime_frames(task_id) or [])
            state = {
                'active_node_ids': [record.node_id for record in frame_records if bool(record.active)],
                'runnable_node_ids': [record.node_id for record in frame_records if bool(record.runnable)],
                'waiting_node_ids': [record.node_id for record in frame_records if bool(record.waiting)],
                'frames': [dict(record.payload or {}) for record in frame_records],
            }
        return {
            'active_node_ids': [str(item) for item in list(state.get('active_node_ids') or []) if str(item or '').strip()],
            'runnable_node_ids': [str(item) for item in list(state.get('runnable_node_ids') or []) if str(item or '').strip()],
            'waiting_node_ids': [str(item) for item in list(state.get('waiting_node_ids') or []) if str(item or '').strip()],
            'frames': [self._public_runtime_frame(item) for item in list(state.get('frames') or []) if isinstance(item, dict)],
        }

    @classmethod
    def _sanitize_runtime_state(cls, payload: dict[str, Any]) -> dict[str, Any]:
        state = dict(payload or {})
        state['last_visible_output_at'] = str(state.get('last_visible_output_at') or '').strip()
        try:
            state['last_stall_notice_bucket_minutes'] = max(0, int(state.get('last_stall_notice_bucket_minutes') or 0))
        except (TypeError, ValueError):
            state['last_stall_notice_bucket_minutes'] = 0
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
            'stage_mode': str(payload.get('stage_mode') or ''),
            'stage_status': str(payload.get('stage_status') or ''),
            'stage_goal': str(payload.get('stage_goal') or ''),
            'stage_total_steps': int(payload.get('stage_total_steps') or 0),
            'messages': [dict(item) for item in list(payload.get('messages') or []) if isinstance(item, dict)],
            'pending_tool_calls': [dict(item) for item in list(payload.get('pending_tool_calls') or []) if isinstance(item, dict)],
            'pending_child_specs': [dict(item) for item in list(payload.get('pending_child_specs') or []) if isinstance(item, dict)],
            'partial_child_results': [dict(item) for item in list(payload.get('partial_child_results') or []) if isinstance(item, dict)],
            'tool_calls': [dict(item) for item in list(payload.get('tool_calls') or []) if isinstance(item, dict)],
            'child_pipelines': [dict(item) for item in list(payload.get('child_pipelines') or []) if isinstance(item, dict)],
            'last_error': str(payload.get('last_error') or ''),
        }

    @staticmethod
    def _public_runtime_frame(frame: dict[str, Any]) -> dict[str, Any]:
        payload = dict(frame or {})
        return {
            'node_id': str(payload.get('node_id') or '').strip(),
            'depth': int(payload.get('depth') or 0),
            'node_kind': str(payload.get('node_kind') or 'execution'),
            'phase': str(payload.get('phase') or ''),
            'stage_mode': str(payload.get('stage_mode') or ''),
            'stage_status': str(payload.get('stage_status') or ''),
            'stage_goal': str(payload.get('stage_goal') or ''),
            'stage_total_steps': int(payload.get('stage_total_steps') or 0),
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

    def _notify_task_visible_output(self, task_id: str, *, occurred_at: str) -> None:
        task_key = str(task_id or '').strip()
        timestamp = str(occurred_at or '').strip() or _precise_now_iso()
        if not task_key:
            return
        for listener in list(self._task_visible_output_listeners):
            try:
                listener(task_key, occurred_at=timestamp)
            except Exception:
                continue
