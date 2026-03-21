from __future__ import annotations

import json
from typing import Any

from g3ku.content import content_summary_and_ref
from main.models import NodeRecord, TaskRecord
from main.monitoring.execution_trace import build_execution_trace
from main.monitoring.models import (
    TaskProjectionMetaRecord,
    TaskProjectionNodeDetailRecord,
    TaskProjectionNodeRecord,
    TaskProjectionRoundRecord,
    TaskProjectionRuntimeFrameRecord,
)
from main.protocol import now_iso

PROJECTION_VERSION = 3


def _single_line_text(value: Any, *, max_chars: int = 240) -> str:
    text = ' '.join(str(value or '').split())
    if len(text) <= max_chars:
        return text
    return f'{text[: max_chars - 3].rstrip()}...'


class TaskProjectionService:
    def __init__(self, *, store, tree_builder) -> None:
        self._store = store
        self._tree_builder = tree_builder

    def ensure_task_projection(self, task_id: str) -> None:
        meta = self._store.get_task_projection_meta(task_id)
        if meta is not None and int(meta.version or 0) == PROJECTION_VERSION:
            if self._store.list_task_nodes(task_id) and self._store.list_task_node_details(task_id):
                return
        self.sync_task(task_id)

    def sync_task(
        self,
        task_id: str,
        *,
        task: TaskRecord | None = None,
        nodes: list[NodeRecord] | None = None,
        runtime_state: dict[str, Any] | None = None,
    ) -> None:
        current_task = task or self._store.get_task(task_id)
        if current_task is None:
            return
        current_nodes = list(nodes or self._store.list_nodes(task_id))
        current_runtime = runtime_state if isinstance(runtime_state, dict) else (self._store.get_runtime_state(task_id) or {})
        root = self._tree_builder.build_tree(current_task, current_nodes)
        node_map = {item.node_id: item for item in current_nodes}
        projection_nodes: dict[str, TaskProjectionNodeRecord] = {}
        projection_rounds: list[TaskProjectionRoundRecord] = []

        def _walk(tree_node) -> None:
            if tree_node is None:
                return
            record = node_map.get(str(tree_node.node_id or '').strip())
            if record is None:
                return
            sort_key = f'{str(record.created_at or "")}:{str(record.node_id or "")}'
            projection_nodes[record.node_id] = TaskProjectionNodeRecord(
                node_id=record.node_id,
                task_id=record.task_id,
                parent_node_id=record.parent_node_id,
                root_node_id=record.root_node_id,
                depth=int(record.depth or 0),
                node_kind=str(record.node_kind or 'execution'),
                status=record.status,
                title=record.goal or record.node_id,
                updated_at=str(record.updated_at or ''),
                default_round_id=str(tree_node.default_round_id or ''),
                selected_round_id=str(tree_node.default_round_id or ''),
                round_options_count=len(list(tree_node.spawn_rounds or [])),
                sort_key=sort_key,
                payload={
                    'node_id': record.node_id,
                    'parent_node_id': record.parent_node_id,
                    'depth': int(record.depth or 0),
                    'node_kind': str(record.node_kind or 'execution'),
                    'status': record.status,
                    'title': record.goal or record.node_id,
                    'updated_at': str(record.updated_at or ''),
                    'default_round_id': str(tree_node.default_round_id or ''),
                    'selected_round_id': str(tree_node.default_round_id or ''),
                    'round_options_count': len(list(tree_node.spawn_rounds or [])),
                },
            )
            for round_item in list(tree_node.spawn_rounds or []):
                projection_rounds.append(
                    TaskProjectionRoundRecord(
                        task_id=current_task.task_id,
                        parent_node_id=record.node_id,
                        round_id=str(round_item.round_id or ''),
                        round_index=int(round_item.round_index or 0),
                        label=str(round_item.label or ''),
                        is_latest=bool(round_item.is_latest),
                        created_at=str(round_item.created_at or ''),
                        source=str(round_item.source or 'explicit'),
                        total_children=int(round_item.total_children or 0),
                        completed_children=int(round_item.completed_children or 0),
                        running_children=int(round_item.running_children or 0),
                        failed_children=int(round_item.failed_children or 0),
                        child_node_ids=[str(item or '') for item in list(round_item.child_node_ids or []) if str(item or '').strip()],
                    )
                )
                for child in list(round_item.children or []):
                    _walk(child)
            for child in list(tree_node.auxiliary_children or []):
                _walk(child)

        _walk(root)

        detail_records = [self._build_node_detail(record) for record in current_nodes]
        frame_records = self._build_runtime_frame_records(current_task, current_runtime)

        self._store.replace_task_nodes(current_task.task_id, list(projection_nodes.values()))
        self._store.replace_task_node_details(current_task.task_id, detail_records)
        self._store.replace_task_runtime_frames(current_task.task_id, frame_records)
        self._store.replace_task_node_rounds(current_task.task_id, projection_rounds)
        self._store.upsert_task_projection_meta(
            TaskProjectionMetaRecord(task_id=current_task.task_id, version=PROJECTION_VERSION, updated_at=now_iso())
        )

    def sync_runtime_state(
        self,
        task_id: str,
        *,
        task: TaskRecord | None = None,
        runtime_state: dict[str, Any] | None = None,
    ) -> None:
        current_task = task or self._store.get_task(task_id)
        if current_task is None:
            return
        current_runtime = runtime_state if isinstance(runtime_state, dict) else (self._store.get_runtime_state(task_id) or {})
        frame_records = self._build_runtime_frame_records(current_task, current_runtime)
        self._store.replace_task_runtime_frames(current_task.task_id, frame_records)
        self._store.upsert_task_projection_meta(
            TaskProjectionMetaRecord(
                task_id=current_task.task_id,
                version=PROJECTION_VERSION,
                updated_at=now_iso(),
            )
        )

    def _build_runtime_frame_records(
        self,
        task: TaskRecord,
        runtime_state: dict[str, Any],
    ) -> list[TaskProjectionRuntimeFrameRecord]:
        active_ids = {str(item or '').strip() for item in list(runtime_state.get('active_node_ids') or []) if str(item or '').strip()}
        runnable_ids = {str(item or '').strip() for item in list(runtime_state.get('runnable_node_ids') or []) if str(item or '').strip()}
        waiting_ids = {str(item or '').strip() for item in list(runtime_state.get('waiting_node_ids') or []) if str(item or '').strip()}
        records: list[TaskProjectionRuntimeFrameRecord] = []
        for frame in list(runtime_state.get('frames') or []):
            if not isinstance(frame, dict):
                continue
            node_id = str(frame.get('node_id') or '').strip()
            if not node_id:
                continue
            payload = {
                'node_id': node_id,
                'depth': int(frame.get('depth') or 0),
                'node_kind': str(frame.get('node_kind') or 'execution'),
                'phase': str(frame.get('phase') or ''),
                'tool_calls': [dict(item) for item in list(frame.get('tool_calls') or []) if isinstance(item, dict)],
                'child_pipelines': [dict(item) for item in list(frame.get('child_pipelines') or []) if isinstance(item, dict)],
            }
            records.append(
                TaskProjectionRuntimeFrameRecord(
                    task_id=task.task_id,
                    node_id=node_id,
                    depth=int(payload['depth']),
                    node_kind=str(payload['node_kind']),
                    phase=str(payload['phase']),
                    active=node_id in active_ids,
                    runnable=node_id in runnable_ids,
                    waiting=node_id in waiting_ids,
                    updated_at=str(runtime_state.get('updated_at') or now_iso()),
                    payload=payload,
                )
            )
        return records

    def _build_node_detail(self, node: NodeRecord) -> TaskProjectionNodeDetailRecord:
        output_text = self._node_output_text(node)
        output_ref = self._node_output_ref(node)
        execution_trace = self._execution_trace(node)
        prompt_summary = _single_line_text(node.prompt or node.goal or '', max_chars=400)
        return TaskProjectionNodeDetailRecord(
            node_id=node.node_id,
            task_id=node.task_id,
            updated_at=str(node.updated_at or ''),
            input_text=str(node.input or ''),
            input_ref=str(node.input_ref or ''),
            output_text=output_text,
            output_ref=output_ref,
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
                'output_text': output_text,
                'output_ref': output_ref,
                'check_result': str(node.check_result or ''),
                'check_result_ref': str(node.check_result_ref or ''),
                'final_output': str(node.final_output or ''),
                'final_output_ref': str(node.final_output_ref or ''),
                'failure_reason': str(node.failure_reason or ''),
                'updated_at': str(node.updated_at or ''),
                'execution_trace': execution_trace,
                'token_usage': node.token_usage.model_dump(mode='json'),
                'token_usage_by_model': [item.model_dump(mode='json') for item in list(node.token_usage_by_model or [])],
            },
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

    def _execution_trace(self, node: NodeRecord) -> dict[str, object]:
        return build_execution_trace(node)

    @staticmethod
    def _parse_input_messages(raw: str) -> list[dict[str, object]]:
        text = str(raw or '').strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    def _tool_message_map(self, node: NodeRecord) -> dict[str, dict[str, Any]]:
        messages = self._parse_input_messages(node.input)
        result: dict[str, dict[str, Any]] = {}
        for item in messages:
            if not isinstance(item, dict):
                continue
            if str(item.get('role') or '').strip().lower() != 'tool':
                continue
            tool_call_id = str(item.get('tool_call_id') or '').strip()
            if not tool_call_id:
                continue
            result[tool_call_id] = dict(item)
        return result

    def _tool_output_map(self, node: NodeRecord) -> dict[str, str]:
        messages = self._parse_input_messages(node.input)
        result: dict[str, str] = {}
        for item in messages:
            if not isinstance(item, dict):
                continue
            if str(item.get('role') or '').strip().lower() != 'tool':
                continue
            tool_call_id = str(item.get('tool_call_id') or '').strip()
            if not tool_call_id:
                continue
            content = item.get('content')
            summary, _ref = content_summary_and_ref(content)
            result[tool_call_id] = summary
        return result

    def _tool_output_ref_map(self, node: NodeRecord) -> dict[str, str]:
        messages = self._parse_input_messages(node.input)
        result: dict[str, str] = {}
        for item in messages:
            if not isinstance(item, dict):
                continue
            if str(item.get('role') or '').strip().lower() != 'tool':
                continue
            tool_call_id = str(item.get('tool_call_id') or '').strip()
            if not tool_call_id:
                continue
            _summary, ref = content_summary_and_ref(item.get('content'))
            result[tool_call_id] = ref
        return result

    @staticmethod
    def _parse_tool_payload(content: object) -> dict[str, object] | None:
        if not isinstance(content, str):
            return None
        text = content.strip()
        if not text or text[:1] not in {'{', '['}:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _resolve_tool_elapsed_seconds(
        *,
        message_meta: dict[str, Any],
        payload: dict[str, Any] | None,
        started_at: str,
        is_running: bool,
    ) -> float | None:
        raw_elapsed = None
        if isinstance(payload, dict):
            raw_elapsed = payload.get('elapsed_seconds')
        if raw_elapsed is None:
            raw_elapsed = message_meta.get('elapsed_seconds')
        try:
            if raw_elapsed is not None:
                return round(max(0.0, float(raw_elapsed)), 1)
        except (TypeError, ValueError):
            pass
        _ = started_at, is_running
        return None

    @classmethod
    def _merge_background_execution_update(
        cls,
        step: dict[str, Any],
        *,
        payload: dict[str, Any],
        message_meta: dict[str, Any],
        output_text: str,
        output_ref: str,
    ) -> None:
        status = str(payload.get('status') or '').strip().lower()
        if status == 'completed':
            step['status'] = 'success'
        elif status in {'stopped', 'failed', 'error', 'not_found', 'unavailable'}:
            step['status'] = 'error'
        elif status == 'background_running':
            step['status'] = 'running'
        if output_text:
            step['output_text'] = output_text
        if output_ref:
            step['output_ref'] = output_ref
        if str(message_meta.get('finished_at') or '').strip():
            step['finished_at'] = str(message_meta.get('finished_at') or '')

    @staticmethod
    def _tool_step_status(output_text: str, node_status: str, *, payload: dict[str, Any] | None = None) -> str:
        payload_status = str((payload or {}).get('status') or '').strip().lower()
        if payload_status == 'background_running':
            return 'running'
        if payload_status in {'completed'}:
            return 'success'
        if payload_status in {'stopped', 'failed', 'error', 'not_found', 'unavailable'}:
            return 'error'
        text = str(output_text or '').strip()
        if text:
            lowered = text.lower()
            if text.startswith('Error:') or '"status":"error"' in lowered or '"status": "error"' in lowered:
                return 'error'
            return 'success'
        if str(node_status or '').strip().lower() == 'in_progress':
            return 'running'
        return 'success'
