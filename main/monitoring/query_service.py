from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from g3ku.content import content_summary_and_ref
from main.models import ModelTokenUsageRecord, NodeRecord, TokenUsageSummary, normalize_tool_file_changes
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
    TaskSummaryResult,
    TaskTreeSnapshot,
    TaskTreeSnapshotNode,
    TaskTreeSnapshotRound,
)


_CONTROL_TOOL_NAMES = {'wait_tool_execution', 'stop_tool_execution'}


class TaskQueryService:
    def __init__(self, *, store, file_store, log_service, debug_recorder=None):
        self._store = store
        self._file_store = file_store
        self._log_service = log_service
        self._debug_recorder = debug_recorder

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
        node_map, rounds_by_parent, direct_children = self._projection_maps(task.task_id)
        token_usage = task.token_usage
        runtime_nodes = self._store.list_nodes(task.task_id)
        _runtime_token_usage, token_usage_by_model = aggregate_node_token_usage(
            runtime_nodes,
            tracked=bool(getattr(token_usage, 'tracked', False)),
        )
        latest_node = self._latest_projection_node(task_id)
        live_state = self._projection_live_state(task_id)
        tree_text = self._render_projection_tree_text(
            task.task_id,
            root_node_id=str(task.root_node_id or '').strip(),
            node_map=node_map,
            rounds_by_parent=rounds_by_parent,
            direct_children=direct_children,
            live_state=live_state,
        )
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
        root_node_id = str(task.root_node_id or '').strip()
        if not root_node_id:
            return []
        node_map, rounds_by_parent, direct_children = self._projection_maps(task.task_id)
        if root_node_id not in node_map:
            return []

        failed_node_ids: list[str] = []

        def _walk(node_id: str, seen: set[str]) -> None:
            normalized_node_id = str(node_id or '').strip()
            if not normalized_node_id or normalized_node_id in seen:
                return
            record = node_map.get(normalized_node_id)
            if record is None:
                return
            seen.add(normalized_node_id)
            status = str(getattr(record, 'status', 'in_progress') or 'in_progress').strip()
            if normalized_node_id and status == 'failed':
                failed_node_ids.append(normalized_node_id)
            for child_id in self._projection_visible_child_ids(
                normalized_node_id,
                node_map=node_map,
                rounds_by_parent=rounds_by_parent,
                direct_children=direct_children,
            ):
                _walk(child_id, seen)

        _walk(root_node_id, set())
        return failed_node_ids

    def get_task_snapshot(
        self,
        task_id: str,
        *,
        mark_read: bool = True,
    ) -> dict[str, Any] | None:
        started_at = datetime.now().astimezone().isoformat(timespec='seconds')
        started_mono = time.perf_counter()
        task = self._store.get_task(task_id)
        if task is None:
            return None
        if mark_read:
            self._log_service.mark_task_read(task_id)
            task = self._store.get_task(task_id) or task
        live_state = self._projection_live_state(task.task_id)
        root_node = self.get_node_detail(task.task_id, task.root_node_id)
        runtime_summary = live_state.model_dump(mode='json') if live_state is not None else {
            'active_node_ids': [],
            'runnable_node_ids': [],
            'waiting_node_ids': [],
            'dispatch_limits': {'execution': 0, 'inspection': 0},
            'dispatch_running': {'execution': 0, 'inspection': 0},
            'dispatch_queued': {'execution': 0, 'inspection': 0},
            'frames': [],
        }
        counts = {
            'total_nodes': len(self._store.list_task_nodes(task.task_id)),
            'total_rounds': len(self._store.list_task_node_rounds(task.task_id)),
            'active_node_count': len(list(runtime_summary.get('active_node_ids') or [])),
            'runnable_node_count': len(list(runtime_summary.get('runnable_node_ids') or [])),
            'waiting_node_count': len(list(runtime_summary.get('waiting_node_ids') or [])),
        }
        frontier = [
            frame.model_dump(mode='json')
            for frame in list(live_state.frames or [])
        ] if live_state is not None else []
        token_usage_by_model = [
            item.model_dump(mode='json')
            for item in self._projection_token_usage_by_model(task.task_id)
        ]
        payload = {
            'task': task.model_dump(mode='json'),
            'summary': {
                'task_id': task.task_id,
                'status': task.status,
                'brief': task.brief_text,
                'updated_at': task.updated_at,
                'token_usage_by_model': token_usage_by_model,
                **counts,
            },
            'runtime_summary': runtime_summary,
            'root_node': root_node.model_dump(mode='json') if root_node is not None else None,
            'frontier': frontier,
            'counts': counts,
            'recent_model_calls': [item.model_dump(mode='json') for item in self._recent_model_calls(task.task_id)],
        }
        self._record_debug('query_service.get_task_snapshot', started_at=started_at, started_mono=started_mono)
        return payload

    def get_node_detail(self, task_id: str, node_id: str) -> TaskNodeDetail | None:
        task = self._store.get_task(task_id)
        detail_record = self._store.get_task_node_detail(node_id)
        node_record = self._store.get_task_node(node_id)
        if task is None or detail_record is None or detail_record.task_id != task.task_id:
            return None
        if node_record is not None and str(node_record.task_id or '').strip() != task.task_id:
            node_record = None
        payload = dict(detail_record.payload or {})
        node_payload = dict(getattr(node_record, 'payload', None) or {}) if node_record is not None else {}
        detail = TaskNodeDetail(
            node_id=str(payload.get('node_id') or detail_record.node_id),
            task_id=str(payload.get('task_id') or detail_record.task_id),
            parent_node_id=payload.get('parent_node_id', node_payload.get('parent_node_id')),
            depth=int(payload.get('depth') or node_payload.get('depth') or 0),
            node_kind=str(payload.get('node_kind') or node_payload.get('node_kind') or 'execution'),
            status=str(payload.get('status') or node_payload.get('status') or 'in_progress'),
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
            updated_at=str(payload.get('updated_at') or node_payload.get('updated_at') or detail_record.updated_at or ''),
            children_fingerprint=str(payload.get('children_fingerprint') or node_payload.get('children_fingerprint') or ''),
            execution_trace=dict(payload.get('execution_trace') or {}),
            tool_file_changes=normalize_tool_file_changes(payload.get('tool_file_changes')),
            token_usage=TokenUsageSummary.model_validate(payload.get('token_usage') or {}),
            token_usage_by_model=[
                ModelTokenUsageRecord.model_validate(item)
                for item in list(payload.get('token_usage_by_model') or [])
                if isinstance(item, dict)
            ],
        )
        final_output_full = self._resolve_detail_text(detail.final_output, detail.final_output_ref)
        if final_output_full:
            detail.final_output = final_output_full
            detail.execution_trace['final_output'] = final_output_full
        acceptance_result_full = self._resolve_detail_text(detail.check_result, detail.check_result_ref)
        if acceptance_result_full:
            detail.check_result = acceptance_result_full
            detail.execution_trace['acceptance_result'] = acceptance_result_full
        return detail

    def get_tree_snapshot(self, task_id: str) -> TaskTreeSnapshot | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        return self._build_tree_snapshot(task_id=task.task_id, root_node_id=task.root_node_id)

    def get_tree_subtree(
        self,
        task_id: str,
        node_id: str,
        *,
        round_id: str | None = None,
    ) -> TaskTreeSnapshot | None:
        task = self._store.get_task(task_id)
        root = self._store.get_task_node(node_id)
        if task is None or root is None or str(root.task_id or '').strip() != task.task_id:
            return None
        return self._build_tree_snapshot(
            task_id=task.task_id,
            root_node_id=task.root_node_id,
            scope_root_id=str(node_id or '').strip(),
            root_round_id=str(round_id or '').strip(),
        )

    def _projection_maps(self, task_id: str) -> tuple[dict[str, Any], dict[str, list[Any]], dict[str, list[str]]]:
        projection_nodes = list(self._store.list_task_nodes(task_id) or [])
        node_map = {
            str(item.node_id or '').strip(): item
            for item in projection_nodes
            if str(item.node_id or '').strip()
        }
        rounds = list(self._store.list_task_node_rounds(task_id) or [])
        rounds_by_parent: dict[str, list[Any]] = {}
        for item in rounds:
            rounds_by_parent.setdefault(str(item.parent_node_id or '').strip(), []).append(item)
        for parent_id, items in rounds_by_parent.items():
            rounds_by_parent[parent_id] = sorted(
                [item for item in items if str(getattr(item, 'round_id', '') or '').strip()],
                key=lambda item: (int(getattr(item, 'round_index', 0) or 0), str(getattr(item, 'round_id', '') or '')),
            )
        direct_children: dict[str, list[str]] = {}
        for item in projection_nodes:
            parent_id = str(getattr(item, 'parent_node_id', '') or '').strip()
            node_id = str(getattr(item, 'node_id', '') or '').strip()
            if not parent_id or not node_id:
                continue
            direct_children.setdefault(parent_id, []).append(node_id)
        for parent_id, child_ids in list(direct_children.items()):
            direct_children[parent_id] = sorted(
                [child_id for child_id in child_ids if child_id in node_map],
                key=lambda child_id: (
                    str(getattr(node_map[child_id], 'sort_key', '') or ''),
                    str(child_id or ''),
                ),
            )
        return node_map, rounds_by_parent, direct_children

    def _resolve_detail_text(self, text: str, ref: str) -> str:
        normalized_ref = str(ref or '').strip()
        if normalized_ref:
            resolver = getattr(self._log_service, 'resolve_content_ref', None)
            if callable(resolver):
                resolved = str(resolver(normalized_ref) or '')
                if resolved:
                    return resolved
        return str(text or '')

    @staticmethod
    def _projection_default_round_id(record: Any, rounds: list[Any]) -> str:
        explicit_default = str(getattr(record, 'default_round_id', '') or '').strip()
        if explicit_default and any(str(getattr(item, 'round_id', '') or '').strip() == explicit_default for item in rounds):
            return explicit_default
        if rounds:
            latest = next((item for item in rounds if bool(getattr(item, 'is_latest', False))), rounds[-1])
            return str(getattr(latest, 'round_id', '') or '').strip()
        return ''

    @staticmethod
    def _projection_round_child_ids(round_record: Any, *, node_map: dict[str, Any] | None = None) -> list[str]:
        child_ids = [
            str(child_id or '').strip()
            for child_id in list(getattr(round_record, 'child_node_ids', []) or [])
            if str(child_id or '').strip()
        ]
        if node_map is None:
            return child_ids
        return [child_id for child_id in child_ids if child_id in node_map]

    def _projection_auxiliary_child_ids(
        self,
        parent_node_id: str,
        *,
        node_map: dict[str, Any],
        rounds_by_parent: dict[str, list[Any]],
        direct_children: dict[str, list[str]],
    ) -> list[str]:
        normalized_parent_id = str(parent_node_id or '').strip()
        round_child_ids = {
            child_id
            for round_record in list(rounds_by_parent.get(normalized_parent_id, []) or [])
            for child_id in self._projection_round_child_ids(round_record, node_map=node_map)
        }
        return [
            child_id
            for child_id in list(direct_children.get(normalized_parent_id, []) or [])
            if child_id not in round_child_ids
        ]

    def _projection_selected_round(
        self,
        record: Any,
        rounds: list[Any],
        *,
        round_override: str = '',
    ) -> Any | None:
        if not rounds:
            return None
        requested_round_id = str(round_override or '').strip()
        selected_round_id = str(getattr(record, 'selected_round_id', '') or '').strip()
        default_round_id = self._projection_default_round_id(record, rounds)
        for candidate_round_id in [requested_round_id, selected_round_id, default_round_id]:
            if not candidate_round_id:
                continue
            selected = next(
                (item for item in rounds if str(getattr(item, 'round_id', '') or '').strip() == candidate_round_id),
                None,
            )
            if selected is not None:
                return selected
        return next((item for item in rounds if bool(getattr(item, 'is_latest', False))), None) or rounds[-1]

    def _projection_visible_child_ids(
        self,
        parent_node_id: str,
        *,
        node_map: dict[str, Any],
        rounds_by_parent: dict[str, list[Any]],
        direct_children: dict[str, list[str]],
        round_override: str = '',
    ) -> list[str]:
        normalized_parent_id = str(parent_node_id or '').strip()
        seen: set[str] = set()
        out: list[str] = []
        for child_id in self._projection_auxiliary_child_ids(
            normalized_parent_id,
            node_map=node_map,
            rounds_by_parent=rounds_by_parent,
            direct_children=direct_children,
        ):
            if child_id in seen:
                continue
            seen.add(child_id)
            out.append(child_id)
        record = node_map.get(normalized_parent_id)
        if record is None:
            return out
        selected_round = self._projection_selected_round(
            record,
            list(rounds_by_parent.get(normalized_parent_id, []) or []),
            round_override=round_override,
        )
        if selected_round is None:
            return out
        for child_id in self._projection_round_child_ids(selected_round, node_map=node_map):
            if child_id in seen:
                continue
            seen.add(child_id)
            out.append(child_id)
        return out

    def _snapshot_node_from_projection(
        self,
        record: Any,
        *,
        node_map: dict[str, Any],
        rounds_by_parent: dict[str, list[Any]],
        direct_children: dict[str, list[str]],
    ) -> TaskTreeSnapshotNode:
        node_id = str(getattr(record, 'node_id', '') or '').strip()
        parent_rounds = list(rounds_by_parent.get(node_id, []))
        auxiliary_child_ids = self._projection_auxiliary_child_ids(
            node_id,
            node_map=node_map,
            rounds_by_parent=rounds_by_parent,
            direct_children=direct_children,
        )
        snapshot_rounds = [
            TaskTreeSnapshotRound(
                round_id=str(getattr(round_record, 'round_id', '') or '').strip(),
                label=str(getattr(round_record, 'label', '') or '').strip(),
                is_latest=bool(getattr(round_record, 'is_latest', False)),
                total_children=int(getattr(round_record, 'total_children', 0) or 0),
                completed_children=int(getattr(round_record, 'completed_children', 0) or 0),
                running_children=int(getattr(round_record, 'running_children', 0) or 0),
                failed_children=int(getattr(round_record, 'failed_children', 0) or 0),
                child_ids=self._projection_round_child_ids(round_record),
            )
            for round_record in parent_rounds
        ]
        return TaskTreeSnapshotNode(
            node_id=node_id,
            parent_node_id=str(getattr(record, 'parent_node_id', '') or '').strip() or None,
            node_kind=str(getattr(record, 'node_kind', '') or 'execution').strip() or 'execution',
            status=str(getattr(record, 'status', '') or 'in_progress').strip() or 'in_progress',
            title=str(getattr(record, 'title', '') or node_id).strip() or node_id,
            updated_at=str(getattr(record, 'updated_at', '') or '').strip(),
            children_fingerprint=str(getattr(record, 'children_fingerprint', '') or '').strip(),
            default_round_id=self._projection_default_round_id(record, parent_rounds),
            rounds=snapshot_rounds,
            auxiliary_child_ids=auxiliary_child_ids,
        )

    def _build_tree_snapshot(
        self,
        *,
        task_id: str,
        root_node_id: str,
        scope_root_id: str = '',
        root_round_id: str = '',
    ) -> TaskTreeSnapshot:
        node_map, rounds_by_parent, direct_children = self._projection_maps(task_id)
        snapshot_nodes = {
            node_id: self._snapshot_node_from_projection(
                record,
                node_map=node_map,
                rounds_by_parent=rounds_by_parent,
                direct_children=direct_children,
            )
            for node_id, record in node_map.items()
        }
        included_ids: set[str]
        normalized_scope_root_id = str(scope_root_id or '').strip()
        if normalized_scope_root_id:
            included_ids = set()
            queue: list[tuple[str, str]] = [(normalized_scope_root_id, str(root_round_id or '').strip())]
            while queue:
                current_id, round_override = queue.pop(0)
                if current_id in included_ids:
                    continue
                if current_id not in node_map:
                    continue
                included_ids.add(current_id)
                for child_id in self._projection_visible_child_ids(
                    current_id,
                    node_map=node_map,
                    rounds_by_parent=rounds_by_parent,
                    direct_children=direct_children,
                    round_override=round_override,
                ):
                    if child_id not in included_ids:
                        queue.append((child_id, ''))
        else:
            included_ids = set(snapshot_nodes.keys())
        projection_meta = self._store.get_task_projection_meta(task_id)
        snapshot_version = str(getattr(projection_meta, 'version', '') or '').strip() or str(
            max(0, len(snapshot_nodes))
        )
        return TaskTreeSnapshot(
            task_id=task_id,
            root_node_id=normalized_scope_root_id or str(root_node_id or '').strip(),
            generated_at=datetime.now().astimezone().isoformat(timespec='seconds'),
            snapshot_version=snapshot_version,
            nodes_by_id={node_id: snapshot_nodes[node_id] for node_id in included_ids if node_id in snapshot_nodes},
        )

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

    def _recent_model_calls(self, task_id: str, *, limit: int = 50) -> list[TaskModelCallRecord]:
        records: list[TaskModelCallRecord] = []
        for event in list(self._store.list_task_model_calls(task_id, limit=max(1, int(limit or 50))) or []):
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
        return records[-max(1, int(limit or 1)) :]

    def _projection_token_usage_by_model(self, task_id: str) -> list[ModelTokenUsageRecord]:
        aggregates: dict[tuple[str, str, str], dict[str, Any]] = {}
        for record in list(self._store.list_task_node_details(task_id) or []):
            payload = dict(record.payload or {})
            for item in list(payload.get('token_usage_by_model') or []):
                if not isinstance(item, dict):
                    continue
                model_usage = ModelTokenUsageRecord.model_validate(item)
                key = (
                    str(model_usage.model_key or '').strip(),
                    str(model_usage.provider_id or '').strip(),
                    str(model_usage.provider_model or '').strip(),
                )
                bucket = aggregates.setdefault(
                    key,
                    {
                        'model_key': key[0],
                        'provider_id': key[1],
                        'provider_model': key[2],
                        'tracked': False,
                        'input_tokens': 0,
                        'output_tokens': 0,
                        'cache_hit_tokens': 0,
                        'call_count': 0,
                        'calls_with_usage': 0,
                        'calls_without_usage': 0,
                        'is_partial': False,
                    },
                )
                bucket['tracked'] = bool(bucket['tracked']) or bool(model_usage.tracked)
                bucket['input_tokens'] += int(model_usage.input_tokens or 0)
                bucket['output_tokens'] += int(model_usage.output_tokens or 0)
                bucket['cache_hit_tokens'] += int(model_usage.cache_hit_tokens or 0)
                bucket['call_count'] += int(model_usage.call_count or 0)
                bucket['calls_with_usage'] += int(model_usage.calls_with_usage or 0)
                bucket['calls_without_usage'] += int(model_usage.calls_without_usage or 0)
                bucket['is_partial'] = bool(bucket['is_partial']) or bool(model_usage.is_partial)
        items = [ModelTokenUsageRecord.model_validate(item) for item in aggregates.values()]
        items.sort(
            key=lambda item: (
                -(int(item.input_tokens or 0) + int(item.output_tokens or 0)),
                str(item.model_key or ''),
                str(item.provider_model or ''),
            )
        )
        return items

    def _projection_live_state(self, task_id: str) -> TaskLiveState | None:
        frames = self._store.list_task_runtime_frames(task_id)
        runtime_meta = self._log_service.read_task_runtime_meta(task_id) or {}
        dispatch_limits = self._sanitize_dispatch_counters(runtime_meta.get('dispatch_limits'))
        dispatch_running = self._sanitize_dispatch_counters(runtime_meta.get('dispatch_running'))
        dispatch_queued = self._sanitize_dispatch_counters(runtime_meta.get('dispatch_queued'))
        if not frames:
            if any(dispatch_limits.values()) or any(dispatch_running.values()) or any(dispatch_queued.values()):
                return TaskLiveState(
                    active_node_ids=[],
                    runnable_node_ids=[],
                    waiting_node_ids=[],
                    dispatch_limits=dispatch_limits,
                    dispatch_running=dispatch_running,
                    dispatch_queued=dispatch_queued,
                    frames=[],
                )
            return None
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
            dispatch_limits=dispatch_limits,
            dispatch_running=dispatch_running,
            dispatch_queued=dispatch_queued,
            frames=live_frames,
        )

    @staticmethod
    def _sanitize_dispatch_counters(payload: Any) -> dict[str, int]:
        counters = dict(payload or {}) if isinstance(payload, dict) else {}
        return {
            'execution': max(0, int(counters.get('execution') or 0)),
            'inspection': max(0, int(counters.get('inspection') or 0)),
        }

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
        task_id: str,
        *,
        root_node_id: str,
        node_map: dict[str, Any],
        rounds_by_parent: dict[str, list[Any]],
        direct_children: dict[str, list[str]],
        live_state: TaskLiveState | None = None,
    ) -> str:
        normalized_root_node_id = str(root_node_id or '').strip()
        if not normalized_root_node_id or normalized_root_node_id not in node_map:
            return '(empty tree)'
        lines: list[str] = []
        stage_goals = self._node_stage_goal_map(task_id, live_state=live_state)

        def _walk(node_id: str, prefix: str = '', *, is_root: bool = False, seen: set[str]) -> None:
            normalized_node_id = str(node_id or '').strip()
            if not normalized_node_id or normalized_node_id in seen:
                return
            record = node_map.get(normalized_node_id)
            if record is None:
                return
            seen.add(normalized_node_id)
            label = self._tree_text_label(record, stage_goals)
            lines.append(label if is_root else f'{prefix}|-{label}')
            child_prefix = '' if is_root else f'{prefix}  '
            for child_id in self._projection_visible_child_ids(
                str(getattr(record, 'node_id', '') or '').strip(),
                node_map=node_map,
                rounds_by_parent=rounds_by_parent,
                direct_children=direct_children,
            ):
                _walk(child_id, child_prefix, is_root=False, seen=seen)

        _walk(normalized_root_node_id, is_root=True, seen=set())
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
    def _tree_display_stage_goal(node: Any, stage_goals: dict[str, str]) -> str:
        stage_goal = str(stage_goals.get(str(node.node_id or '').strip()) or '').strip()
        if stage_goal:
            return stage_goal
        if str(getattr(node, 'node_kind', 'execution') or 'execution').strip().lower() == 'acceptance':
            return '检验中'
        return '无阶段目标'

    @classmethod
    def _tree_text_label(cls, node: Any, stage_goals: dict[str, str]) -> str:
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
