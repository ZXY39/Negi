from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from g3ku.content import ContentNavigationService, content_summary_and_ref, parse_content_envelope
from g3ku.content.navigation import INLINE_CHAR_LIMIT
from main.ids import new_stage_id, new_stage_round_id
from main.models import (
    FAILURE_CLASS_BUSINESS_UNPASSED,
    FAILURE_CLASS_ENGINE,
    FAILURE_CLASS_NON_RETRYABLE_BLOCKED,
    ExecutionStageKeyRef,
    ExecutionStageRecord,
    ExecutionStageRound,
    ExecutionStageState,
    NodeToolFileChange,
    NodeOutputEntry,
    NodeRecord,
    TaskRecord,
    normalize_failure_class,
    normalize_execution_stage_metadata,
    normalize_final_acceptance_metadata,
    normalize_tool_file_changes,
)
from main.runtime.stage_budget import (
    CONTROL_STAGE_TOOL_NAMES,
    FINAL_RESULT_TOOL_NAME,
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
    TaskProjectionToolResultRecord,
)
from main.monitoring.task_event_writer import TaskEventWriter
from main.monitoring.task_projector import TaskProjector
from main.protocol import build_envelope, now_iso
from main.runtime.execution_trace_compaction import compact_tool_step_for_summary
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
_EXECUTION_STAGE_KIND_NORMAL = 'normal'
_EXECUTION_STAGE_KIND_COMPRESSION = 'compression'
_STAGE_ARCHIVE_RETAIN_COMPLETED = 20
_STAGE_ARCHIVE_BATCH_SIZE = 10
_NON_BUDGET_EXECUTION_TOOLS = {
    _EXECUTION_STAGE_TOOL_NAME,
    FINAL_RESULT_TOOL_NAME,
    'spawn_child_nodes',
    'wait_tool_execution',
    'stop_tool_execution',
}
_NON_SUBSTANTIVE_EXECUTION_PROGRESS_TOOLS = {
    _EXECUTION_STAGE_TOOL_NAME,
    FINAL_RESULT_TOOL_NAME,
    *CONTROL_STAGE_TOOL_NAMES,
}
_LATEST_SPAWN_STAGE_KEY_REF_NOTE = '最近一次 spawn_child_nodes 返回结果'


_STAGE_GOAL_CHAR_LIMIT = 240
_STAGE_SUMMARY_CHAR_LIMIT = 800
_STAGE_KEY_REF_LIMIT = 4


def _default_governance_state(*, node_count_baseline: int = 1) -> dict[str, Any]:
    return {
        'enabled': True,
        'frozen': False,
        'review_inflight': False,
        'depth_baseline': 1,
        'node_count_baseline': max(1, int(node_count_baseline or 1)),
        'hard_limited_depth': None,
        'latest_limit_reason': '',
        'supervision_disabled_after_limit': False,
        'history': [],
    }

