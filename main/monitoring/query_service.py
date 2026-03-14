from __future__ import annotations

from main.models import NodeRecord
from main.monitoring.models import LatestTaskNodeOutput, TaskListItem, TaskProgressResult, TaskSummaryResult


class TaskQueryService:
    def __init__(self, *, store, file_store, log_service):
        self._store = store
        self._file_store = file_store
        self._log_service = log_service

    def summary(self, session_id: str) -> TaskSummaryResult:
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

    def get_tasks(self, session_id: str, task_type: int) -> list[TaskListItem]:
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
                title=item.title or item.task_id,
                brief=item.brief_text or '',
                status=item.status,
                is_unread=bool(item.is_unread),
                is_paused=bool(item.is_paused),
                created_at=item.created_at,
                updated_at=item.updated_at,
                max_depth=int(item.max_depth or 0),
            )
            for item in tasks
        ]

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> TaskProgressResult | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        tree_text = self._file_store.read_text(task.tree_text_path)
        snapshot = self._file_store.read_json(task.tree_snapshot_path)
        if not tree_text or not isinstance(snapshot, dict):
            self._log_service.bootstrap_missing_files(task_id)
            task = self._store.get_task(task_id) or task
            tree_text = self._file_store.read_text(task.tree_text_path) or '(empty tree)'
            snapshot = self._file_store.read_json(task.tree_snapshot_path) or {}
        if mark_read:
            self._log_service.mark_task_read(task_id)
            task = self._store.get_task(task_id) or task
        nodes = self._store.list_nodes(task_id)
        latest_node = self._latest_node(nodes)
        text = f'Task status: {task.status}'
        if tree_text:
            text = f'{text}\n{tree_text}'
        if latest_node is not None:
            latest_output = latest_node.output.strip() or '(empty)'
            text = f'{text}\nLatest node output [{latest_node.node_id}]:\n{latest_output}'
        return TaskProgressResult(
            task_id=task.task_id,
            task_status=task.status,
            tree_text=str(tree_text or '(empty tree)'),
            root=snapshot.get('root') if isinstance(snapshot.get('root'), dict) else None,
            latest_node=latest_node,
            nodes=[item.model_dump(mode='json') for item in nodes],
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
