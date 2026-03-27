from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from g3ku.content import content_summary_and_ref
from main.models import ModelTokenUsageRecord, NodeRecord, TokenUsageSummary
from main.monitoring.execution_trace import build_execution_trace
from main.token_usage import aggregate_node_token_usage
from main.monitoring.models import (
    LatestTaskNodeOutput,
    TaskListItem,
    TaskLiveChildPipeline,
    TaskLiveFrame,
    TaskLiveState,
    TaskLiveToolCall,
    TaskModelCallRecord,
    TaskNodeDetail,
    TaskProgressResult,
    TaskSpawnRound,
    TaskSummaryResult,
    TaskTreeNodeSummary,
)


_CONTROL_TOOL_NAMES = {'wait_tool_execution', 'stop_tool_execution'}


class TaskQueryService:
    def __init__(self, *, store, file_store, log_service):
        self._store = store
        self._file_store = file_store
        self._log_service = log_service
        self._tree_builder = getattr(log_service, '_tree_builder', None)

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
        if mark_read:
            self._log_service.mark_task_read(task_id)
            task = self._store.get_task(task_id) or task
        root = self._projection_root(task)
        token_usage = task.token_usage
        runtime_nodes = self._store.list_nodes(task.task_id)
        _runtime_token_usage, token_usage_by_model = aggregate_node_token_usage(
            runtime_nodes,
            tracked=bool(getattr(token_usage, 'tracked', False)),
        )
        latest_node = self._latest_projection_node(task_id)
        live_state = self._projection_live_state(task_id)
        tree_text = self._render_projection_tree_text(root, task_id=task.task_id, live_state=live_state)
        latest_node = self._with_display_fallback_for_latest_node(
            task_id,
            latest_node=latest_node,
            live_state=live_state,
        )
        model_calls = self._recent_model_calls(task_id)
        text = f'Task status: {task.status}'
        if tree_text:
            text = f'{text}\n{tree_text}'
        return TaskProgressResult(
            task_id=task.task_id,
            task_status=task.status,
            tree_text=str(tree_text or '(empty tree)'),
            root=root,
            latest_node=latest_node,
            live_state=live_state,
            nodes=[self._serialize_node(node) for node in runtime_nodes],
            token_usage=token_usage,
            token_usage_by_model=token_usage_by_model,
            model_calls=model_calls,
            text=text,
        )

    def failed_node_ids(self, task_id: str) -> list[str] | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        root = self._projection_root(task)
        if root is None:
            return []

        failed_node_ids: list[str] = []

        def _walk(node: TaskTreeNodeSummary | None) -> None:
            if node is None:
                return
            node_id = str(getattr(node, 'node_id', '') or '').strip()
            status = str(getattr(node, 'status', 'in_progress') or 'in_progress').strip()
            if node_id and status == 'failed':
                failed_node_ids.append(node_id)
            for child in list(getattr(node, 'children', []) or []):
                _walk(child)

        _walk(root)
        return failed_node_ids

    def get_task_snapshot(self, task_id: str, *, mark_read: bool = True) -> dict[str, Any] | None:
        progress = self.view_progress(task_id, mark_read=mark_read)
        if progress is None:
            return None
        task = self._store.get_task(task_id)
        if task is None:
            return None
        root = self._compact_tree_payload(progress.root)
        runtime_summary = progress.live_state.model_dump(mode='json') if progress.live_state is not None else {
            'active_node_ids': [],
            'runnable_node_ids': [],
            'waiting_node_ids': [],
            'frames': [],
        }
        return {
            'task': task.model_dump(mode='json'),
            'tree_root': root,
            'runtime_summary': runtime_summary,
            'default_selected_node_id': str(getattr(progress.root, 'node_id', '') or ''),
            'progress': {
                'task_id': progress.task_id,
                'task_status': progress.task_status,
                'tree_text': progress.tree_text,
                'root': root,
                'latest_node': progress.latest_node.model_dump(mode='json') if progress.latest_node is not None else None,
                'live_state': runtime_summary,
                'nodes': list(progress.nodes or []),
                'token_usage': progress.token_usage.model_dump(mode='json'),
                'token_usage_by_model': [item.model_dump(mode='json') for item in list(progress.token_usage_by_model or [])],
                'model_calls': [item.model_dump(mode='json') for item in list(progress.model_calls or [])],
                'text': progress.text,
            },
        }

    def get_node_detail(self, task_id: str, node_id: str) -> TaskNodeDetail | None:
        task = self._store.get_task(task_id)
        detail_record = self._store.get_task_node_detail(node_id)
        if task is None or detail_record is None or detail_record.task_id != task.task_id:
            return None
        payload = dict(detail_record.payload or {})
        detail = TaskNodeDetail(
            node_id=str(payload.get('node_id') or detail_record.node_id),
            task_id=str(payload.get('task_id') or detail_record.task_id),
            parent_node_id=payload.get('parent_node_id'),
            depth=int(payload.get('depth') or 0),
            node_kind=str(payload.get('node_kind') or 'execution'),
            status=str(payload.get('status') or 'in_progress'),
            goal=str(payload.get('goal') or ''),
            prompt=str(payload.get('prompt_summary') or detail_record.prompt_summary or ''),
            input=str(payload.get('input_text') or detail_record.input_text or ''),
            input_ref=str(payload.get('input_ref') or detail_record.input_ref or ''),
            output=str(payload.get('output_text') or detail_record.output_text or ''),
            output_ref=str(payload.get('output_ref') or detail_record.output_ref or ''),
            check_result=str(payload.get('check_result') or detail_record.check_result or ''),
            check_result_ref=str(payload.get('check_result_ref') or detail_record.check_result_ref or ''),
            final_output=str(payload.get('final_output') or detail_record.final_output or ''),
            final_output_ref=str(payload.get('final_output_ref') or detail_record.final_output_ref or ''),
            failure_reason=str(payload.get('failure_reason') or detail_record.failure_reason or ''),
            updated_at=str(payload.get('updated_at') or detail_record.updated_at or ''),
            execution_trace=dict(payload.get('execution_trace') or {}),
            token_usage=TokenUsageSummary.model_validate(payload.get('token_usage') or {}),
            token_usage_by_model=[
                ModelTokenUsageRecord.model_validate(item)
                for item in list(payload.get('token_usage_by_model') or [])
                if isinstance(item, dict)
            ],
        )
        return detail

    def _compact_tree_payload(self, root) -> dict[str, Any] | None:
        if root is None:
            return None
        return {
            'node_id': root.node_id,
            'parent_node_id': root.parent_node_id,
            'depth': int(root.depth or 0),
            'node_kind': str(getattr(root, 'node_kind', 'execution') or 'execution'),
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
            'auxiliary_children': [self._compact_tree_payload(child) for child in list(getattr(root, 'auxiliary_children', []) or [])],
            'children': [self._compact_tree_payload(child) for child in list(root.children or [])],
        }

    def _projection_root(self, task) -> TaskTreeNodeSummary | None:
        nodes = self._store.list_task_nodes(task.task_id)
        if not nodes:
            return None
        node_map = {str(item.node_id or '').strip(): item for item in nodes if str(item.node_id or '').strip()}
        rounds = self._store.list_task_node_rounds(task.task_id)
        rounds_by_parent: dict[str, list[Any]] = {}
        for item in rounds:
            rounds_by_parent.setdefault(str(item.parent_node_id or '').strip(), []).append(item)

        direct_children: dict[str, list[str]] = {}
        for item in nodes:
            parent_id = str(item.parent_node_id or '').strip()
            if not parent_id:
                continue
            direct_children.setdefault(parent_id, []).append(item.node_id)

        def _sort_node_ids(node_ids: list[str]) -> list[str]:
            return sorted(
                [str(node_id or '').strip() for node_id in node_ids if str(node_id or '').strip() in node_map],
                key=lambda node_id: (
                    str(getattr(node_map[node_id], 'sort_key', '') or ''),
                    str(node_id or ''),
                ),
            )

        def _build(node_id: str):
            record = node_map.get(str(node_id or '').strip())
            if record is None:
                return None
            parent_rounds = sorted(
                rounds_by_parent.get(record.node_id, []),
                key=lambda item: (int(item.round_index or 0), str(item.round_id or '')),
            )
            selected_round_id = str(record.selected_round_id or record.default_round_id or '')
            if not selected_round_id and parent_rounds:
                selected_round_id = str(parent_rounds[-1].round_id or '')
            round_child_ids = {
                child_id
                for round_item in parent_rounds
                for child_id in list(round_item.child_node_ids or [])
                if str(child_id or '').strip()
            }
            auxiliary_child_ids = [
                child_id
                for child_id in _sort_node_ids(direct_children.get(record.node_id, []))
                if child_id not in round_child_ids
            ]
            spawn_rounds: list[TaskSpawnRound] = []
            for round_item in parent_rounds:
                round_children = [
                    child
                    for child in (
                        _build(str(child_id or '').strip())
                        for child_id in list(round_item.child_node_ids or [])
                        if str(child_id or '').strip() in node_map
                    )
                    if child is not None
                ]
                spawn_rounds.append(
                    TaskSpawnRound(
                        round_id=str(round_item.round_id or ''),
                        round_index=int(round_item.round_index or 0),
                        label=str(round_item.label or ''),
                        is_latest=bool(round_item.is_latest),
                        created_at=str(round_item.created_at or ''),
                        child_node_ids=[str(item) for item in list(round_item.child_node_ids or []) if str(item or '').strip()],
                        source=str(round_item.source or 'explicit'),
                        total_children=int(round_item.total_children or 0),
                        completed_children=int(round_item.completed_children or 0),
                        running_children=int(round_item.running_children or 0),
                        failed_children=int(round_item.failed_children or 0),
                        children=round_children,
                    )
                )
            selected_round = next((item for item in spawn_rounds if item.round_id == selected_round_id), None)
            auxiliary_children = [
                child
                for child in (_build(child_id) for child_id in auxiliary_child_ids)
                if child is not None
            ]
            children = [
                child
                for child in (
                    auxiliary_children
                    + list(selected_round.children if selected_round is not None else [])
                )
                if child is not None
            ]
            return TaskTreeNodeSummary(
                node_id=record.node_id,
                parent_node_id=record.parent_node_id,
                depth=int(record.depth or 0),
                node_kind=str(record.node_kind or 'execution'),
                status=record.status,
                title=str(record.title or record.node_id),
                updated_at=str(record.updated_at or ''),
                spawn_rounds=spawn_rounds,
                default_round_id=str(record.default_round_id or ''),
                auxiliary_children=auxiliary_children,
                children=children,
            )

        return _build(task.root_node_id)

    def _recent_model_calls(self, task_id: str, *, limit: int = 50) -> list[TaskModelCallRecord]:
        events = self._store.list_task_events(after_seq=0, task_id=task_id, limit=1000)
        records: list[TaskModelCallRecord] = []
        for event in list(events or []):
            if str(event.get('event_type') or '') != 'task.model.call':
                continue
            payload = dict(event.get('payload') or {})
            records.append(
                TaskModelCallRecord(
                    call_index=int(payload.get('call_index') or 0),
                    created_at=str(event.get('created_at') or ''),
                    prepared_message_count=int(payload.get('prepared_message_count') or 0),
                    prepared_message_chars=int(payload.get('prepared_message_chars') or 0),
                    response_tool_call_count=int(payload.get('response_tool_call_count') or 0),
                    delta_usage=TokenUsageSummary.model_validate(payload.get('delta_usage') or {}),
                    delta_usage_by_model=[
                        ModelTokenUsageRecord.model_validate(item)
                        for item in list(payload.get('delta_usage_by_model') or [])
                        if isinstance(item, dict)
                    ],
                )
            )
        records.sort(key=lambda item: int(item.call_index or 0))
        return records[-max(1, int(limit or 1)) :]

    def _projection_live_state(self, task_id: str) -> TaskLiveState | None:
        frames = self._store.list_task_runtime_frames(task_id)
        if not frames:
            runtime_state = self._log_service.read_runtime_state(task_id)
            return self._live_state(runtime_state or {})
        live_frames: list[TaskLiveFrame] = []
        active_node_ids: list[str] = []
        runnable_node_ids: list[str] = []
        waiting_node_ids: list[str] = []
        for record in frames:
            payload = dict(record.payload or {})
            if record.active:
                active_node_ids.append(record.node_id)
            if record.runnable:
                runnable_node_ids.append(record.node_id)
            if record.waiting:
                waiting_node_ids.append(record.node_id)
            live_frames.append(
                TaskLiveFrame(
                    node_id=record.node_id,
                    depth=int(record.depth or 0),
                    node_kind=str(record.node_kind or 'execution'),
                    phase=str(record.phase or ''),
                    stage_mode=str(payload.get('stage_mode') or ''),
                    stage_status=str(payload.get('stage_status') or ''),
                    stage_goal=str(payload.get('stage_goal') or ''),
                    stage_total_steps=int(payload.get('stage_total_steps') or 0),
                    tool_calls=[
                        TaskLiveToolCall(
                            tool_call_id=str(item.get('tool_call_id') or ''),
                            tool_name=str(item.get('tool_name') or ''),
                            status=str(item.get('status') or 'queued'),
                            started_at=str(item.get('started_at') or ''),
                            finished_at=str(item.get('finished_at') or ''),
                            elapsed_seconds=self._coerce_elapsed_seconds(item.get('elapsed_seconds')),
                        )
                        for item in list(payload.get('tool_calls') or [])
                        if isinstance(item, dict)
                    ],
                    child_pipelines=[
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
                        for item in list(payload.get('child_pipelines') or [])
                        if isinstance(item, dict)
                    ],
                )
            )
        return TaskLiveState(
            active_node_ids=sorted(active_node_ids),
            runnable_node_ids=sorted(runnable_node_ids),
            waiting_node_ids=sorted(waiting_node_ids),
            frames=live_frames,
        )

    def _latest_projection_node(self, task_id: str) -> LatestTaskNodeOutput | None:
        details = self._store.list_task_node_details(task_id)
        if not details:
            return None
        nodes = {item.node_id: item for item in self._store.list_task_nodes(task_id)}
        scored = []
        for detail in details:
            payload = dict(detail.payload or {})
            output = str(payload.get('output_text') or detail.output_text or '').strip()
            final_output = str(payload.get('final_output') or detail.final_output or '').strip()
            failure_reason = str(payload.get('failure_reason') or detail.failure_reason or '').strip()
            effective_output = final_output or output or failure_reason
            node = nodes.get(detail.node_id)
            scored.append((bool(effective_output), str(detail.updated_at or ''), detail.node_id, effective_output, node))
        best = max(scored, key=lambda item: (item[0], item[1], item[2]))
        _has_output, _updated_at, detail_node_id, effective_output, node = best
        detail = next((item for item in details if item.node_id == detail_node_id), None)
        return LatestTaskNodeOutput(
            node_id=detail_node_id,
            parent_node_id=getattr(node, 'parent_node_id', None),
            depth=int(getattr(node, 'depth', 0) or 0),
            status=getattr(node, 'status', 'in_progress'),
            title=str(getattr(node, 'title', detail_node_id) or detail_node_id),
            updated_at=str(_updated_at or ''),
            output=effective_output,
            output_ref=str(getattr(detail, 'output_ref', '') or getattr(detail, 'final_output_ref', '') or ''),
        )

    def _with_display_fallback_for_latest_node(
        self,
        task_id: str,
        *,
        latest_node: LatestTaskNodeOutput | None,
        live_state: TaskLiveState | None,
    ) -> LatestTaskNodeOutput | None:
        if latest_node is None:
            return None
        if str(latest_node.output or '').strip():
            return latest_node
        projection_output, projection_ref = self._latest_projection_plain_output(task_id)
        if projection_output:
            return latest_node.model_copy(
                update={
                    'output': projection_output,
                    'output_ref': projection_ref or latest_node.output_ref,
                }
            )
        live_output = self._live_tool_call_summary(live_state, preferred_node_id=latest_node.node_id)
        if live_output:
            return latest_node.model_copy(update={'output': live_output})
        historical_output = self._historical_tool_call_summary(task_id, preferred_node_id=latest_node.node_id)
        if not historical_output:
            return latest_node
        return latest_node.model_copy(update={'output': historical_output})

    def _latest_projection_plain_output(self, task_id: str) -> tuple[str, str]:
        details = self._store.list_task_node_details(task_id)
        scored: list[tuple[str, str, str, str]] = []
        for detail in details:
            payload = dict(detail.payload or {})
            output = str(payload.get('output_text') or detail.output_text or '').strip()
            if not output:
                continue
            output_ref = str(payload.get('output_ref') or detail.output_ref or '').strip()
            scored.append((str(detail.updated_at or ''), str(detail.node_id or ''), output, output_ref))
        if not scored:
            return '', ''
        _updated_at, _node_id, output, output_ref = max(scored, key=lambda item: (item[0], item[1]))
        return output, output_ref

    @staticmethod
    def _live_tool_call_summary(
        live_state: TaskLiveState | None,
        *,
        preferred_node_id: str = '',
        limit: int = 5,
    ) -> str:
        if live_state is None or not list(live_state.frames or []):
            return ''
        frames = list(live_state.frames or [])
        selected = None
        preferred = str(preferred_node_id or '').strip()
        if preferred:
            selected = next((frame for frame in frames if str(frame.node_id or '').strip() == preferred), None)
        if selected is None:
            frames_by_node_id = {str(frame.node_id or '').strip(): frame for frame in frames if str(frame.node_id or '').strip()}
            for node_id in [
                *list(live_state.active_node_ids or []),
                *list(live_state.runnable_node_ids or []),
                *list(live_state.waiting_node_ids or []),
            ]:
                selected = frames_by_node_id.get(str(node_id or '').strip())
                if selected is not None:
                    break
        if selected is None:
            selected = frames[0]
        lines: list[str] = []
        tool_calls = [item for item in list(selected.tool_calls or []) if str(item.tool_name or '').strip()]
        if tool_calls:
            lines.append('Recent tool calls:')
            for item in tool_calls[-max(1, int(limit or 1)) :]:
                tool_name = str(item.tool_name or 'tool').strip() or 'tool'
                status = str(item.status or 'queued').strip() or 'queued'
                lines.append(f'- {tool_name} [{status}]')
        return '\n'.join(lines)


    def _historical_tool_call_summary(
        self,
        task_id: str,
        *,
        preferred_node_id: str = '',
        limit: int = 5,
    ) -> str:
        tool_steps = self._historical_tool_steps(task_id, preferred_node_id=preferred_node_id)
        return self._tool_steps_summary(tool_steps, limit=limit)

    def _historical_tool_steps(
        self,
        task_id: str,
        *,
        preferred_node_id: str = '',
    ) -> list[dict[str, Any]]:
        details = list(self._store.list_task_node_details(task_id) or [])
        selected_detail = None
        preferred = str(preferred_node_id or '').strip()
        if preferred:
            selected_detail = next((item for item in details if str(item.node_id or '').strip() == preferred), None)
        if selected_detail is None and details:
            selected_detail = max(details, key=lambda item: (str(item.updated_at or ''), str(item.node_id or '')))
        if selected_detail is not None:
            payload = dict(selected_detail.payload or {})
            execution_trace = payload.get('execution_trace') if isinstance(payload.get('execution_trace'), dict) else None
            if isinstance(execution_trace, dict):
                tool_steps = [item for item in list(execution_trace.get('tool_steps') or []) if isinstance(item, dict)]
                if tool_steps:
                    return tool_steps
        node_id = preferred or str(getattr(selected_detail, 'node_id', '') or '').strip()
        if not node_id:
            return []
        node = self._store.get_node(node_id)
        if node is None:
            return []
        execution_trace = self._execution_trace(node)
        return [item for item in list(execution_trace.get('tool_steps') or []) if isinstance(item, dict)]

    @staticmethod
    def _tool_steps_summary(tool_steps: list[dict[str, Any]], *, limit: int = 5) -> str:
        steps = [item for item in list(tool_steps or []) if isinstance(item, dict) and str(item.get('tool_name') or '').strip()]
        if not steps:
            return ''
        lines = ['Recent tool calls:']
        for item in steps[-max(1, int(limit or 1)) :]:
            tool_name = str(item.get('tool_name') or 'tool').strip() or 'tool'
            status = str(item.get('status') or 'queued').strip() or 'queued'
            lines.append(f'- {tool_name} [{status}]')
        return '\n'.join(lines)

    def _render_projection_tree_text(
        self,
        root: TaskTreeNodeSummary | None,
        *,
        task_id: str,
        live_state: TaskLiveState | None = None,
    ) -> str:
        if root is None:
            return '(empty tree)'
        lines: list[str] = []
        stage_goals = self._node_stage_goal_map(task_id, live_state=live_state)

        def _walk(node: TaskTreeNodeSummary, prefix: str = '', *, is_root: bool = False) -> None:
            label = self._tree_text_label(node, stage_goals)
            lines.append(label if is_root else f'{prefix}|-{label}')
            child_prefix = '' if is_root else f'{prefix}  '
            for child in list(node.children or []):
                _walk(child, child_prefix, is_root=False)

        _walk(root, is_root=True)
        return '\n'.join(lines)

    def _node_stage_goal_map(
        self,
        task_id: str,
        *,
        live_state: TaskLiveState | None = None,
    ) -> dict[str, str]:
        goals: dict[str, str] = {}
        for detail in list(self._store.list_task_node_details(task_id) or []):
            node_id = str(getattr(detail, 'node_id', '') or '').strip()
            if not node_id:
                continue
            payload = dict(getattr(detail, 'payload', {}) or {})
            execution_trace = payload.get('execution_trace') if isinstance(payload.get('execution_trace'), dict) else {}
            stage_goal = self._latest_stage_goal_from_execution_trace(execution_trace)
            if stage_goal:
                goals[node_id] = stage_goal
        if live_state is None:
            return goals
        for frame in list(live_state.frames or []):
            node_id = str(frame.node_id or '').strip()
            stage_goal = str(frame.stage_goal or '').strip()
            if node_id and stage_goal:
                goals[node_id] = stage_goal
        return goals

    @staticmethod
    def _latest_stage_goal_from_execution_trace(execution_trace: dict[str, Any] | None) -> str:
        if not isinstance(execution_trace, dict):
            return ''
        scored: list[tuple[int, str, str]] = []
        for stage in list(execution_trace.get('stages') or []):
            if not isinstance(stage, dict):
                continue
            stage_goal = str(stage.get('stage_goal') or '').strip()
            if not stage_goal:
                continue
            try:
                stage_index = int(stage.get('stage_index') or 0)
            except (TypeError, ValueError):
                stage_index = 0
            scored.append((stage_index, str(stage.get('stage_id') or ''), stage_goal))
        if not scored:
            return ''
        return max(scored, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    def _tree_display_stage_goal(node: TaskTreeNodeSummary, stage_goals: dict[str, str]) -> str:
        stage_goal = str(stage_goals.get(str(node.node_id or '').strip()) or '').strip()
        if stage_goal:
            return stage_goal
        if str(getattr(node, 'node_kind', 'execution') or 'execution').strip().lower() == 'acceptance':
            return '检验中'
        return '无阶段目标'

    @classmethod
    def _tree_text_label(cls, node: TaskTreeNodeSummary, stage_goals: dict[str, str]) -> str:
        node_id = str(getattr(node, 'node_id', '') or '').strip()
        status = str(getattr(node, 'status', '') or '').strip()
        stage_goal = cls._tree_display_stage_goal(node, stage_goals)
        if str(getattr(node, 'node_kind', 'execution') or 'execution').strip().lower() == 'acceptance':
            parent_node_id = str(getattr(node, 'parent_node_id', '') or '').strip() or '?'
            return f'([验收上层父节点:{parent_node_id}] {node_id},{status},{stage_goal})'
        return f'({node_id},{status},{stage_goal})'

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
        final_output = str(node.final_output or '')
        acceptance_result = str(node.check_result or '')
        return {
            'node_id': node.node_id,
            'task_id': node.task_id,
            'parent_node_id': node.parent_node_id,
            'root_node_id': node.root_node_id,
            'depth': int(node.depth or 0),
            'node_kind': str(node.node_kind or 'execution'),
            'status': node.status,
            'goal': str(node.goal or ''),
            'title': str(node.goal or node.node_id),
            'output': self._node_output_text(node),
            'output_ref': self._node_output_ref(node),
            'check_result': acceptance_result,
            'check_result_ref': str(node.check_result_ref or ''),
            'final_output': final_output,
            'final_output_ref': str(getattr(node, 'final_output_ref', '') or ''),
            'failure_reason': str(node.failure_reason or ''),
            'updated_at': str(node.updated_at or ''),
            'execution_trace': {
                'initial_prompt': '',
                'tool_steps': [],
                'stages': [],
                'live_tool_calls': [],
                'live_child_pipelines': [],
                'final_output': final_output,
                'acceptance_result': acceptance_result,
            },
        }

    def _execution_trace(self, node: NodeRecord) -> dict[str, object]:
        return build_execution_trace(node)

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
                    stage_mode=str(frame.get('stage_mode') or ''),
                    stage_status=str(frame.get('stage_status') or ''),
                    stage_goal=str(frame.get('stage_goal') or ''),
                    stage_total_steps=int(frame.get('stage_total_steps') or 0),
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
