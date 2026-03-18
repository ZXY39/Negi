from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from g3ku.content import content_summary_and_ref
from main.models import NodeRecord
from main.monitoring.models import (
    LatestTaskNodeOutput,
    TaskListItem,
    TaskLiveChildPipeline,
    TaskLiveFrame,
    TaskLiveState,
    TaskLiveToolCall,
    TaskProgressResult,
    TaskSummaryResult,
)
from main.token_usage import aggregate_node_token_usage


_CONTROL_TOOL_NAMES = {'wait_tool_execution', 'stop_tool_execution'}


class TaskQueryService:
    def __init__(self, *, store, file_store, log_service):
        self._store = store
        self._file_store = file_store
        self._log_service = log_service

    def summary(self, session_id: str | None = None) -> TaskSummaryResult:
        tasks = self._store.list_tasks(session_id)
        total = len(tasks)
        in_progress = sum(1 for item in tasks if item.status == 'in_progress')
        failed = sum(1 for item in tasks if item.status == 'failed')
        unread = sum(1 for item in tasks if bool(item.is_unread))
        return TaskSummaryResult(
            total_tasks=total,
            in_progress_tasks=in_progress,
            failed_tasks=failed,
            unread_tasks=unread,
            text=f'Tasks: {total} total, {in_progress} in progress, {failed} failed, {unread} unread',
        )

    def get_tasks(self, session_id: str | None, task_type: int) -> list[TaskListItem]:
        tasks = self._store.list_tasks(session_id)
        scope = int(task_type)
        if scope == 2:
            tasks = [item for item in tasks if item.status == 'in_progress']
        elif scope == 3:
            tasks = [item for item in tasks if item.status == 'failed']
        elif scope == 4:
            tasks = [item for item in tasks if bool(item.is_unread)]
        return [
            TaskListItem(
                task_id=item.task_id,
                session_id=item.session_id,
                title=item.title or item.task_id,
                brief=item.brief_text or '',
                status=item.status,
                is_unread=bool(item.is_unread),
                is_paused=bool(item.is_paused),
                created_at=item.created_at,
                updated_at=item.updated_at,
                max_depth=int(item.max_depth or 0),
                token_usage=item.token_usage,
            )
            for item in tasks
        ]

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> TaskProgressResult | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        tree_text = self._file_store.read_text(task.tree_text_path)
        snapshot = self._file_store.read_json(task.tree_snapshot_path)
        runtime_state = self._log_service.read_runtime_state(task_id) or {}
        if not tree_text or not isinstance(snapshot, dict):
            self._log_service.bootstrap_missing_files(task_id)
            task = self._store.get_task(task_id) or task
            tree_text = self._file_store.read_text(task.tree_text_path) or '(empty tree)'
            snapshot = self._file_store.read_json(task.tree_snapshot_path) or {}
            runtime_state = self._log_service.read_runtime_state(task_id) or {}
        if mark_read:
            self._log_service.mark_task_read(task_id)
            task = self._store.get_task(task_id) or task
        nodes = self._store.list_nodes(task_id)
        token_usage, token_usage_by_model = aggregate_node_token_usage(nodes, tracked=bool(getattr(task.token_usage, 'tracked', False)))
        latest_node = self._latest_node(nodes)
        live_state = self._live_state(runtime_state)
        text = f'Task status: {task.status}'
        if tree_text:
            text = f'{text}\n{tree_text}'
        live_summary_lines = self._live_summary_lines(live_state)
        if live_summary_lines:
            text = f'{text}\n' + '\n'.join(live_summary_lines)
        if latest_node is not None:
            latest_output = latest_node.output.strip() or '(empty)'
            text = f'{text}\nLatest node output [{latest_node.node_id}]:\n{latest_output}'
        return TaskProgressResult(
            task_id=task.task_id,
            task_status=task.status,
            tree_text=str(tree_text or '(empty tree)'),
            root=snapshot.get('root') if isinstance(snapshot.get('root'), dict) else None,
            latest_node=latest_node,
            live_state=live_state,
            nodes=[self._serialize_node(item) for item in nodes],
            token_usage=token_usage,
            token_usage_by_model=token_usage_by_model,
            text=text,
        )

    def _latest_node(self, nodes: list[NodeRecord]) -> LatestTaskNodeOutput | None:
        if not nodes:
            return None
        with_output = [node for node in nodes if self._node_output_text(node).strip()]
        target = max(with_output or nodes, key=lambda item: (str(item.updated_at or ''), str(item.created_at or ''), str(item.node_id or '')))
        return LatestTaskNodeOutput(
            node_id=target.node_id,
            parent_node_id=target.parent_node_id,
            depth=int(target.depth or 0),
            status=target.status,
            title=target.goal or target.node_id,
            updated_at=str(target.updated_at or ''),
            output=self._node_output_text(target),
            output_ref=self._node_output_ref(target),
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

    def _serialize_node(self, node: NodeRecord) -> dict[str, object]:
        payload = node.model_dump(mode='json')
        payload['execution_trace'] = self._execution_trace(node)
        return payload

    def _execution_trace(self, node: NodeRecord) -> dict[str, object]:
        tool_message_map = self._tool_message_map(node)
        tool_output_map = self._tool_output_map(node)
        tool_ref_map = self._tool_output_ref_map(node)
        tool_steps: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        background_steps: dict[str, dict[str, Any]] = {}

        for entry in list(node.output or []):
            for call in list(entry.tool_calls or []):
                tool_call_id = str(call.get('id') or '').strip()
                if not tool_call_id or tool_call_id in seen_ids:
                    continue
                seen_ids.add(tool_call_id)
                tool_name = str(call.get('name') or 'tool')
                output_text = str(tool_output_map.get(tool_call_id) or '')
                output_ref = str(tool_ref_map.get(tool_call_id) or '')
                message_meta = dict(tool_message_map.get(tool_call_id) or {})
                payload = self._parse_tool_payload(message_meta.get('content'))
                arguments = call.get('arguments')
                if isinstance(arguments, (dict, list)):
                    arguments_text = json.dumps(arguments, ensure_ascii=False, indent=2)
                else:
                    arguments_text = str(arguments or '')

                if tool_name in _CONTROL_TOOL_NAMES:
                    execution_id = str((payload or {}).get('execution_id') or '').strip()
                    if execution_id and execution_id in background_steps:
                        self._merge_background_execution_update(
                            background_steps[execution_id],
                            payload=payload or {},
                            message_meta=message_meta,
                            output_text=output_text,
                            output_ref=output_ref,
                        )
                    continue

                step: dict[str, Any] = {
                    'tool_call_id': tool_call_id,
                    'tool_name': tool_name,
                    'arguments_text': arguments_text,
                    'output_text': output_text,
                    'output_ref': output_ref,
                    'status': self._tool_step_status(output_text, node.status, payload=payload),
                    'started_at': str(entry.created_at or ''),
                    'finished_at': str(message_meta.get('finished_at') or ''),
                }
                step['elapsed_seconds'] = self._resolve_tool_elapsed_seconds(
                    message_meta=message_meta,
                    payload=payload,
                    started_at=str(entry.created_at or ''),
                    is_running=step['status'] == 'running',
                )
                execution_id = str((payload or {}).get('execution_id') or '').strip()
                if execution_id:
                    step['execution_id'] = execution_id
                    background_steps[execution_id] = step
                tool_steps.append(step)

        return {
            'initial_prompt': str(node.prompt or node.goal or ''),
            'tool_steps': tool_steps,
            'final_output': self._node_output_text(node),
            'final_output_ref': self._node_output_ref(node),
            'acceptance_result': str(node.check_result or ''),
            'acceptance_result_ref': str(getattr(node, 'check_result_ref', '') or ''),
        }

    @staticmethod
    def _live_state(runtime_state: dict[str, Any]) -> TaskLiveState | None:
        if not isinstance(runtime_state, dict):
            return None
        frames: list[TaskLiveFrame] = []
        for frame in list(runtime_state.get('frames') or []):
            if not isinstance(frame, dict):
                continue
            node_id = str(frame.get('node_id') or '').strip()
            if not node_id:
                continue
            tool_calls = [
                TaskLiveToolCall(
                    tool_call_id=str(item.get('tool_call_id') or ''),
                    tool_name=str(item.get('tool_name') or ''),
                    status=str(item.get('status') or 'queued'),
                    started_at=str(item.get('started_at') or ''),
                    finished_at=str(item.get('finished_at') or ''),
                    elapsed_seconds=TaskQueryService._coerce_elapsed_seconds(item.get('elapsed_seconds')),
                )
                for item in list(frame.get('tool_calls') or [])
                if isinstance(item, dict)
            ]
            child_pipelines = [
                TaskLiveChildPipeline(
                    index=int(item.get('index') or 0),
                    goal=str(item.get('goal') or ''),
                    status=str(item.get('status') or 'queued'),
                    child_node_id=str(item.get('child_node_id') or ''),
                    acceptance_node_id=str(item.get('acceptance_node_id') or ''),
                    check_status=str(item.get('check_status') or ''),
                    started_at=str(item.get('started_at') or ''),
                    finished_at=str(item.get('finished_at') or ''),
                )
                for item in list(frame.get('child_pipelines') or [])
                if isinstance(item, dict)
            ]
            frames.append(
                TaskLiveFrame(
                    node_id=node_id,
                    depth=int(frame.get('depth') or 0),
                    node_kind=str(frame.get('node_kind') or 'execution'),
                    phase=str(frame.get('phase') or ''),
                    tool_calls=tool_calls,
                    child_pipelines=child_pipelines,
                )
            )
        active_node_ids = [str(item) for item in list(runtime_state.get('active_node_ids') or []) if str(item or '').strip()]
        runnable_node_ids = [str(item) for item in list(runtime_state.get('runnable_node_ids') or []) if str(item or '').strip()]
        waiting_node_ids = [str(item) for item in list(runtime_state.get('waiting_node_ids') or []) if str(item or '').strip()]
        if not frames and not active_node_ids and not runnable_node_ids and not waiting_node_ids:
            return None
        return TaskLiveState(
            active_node_ids=active_node_ids,
            runnable_node_ids=runnable_node_ids,
            waiting_node_ids=waiting_node_ids,
            frames=frames,
        )

    @staticmethod
    def _coerce_elapsed_seconds(value: Any) -> float | None:
        try:
            if value is None or value == '':
                return None
            return round(max(0.0, float(value)), 1)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _live_summary_lines(live_state: TaskLiveState | None) -> list[str]:
        if live_state is None:
            return []
        lines: list[str] = []
        for frame in list(live_state.frames or []):
            running_tools = sum(1 for item in frame.tool_calls if str(item.status or '').strip().lower() in {'queued', 'running'})
            total_tools = len(list(frame.tool_calls or []))
            if total_tools and running_tools:
                lines.append(f'- {frame.node_id} tools {running_tools}/{total_tools} running')
            total_children = len(list(frame.child_pipelines or []))
            running_children = sum(1 for item in frame.child_pipelines if str(item.status or '').strip().lower() in {'queued', 'running'})
            completed_children = sum(1 for item in frame.child_pipelines if str(item.status or '').strip().lower() == 'success')
            failed_children = sum(1 for item in frame.child_pipelines if str(item.status or '').strip().lower() == 'error')
            if total_children and (running_children or completed_children or failed_children):
                lines.append(
                    f'- {frame.node_id} children {completed_children}/{total_children} completed'
                    + (f', {running_children} running' if running_children else '')
                    + (f', {failed_children} failed' if failed_children else '')
                )
        if not lines:
            return []
        return ['Active parallel work:', *lines]

    @staticmethod
    def _merge_background_execution_update(
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
        elapsed = TaskQueryService._resolve_tool_elapsed_seconds(
            message_meta=message_meta,
            payload=payload,
            started_at=str(step.get('started_at') or ''),
            is_running=str(step.get('status') or '') == 'running',
        )
        if elapsed is not None:
            step['elapsed_seconds'] = elapsed

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
    def _parse_input_messages(raw: str) -> list[dict[str, object]]:
        text = str(raw or '').strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

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
        started_ts = TaskQueryService._iso_to_epoch_seconds(started_at)
        if started_ts is None:
            return None
        finished_ts = TaskQueryService._iso_to_epoch_seconds(str(message_meta.get('finished_at') or ''))
        if finished_ts is not None:
            return round(max(0.0, finished_ts - started_ts), 1)
        if is_running:
            return round(max(0.0, datetime.now(timezone.utc).timestamp() - started_ts), 1)
        return None

    @staticmethod
    def _iso_to_epoch_seconds(value: str) -> float | None:
        text = str(value or '').strip()
        if not text:
            return None
        normalized = text.replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.timestamp()
        return parsed.astimezone(timezone.utc).timestamp()

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
