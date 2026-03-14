from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

from main.models import NodeOutputEntry, NodeRecord, TaskRecord
from main.monitoring.file_store import TaskFileStore
from main.protocol import build_envelope, now_iso
from main.monitoring.tree_builder import TaskTreeBuilder


def _single_line_text(value: Any, *, max_chars: int = 120) -> str:
    text = ' '.join(str(value or '').split())
    if len(text) <= max_chars:
        return text
    return f'{text[: max_chars - 3].rstrip()}...'


class TaskLogService:
    def __init__(self, *, store, file_store: TaskFileStore, tree_builder: TaskTreeBuilder, registry=None):
        self._store = store
        self._file_store = file_store
        self._tree_builder = tree_builder
        self._registry = registry

    def initialize_task(self, task: TaskRecord, root: NodeRecord) -> tuple[TaskRecord, NodeRecord]:
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
            frames=[
                {
                    'node_id': root.node_id,
                    'depth': root.depth,
                    'node_kind': root.node_kind,
                    'phase': 'before_model',
                    'messages': [],
                    'pending_tool_calls': [],
                    'pending_child_specs': [],
                    'partial_child_results': [],
                    'last_error': '',
                }
            ],
        )
        self.refresh_task_view(task.task_id, mark_unread=True)
        return task, root

    def create_node(self, task_id: str, node: NodeRecord) -> NodeRecord:
        self._store.upsert_node(node)
        self.refresh_task_view(task_id, mark_unread=True)
        return node

    def update_node_input(self, task_id: str, node_id: str, content: str) -> NodeRecord | None:
        updated = self._store.update_node(
            node_id,
            lambda record: record.model_copy(update={'input': str(content or ''), 'updated_at': now_iso()}),
        )
        self.refresh_task_view(task_id, mark_unread=True)
        return updated

    def append_node_output(
        self,
        task_id: str,
        node_id: str,
        *,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> NodeRecord | None:
        def _mutate(record: NodeRecord) -> NodeRecord:
            output = list(record.output)
            output.append(
                NodeOutputEntry(
                    seq=len(output) + 1,
                    content=str(content or ''),
                    tool_calls=list(tool_calls or []),
                    created_at=now_iso(),
                )
            )
            return record.model_copy(update={'output': output, 'updated_at': now_iso()})

        updated = self._store.update_node(node_id, _mutate)
        self.refresh_task_view(task_id, mark_unread=True)
        return updated

    def update_node_check_result(self, task_id: str, node_id: str, check_result: str) -> NodeRecord | None:
        updated = self._store.update_node(
            node_id,
            lambda record: record.model_copy(update={'check_result': str(check_result or ''), 'updated_at': now_iso()}),
        )
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
        updated = self._store.update_node(
            node_id,
            lambda record: record.model_copy(
                update={
                    'status': str(status or record.status),
                    'final_output': str(final_output or record.final_output),
                    'failure_reason': str(failure_reason or record.failure_reason),
                    'finished_at': now_iso() if status in {'success', 'failed'} else record.finished_at,
                    'updated_at': now_iso(),
                }
            ),
        )
        self.refresh_task_view(task_id, mark_unread=True)
        return updated

    def mark_task_read(self, task_id: str) -> TaskRecord | None:
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

    def set_pause_state(self, task_id: str, *, pause_requested: bool | None = None, is_paused: bool | None = None) -> TaskRecord | None:
        return self.update_task_control(task_id, pause_requested=pause_requested, is_paused=is_paused)

    def update_node_metadata(self, node_id: str, metadata_mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> NodeRecord | None:
        def _mutate(record: NodeRecord) -> NodeRecord:
            metadata = metadata_mutator(dict(record.metadata or {}))
            if not isinstance(metadata, dict):
                raise TypeError('node metadata mutator must return a dict')
            return record.model_copy(update={'metadata': metadata, 'updated_at': now_iso()})

        return self._store.update_node(node_id, _mutate)

    def mark_task_failed(self, task_id: str, *, reason: str) -> TaskRecord | None:
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
        return updated

    def update_runtime_state(self, task_id: str, **payload: Any) -> dict[str, Any]:
        task = self._require_task(task_id)
        current = self._file_store.read_json(task.runtime_state_path) or {
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
        self._file_store.write_json(task.runtime_state_path, current)
        return current

    def read_runtime_state(self, task_id: str) -> dict[str, Any] | None:
        task = self._store.get_task(task_id)
        if task is None or not task.runtime_state_path:
            return None
        return self._file_store.read_json(task.runtime_state_path)

    def upsert_frame(self, task_id: str, frame: dict[str, Any]) -> dict[str, Any]:
        state = self.read_runtime_state(task_id) or {}
        frames = [item for item in list(state.get('frames') or []) if str(item.get('node_id') or '') != str(frame.get('node_id') or '')]
        frames.append(copy.deepcopy(frame))
        active_ids = sorted({str(item.get('node_id') or '') for item in frames if str(item.get('node_id') or '').strip()})
        runnable_ids = sorted({str(item.get('node_id') or '') for item in frames if str(item.get('phase') or '') in {'before_model', 'waiting_tool_results', 'after_model'}})
        waiting_ids = sorted({str(item.get('node_id') or '') for item in frames if str(item.get('phase') or '') in {'waiting_children', 'waiting_acceptance'}})
        return self.update_runtime_state(task_id, frames=frames, active_node_ids=active_ids, runnable_node_ids=runnable_ids, waiting_node_ids=waiting_ids)

    def remove_frame(self, task_id: str, node_id: str) -> dict[str, Any]:
        state = self.read_runtime_state(task_id) or {}
        frames = [item for item in list(state.get('frames') or []) if str(item.get('node_id') or '') != str(node_id or '')]
        active_ids = sorted({str(item.get('node_id') or '') for item in frames if str(item.get('node_id') or '').strip()})
        runnable_ids = sorted({str(item.get('node_id') or '') for item in frames if str(item.get('phase') or '') in {'before_model', 'waiting_tool_results', 'after_model'}})
        waiting_ids = sorted({str(item.get('node_id') or '') for item in frames if str(item.get('phase') or '') in {'waiting_children', 'waiting_acceptance'}})
        return self.update_runtime_state(task_id, frames=frames, active_node_ids=active_ids, runnable_node_ids=runnable_ids, waiting_node_ids=waiting_ids)

    def refresh_task_view(self, task_id: str, *, mark_unread: bool) -> TaskRecord | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        nodes = self._store.list_nodes(task_id)
        root = self._tree_builder.build_tree(task, nodes)
        tree_text = self._tree_builder.render_tree_text(root)
        root_node = self._store.get_node(task.root_node_id)
        next_status = root_node.status if root_node is not None else task.status
        brief_text = self._brief_text(task=task, root_node=root_node)
        updated = task.model_copy(
            update={
                'status': next_status,
                'brief_text': brief_text,
                'is_unread': True if mark_unread else task.is_unread,
                'updated_at': now_iso(),
                'final_output': (root_node.final_output if root_node and next_status == 'success' else task.final_output),
                'failure_reason': (root_node.failure_reason if root_node and next_status == 'failed' else task.failure_reason),
                'finished_at': now_iso() if next_status in {'success', 'failed'} and not task.finished_at else task.finished_at,
            }
        )
        self._store.upsert_task(updated)
        self._file_store.write_json(updated.tree_snapshot_path, {'task_id': updated.task_id, 'root': root.model_dump(mode='json') if root is not None else None})
        self._file_store.write_text(updated.tree_text_path, tree_text)
        runtime_state = self.read_runtime_state(task_id)
        if runtime_state is not None:
            runtime_state['updated_at'] = now_iso()
            runtime_state['paused'] = bool(updated.is_paused)
            runtime_state['pause_requested'] = bool(updated.pause_requested)
            runtime_state['cancel_requested'] = bool(updated.cancel_requested)
            self._file_store.write_json(updated.runtime_state_path, runtime_state)
        if self._registry is not None:
            payload = {
                'task': updated.model_dump(mode='json'),
                'root': root.model_dump(mode='json') if root is not None else None,
                'tree_text': tree_text,
                'nodes': [item.model_dump(mode='json') for item in nodes],
            }
            self._registry.publish_task(
                updated.session_id,
                updated.task_id,
                build_envelope(
                    channel='task',
                    session_id=updated.session_id,
                    task_id=updated.task_id,
                    seq=self._registry.next_task_seq(updated.session_id, updated.task_id),
                    type='snapshot.task',
                    data=payload,
                ),
            )
            self._registry.publish_ceo(
                updated.session_id,
                build_envelope(
                    channel='ceo',
                    session_id=updated.session_id,
                    task_id=updated.task_id,
                    seq=self._registry.next_ceo_seq(updated.session_id),
                    type='task.summary.changed',
                    data={'task_id': updated.task_id, 'status': updated.status, 'brief': updated.brief_text, 'is_unread': bool(updated.is_unread)},
                ),
            )
        return updated

    def bootstrap_missing_files(self, task_id: str) -> TaskRecord | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        self.refresh_task_view(task_id, mark_unread=False)
        if not Path(task.runtime_state_path).exists():
            self.update_runtime_state(task.task_id, paused=bool(task.is_paused), active_node_ids=[], runnable_node_ids=[], waiting_node_ids=[], frames=[])
        return self._store.get_task(task_id)

    def _brief_text(self, *, task: TaskRecord, root_node: NodeRecord | None) -> str:
        if root_node is not None:
            if str(root_node.check_result or '').strip():
                return _single_line_text(root_node.check_result)
            if root_node.output:
                last = str(root_node.output[-1].content or '').strip()
                if last:
                    return _single_line_text(last)
        if str(task.final_output or '').strip():
            return _single_line_text(task.final_output)
        if str(task.failure_reason or '').strip():
            return _single_line_text(task.failure_reason)
        return _single_line_text(task.user_request)

    def _require_task(self, task_id: str) -> TaskRecord:
        task = self._store.get_task(task_id)
        if task is None:
            raise ValueError(f'task not found: {task_id}')
        return task