class TaskLogService:
    def __init__(
        self,
        *,
        store,
        file_store: TaskFileStore,
        registry=None,
        content_store: ContentNavigationService | None = None,
        debug_recorder=None,
        event_history_enabled: bool = True,
        live_patch_persist_window_ms: int = 1000,
    ):
        self._store = store
        self._file_store = file_store
        self._registry = registry
        self._content_store = content_store
        self._debug_recorder = debug_recorder
        self._event_writer = TaskEventWriter(store=store)
        self._projector = TaskProjector(store=store)
        self._live_snapshot_publishers: list[Callable[[TaskRecord, dict[str, Any], bool], None]] = []
        self._task_terminal_listeners: list[Callable[[TaskRecord], None]] = []
        self._task_visible_output_listeners: list[Callable[[str, str], None]] = []
        self._summary_metric_reporters: list[Callable[[str, float], None]] = []
        self._task_locks: dict[str, threading.RLock] = {}
        self._task_locks_guard = threading.Lock()
        self._event_history_enabled = bool(event_history_enabled)
        self._live_patch_persist_window_ms = max(0, int(live_patch_persist_window_ms or 0))
        self._live_patch_history_guard = threading.Lock()
        self._pending_live_patch_history: dict[str, dict[str, Any]] = {}
        self._live_patch_history_timers: dict[str, threading.Timer] = {}
        self._last_live_patch_boundary_key: dict[str, tuple[Any, ...]] = {}
        self._last_node_patch_persist_fingerprints: dict[tuple[str, str], str] = {}

    def add_live_snapshot_publisher(self, publisher: Callable[[TaskRecord, dict[str, Any], bool], None]) -> None:
        if callable(publisher):
            self._live_snapshot_publishers.append(publisher)

    def add_task_terminal_listener(self, listener: Callable[[TaskRecord], None]) -> None:
        if callable(listener):
            self._task_terminal_listeners.append(listener)

    def add_task_visible_output_listener(self, listener: Callable[[str, str], None]) -> None:
        if callable(listener):
            self._task_visible_output_listeners.append(listener)

    def add_summary_metric_reporter(self, reporter: Callable[[str, float], None]) -> None:
        if callable(reporter):
            self._summary_metric_reporters.append(reporter)

    def close(self) -> None:
        pending_task_ids: list[str] = []
        with self._live_patch_history_guard:
            timers = list(self._live_patch_history_timers.items())
            self._live_patch_history_timers = {}
            pending_task_ids = list(self._pending_live_patch_history.keys())
        for _task_id, timer in timers:
            try:
                timer.cancel()
            except Exception:
                continue
        for task_id in pending_task_ids:
            self.flush_live_patch_history(task_id)

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

    def flush_live_patch_history(self, task_id: str) -> None:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return
        with self._live_patch_history_guard:
            entry = self._pending_live_patch_history.pop(normalized_task_id, None)
            timer = self._live_patch_history_timers.pop(normalized_task_id, None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if not isinstance(entry, dict):
            return
        task = entry.get('task')
        payload = entry.get('payload')
        if not isinstance(task, TaskRecord) or not isinstance(payload, dict):
            return
        self._append_task_event(task=task, event_type='task.live.patch', data=payload)
        self._last_live_patch_boundary_key[normalized_task_id] = self._live_patch_boundary_key(task=task, payload=payload)

    def _task_lock(self, task_id: str) -> threading.RLock:
        key = str(task_id or '').strip()
        with self._task_locks_guard:
            lock = self._task_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._task_locks[key] = lock
            return lock

    def _buffer_task_live_patch_locked(self, *, task: TaskRecord, payload: dict[str, Any]) -> None:
        if not self._event_history_enabled:
            self._append_task_event(task=task, event_type='task.live.patch', data=payload)
            return
        task_id = str(task.task_id or '').strip()
        if not task_id:
            return
        boundary_key = self._live_patch_boundary_key(task=task, payload=payload)
        immediate = (
            self._live_patch_persist_window_ms <= 0
            or bool(task.is_paused)
            or bool(task.pause_requested)
            or self._is_terminal_status(task.status)
            or boundary_key != self._last_live_patch_boundary_key.get(task_id)
            or str(payload.get('removed_node_id') or '').strip()
        )
        if immediate:
            self.flush_live_patch_history(task_id)
            self._append_task_event(task=task, event_type='task.live.patch', data=payload)
            self._last_live_patch_boundary_key[task_id] = boundary_key
            return
        with self._live_patch_history_guard:
            self._pending_live_patch_history[task_id] = {
                'task': task.model_copy(deep=True),
                'payload': copy.deepcopy(payload),
            }
            timer = self._live_patch_history_timers.get(task_id)
            if timer is not None:
                return
            timer = threading.Timer(
                max(0.001, float(self._live_patch_persist_window_ms) / 1000.0),
                lambda target_task_id=task_id: self.flush_live_patch_history(target_task_id),
            )
            timer.daemon = True
            self._live_patch_history_timers[task_id] = timer
            timer.start()

    @staticmethod
    def _live_patch_boundary_key(*, task: TaskRecord, payload: dict[str, Any]) -> tuple[Any, ...]:
        runtime_summary = dict(payload.get('runtime_summary') or {}) if isinstance(payload.get('runtime_summary'), dict) else {}
        frame = dict(payload.get('frame') or {}) if isinstance(payload.get('frame'), dict) else {}
        active_ids = tuple(str(item or '').strip() for item in list(runtime_summary.get('active_node_ids') or []) if str(item or '').strip())
        runnable_ids = tuple(str(item or '').strip() for item in list(runtime_summary.get('runnable_node_ids') or []) if str(item or '').strip())
        waiting_ids = tuple(str(item or '').strip() for item in list(runtime_summary.get('waiting_node_ids') or []) if str(item or '').strip())
        return (
            active_ids,
            runnable_ids,
            waiting_ids,
            str(frame.get('phase') or '').strip(),
            str(frame.get('node_id') or '').strip(),
            bool(task.is_paused),
            bool(task.pause_requested),
            str(task.status or '').strip().lower(),
        )

    @staticmethod
    def _node_patch_persist_fingerprint(payload: dict[str, Any]) -> str:
        node_payload = dict(payload.get('node') or {}) if isinstance(payload.get('node'), dict) else {}
        normalized = {
            'node_id': str(node_payload.get('node_id') or '').strip(),
            'parent_node_id': str(node_payload.get('parent_node_id') or '').strip(),
            'depth': int(node_payload.get('depth') or 0),
            'node_kind': str(node_payload.get('node_kind') or '').strip(),
            'status': str(node_payload.get('status') or '').strip(),
            'title': str(node_payload.get('title') or '').strip(),
            'children_fingerprint': str(node_payload.get('children_fingerprint') or '').strip(),
        }
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _default_frame(*, node_id: str = '', depth: int = 0, node_kind: str = 'execution', phase: str = '') -> dict[str, Any]:
        return {
            'node_id': str(node_id or '').strip(),
            'depth': int(depth or 0),
            'node_kind': str(node_kind or 'execution').strip() or 'execution',
            'phase': str(phase or '').strip(),
            'await_marker': '',
            'await_started_at': '',
            'stage_mode': '',
            'stage_status': '',
            'stage_goal': '',
            'stage_total_steps': 0,
            'active_round_id': '',
            'active_round_tool_call_ids': [],
            'active_round_started_at': '',
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

    def _workspace_root_for_runtime_meta(self) -> Path:
        workspace = getattr(self._content_store, '_workspace', None) if self._content_store is not None else None
        return Path(workspace).resolve(strict=False) if workspace is not None else Path.cwd().resolve()

    def _fallback_task_temp_dir(self) -> str:
        return str((self._workspace_root_for_runtime_meta() / 'temp').resolve(strict=False))

    def _normalize_task_temp_dir(self, value: Any) -> str:
        raw = str(value or '').strip()
        if not raw:
            return self._fallback_task_temp_dir()
        try:
            return str(Path(raw).expanduser().resolve(strict=False))
        except Exception:
            return self._fallback_task_temp_dir()

    def _default_runtime_meta(self, *, last_visible_output_at: str = '') -> dict[str, Any]:
        return {
            'updated_at': now_iso(),
            'last_visible_output_at': str(last_visible_output_at or '').strip(),
            'last_stall_notice_bucket_minutes': 0,
            'task_temp_dir': self._fallback_task_temp_dir(),
            'dispatch_limits': {'execution': 0, 'inspection': 0},
            'dispatch_running': {'execution': 0, 'inspection': 0},
            'dispatch_queued': {'execution': 0, 'inspection': 0},
            'summary_fingerprint': '',
            'summary_last_published_at': '',
            'governance': _default_governance_state(),
        }

    @classmethod
    def _sanitize_governance_state(cls, payload: Any) -> dict[str, Any]:
        current = dict(payload or {}) if isinstance(payload, dict) else {}
        history_payload: list[dict[str, Any]] = []
        for item in list(current.get('history') or []):
            if not isinstance(item, dict):
                continue
            snapshot = dict(item.get('trigger_snapshot') or {}) if isinstance(item.get('trigger_snapshot'), dict) else {}
            history_payload.append(
                {
                    'triggered_at': str(item.get('triggered_at') or '').strip(),
                    'trigger_reason': str(item.get('trigger_reason') or '').strip(),
                    'trigger_snapshot': {
                        'max_depth': max(0, int(snapshot.get('max_depth') or 0)),
                        'total_nodes': max(0, int(snapshot.get('total_nodes') or 0)),
                    },
                    'decision': str(item.get('decision') or '').strip(),
                    'decision_reason': str(item.get('decision_reason') or '').strip(),
                    'decision_evidence': [
                        str(line).strip()
                        for line in list(item.get('decision_evidence') or [])
                        if str(line).strip()
                    ],
                    'limited_depth': (
                        None if item.get('limited_depth') in {None, ''} else max(0, int(item.get('limited_depth') or 0))
                    ),
                    'error_text': str(item.get('error_text') or '').strip(),
                }
            )
        hard_limited_depth = current.get('hard_limited_depth')
        return {
            'enabled': bool(current.get('enabled', True)),
            'frozen': bool(current.get('frozen')),
            'review_inflight': bool(current.get('review_inflight')),
            'depth_baseline': max(1, int(current.get('depth_baseline') or 1)),
            'node_count_baseline': max(1, int(current.get('node_count_baseline') or 1)),
            'hard_limited_depth': None if hard_limited_depth in {None, ''} else max(0, int(hard_limited_depth or 0)),
            'latest_limit_reason': str(current.get('latest_limit_reason') or '').strip(),
            'supervision_disabled_after_limit': bool(current.get('supervision_disabled_after_limit')),
            'history': history_payload,
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
                parent_node_id = str(node.parent_node_id or '').strip()
                if parent_node_id:
                    parent = self._store.get_node(parent_node_id)
                    if parent is not None and str(parent.task_id or '').strip() == str(task.task_id or '').strip():
                        self._sync_node_read_models_locked(parent)
                        self._sync_task_node_rounds_locked(parent)
                        self._publish_task_node_patch_locked(task=task, node=parent)
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

    def record_tool_result_batch(
        self,
        *,
        task_id: str,
        node_id: str,
        response_tool_calls: list[Any],
        results: list[dict[str, Any]],
    ) -> list[TaskProjectionToolResultRecord]:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            node = self._store.get_node(node_id)
            if task is None or node is None:
                return []
            if str(node.task_id or '').strip() != str(task_id or '').strip():
                return []

            existing_records = list(self._store.list_task_node_tool_results(task_id, node_id) or [])
            order_by_call_id = {
                str(item.tool_call_id or '').strip(): int(item.order_index or 0)
                for item in existing_records
                if str(item.tool_call_id or '').strip()
            }
            next_order_index = max([int(item.order_index or 0) for item in existing_records], default=0) + 1
            persisted: list[TaskProjectionToolResultRecord] = []

            for index, call in enumerate(list(response_tool_calls or [])):
                tool_message = dict((results[index] or {}).get('tool_message') or {}) if index < len(results) else {}
                live_state = dict((results[index] or {}).get('live_state') or {}) if index < len(results) else {}
                tool_call_id = str(
                    tool_message.get('tool_call_id')
                    or getattr(call, 'id', '')
                    or live_state.get('tool_call_id')
                    or ''
                ).strip()
                if not tool_call_id:
                    continue
                order_index = order_by_call_id.get(tool_call_id)
                if not order_index:
                    order_index = next_order_index
                    next_order_index += 1
                    order_by_call_id[tool_call_id] = order_index

                arguments = self._normalize_tool_call_arguments(getattr(call, 'arguments', {}))
                arguments_text = json.dumps(arguments, ensure_ascii=False, indent=2) if arguments else ''
                content = tool_message.get('content')
                preview_text, output_ref = self._tool_result_summary_and_ref(content)
                record = TaskProjectionToolResultRecord(
                    task_id=task_id,
                    node_id=node_id,
                    tool_call_id=tool_call_id,
                    order_index=order_index,
                    tool_name=str(
                        tool_message.get('name')
                        or getattr(call, 'name', '')
                        or live_state.get('tool_name')
                        or 'tool'
                    ),
                    arguments_text=arguments_text,
                    status=str(live_state.get('status') or tool_message.get('status') or 'success'),
                    started_at=str(tool_message.get('started_at') or live_state.get('started_at') or ''),
                    finished_at=str(tool_message.get('finished_at') or live_state.get('finished_at') or ''),
                    elapsed_seconds=self._coerce_elapsed_seconds(
                        live_state.get('elapsed_seconds', tool_message.get('elapsed_seconds'))
                    ),
                    output_preview_text=str(preview_text or ''),
                    output_ref=str(output_ref or ''),
                    ephemeral=bool(tool_message.get('ephemeral') or live_state.get('ephemeral')),
                    payload={
                        'parsed_payload': self._parse_tool_result_payload(content),
                    },
                )
                self._store.upsert_task_node_tool_result(record)
                persisted.append(record)

            if persisted:
                self._sync_node_read_models_locked(node)
                self._publish_task_node_patch_locked(task=task, node=node)
                self.refresh_task_view(task_id, mark_unread=True)
            return persisted

    @staticmethod
    def _tool_result_payload_ref(payload: dict[str, Any]) -> str:
        output_ref = str(
            payload.get('output_ref')
            or payload.get('resolved_ref')
            or payload.get('wrapper_ref')
            or payload.get('requested_ref')
            or payload.get('ref')
            or ''
        ).strip()
        if output_ref:
            return output_ref
        nested = parse_content_envelope(payload.get('content_ref'))
        if nested is not None:
            return str(nested.ref or nested.wrapper_ref or nested.resolved_ref or '').strip()
        return ''

    @classmethod
    def _tool_result_summary_and_ref(cls, value: Any) -> tuple[str, str]:
        summary, output_ref = content_summary_and_ref(value)
        envelope = parse_content_envelope(value)
        if envelope is not None:
            resolved_ref = str(envelope.resolved_ref or envelope.ref or envelope.wrapper_ref or '').strip()
            if resolved_ref:
                output_ref = resolved_ref
            if str(envelope.summary or '').strip():
                summary = str(envelope.summary or '').strip()
            return summary, output_ref
        parsed_payload = cls._parse_tool_result_payload(value)
        if isinstance(parsed_payload, dict):
            payload_ref = cls._tool_result_payload_ref(parsed_payload)
            if payload_ref:
                output_ref = payload_ref
            payload_summary = str(parsed_payload.get('summary') or '').strip()
            if payload_summary:
                summary = payload_summary
        return summary, output_ref

    def upsert_synthetic_tool_result(
        self,
        *,
        task_id: str,
        node_id: str,
        tool_call_id: str,
        tool_name: str,
        status: str,
        output_text: str = '',
        output_ref: str = '',
        arguments_text: str = '',
        started_at: str = '',
        finished_at: str = '',
        elapsed_seconds: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TaskProjectionToolResultRecord | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            node = self._store.get_node(node_id)
            if task is None or node is None:
                return None
            if str(node.task_id or '').strip() != str(task_id or '').strip():
                return None
            existing_records = list(self._store.list_task_node_tool_results(task_id, node_id) or [])
            order_by_call_id = {
                str(item.tool_call_id or '').strip(): int(item.order_index or 0)
                for item in existing_records
                if str(item.tool_call_id or '').strip()
            }
            order_index = order_by_call_id.get(str(tool_call_id or '').strip())
            if not order_index:
                order_index = max([int(item.order_index or 0) for item in existing_records], default=0) + 1
            record = TaskProjectionToolResultRecord(
                task_id=task_id,
                node_id=node_id,
                tool_call_id=str(tool_call_id or '').strip(),
                order_index=int(order_index or 0),
                tool_name=str(tool_name or '').strip() or 'tool',
                arguments_text=str(arguments_text or ''),
                status=str(status or '').strip(),
                started_at=str(started_at or ''),
                finished_at=str(finished_at or ''),
                elapsed_seconds=self._coerce_elapsed_seconds(elapsed_seconds),
                output_preview_text=str(output_text or ''),
                output_ref=str(output_ref or ''),
                ephemeral=False,
                payload={'parsed_payload': dict(payload or {})},
            )
            self._store.upsert_task_node_tool_result(record)
            return record

    @staticmethod
    def _normalize_tool_call_arguments(arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            source = arguments
        elif isinstance(arguments, str):
            text = str(arguments or '').strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            if not isinstance(parsed, dict):
                return {}
            source = parsed
        elif arguments is None:
            return {}
        else:
            try:
                parsed = dict(arguments)
            except Exception:
                return {}
            if not isinstance(parsed, dict):
                return {}
            source = parsed
        normalized: dict[str, Any] = {}
        for key, value in source.items():
            key_text = str(key or '').strip()
            if key_text:
                normalized[key_text] = value
        return normalized

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
                    self._publish_task_token_patch_locked(task=task)
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
            root_node_id = str(getattr(task, 'root_node_id', '') or '').strip() if task is not None else ''
            if updated is not None and task is not None and delta_usage is not None and root_node_id and node_id != root_node_id:
                self._touch_task_summary_locked(task=task, mark_unread=True, updated_at=changed_at)
            else:
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

    def record_node_file_change(
        self,
        task_id: str,
        node_id: str,
        *,
        path: str,
        change_type: str,
    ) -> NodeRecord | None:
        normalized_path = str(path or '').strip()
        normalized_change_type = str(change_type or 'modified').strip().lower()
        if not normalized_path:
            return None
        if normalized_change_type not in {'created', 'modified'}:
            normalized_change_type = 'modified'
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            if task is None:
                return None

            def _mutate(record: NodeRecord) -> NodeRecord:
                metadata = dict(record.metadata or {})
                current_changes = normalize_tool_file_changes(metadata.get('tool_file_changes'))
                next_changes = self._merge_tool_file_changes(
                    current_changes,
                    path=normalized_path,
                    change_type=normalized_change_type,
                )
                current_payload = [item.model_dump(mode='json') for item in current_changes]
                next_payload = [item.model_dump(mode='json') for item in next_changes]
                if current_payload == next_payload:
                    return record
                metadata['tool_file_changes'] = next_payload
                return record.model_copy(update={'metadata': metadata, 'updated_at': now_iso()})

            updated = self._store.update_node(node_id, _mutate)
            if updated is not None:
                self._sync_node_read_models_locked(updated)
                self._publish_task_node_patch_locked(task=task, node=updated)
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

    @staticmethod
    def _merge_tool_file_changes(
        current_changes: list[NodeToolFileChange],
        *,
        path: str,
        change_type: str,
    ) -> list[NodeToolFileChange]:
        normalized_path = str(path or '').strip()
        normalized_change_type = 'created' if str(change_type or '').strip().lower() == 'created' else 'modified'
        merged: list[NodeToolFileChange] = []
        found = False
        for item in list(current_changes or []):
            if str(item.path or '').strip() != normalized_path:
                merged.append(item)
                continue
            found = True
            merged.append(
                item.model_copy(
                    update={
                        'path': normalized_path,
                        'change_type': 'created'
                        if str(item.change_type or '').strip().lower() == 'created' or normalized_change_type == 'created'
                        else 'modified',
                    }
                )
            )
        if not found:
            merged.append(NodeToolFileChange(path=normalized_path, change_type=normalized_change_type))
        return merged

    def mark_task_read(self, task_id: str) -> TaskRecord | None:
        with self._task_lock(task_id):
            previous = self._store.get_task(task_id)
            updated = self._store.update_task(
                task_id,
                lambda task: task.model_copy(update={'is_unread': False, 'updated_at': now_iso()}),
            )
            if updated is not None:
                self._publish_task_summary_patch_locked(task=updated, previous_task=previous)
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
            if bool(updated.is_paused) or bool(updated.pause_requested) or bool(updated.cancel_requested):
                self.flush_live_patch_history(task_id)
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
    def _parse_tool_result_payload(content: Any) -> dict[str, Any]:
        if not isinstance(content, str):
            return {}
        text = content.strip()
        if not text or text[:1] not in {'{', '['}:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        if not isinstance(parsed, dict):
            return {}
        payload = dict(parsed)
        wrapper_ref = str(payload.get('wrapper_ref') or payload.get('ref') or '').strip()
        if wrapper_ref and not str(payload.get('wrapper_ref') or '').strip():
            payload['wrapper_ref'] = wrapper_ref
        return payload

    @staticmethod
    def _coerce_elapsed_seconds(value: Any) -> float | None:
        try:
            if value is None or value == '':
                return None
            return round(max(0.0, float(value)), 1)
        except (TypeError, ValueError):
            return None

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
    def _execution_stage_has_substantive_progress(stage: ExecutionStageRecord | None) -> bool:
        if stage is None:
            return False
        for round_item in list(stage.rounds or []):
            tool_names = [
                str(name or '').strip()
                for name in list(round_item.tool_names or [])
                if str(name or '').strip()
            ]
            if any(name not in _NON_SUBSTANTIVE_EXECUTION_PROGRESS_TOOLS for name in tool_names):
                return True
        return False

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

    @staticmethod
    def _normalize_stage_key_refs(value: Any) -> list[ExecutionStageKeyRef]:
        refs: list[ExecutionStageKeyRef] = []
        for item in list(value or []):
            try:
                key_ref = ExecutionStageKeyRef.model_validate(item)
            except Exception:
                continue
            normalized_ref = str(key_ref.ref or '').strip()
            normalized_note = str(key_ref.note or '').strip()
            if not normalized_ref or not normalized_note:
                continue
            refs.append(
                key_ref.model_copy(
                    update={
                        'ref': normalized_ref,
                        'note': _single_line_text(normalized_note, max_chars=160),
                    }
                )
            )
        return refs

    @staticmethod
    def _clip_stage_text(value: Any, *, limit: int) -> str:
        text = " ".join(str(value or "").split()).strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)].rstrip()}..."

    def _canonicalize_stage_key_refs(self, refs: list[ExecutionStageKeyRef]) -> list[ExecutionStageKeyRef]:
        normalized: list[ExecutionStageKeyRef] = []
        seen: set[str] = set()
        navigator = getattr(self, '_content_store', None)
        for item in list(refs or []):
            current = item
            target_ref = str(item.ref or '').strip()
            if navigator is not None and target_ref:
                try:
                    described = navigator.describe(ref=target_ref, view='canonical')
                    resolved_ref = str((described or {}).get('resolved_ref') or '').strip()
                    if resolved_ref:
                        current = item.model_copy(update={'ref': resolved_ref})
                except Exception:
                    current = item
            dedupe_key = str(current.ref or '').strip()
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized.append(current)
            if len(normalized) >= _STAGE_KEY_REF_LIMIT:
                break
        return normalized

    def _latest_spawn_stage_key_ref_locked(
        self,
        *,
        task_id: str,
        node_id: str,
        active_stage: ExecutionStageRecord | None,
    ) -> ExecutionStageKeyRef | None:
        if active_stage is None:
            return None
        spawn_call_ids: list[str] = []
        for round_record in list(active_stage.rounds or []):
            tool_names = [str(item or '').strip() for item in list(round_record.tool_names or [])]
            tool_call_ids = [str(item or '').strip() for item in list(round_record.tool_call_ids or [])]
            for index, tool_name in enumerate(tool_names):
                if tool_name != 'spawn_child_nodes':
                    continue
                tool_call_id = tool_call_ids[index] if index < len(tool_call_ids) else ''
                if tool_call_id:
                    spawn_call_ids.append(tool_call_id)
        if not spawn_call_ids:
            return None
        records_by_call_id = {
            str(item.tool_call_id or '').strip(): item
            for item in list(self._store.list_task_node_tool_results(task_id, node_id) or [])
            if str(item.tool_call_id or '').strip()
        }
        for tool_call_id in reversed(spawn_call_ids):
            record = records_by_call_id.get(tool_call_id)
            output_ref = str(getattr(record, 'output_ref', '') or '').strip() if record is not None else ''
            if output_ref:
                return ExecutionStageKeyRef(
                    ref=output_ref,
                    note=_LATEST_SPAWN_STAGE_KEY_REF_NOTE,
                )
        return None

    def _externalize_completed_stage_batches_locked(
        self,
        *,
        task: TaskRecord,
        node_id: str,
        state: ExecutionStageState,
    ) -> ExecutionStageState:
        stages = list(state.stages or [])
        if not stages:
            return state
        while True:
            completed_normal = [
                (index, stage)
                for index, stage in enumerate(stages)
                if str(stage.stage_kind or _EXECUTION_STAGE_KIND_NORMAL) == _EXECUTION_STAGE_KIND_NORMAL
                and str(stage.status or '') != _EXECUTION_STAGE_STATUS_ACTIVE
            ]
            if len(completed_normal) <= _STAGE_ARCHIVE_RETAIN_COMPLETED:
                break
            batch = completed_normal[:_STAGE_ARCHIVE_BATCH_SIZE]
            archive_stages = [stage.model_dump(mode='json') for _, stage in batch]
            if not archive_stages:
                break
            stage_index_start = int(batch[0][1].stage_index or 0)
            stage_index_end = int(batch[-1][1].stage_index or 0)
            archive_payload = {
                'task_id': str(task.task_id or ''),
                'node_id': str(node_id or ''),
                'stage_index_start': stage_index_start,
                'stage_index_end': stage_index_end,
                'stages': archive_stages,
            }
            archive_summary, archive_ref = self._summarize_content(
                json.dumps(archive_payload, ensure_ascii=False, indent=2),
                task_id=task.task_id,
                node_id=node_id,
                display_name=f'stage-history:{node_id}:{stage_index_start}-{stage_index_end}',
                source_kind='stage_history_archive',
                force=True,
            )
            compression_summary = (
                archive_summary
                or f'Archived completed stages {stage_index_start}-{stage_index_end} into stage history archive.'
            )
            compression_stage = ExecutionStageRecord(
                stage_id=new_stage_id(),
                stage_index=stage_index_end,
                stage_kind=_EXECUTION_STAGE_KIND_COMPRESSION,
                system_generated=True,
                mode=_EXECUTION_STAGE_MODE_SELF,
                status=_EXECUTION_STAGE_STATUS_COMPLETED,
                stage_goal=f'Archive completed stage history {stage_index_start}-{stage_index_end}',
                completed_stage_summary=compression_summary,
                key_refs=[],
                archive_ref=str(archive_ref or '').strip(),
                archive_stage_index_start=stage_index_start,
                archive_stage_index_end=stage_index_end,
                tool_round_budget=0,
                tool_rounds_used=0,
                created_at=now_iso(),
                finished_at=now_iso(),
                rounds=[],
            )
            batch_indexes = {index for index, _stage in batch}
            insert_at = min(batch_indexes)
            next_stages: list[ExecutionStageRecord] = []
            for index, stage in enumerate(stages):
                if index == insert_at:
                    next_stages.append(compression_stage)
                if index in batch_indexes:
                    continue
                next_stages.append(stage)
            stages = next_stages
        return ExecutionStageState(
            active_stage_id=str(state.active_stage_id or '').strip(),
            transition_required=bool(state.transition_required),
            stages=stages,
        )

    def execution_stage_gate_snapshot(self, task_id: str, node_id: str) -> dict[str, Any]:
        with self._task_lock(task_id):
            node = self._store.get_node(node_id)
            state = self._execution_stage_state(node)
            active = self._active_execution_stage(state)
            completed_stages = [
                {
                    'stage_index': int(stage.stage_index or 0),
                    'stage_kind': str(stage.stage_kind or _EXECUTION_STAGE_KIND_NORMAL),
                    'system_generated': bool(stage.system_generated),
                    'mode': str(stage.mode or ''),
                    'status': str(stage.status or ''),
                    'stage_goal': str(stage.stage_goal or ''),
                    'completed_stage_summary': str(stage.completed_stage_summary or ''),
                    'key_refs': [item.model_dump(mode='json') for item in list(stage.key_refs or [])],
                    'archive_ref': str(stage.archive_ref or ''),
                    'archive_stage_index_start': int(stage.archive_stage_index_start or 0),
                    'archive_stage_index_end': int(stage.archive_stage_index_end or 0),
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
        return {
            'has_active_stage': bool(snapshot.get('has_active_stage')) if isinstance(snapshot, dict) else False,
            'transition_required': bool(snapshot.get('transition_required')) if isinstance(snapshot, dict) else False,
            'active_stage': dict(active or {}) if isinstance(active, dict) else None,
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

    def submit_next_stage(
        self,
        task_id: str,
        node_id: str,
        *,
        stage_goal: str,
        tool_round_budget: int,
        completed_stage_summary: str = '',
        key_refs: list[dict[str, Any]] | None = None,
        final: bool = False,
    ) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            node = self._store.get_node(node_id)
            if node is None:
                raise ValueError(f'node not found: {node_id}')
            normalized_goal = self._clip_stage_text(stage_goal, limit=_STAGE_GOAL_CHAR_LIMIT)
            normalized_budget = int(tool_round_budget or 0)
            normalized_completed_summary = self._clip_stage_text(completed_stage_summary, limit=_STAGE_SUMMARY_CHAR_LIMIT)
            normalized_key_refs = self._canonicalize_stage_key_refs(self._normalize_stage_key_refs(key_refs))
            if not normalized_goal:
                raise ValueError('stage_goal must not be empty')
            if normalized_budget < 1 or normalized_budget > 10:
                raise ValueError('tool_round_budget must be between 1 and 10')
            state = self._execution_stage_state(node)
            active = self._active_execution_stage(state)
            if (
                active is not None
                and str(active.status or '') == _EXECUTION_STAGE_STATUS_ACTIVE
                and not self._execution_stage_has_substantive_progress(active)
            ):
                raise ValueError(
                    'current active stage has no substantive progress yet; '
                    'do not call submit_next_stage again before using a non-control tool '
                    'or spawn_child_nodes in this stage'
                )
            latest_spawn_key_ref = self._latest_spawn_stage_key_ref_locked(
                task_id=task_id,
                node_id=node_id,
                active_stage=active,
            )
            if (
                latest_spawn_key_ref is not None
                and not any(str(item.ref or '').strip() == str(latest_spawn_key_ref.ref or '').strip() for item in normalized_key_refs)
            ):
                normalized_key_refs = [*normalized_key_refs, latest_spawn_key_ref]
            now = now_iso()
            stages: list[ExecutionStageRecord] = []
            for stage in list(state.stages or []):
                current = stage
                if str(stage.stage_id or '').strip() == str(state.active_stage_id or '').strip() and str(stage.status or '') == _EXECUTION_STAGE_STATUS_ACTIVE:
                    current = stage.model_copy(
                        update={
                            'status': _EXECUTION_STAGE_STATUS_COMPLETED,
                            'finished_at': now,
                            'completed_stage_summary': normalized_completed_summary,
                            'key_refs': normalized_key_refs,
                        }
                    )
                stages.append(current)
            next_stage_index = max((int(stage.stage_index or 0) for stage in stages), default=0) + 1
            next_stage = ExecutionStageRecord(
                stage_id=new_stage_id(),
                stage_index=next_stage_index,
                stage_kind=_EXECUTION_STAGE_KIND_NORMAL,
                system_generated=False,
                mode=_EXECUTION_STAGE_MODE_SELF,
                status=_EXECUTION_STAGE_STATUS_ACTIVE,
                stage_goal=normalized_goal,
                completed_stage_summary='',
                final_stage=bool(final),
                key_refs=[],
                archive_ref='',
                archive_stage_index_start=0,
                archive_stage_index_end=0,
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
            next_state = self._externalize_completed_stage_batches_locked(task=task, node_id=node_id, state=next_state)
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
                    and not bool(getattr(latest_active, 'final_stage', False))
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
            next_state = self._externalize_completed_stage_batches_locked(task=task, node_id=node_id, state=next_state)
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
            previous_metadata = dict(record.metadata or {})

            def _mutate(current: NodeRecord) -> NodeRecord:
                metadata = metadata_mutator(dict(current.metadata or {}))
                if not isinstance(metadata, dict):
                    raise TypeError('node metadata mutator must return a dict')
                return current.model_copy(update={'metadata': metadata, 'updated_at': now_iso()})

            updated = self._store.update_node(node_id, _mutate)
            task = self._store.get_task(record.task_id)
            if updated is not None and task is not None:
                next_metadata = dict(updated.metadata or {})
                spawn_changed = previous_metadata.get('spawn_operations') != next_metadata.get('spawn_operations')
                execution_stage_changed = previous_metadata.get(_EXECUTION_STAGE_METADATA_KEY) != next_metadata.get(_EXECUTION_STAGE_METADATA_KEY)
                result_payload_only_keys = {'result_schema_version', 'result_payload', 'result_payload_ref', 'result_payload_summary'}
                changed_keys = {
                    key
                    for key in set(previous_metadata) | set(next_metadata)
                    if previous_metadata.get(key) != next_metadata.get(key)
                }
                if spawn_changed or execution_stage_changed or any(key not in result_payload_only_keys for key in changed_keys):
                    self._sync_node_read_models_locked(updated)
                if spawn_changed:
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
            if len(final_output) <= INLINE_CHAR_LIMIT:
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
            if 'task_temp_dir' in payload:
                current['task_temp_dir'] = self._normalize_task_temp_dir(payload.get('task_temp_dir'))
            if 'dispatch_limits' in payload:
                current['dispatch_limits'] = self._sanitize_dispatch_counters(payload.get('dispatch_limits'))
            if 'dispatch_running' in payload:
                current['dispatch_running'] = self._sanitize_dispatch_counters(payload.get('dispatch_running'))
            if 'dispatch_queued' in payload:
                current['dispatch_queued'] = self._sanitize_dispatch_counters(payload.get('dispatch_queued'))
            if 'governance' in payload:
                current['governance'] = self._sanitize_governance_state(payload.get('governance'))
            current['updated_at'] = now_iso()
            self._store.upsert_task_runtime_meta(
                task_id=task.task_id,
                updated_at=str(current.get('updated_at') or now_iso()),
                payload=current,
            )
            return self.read_task_runtime_meta(task.task_id) or current

    def update_task_governance(self, task_id: str, governance: dict[str, Any], *, publish_patch: bool = True) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            current = dict(self._store.get_task_runtime_meta(task.task_id) or self._default_runtime_meta())
            current['governance'] = self._sanitize_governance_state(governance)
            current['updated_at'] = now_iso()
            self._store.upsert_task_runtime_meta(
                task_id=task.task_id,
                updated_at=str(current.get('updated_at') or now_iso()),
                payload=current,
            )
            sanitized = self.read_task_runtime_meta(task.task_id) or current
            if publish_patch:
                self._publish_task_governance_patch_locked(task=task, governance=dict(sanitized.get('governance') or {}))
                self._publish_task_live_patch_locked(task=task)
            return sanitized

    def capture_retry_resume_snapshot(self, task_id: str, node_id: str, *, failure_reason: str = '') -> dict[str, Any] | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            node = self._store.get_node(node_id)
            if task is None or node is None:
                return None
            frame = self.read_runtime_frame(task_id, node_id) or {}
            messages = [dict(item) for item in list(frame.get('messages') or []) if isinstance(item, dict)]
            if not messages:
                try:
                    parsed_input = json.loads(str(node.input or ''))
                except Exception:
                    parsed_input = []
                if isinstance(parsed_input, list):
                    messages = [dict(item) for item in parsed_input if isinstance(item, dict)]
            snapshot = {
                'captured_at': now_iso(),
                'task_id': str(task.task_id or '').strip(),
                'node_id': str(node.node_id or '').strip(),
                'failure_reason': str(failure_reason or '').strip(),
                'task_metadata': copy.deepcopy(dict(task.metadata or {})),
                'node_metadata': copy.deepcopy(dict(node.metadata or {})),
                'node_input_text': str(node.input or ''),
                'frame': self._sanitize_runtime_frame({**dict(frame or {}), 'messages': messages}),
            }
            current = dict(self._store.get_task_runtime_meta(task.task_id) or self._default_runtime_meta())
            current['retry_resume_snapshot'] = snapshot
            current['updated_at'] = now_iso()
            self._store.upsert_task_runtime_meta(
                task_id=task.task_id,
                updated_at=str(current.get('updated_at') or now_iso()),
                payload=current,
            )
            return copy.deepcopy(snapshot)

    def read_retry_resume_snapshot(self, task_id: str) -> dict[str, Any] | None:
        current = self.read_task_runtime_meta(task_id) or {}
        snapshot = current.get('retry_resume_snapshot')
        return copy.deepcopy(snapshot) if isinstance(snapshot, dict) else None

    def clear_retry_resume_snapshot(self, task_id: str) -> dict[str, Any] | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            if task is None:
                return None
            current = dict(self._store.get_task_runtime_meta(task.task_id) or self._default_runtime_meta())
            if 'retry_resume_snapshot' not in current:
                return self.read_task_runtime_meta(task.task_id) or current
            current.pop('retry_resume_snapshot', None)
            current['updated_at'] = now_iso()
            self._store.upsert_task_runtime_meta(
                task_id=task.task_id,
                updated_at=str(current.get('updated_at') or now_iso()),
                payload=current,
            )
            return self.read_task_runtime_meta(task.task_id) or current

    def update_task_max_depth(self, task_id: str, max_depth: int) -> TaskRecord | None:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            next_depth = max(0, int(max_depth or 0))
            if int(task.max_depth or 0) == next_depth:
                return task
            updated = self._store.update_task(
                task.task_id,
                lambda record: record.model_copy(
                    update={
                        'max_depth': next_depth,
                        'updated_at': now_iso(),
                        'is_unread': True,
                    }
                ),
            )
            if updated is None:
                return None
            self._publish_task_summary_patch_locked(task=updated, previous_task=task)
            return updated

    def read_task_runtime_meta(self, task_id: str) -> dict[str, Any] | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        current = dict(self._store.get_task_runtime_meta(task_id) or self._default_runtime_meta())
        current['task_id'] = task.task_id
        current.setdefault('updated_at', now_iso())
        current.setdefault('last_visible_output_at', '')
        current.setdefault('dispatch_limits', {'execution': 0, 'inspection': 0})
        current.setdefault('dispatch_running', {'execution': 0, 'inspection': 0})
        current.setdefault('dispatch_queued', {'execution': 0, 'inspection': 0})
        current.setdefault('summary_fingerprint', '')
        current.setdefault('summary_last_published_at', '')
        current['governance'] = self._sanitize_governance_state(current.get('governance'))
        current['task_temp_dir'] = self._normalize_task_temp_dir(current.get('task_temp_dir'))
        try:
            current['last_stall_notice_bucket_minutes'] = max(0, int(current.get('last_stall_notice_bucket_minutes') or 0))
        except (TypeError, ValueError):
            current['last_stall_notice_bucket_minutes'] = 0
        return self._sanitize_runtime_state(current)

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
            'governance': self._sanitize_governance_state(meta.get('governance')),
            'frames': frames,
            'active_node_ids': [record.node_id for record in frame_records if bool(record.active)],
            'runnable_node_ids': [record.node_id for record in frame_records if bool(record.runnable)],
            'waiting_node_ids': [record.node_id for record in frame_records if bool(record.waiting)],
        }

    def read_runtime_frame(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        record = self._store.get_task_runtime_frame(task_id, node_id)
        return self._hydrate_runtime_frame_record(record) if record is not None else None

    def upsert_frame(self, task_id: str, frame: dict[str, Any], *, publish_snapshot: bool = False) -> dict[str, Any]:
        started_at = _precise_now_iso()
        started_mono = time.perf_counter()
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            if self._is_terminal_status(task.status):
                self._store.replace_task_runtime_frames(task.task_id, [])
                result = self.read_runtime_state(task_id) or {}
                self._record_debug('log_service.upsert_frame', started_at=started_at, started_mono=started_mono)
                return result
            sanitized = self._sanitize_runtime_frame(frame)
            record = self._runtime_frame_record(task=task, frame=sanitized)
            self._store.upsert_task_runtime_frame(record)
            if publish_snapshot:
                self._publish_task_live_patch_locked(task=task, frame=record)
            result = self.read_runtime_state(task_id) or {}
            self._record_debug('log_service.upsert_frame', started_at=started_at, started_mono=started_mono)
            return result

    def update_frame(
        self,
        task_id: str,
        node_id: str,
        frame_mutator: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        publish_snapshot: bool = False,
    ) -> dict[str, Any]:
        started_at = _precise_now_iso()
        started_mono = time.perf_counter()
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            if self._is_terminal_status(task.status):
                self._store.replace_task_runtime_frames(task.task_id, [])
                result = self.read_runtime_state(task_id) or {}
                self._record_debug('log_service.update_frame', started_at=started_at, started_mono=started_mono)
                return result
            current = self._store.get_task_runtime_frame(task_id, node_id)
            target = self._hydrate_runtime_frame_record(current) if current is not None else self._default_frame(node_id=node_id)
            mutated = frame_mutator(copy.deepcopy(target))
            if not isinstance(mutated, dict):
                raise TypeError('frame mutator must return a dict')
            record = self._runtime_frame_record(task=task, frame=self._sanitize_runtime_frame(mutated))
            if current is None or self._runtime_frame_record_fingerprint(current) != self._runtime_frame_record_fingerprint(record):
                self._store.upsert_task_runtime_frame(record)
            if publish_snapshot:
                self._publish_task_live_patch_locked(task=task, frame=record)
            result = self.read_runtime_state(task_id) or {}
            self._record_debug('log_service.update_frame', started_at=started_at, started_mono=started_mono)
            return result

    def remove_frame(self, task_id: str, node_id: str, *, publish_snapshot: bool = False) -> dict[str, Any]:
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            current = self._store.get_task_runtime_frame(task_id, node_id)
            latest_messages_ref = ''
            if current is not None:
                payload = dict(current.payload or {})
                latest_messages_ref = str(payload.get('messages_ref') or '').strip()
            if latest_messages_ref:
                node = self._store.get_node(node_id)
                if node is not None and str(node.task_id or '').strip() == str(task.task_id or '').strip():
                    updated_node = self._store.update_node(
                        node_id,
                        lambda record: record.model_copy(
                            update={
                                'metadata': {
                                    **dict(record.metadata or {}),
                                    'latest_runtime_messages_ref': latest_messages_ref,
                                },
                                'updated_at': now_iso(),
                            }
                        ),
                    )
                    if updated_node is not None:
                        self._sync_node_read_models_locked(updated_node)
                        self._publish_task_node_patch_locked(task=task, node=updated_node)
            self._store.delete_task_runtime_frame(task_id, node_id)
            if publish_snapshot:
                self._publish_task_live_patch_locked(task=task, removed_node_id=node_id)
            return self.read_runtime_state(task_id) or {}

    def refresh_task_view(self, task_id: str, *, mark_unread: bool) -> TaskRecord | None:
        started_at = _precise_now_iso()
        started_mono = time.perf_counter()
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
            next_is_unread = True if mark_unread else task.is_unread
            next_final_output = str(output_fields.get('final_output') or '')
            next_final_output_ref = str(output_fields.get('final_output_ref') or '')
            next_failure_reason = str(output_fields.get('failure_reason') or '')
            next_failure_class = self._next_failure_class(task_status=next_status, final_acceptance=final_acceptance)
            current_failure_class = normalize_failure_class((task.metadata or {}).get('failure_class'))
            if next_status == 'failed' and current_failure_class == FAILURE_CLASS_NON_RETRYABLE_BLOCKED:
                next_failure_class = FAILURE_CLASS_NON_RETRYABLE_BLOCKED
            next_metadata = self._task_metadata_with_failure_class(dict(task.metadata or {}), failure_class=next_failure_class)
            next_finished_at = now_iso() if next_status in {'success', 'failed'} and not task.finished_at else task.finished_at
            if (
                str(task.status or '').strip() == str(next_status or '').strip()
                and str(task.brief_text or '') == str(brief_text or '')
                and bool(task.is_unread) == bool(next_is_unread)
                and str(task.final_output or '') == next_final_output
                and str(task.final_output_ref or '') == next_final_output_ref
                and str(task.failure_reason or '') == next_failure_reason
                and str(task.finished_at or '') == str(next_finished_at or '')
                and dict(task.metadata or {}) == next_metadata
            ):
                self._record_debug('log_service.refresh_task_view', started_at=started_at, started_mono=started_mono)
                return task
            updated = task.model_copy(
                update={
                    'status': next_status,
                    'brief_text': brief_text,
                    'is_unread': next_is_unread,
                    'updated_at': now_iso(),
                    'final_output': next_final_output,
                    'final_output_ref': next_final_output_ref,
                    'failure_reason': next_failure_reason,
                    'finished_at': next_finished_at,
                    'metadata': next_metadata,
                }
            )
            self._store.upsert_task(updated)
            if self._is_terminal_status(next_status):
                self.flush_live_patch_history(updated.task_id)
                self._store.replace_task_runtime_frames(updated.task_id, [])
                self._projector.replace_runtime_frames(updated.task_id, [])
                self.update_task_runtime_meta(updated.task_id)
            self._publish_task_summary_patch_locked(task=updated, previous_task=task)
            if terminal_transition:
                self._publish_task_terminal_locked(task=updated)
            self._notify_task_terminal(updated, previous_status=previous_status)
            self._record_debug('log_service.refresh_task_view', started_at=started_at, started_mono=started_mono)
            return updated

    def _record_debug(self, section: str, *, started_at: str, started_mono: float) -> None:
        recorder = self._debug_recorder
        if recorder is None or not hasattr(recorder, 'record'):
            return
        try:
            recorder.record(
                section=section,
                elapsed_ms=(time.perf_counter() - started_mono) * 1000.0,
                started_at=started_at,
            )
        except Exception:
            return

    def _report_summary_metric(self, key: str, amount: float = 1.0) -> None:
        for reporter in list(self._summary_metric_reporters):
            try:
                reporter(str(key or '').strip(), float(amount or 0.0))
            except Exception:
                continue

    def sync_task_read_models(self, task_id: str, *, externalize_execution_trace: bool = True) -> TaskRecord | None:
        with self._task_lock(task_id):
            task = self._store.get_task(task_id)
            if task is None:
                return None
            preserved_execution_trace_refs: dict[str, str] = {}
            if not externalize_execution_trace:
                preserved_execution_trace_refs = {
                    str(record.node_id or '').strip(): str(record.execution_trace_ref or '').strip()
                    for record in list(self._store.list_task_node_details(task_id) or [])
                    if str(record.node_id or '').strip()
                }
            for node in list(self._store.list_nodes(task_id) or []):
                self._sync_node_read_models_locked(
                    node,
                    externalize_execution_trace=externalize_execution_trace,
                    preserved_execution_trace_ref=preserved_execution_trace_refs.get(str(node.node_id or '').strip(), ''),
                )
                self._sync_task_node_rounds_locked(node)
            self.refresh_task_view(task_id, mark_unread=False)
            if self._store.get_task_runtime_meta(task.task_id) is None:
                self.update_task_runtime_meta(task.task_id, last_stall_notice_bucket_minutes=0)
            return self._store.get_task(task_id)

    def sync_node_read_model(
        self,
        task_id: str,
        node_id: str,
        *,
        externalize_execution_trace: bool = True,
    ) -> TaskProjectionNodeDetailRecord | None:
        with self._task_lock(task_id):
            node = self._store.get_node(node_id)
            if node is None or str(node.task_id or '').strip() != str(task_id or '').strip():
                return None
            preserved_execution_trace_ref = ''
            if not externalize_execution_trace:
                current = self._store.get_task_node_detail(node_id)
                preserved_execution_trace_ref = str(getattr(current, 'execution_trace_ref', '') or '').strip()
            self._sync_node_read_models_locked(
                node,
                externalize_execution_trace=externalize_execution_trace,
                preserved_execution_trace_ref=preserved_execution_trace_ref,
            )
            self._sync_task_node_rounds_locked(node)
            return self._store.get_task_node_detail(node_id)

    def _sync_node_read_models_locked(
        self,
        node: NodeRecord,
        *,
        externalize_execution_trace: bool = True,
        preserved_execution_trace_ref: str = '',
    ) -> None:
        self._projector.sync_node(
            self._task_projection_node_record(node),
            self._task_projection_node_detail_record(
                node,
                externalize_execution_trace=externalize_execution_trace,
                preserved_execution_trace_ref=preserved_execution_trace_ref,
            ),
        )

    def _sync_task_node_rounds_locked(self, node: NodeRecord) -> None:
        if node is None:
            return
        next_records = self._task_projection_round_records(node)
        current_records = [
            item
            for item in list(self._store.list_task_node_rounds(node.task_id) or [])
            if str(getattr(item, 'parent_node_id', '') or '').strip() == str(node.node_id or '').strip()
        ]
        if self._round_records_fingerprint(current_records) == self._round_records_fingerprint(next_records):
            return
        self._projector.sync_rounds_for_parent(node.task_id, node.node_id, next_records)

    @staticmethod
    def _round_records_fingerprint(records: list[TaskProjectionRoundRecord]) -> str:
        normalized = [
            {
                'round_id': str(item.round_id or '').strip(),
                'round_index': int(item.round_index or 0),
                'label': str(item.label or '').strip(),
                'is_latest': bool(item.is_latest),
                'created_at': str(item.created_at or '').strip(),
                'source': str(item.source or '').strip(),
                'total_children': int(item.total_children or 0),
                'completed_children': int(item.completed_children or 0),
                'running_children': int(item.running_children or 0),
                'failed_children': int(item.failed_children or 0),
                'child_node_ids': [str(child_id or '').strip() for child_id in list(item.child_node_ids or []) if str(child_id or '').strip()],
            }
            for item in list(records or [])
        ]
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

    def _task_projection_node_children_fingerprint(
        self,
        node: NodeRecord,
        *,
        rounds: list[TaskProjectionRoundRecord] | None = None,
    ) -> str:
        round_records = list(rounds or self._task_projection_round_records(node))
        default_round_id = str(round_records[-1].round_id or '') if round_records else ''
        direct_children = list(self._store.list_children(node.node_id) or [])
        direct_child_ids = [
            str(child.node_id or '').strip()
            for child in direct_children
            if str(child.node_id or '').strip()
        ]
        round_child_ids = {
            str(child_id or '').strip()
            for round_record in round_records
            for child_id in list(round_record.child_node_ids or [])
            if str(child_id or '').strip()
        }
        auxiliary_child_ids = [child_id for child_id in direct_child_ids if child_id not in round_child_ids]
        payload = {
            'default_round_id': default_round_id,
            'auxiliary_child_ids': auxiliary_child_ids,
            'rounds': [
                {
                    'round_id': str(round_record.round_id or ''),
                    'round_index': int(round_record.round_index or 0),
                    'child_node_ids': [
                        str(child_id or '').strip()
                        for child_id in list(round_record.child_node_ids or [])
                        if str(child_id or '').strip()
                    ],
                }
                for round_record in round_records
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _task_projection_node_record(self, node: NodeRecord) -> TaskProjectionNodeRecord:
        rounds = self._task_projection_round_records(node)
        default_round_id = str(rounds[-1].round_id or '') if rounds else ''
        children_fingerprint = self._task_projection_node_children_fingerprint(node, rounds=rounds)
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
            children_fingerprint=children_fingerprint,
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
                'children_fingerprint': children_fingerprint,
            },
        )

    def _task_projection_node_detail_record(
        self,
        node: NodeRecord,
        *,
        externalize_execution_trace: bool = True,
        preserved_execution_trace_ref: str = '',
    ) -> TaskProjectionNodeDetailRecord:
        prompt_summary = _single_line_text(node.prompt or node.goal or '', max_chars=400)
        tool_file_changes = normalize_tool_file_changes((node.metadata or {}).get('tool_file_changes'))
        execution_trace = self._projection_execution_trace(node)
        execution_trace_summary = self._execution_trace_summary(execution_trace)
        latest_spawn_round_id, direct_child_results = self._latest_direct_child_results_payload(node)
        spawn_review_rounds = self._spawn_review_rounds_payload(node)
        execution_trace_ref = str(preserved_execution_trace_ref or '').strip()
        if externalize_execution_trace:
            execution_trace_ref = self._externalize_execution_trace(node, execution_trace)
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
            execution_trace_ref=execution_trace_ref,
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
                'execution_trace_summary': execution_trace_summary,
                'execution_trace_ref': execution_trace_ref,
                'latest_spawn_round_id': latest_spawn_round_id,
                'direct_child_results': direct_child_results,
                'spawn_review_rounds': spawn_review_rounds,
                'tool_file_changes': [item.model_dump(mode='json') for item in list(tool_file_changes or [])],
                'token_usage': node.token_usage.model_dump(mode='json'),
                'token_usage_by_model': [item.model_dump(mode='json') for item in list(node.token_usage_by_model or [])],
            },
        )

    def _projection_execution_trace(self, node: NodeRecord) -> dict[str, Any]:
        frame = self.read_runtime_frame(node.task_id, node.node_id)
        live_tool_calls = [dict(item) for item in list((frame or {}).get('tool_calls') or []) if isinstance(item, dict)]
        tool_results = list(self._store.list_task_node_tool_results(node.task_id, node.node_id) or [])
        return build_execution_trace(node, tool_results=tool_results, live_tool_calls=live_tool_calls)

    def _externalize_execution_trace(self, node: NodeRecord, execution_trace: dict[str, Any]) -> str:
        store = self._content_store
        if store is None:
            return ''
        runtime = {'task_id': node.task_id, 'node_id': node.node_id}
        try:
            envelope = store.maybe_externalize_text(
                execution_trace,
                runtime=runtime,
                display_name=f'execution-trace:{node.node_id}',
                source_kind='task_execution_trace',
                mime_type='application/json',
                force=True,
            )
        except Exception:
            return ''
        if envelope is None:
            return ''
        return str(envelope.ref or '')

    @staticmethod
    def _execution_trace_summary(execution_trace: dict[str, Any] | None) -> dict[str, Any]:
        trace = execution_trace if isinstance(execution_trace, dict) else {}
        stages_payload: list[dict[str, Any]] = []
        for stage in list(trace.get('stages') or []):
            if not isinstance(stage, dict):
                continue
            tool_calls: list[dict[str, str]] = []
            rounds_payload: list[dict[str, Any]] = []
            for round_item in list(stage.get('rounds') or []):
                if not isinstance(round_item, dict):
                    continue
                compact_tools: list[dict[str, str]] = []
                for step in list(round_item.get('tools') or []):
                    compact_step = TaskLogService._compact_execution_trace_tool_call(step)
                    if compact_step is not None:
                        tool_calls.append(compact_step)
                        compact_tools.append(compact_step)
                rounds_payload.append(
                    {
                        'round_id': str(round_item.get('round_id') or ''),
                        'round_index': int(round_item.get('round_index') or 0),
                        'created_at': str(round_item.get('created_at') or ''),
                        'budget_counted': bool(round_item.get('budget_counted')),
                        'tools': compact_tools,
                    }
                )
            stages_payload.append(
                {
                    'stage_id': str(stage.get('stage_id') or ''),
                    'stage_index': int(stage.get('stage_index') or 0),
                    'mode': str(stage.get('mode') or ''),
                    'status': str(stage.get('status') or ''),
                    'stage_goal': str(stage.get('stage_goal') or ''),
                    'tool_round_budget': int(stage.get('tool_round_budget') or 0),
                    'tool_rounds_used': int(stage.get('tool_rounds_used') or 0),
                    'created_at': str(stage.get('created_at') or ''),
                    'finished_at': str(stage.get('finished_at') or ''),
                    'rounds': rounds_payload,
                    'tool_calls': tool_calls,
                }
            )
        if stages_payload:
            return {'stages': stages_payload}
        fallback_tool_calls: list[dict[str, str]] = []
        for step in list(trace.get('tool_steps') or []):
            compact_step = TaskLogService._compact_execution_trace_tool_call(step)
            if compact_step is not None:
                fallback_tool_calls.append(compact_step)
        if fallback_tool_calls:
            return {
                'stages': [{
                    'stage_goal': '',
                    'rounds': [{
                        'round_id': '',
                        'round_index': 1,
                        'created_at': '',
                        'budget_counted': False,
                        'tools': fallback_tool_calls,
                    }],
                    'tool_calls': fallback_tool_calls,
                }]
            }
        return {'stages': []}

    @staticmethod
    def _spawn_review_rounds_payload(node: NodeRecord) -> list[dict[str, Any]]:
        operations = (node.metadata or {}).get('spawn_operations') if isinstance(node.metadata, dict) else {}
        if not isinstance(operations, dict):
            return []
        rounds: list[dict[str, Any]] = []
        for index, (round_id, operation) in enumerate(operations.items(), start=1):
            if not isinstance(operation, dict):
                continue
            review = dict(operation.get('spawn_review') or {}) if isinstance(operation.get('spawn_review'), dict) else {}
            entries = [dict(item) for item in list(operation.get('entries') or []) if isinstance(item, dict)]
            has_review_data = bool(review) or any(
                str(item.get('review_decision') or '').strip()
                or str(item.get('blocked_reason') or '').strip()
                or str(item.get('blocked_suggestion') or '').strip()
                or str(item.get('synthetic_result_summary') or '').strip()
                for item in entries
            )
            if not has_review_data:
                continue
            rounds.append(
                {
                    'round_id': str(round_id or '').strip() or f'round:{index}',
                    'round_index': index,
                    'reviewed_at': str(
                        review.get('reviewed_at')
                        or operation.get('created_at')
                        or next((item.get('started_at') for item in entries if str(item.get('started_at') or '').strip()), '')
                        or str(node.updated_at or '')
                    ),
                    'requested_specs': [
                        dict(item)
                        for item in list(review.get('requested_specs') or operation.get('specs') or [])
                        if isinstance(item, dict)
                    ],
                    'allowed_indexes': [
                        int(item)
                        for item in list(review.get('allowed_indexes') or [])
                        if isinstance(item, int) or (isinstance(item, str) and str(item).strip().isdigit())
                    ],
                    'blocked_specs': [
                        {
                            'index': int(item.get('index') or 0),
                            'reason': str(item.get('reason') or ''),
                            'suggestion': str(item.get('suggestion') or ''),
                        }
                        for item in list(review.get('blocked_specs') or [])
                        if isinstance(item, dict)
                    ],
                    'error_text': str(review.get('error_text') or ''),
                    'entries': [
                        {
                            'index': int(item.get('index') or 0),
                            'goal': str(item.get('goal') or ''),
                            'review_decision': str(item.get('review_decision') or ''),
                            'blocked_reason': str(item.get('blocked_reason') or ''),
                            'blocked_suggestion': str(item.get('blocked_suggestion') or ''),
                            'synthetic_result_summary': str(item.get('synthetic_result_summary') or ''),
                            'child_node_id': str(item.get('child_node_id') or ''),
                            'acceptance_node_id': str(item.get('acceptance_node_id') or ''),
                        }
                        for item in entries
                    ],
                }
            )
        return rounds

    @staticmethod
    def _latest_direct_child_results_payload(node: NodeRecord) -> tuple[str, list[dict[str, Any]]]:
        operations = (node.metadata or {}).get('spawn_operations') if isinstance(node.metadata, dict) else {}
        if not isinstance(operations, dict):
            return '', []
        latest_round_id = ''
        latest_entries: list[dict[str, Any]] = []
        for index, (round_id, operation) in enumerate(operations.items(), start=1):
            if not isinstance(operation, dict):
                continue
            latest_round_id = str(round_id or '').strip() or f'round:{index}'
            latest_entries = [dict(item) for item in list(operation.get('entries') or []) if isinstance(item, dict)]
        if not latest_entries:
            return '', []
        results: list[dict[str, Any]] = []
        for item in latest_entries:
            result = dict(item.get('result') or {}) if isinstance(item.get('result'), dict) else {}
            failure_info = dict(result.get('failure_info') or {}) if isinstance(result.get('failure_info'), dict) else {}
            results.append(
                {
                    'index': int(item.get('index') or 0),
                    'goal': str(item.get('goal') or ''),
                    'status': str(item.get('status') or ''),
                    'started_at': str(item.get('started_at') or ''),
                    'finished_at': str(item.get('finished_at') or ''),
                    'requires_acceptance': bool(item.get('requires_acceptance')),
                    'child_node_id': str(item.get('child_node_id') or ''),
                    'acceptance_node_id': str(item.get('acceptance_node_id') or ''),
                    'check_status': str(item.get('check_status') or ''),
                    'review_decision': str(item.get('review_decision') or ''),
                    'blocked_reason': str(item.get('blocked_reason') or ''),
                    'blocked_suggestion': str(item.get('blocked_suggestion') or ''),
                    'check_result': str(result.get('check_result') or ''),
                    'node_output_summary': str(result.get('node_output_summary') or result.get('node_output') or item.get('synthetic_result_summary') or ''),
                    'node_output_ref': str(result.get('node_output_ref') or ''),
                    'failure_info': failure_info,
                }
            )
        return latest_round_id, results

    @staticmethod
    def _compact_execution_trace_tool_call(step: Any) -> dict[str, Any] | None:
        return compact_tool_step_for_summary(step if isinstance(step, dict) else None)

    def _task_projection_round_records(self, node: NodeRecord) -> list[TaskProjectionRoundRecord]:
        payload = (node.metadata or {}).get('spawn_operations') if isinstance(node.metadata, dict) else {}
        if not isinstance(payload, dict):
            return []
        records: list[TaskProjectionRoundRecord] = []
        for index, (round_id, operation) in enumerate(payload.items(), start=1):
            if not isinstance(operation, dict):
                continue
            entries = [item for item in list(operation.get('entries') or []) if isinstance(item, dict)]
            materialized_entries = [
                item for item in entries
                if str(item.get('child_node_id') or '').strip()
            ]
            child_node_ids = [
                str(item.get('child_node_id') or '').strip()
                for item in materialized_entries
                if str(item.get('child_node_id') or '').strip()
            ]
            total_children = len(child_node_ids)
            completed_children = sum(1 for item in materialized_entries if str(item.get('status') or '').strip().lower() == 'success')
            failed_children = sum(1 for item in materialized_entries if str(item.get('status') or '').strip().lower() == 'error')
            running_children = sum(
                1 for item in materialized_entries if str(item.get('status') or '').strip().lower() in {'queued', 'running'}
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
                'await_marker': str(next_frame.get('await_marker') or ''),
                'await_started_at': str(next_frame.get('await_started_at') or ''),
                'stage_mode': str(next_frame.get('stage_mode') or ''),
                'stage_status': str(next_frame.get('stage_status') or ''),
                'stage_goal': str(next_frame.get('stage_goal') or ''),
                'stage_total_steps': int(next_frame.get('stage_total_steps') or 0),
                'active_round_id': str(next_frame.get('active_round_id') or ''),
                'active_round_tool_call_ids': [
                    str(item or '').strip()
                    for item in list(next_frame.get('active_round_tool_call_ids') or [])
                    if str(item or '').strip()
                ],
                'active_round_started_at': str(next_frame.get('active_round_started_at') or ''),
                'pending_tool_calls': [dict(item) for item in list(next_frame.get('pending_tool_calls') or []) if isinstance(item, dict)],
                'pending_child_specs': [dict(item) for item in list(next_frame.get('pending_child_specs') or []) if isinstance(item, dict)],
                'partial_child_results': [dict(item) for item in list(next_frame.get('partial_child_results') or []) if isinstance(item, dict)],
                'tool_calls': [dict(item) for item in list(next_frame.get('tool_calls') or []) if isinstance(item, dict)],
                'child_pipelines': [dict(item) for item in list(next_frame.get('child_pipelines') or []) if isinstance(item, dict)],
                'model_visible_tool_names': [
                    str(item or '').strip()
                    for item in list(next_frame.get('model_visible_tool_names') or [])
                    if str(item or '').strip()
                ],
                'hydrated_executor_names': [
                    str(item or '').strip()
                    for item in list(next_frame.get('hydrated_executor_names') or [])
                    if str(item or '').strip()
                ],
                'lightweight_tool_ids': [
                    str(item or '').strip()
                    for item in list(next_frame.get('lightweight_tool_ids') or [])
                    if str(item or '').strip()
                ],
                'model_visible_tool_selection_trace': dict(next_frame.get('model_visible_tool_selection_trace') or {}),
                'last_error': str(next_frame.get('last_error') or ''),
                'messages_ref': str(next_frame.get('messages_ref') or ''),
                'messages_count': int(next_frame.get('messages_count') or 0),
            },
        )

    @staticmethod
    def _runtime_frame_record_fingerprint(record: TaskProjectionRuntimeFrameRecord) -> str:
        payload = dict(record.payload or {})
        normalized = {
            'task_id': str(record.task_id or '').strip(),
            'node_id': str(record.node_id or '').strip(),
            'depth': int(record.depth or 0),
            'node_kind': str(record.node_kind or '').strip(),
            'phase': str(record.phase or '').strip(),
            'active': bool(record.active),
            'runnable': bool(record.runnable),
            'waiting': bool(record.waiting),
            'payload': payload,
        }
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

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
            'await_marker': str(payload.get('await_marker') or ''),
            'await_started_at': str(payload.get('await_started_at') or ''),
            'stage_mode': str(payload.get('stage_mode') or ''),
            'stage_status': str(payload.get('stage_status') or ''),
            'stage_goal': str(payload.get('stage_goal') or ''),
            'stage_total_steps': int(payload.get('stage_total_steps') or 0),
            'active_round_id': str(payload.get('active_round_id') or ''),
            'active_round_tool_call_ids': [
                str(item or '').strip()
                for item in list(payload.get('active_round_tool_call_ids') or [])
                if str(item or '').strip()
            ],
            'active_round_started_at': str(payload.get('active_round_started_at') or ''),
            'messages': messages,
            'pending_tool_calls': [dict(item) for item in list(payload.get('pending_tool_calls') or []) if isinstance(item, dict)],
            'pending_child_specs': [dict(item) for item in list(payload.get('pending_child_specs') or []) if isinstance(item, dict)],
            'partial_child_results': [dict(item) for item in list(payload.get('partial_child_results') or []) if isinstance(item, dict)],
            'tool_calls': [dict(item) for item in list(payload.get('tool_calls') or []) if isinstance(item, dict)],
            'child_pipelines': [dict(item) for item in list(payload.get('child_pipelines') or []) if isinstance(item, dict)],
            'model_visible_tool_names': [
                str(item or '').strip()
                for item in list(payload.get('model_visible_tool_names') or [])
                if str(item or '').strip()
            ],
            'hydrated_executor_names': [
                str(item or '').strip()
                for item in list(payload.get('hydrated_executor_names') or [])
                if str(item or '').strip()
            ],
            'lightweight_tool_ids': [
                str(item or '').strip()
                for item in list(payload.get('lightweight_tool_ids') or [])
                if str(item or '').strip()
            ],
            'model_visible_tool_selection_trace': dict(payload.get('model_visible_tool_selection_trace') or {}),
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

    def resolve_content_ref(self, ref: str) -> str:
        return self._resolve_content_ref(ref)

    @classmethod
    def _task_summary_fingerprint(cls, task: TaskRecord) -> str:
        payload = cls._task_summary_payload(task)
        visible = {
            'title': str(payload.get('title') or ''),
            'brief': str(payload.get('brief') or ''),
            'status': str(payload.get('status') or ''),
            'is_unread': bool(payload.get('is_unread')),
            'is_paused': bool(payload.get('is_paused')),
            'max_depth': int(payload.get('max_depth') or 0),
            'token_usage': {
                'input_tokens': int(((payload.get('token_usage') or {}).get('input_tokens') or 0)),
                'output_tokens': int(((payload.get('token_usage') or {}).get('output_tokens') or 0)),
                'cache_hit_tokens': int(((payload.get('token_usage') or {}).get('cache_hit_tokens') or 0)),
            },
        }
        return json.dumps(visible, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _summary_dispatch_immediate(*, task: TaskRecord, previous_task: TaskRecord | None = None) -> bool:
        if previous_task is None:
            return True
        for key in ('status', 'is_unread', 'is_paused'):
            if getattr(previous_task, key, None) != getattr(task, key, None):
                return True
        return False

    def _touch_task_summary_locked(
        self,
        *,
        task: TaskRecord,
        mark_unread: bool,
        updated_at: str,
    ) -> TaskRecord:
        next_task = task.model_copy(
            update={
                'is_unread': True if mark_unread else task.is_unread,
                'updated_at': str(updated_at or now_iso()),
            }
        )
        self._store.upsert_task(next_task)
        self._publish_task_summary_patch_locked(task=next_task, previous_task=task)
        return next_task

    def _publish_task_summary_patch_locked(self, *, task: TaskRecord, previous_task: TaskRecord | None = None) -> bool:
        payload_task = self._task_summary_payload(task)
        fingerprint = self._task_summary_fingerprint(task)
        runtime_meta = dict(self._store.get_task_runtime_meta(task.task_id) or self._default_runtime_meta())
        previous_fingerprint = str(runtime_meta.get('summary_fingerprint') or '').strip()
        if previous_fingerprint and previous_fingerprint == fingerprint:
            self._report_summary_metric('task_summary_skip_unchanged_count')
            return False
        runtime_meta['summary_fingerprint'] = fingerprint
        runtime_meta['summary_last_published_at'] = now_iso()
        runtime_meta['updated_at'] = now_iso()
        self._store.upsert_task_runtime_meta(
            task_id=task.task_id,
            updated_at=str(runtime_meta.get('updated_at') or now_iso()),
            payload=runtime_meta,
        )
        payload = {'task': payload_task}
        self._append_task_event(task=task, event_type='task.summary.patch', data=payload)
        self._dispatch_live_event_locked(
            task=task,
            event_type='task.summary.patch',
            data=payload,
            dispatch_immediate=self._summary_dispatch_immediate(task=task, previous_task=previous_task),
        )
        return True

    def _publish_task_node_patch_locked(self, *, task: TaskRecord, node: NodeRecord) -> None:
        projected = self._store.get_task_node(node.node_id)
        payload = {
            'node': {
                'node_id': node.node_id,
                'parent_node_id': node.parent_node_id,
                'depth': int(node.depth or 0),
                'node_kind': str(node.node_kind or 'execution'),
                'status': str(node.status or 'in_progress'),
                'title': str(node.goal or node.node_id),
                'updated_at': str(node.updated_at or ''),
                'children_fingerprint': str(
                    getattr(projected, 'children_fingerprint', '')
                    or ((getattr(projected, 'payload', None) or {}).get('children_fingerprint') if projected is not None else '')
                    or ''
                ),
            }
        }
        fingerprint = self._node_patch_persist_fingerprint(payload)
        cache_key = (str(task.task_id or '').strip(), str(node.node_id or '').strip())
        previous_fingerprint = self._last_node_patch_persist_fingerprints.get(cache_key)
        if previous_fingerprint != fingerprint:
            self._append_task_event(task=task, event_type='task.node.patch', data=payload)
            self._last_node_patch_persist_fingerprints[cache_key] = fingerprint
        self._dispatch_live_event_locked(task=task, event_type='task.node.patch', data=payload)

    def _publish_task_token_patch_locked(self, *, task: TaskRecord) -> None:
        payload = {
            'task_id': task.task_id,
            'updated_at': str(task.updated_at or ''),
            'token_usage': task.token_usage.model_dump(mode='json'),
        }
        self._dispatch_live_event_locked(task=task, event_type='task.token.patch', data=payload)

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
        self._buffer_task_live_patch_locked(task=task, payload=payload)
        self._dispatch_live_event_locked(task=task, event_type='task.live.patch', data=payload)

    def _publish_task_governance_patch_locked(self, *, task: TaskRecord, governance: dict[str, Any]) -> None:
        payload = {
            'task_id': task.task_id,
            'governance': self._sanitize_governance_state(governance),
            'history': list((governance or {}).get('history') or []),
        }
        self._dispatch_live_event_locked(task=task, event_type='task.governance.patch', data=payload)

    def _publish_task_terminal_locked(self, *, task: TaskRecord) -> None:
        payload = {'task': self._task_summary_payload(task)}
        self._append_task_event(task=task, event_type='task.terminal', data=payload)
        self._dispatch_live_event_locked(task=task, event_type='task.terminal', data=payload)

    def _dispatch_live_event_locked(
        self,
        *,
        task: TaskRecord,
        event_type: str,
        data: dict[str, Any],
        dispatch_immediate: bool = False,
    ) -> None:
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
                            'dispatch_immediate': bool(dispatch_immediate),
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
        if event_type == 'task.token.patch':
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
            return
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
    def _next_failure_class(*, task_status: str, final_acceptance) -> str:
        normalized_status = str(task_status or '').strip().lower()
        acceptance_failed = bool(
            getattr(final_acceptance, 'required', False)
            and str(getattr(final_acceptance, 'status', '') or '').strip().lower() == 'failed'
        )
        if acceptance_failed:
            return FAILURE_CLASS_BUSINESS_UNPASSED
        if normalized_status == 'failed':
            return FAILURE_CLASS_ENGINE
        return ''

    @staticmethod
    def _task_metadata_with_failure_class(metadata: dict[str, Any], *, failure_class: str) -> dict[str, Any]:
        next_metadata = dict(metadata or {})
        normalized_failure_class = normalize_failure_class(failure_class)
        if normalized_failure_class:
            next_metadata['failure_class'] = normalized_failure_class
        else:
            next_metadata.pop('failure_class', None)
        return next_metadata

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
            return 'success'
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
        metadata = dict(task.metadata or {})
        return {
            'task_id': task.task_id,
            'session_id': task.session_id,
            'title': task.title,
            'brief': task.brief_text,
            'status': task.status,
            'failure_class': normalize_failure_class(metadata.get('failure_class')),
            'final_acceptance': normalize_final_acceptance_metadata(metadata.get('final_acceptance')).model_dump(mode='json'),
            'continuation_state': str(metadata.get('continuation_state') or '').strip(),
            'continued_by_task_id': str(metadata.get('continued_by_task_id') or '').strip(),
            'retry_count': len(list(metadata.get('retry_history') or [])),
            'recovery_notice': str(metadata.get('recovery_notice') or '').strip(),
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
            runtime_meta = self.read_task_runtime_meta(task_id) or self._default_runtime_meta()
            state = {
                'active_node_ids': [record.node_id for record in frame_records if bool(record.active)],
                'runnable_node_ids': [record.node_id for record in frame_records if bool(record.runnable)],
                'waiting_node_ids': [record.node_id for record in frame_records if bool(record.waiting)],
                'dispatch_limits': dict(runtime_meta.get('dispatch_limits') or {}),
                'dispatch_running': dict(runtime_meta.get('dispatch_running') or {}),
                'dispatch_queued': dict(runtime_meta.get('dispatch_queued') or {}),
                'governance': dict(runtime_meta.get('governance') or {}),
                'frames': [dict(record.payload or {}) for record in frame_records],
            }
        return {
            'active_node_ids': [str(item) for item in list(state.get('active_node_ids') or []) if str(item or '').strip()],
            'runnable_node_ids': [str(item) for item in list(state.get('runnable_node_ids') or []) if str(item or '').strip()],
            'waiting_node_ids': [str(item) for item in list(state.get('waiting_node_ids') or []) if str(item or '').strip()],
            'dispatch_limits': self._sanitize_dispatch_counters(state.get('dispatch_limits')),
            'dispatch_running': self._sanitize_dispatch_counters(state.get('dispatch_running')),
            'dispatch_queued': self._sanitize_dispatch_counters(state.get('dispatch_queued')),
            'governance': self._sanitize_governance_state(state.get('governance')),
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
        state['dispatch_limits'] = cls._sanitize_dispatch_counters(state.get('dispatch_limits'))
        state['dispatch_running'] = cls._sanitize_dispatch_counters(state.get('dispatch_running'))
        state['dispatch_queued'] = cls._sanitize_dispatch_counters(state.get('dispatch_queued'))
        state['governance'] = cls._sanitize_governance_state(state.get('governance'))
        state['frames'] = [cls._sanitize_runtime_frame(frame) for frame in list(state.get('frames') or []) if isinstance(frame, dict)]
        return state

    @staticmethod
    def _sanitize_dispatch_counters(payload: Any) -> dict[str, int]:
        counters = dict(payload or {}) if isinstance(payload, dict) else {}
        return {
            'execution': max(0, int(counters.get('execution') or 0)),
            'inspection': max(0, int(counters.get('inspection') or 0)),
        }

    @staticmethod
    def _sanitize_runtime_frame(frame: dict[str, Any]) -> dict[str, Any]:
        payload = dict(frame or {})
        return {
            'node_id': str(payload.get('node_id') or '').strip(),
            'depth': int(payload.get('depth') or 0),
            'node_kind': str(payload.get('node_kind') or 'execution'),
            'phase': str(payload.get('phase') or ''),
            'await_marker': str(payload.get('await_marker') or ''),
            'await_started_at': str(payload.get('await_started_at') or ''),
            'stage_mode': str(payload.get('stage_mode') or ''),
            'stage_status': str(payload.get('stage_status') or ''),
            'stage_goal': str(payload.get('stage_goal') or ''),
            'stage_total_steps': int(payload.get('stage_total_steps') or 0),
            'active_round_id': str(payload.get('active_round_id') or ''),
            'active_round_tool_call_ids': [
                str(item or '').strip()
                for item in list(payload.get('active_round_tool_call_ids') or [])
                if str(item or '').strip()
            ],
            'active_round_started_at': str(payload.get('active_round_started_at') or ''),
            'messages': [dict(item) for item in list(payload.get('messages') or []) if isinstance(item, dict)],
            'pending_tool_calls': [dict(item) for item in list(payload.get('pending_tool_calls') or []) if isinstance(item, dict)],
            'pending_child_specs': [dict(item) for item in list(payload.get('pending_child_specs') or []) if isinstance(item, dict)],
            'partial_child_results': [dict(item) for item in list(payload.get('partial_child_results') or []) if isinstance(item, dict)],
            'tool_calls': [dict(item) for item in list(payload.get('tool_calls') or []) if isinstance(item, dict)],
            'child_pipelines': [dict(item) for item in list(payload.get('child_pipelines') or []) if isinstance(item, dict)],
            'model_visible_tool_names': [
                str(item or '').strip()
                for item in list(payload.get('model_visible_tool_names') or [])
                if str(item or '').strip()
            ],
            'hydrated_executor_names': [
                str(item or '').strip()
                for item in list(payload.get('hydrated_executor_names') or [])
                if str(item or '').strip()
            ],
            'lightweight_tool_ids': [
                str(item or '').strip()
                for item in list(payload.get('lightweight_tool_ids') or [])
                if str(item or '').strip()
            ],
            'model_visible_tool_selection_trace': dict(payload.get('model_visible_tool_selection_trace') or {}),
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
            'await_marker': str(payload.get('await_marker') or ''),
            'await_started_at': str(payload.get('await_started_at') or ''),
            'stage_mode': str(payload.get('stage_mode') or ''),
            'stage_status': str(payload.get('stage_status') or ''),
            'stage_goal': str(payload.get('stage_goal') or ''),
            'stage_total_steps': int(payload.get('stage_total_steps') or 0),
            'tool_calls': [dict(item) for item in list(payload.get('tool_calls') or []) if isinstance(item, dict)],
            'child_pipelines': [dict(item) for item in list(payload.get('child_pipelines') or []) if isinstance(item, dict)],
            'model_visible_tool_names': [
                str(item or '').strip()
                for item in list(payload.get('model_visible_tool_names') or [])
                if str(item or '').strip()
            ],
            'hydrated_executor_names': [
                str(item or '').strip()
                for item in list(payload.get('hydrated_executor_names') or [])
                if str(item or '').strip()
            ],
            'lightweight_tool_ids': [
                str(item or '').strip()
                for item in list(payload.get('lightweight_tool_ids') or [])
                if str(item or '').strip()
            ],
            'model_visible_tool_selection_trace': dict(payload.get('model_visible_tool_selection_trace') or {}),
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
