from __future__ import annotations

from main.monitoring.models import TaskListItem, TaskProgressResult, TaskSummaryResult


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
        text = f'Task status: {task.status}'
        if tree_text:
            text = f'{text}\n{tree_text}'
        return TaskProgressResult(
            task_id=task.task_id,
            task_status=task.status,
            tree_text=str(tree_text or '(empty tree)'),
            root=snapshot.get('root') if isinstance(snapshot.get('root'), dict) else None,
            nodes=[item.model_dump(mode='json') for item in self._store.list_nodes(task_id)],
            text=text,
        )
