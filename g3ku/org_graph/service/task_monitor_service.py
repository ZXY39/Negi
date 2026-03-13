from __future__ import annotations

from typing import Any

from g3ku.org_graph.monitoring.models import MonitorTreeNode, TaskMonitorNodeRecord, TaskMonitorProjectRecord
from g3ku.org_graph.protocol import now_iso
from g3ku.org_graph.tracing.monitor_tree_builder import MonitorTreeBuilder


class TaskMonitorService:
    def __init__(self, service, store):
        self._service = service
        self._store = store
        self._tree_builder = MonitorTreeBuilder()

    @staticmethod
    def _node_state(unit_status: str) -> str:
        value = str(unit_status or '').strip().lower()
        if value == 'completed':
            return 'success'
        if value in {'failed', 'canceled'}:
            return 'failed'
        if value == 'blocked':
            return 'waiting'
        return 'in_progress'

    @staticmethod
    def _task_state(project_status: str, root_node_state: str) -> str:
        value = str(project_status or '').strip().lower()
        if value == 'completed' or root_node_state == 'success':
            return 'success'
        if value in {'failed', 'canceled'} or root_node_state == 'failed':
            return 'failed'
        if value == 'blocked' or root_node_state == 'waiting':
            return 'paused'
        return 'in_progress'

    def ensure_project(self, project) -> TaskMonitorProjectRecord:
        record = self._store.get_project(project.project_id)
        if record is None:
            record = TaskMonitorProjectRecord(
                project_id=project.project_id,
                session_id=project.session_id,
                root_node_id=project.root_unit_id,
                task_state=self._task_state(project.status, 'in_progress'),
                updated_at=project.updated_at or now_iso(),
            )
        else:
            record = record.model_copy(
                update={
                    'session_id': project.session_id,
                    'root_node_id': project.root_unit_id,
                    'task_state': self._task_state(project.status, self._root_node_state(project.project_id)),
                    'updated_at': project.updated_at or now_iso(),
                }
            )
        return self._store.upsert_project(record)

    def ensure_node(self, *, project, unit) -> TaskMonitorNodeRecord:
        state = self._node_state(unit.status)
        wait_reason = self._derive_wait_reason(unit=unit)
        record = self._store.get_node(unit.unit_id)
        if record is None:
            record = TaskMonitorNodeRecord(
                node_id=unit.unit_id,
                project_id=project.project_id,
                session_id=project.session_id,
                parent_node_id=unit.parent_unit_id,
                root_node_id=unit.root_unit_id,
                created_at=unit.created_at or now_iso(),
                state=state,
                wait_reason=wait_reason,
                input='',
                output='',
                log=[self._log_entry(kind='lifecycle', content='node.created', stage_id=unit.current_stage_id, meta={'state': state})],
                check='',
                latest_progress_text=unit.current_action or unit.result_summary or project.summary,
                latest_engineering_exception=unit.error_summary or '',
                updated_at=unit.updated_at or now_iso(),
            )
        else:
            new_log = list(record.log)
            if record.state != state or record.wait_reason != wait_reason:
                new_log.append(self._log_entry(kind='state', content=state, stage_id=unit.current_stage_id, meta={'wait_reason': wait_reason}))
            record = record.model_copy(
                update={
                    'project_id': project.project_id,
                    'session_id': project.session_id,
                    'parent_node_id': unit.parent_unit_id,
                    'root_node_id': unit.root_unit_id,
                    'state': state,
                    'wait_reason': wait_reason,
                    'latest_progress_text': unit.current_action or unit.result_summary or record.latest_progress_text,
                    'latest_engineering_exception': unit.error_summary or record.latest_engineering_exception,
                    'updated_at': unit.updated_at or now_iso(),
                    'log': new_log,
                }
            )
        self._store.upsert_node(record)
        self.recompute_project(project.project_id)
        return record

    def _derive_wait_reason(self, *, unit) -> str | None:
        if str(unit.status or '').strip().lower() != 'blocked':
            return None
        failure_kind = str((unit.metadata or {}).get('failure_kind') or '').strip().lower()
        if failure_kind == 'model_chain_unavailable':
            return 'model_chain_unavailable'
        if unit.error_summary:
            return 'engineering_exception'
        return 'manual_pause'

    @staticmethod
    def _log_entry(*, kind: str, content: str, stage_id: str | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            'ts': now_iso(),
            'kind': str(kind or ''),
            'content': str(content or ''),
            'stage_id': stage_id,
            'meta': dict(meta or {}),
        }

    def append_log(self, *, project_id: str, node_id: str, kind: str, content: str, stage_id: str | None = None, meta: dict[str, Any] | None = None) -> TaskMonitorNodeRecord | None:
        record = self._store.get_node(node_id)
        if record is None:
            return None
        log = list(record.log)
        log.append(self._log_entry(kind=kind, content=content, stage_id=stage_id, meta=meta))
        updated = record.model_copy(update={'log': log, 'updated_at': now_iso()})
        self._store.upsert_node(updated)
        self.recompute_project(project_id)
        return updated

    def record_input(self, *, project, unit, content: str, stage_id: str | None = None, kind: str = 'input', meta: dict[str, Any] | None = None) -> None:
        record = self.ensure_node(project=project, unit=unit)
        updated = record.model_copy(update={'input': str(content or ''), 'updated_at': now_iso()})
        self._store.upsert_node(updated)
        self.append_log(project_id=project.project_id, node_id=unit.unit_id, kind=kind, content=str(content or ''), stage_id=stage_id, meta=meta)

    def record_output(self, *, project, unit, content: str, stage_id: str | None = None, kind: str = 'output', meta: dict[str, Any] | None = None) -> None:
        record = self.ensure_node(project=project, unit=unit)
        updated = record.model_copy(update={'output': str(content or ''), 'updated_at': now_iso()})
        self._store.upsert_node(updated)
        self.append_log(project_id=project.project_id, node_id=unit.unit_id, kind=kind, content=str(content or ''), stage_id=stage_id, meta=meta)

    def record_check_input(self, *, project, unit, content: str, stage_id: str | None = None, meta: dict[str, Any] | None = None) -> None:
        self.ensure_node(project=project, unit=unit)
        self.append_log(project_id=project.project_id, node_id=unit.unit_id, kind='check_input', content=str(content or ''), stage_id=stage_id, meta=meta)

    def record_check_output(self, *, project, unit, content: str, stage_id: str | None = None, meta: dict[str, Any] | None = None) -> None:
        record = self.ensure_node(project=project, unit=unit)
        updated = record.model_copy(update={'check': str(content or ''), 'updated_at': now_iso()})
        self._store.upsert_node(updated)
        self.append_log(project_id=project.project_id, node_id=unit.unit_id, kind='check_output', content=str(content or ''), stage_id=stage_id, meta=meta)

    def record_progress(self, *, project, text: str, node_id: str | None = None) -> None:
        record = self.ensure_project(project)
        latest = int(record.latest_progress_rev or 0) + 1
        updated = record.model_copy(
            update={
                'latest_progress_rev': latest,
                'latest_progress_text': str(text or '').strip() or record.latest_progress_text,
                'updated_at': now_iso(),
            }
        )
        self._store.upsert_project(updated)
        if node_id:
            node = self._store.get_node(node_id)
            if node is not None:
                self._store.upsert_node(node.model_copy(update={'latest_progress_text': updated.latest_progress_text, 'updated_at': now_iso()}))

    def record_engineering_exception(self, *, project, text: str, node_id: str | None = None, wait_reason: str = 'engineering_exception') -> None:
        record = self.ensure_project(project)
        latest = int(record.latest_engineering_rev or 0) + 1
        updated = record.model_copy(
            update={
                'latest_engineering_rev': latest,
                'latest_engineering_text': str(text or '').strip() or record.latest_engineering_text,
                'has_active_engineering_exception': wait_reason == 'engineering_exception',
                'task_state': 'paused',
                'updated_at': now_iso(),
            }
        )
        self._store.upsert_project(updated)
        if node_id:
            node = self._store.get_node(node_id)
            if node is not None:
                new_log = list(node.log)
                new_log.append(self._log_entry(kind='state', content='waiting', meta={'wait_reason': wait_reason, 'reason': text}))
                self._store.upsert_node(
                    node.model_copy(
                        update={
                            'state': 'waiting',
                            'wait_reason': wait_reason,
                            'latest_engineering_exception': str(text or '').strip(),
                            'updated_at': now_iso(),
                            'log': new_log,
                        }
                    )
                )

    def clear_engineering_exception(self, project_id: str) -> None:
        record = self._store.get_project(project_id)
        if record is None:
            return
        self._store.upsert_project(record.model_copy(update={'has_active_engineering_exception': False, 'updated_at': now_iso()}))
        for node in self._store.list_nodes(project_id):
            if node.wait_reason == 'engineering_exception':
                self._store.upsert_node(node.model_copy(update={'wait_reason': None, 'updated_at': now_iso()}))

    def mark_progress_read(self, project_id: str) -> None:
        record = self._store.get_project(project_id)
        if record is None:
            return
        self._store.upsert_project(record.model_copy(update={'read_progress_rev': record.latest_progress_rev, 'updated_at': now_iso()}))

    def mark_engineering_read(self, project_id: str) -> None:
        record = self._store.get_project(project_id)
        if record is None:
            return
        self._store.upsert_project(record.model_copy(update={'read_engineering_rev': record.latest_engineering_rev, 'updated_at': now_iso()}))

    def delete_nodes(self, *, project_id: str, node_ids: list[str]) -> None:
        values = [str(item or '').strip() for item in node_ids if str(item or '').strip()]
        if not values:
            return
        self._store.delete_nodes(values)
        self.recompute_project(project_id)

    def recompute_project(self, project_id: str) -> TaskMonitorProjectRecord | None:
        project = self._service.get_project(project_id)
        if project is None:
            return None
        nodes = self._store.list_nodes(project_id)
        root_state = self._root_node_state(project.project_id, nodes=nodes)
        record = self.ensure_project(project)
        updated = record.model_copy(update={'task_state': self._task_state(project.status, root_state), 'updated_at': now_iso()})
        return self._store.upsert_project(updated)

    def sync_from_units(self, project_id: str) -> None:
        project = self._service.get_project(project_id)
        if project is None:
            return
        self.ensure_project(project)
        for unit in self._service.list_units(project_id):
            self.ensure_node(project=project, unit=unit)
        self.recompute_project(project_id)

    def startup_backfill(self) -> None:
        for project in self._service.list_projects():
            self.sync_from_units(project.project_id)

    def get_tree(self, project_id: str) -> MonitorTreeNode | None:
        project = self._service.get_project(project_id)
        if project is None:
            return None
        nodes = self._store.list_nodes(project_id)
        root = self._tree_builder.build(nodes=nodes, root_node_id=project.root_unit_id)
        if root is None:
            return None
        return self._augment_tree(root)

    def _augment_tree(self, node: MonitorTreeNode) -> MonitorTreeNode:
        self._decorate_unit_node(node)
        actual_children = [self._augment_tree(child) for child in node.children]
        checkpoint = self._checkpoint_for_unit(node.node_id)
        stages = checkpoint.get('stages') if isinstance(checkpoint, dict) else None
        if not isinstance(stages, list) or not stages:
            node.children = actual_children
            return node
        actual_children_by_id = {child.node_id: child for child in actual_children}
        used_child_ids: set[str] = set()
        synthetic_updated_at = str(checkpoint.get('updated_at') or node.updated_at or now_iso())
        stage_nodes = [
            self._build_stage_node(
                parent_node=node,
                stage_state=stage_state,
                actual_children_by_id=actual_children_by_id,
                used_child_ids=used_child_ids,
                updated_at=synthetic_updated_at,
            )
            for stage_state in stages
            if isinstance(stage_state, dict)
        ]
        unmatched_children = [child for child in actual_children if child.node_id not in used_child_ids]
        node.children = stage_nodes + unmatched_children
        return node

    def _decorate_unit_node(self, node: MonitorTreeNode) -> None:
        unit = getattr(self._service, 'get_unit', lambda _node_id: None)(node.node_id)
        node.kind = 'unit'
        node.title = str(getattr(unit, 'role_title', '') or node.title or node.node_id)
        node.display_state = str(getattr(unit, 'status', '') or node.display_state or node.state or '').upper()
        if unit is None:
            return
        if not node.input:
            node.input = str(unit.objective_summary or unit.prompt_preview or '')
        if not node.output:
            node.output = str(unit.result_summary or unit.error_summary or '')
        if not node.check and unit.current_action:
            node.check = str(unit.current_action or '')

    def _checkpoint_for_unit(self, unit_id: str) -> dict[str, Any]:
        checkpoint_store = getattr(self._service, 'checkpoint_store', None)
        if checkpoint_store is None:
            return {}
        payload = checkpoint_store.get(unit_id)
        return payload if isinstance(payload, dict) else {}

    def _build_stage_node(
        self,
        *,
        parent_node: MonitorTreeNode,
        stage_state: dict[str, Any],
        actual_children_by_id: dict[str, MonitorTreeNode],
        used_child_ids: set[str],
        updated_at: str,
    ) -> MonitorTreeNode:
        stage_index = int(stage_state.get('index') or 0)
        stage_id = str(stage_state.get('stage_id') or f'stage-{stage_index or 0}')
        stage_status = str(stage_state.get('status') or 'pending').strip().lower()
        title = str(stage_state.get('title') or f'阶段 {stage_index or ""}').strip()
        stage_node_id = f'{parent_node.node_id}::stage:{stage_id}'
        work_nodes = [
            self._build_work_item_node(
                parent_node_id=stage_node_id,
                work_item=work_item,
                actual_children_by_id=actual_children_by_id,
                used_child_ids=used_child_ids,
                updated_at=updated_at,
            )
            for work_item in list(stage_state.get('work_items') or [])
            if isinstance(work_item, dict)
        ]
        validation_profiles = list(stage_state.get('validation_profiles') or [])
        checker = stage_state.get('checker') if isinstance(stage_state.get('checker'), dict) else {}
        return MonitorTreeNode(
            node_id=stage_node_id,
            parent_node_id=parent_node.node_id,
            state=self._status_to_monitor_state(stage_status, error_summary=stage_state.get('error_summary')),
            display_state=stage_status.upper(),
            kind='stage',
            title=f'阶段 {stage_index} · {title}' if stage_index else title,
            input=str(stage_state.get('objective_summary') or ''),
            output=str(stage_state.get('result_summary') or stage_state.get('error_summary') or ''),
            check=self._format_stage_check(checker=checker, validation_profiles=validation_profiles),
            log=self._build_stage_logs(stage_state=stage_state, validation_profiles=validation_profiles),
            children=work_nodes,
            updated_at=updated_at,
        )

    def _build_work_item_node(
        self,
        *,
        parent_node_id: str,
        work_item: dict[str, Any],
        actual_children_by_id: dict[str, MonitorTreeNode],
        used_child_ids: set[str],
        updated_at: str,
    ) -> MonitorTreeNode:
        index = int(work_item.get('index') or 0)
        work_status = str(work_item.get('status') or 'pending').strip().lower()
        child_unit_id = str(work_item.get('child_unit_id') or '').strip()
        child_nodes: list[MonitorTreeNode] = []
        if child_unit_id and child_unit_id in actual_children_by_id:
            child_nodes.append(actual_children_by_id[child_unit_id])
            used_child_ids.add(child_unit_id)
        return MonitorTreeNode(
            node_id=f'{parent_node_id}::work:{index or 0}',
            parent_node_id=parent_node_id,
            state=self._status_to_monitor_state(
                work_status,
                error_summary=work_item.get('error_summary'),
                failure_kind=work_item.get('failure_kind'),
            ),
            display_state=work_status.upper(),
            kind='work_item',
            title=f'任务 {index} · {str(work_item.get("role_title") or "未命名任务").strip()}',
            input=str(work_item.get('objective_summary') or work_item.get('prompt_preview') or ''),
            output=str(work_item.get('result_summary') or work_item.get('error_summary') or ''),
            check=self._format_work_item_check(work_item),
            log=self._build_work_item_logs(work_item),
            children=child_nodes,
            updated_at=updated_at,
        )

    @staticmethod
    def _status_to_monitor_state(status: str, *, error_summary: Any = '', failure_kind: Any = None) -> str:
        value = str(status or '').strip().lower()
        if value in {'completed', 'success', 'passed'}:
            return 'success'
        if value in {'failed', 'canceled', 'error'} or str(error_summary or '').strip() or str(failure_kind or '').strip():
            return 'failed'
        if value in {'pending', 'queued', 'blocked', 'paused', 'waiting'}:
            return 'waiting'
        return 'in_progress'

    @staticmethod
    def _format_stage_check(*, checker: dict[str, Any], validation_profiles: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        checker_status = str(checker.get('status') or '').strip()
        if checker_status:
            parts.append(f'Checker: {checker_status}')
        summary = str(checker.get('summary') or '').strip()
        if summary:
            parts.append(summary)
        rework_instructions = str(checker.get('rework_instructions') or '').strip()
        if rework_instructions:
            parts.append(rework_instructions)
        criteria = [str(item.get('acceptance_criteria') or '').strip() for item in validation_profiles if isinstance(item, dict)]
        criteria = [item for item in criteria if item]
        if criteria:
            parts.append('\n'.join(criteria))
        return '\n\n'.join(parts)

    @staticmethod
    def _format_work_item_check(work_item: dict[str, Any]) -> str:
        parts: list[str] = []
        acceptance_criteria = str(work_item.get('acceptance_criteria') or '').strip()
        if acceptance_criteria:
            parts.append(acceptance_criteria)
        check_status = str(work_item.get('check_status') or '').strip()
        if check_status:
            parts.append(f'Check: {check_status}')
        check_reason = str(work_item.get('check_reason') or '').strip()
        if check_reason:
            parts.append(check_reason)
        return '\n\n'.join(parts)

    def _build_stage_logs(self, *, stage_state: dict[str, Any], validation_profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        logs: list[dict[str, Any]] = []
        dispatch_shape = str(stage_state.get('dispatch_shape') or '').strip()
        if dispatch_shape:
            logs.append(self._log_entry(kind='dispatch', content=f'dispatch_shape={dispatch_shape}', meta={'synthetic': True}))
        planned_work_count = int(stage_state.get('planned_work_count') or 0)
        if planned_work_count:
            logs.append(self._log_entry(kind='plan', content=f'planned_work_count={planned_work_count}', meta={'synthetic': True}))
        for profile in validation_profiles:
            if not isinstance(profile, dict):
                continue
            criteria = str(profile.get('acceptance_criteria') or '').strip()
            if criteria:
                logs.append(self._log_entry(kind='validation', content=criteria, meta={'synthetic': True, 'profile_id': profile.get('profile_id')}))
        return logs

    def _build_work_item_logs(self, work_item: dict[str, Any]) -> list[dict[str, Any]]:
        logs: list[dict[str, Any]] = []
        mode = str(work_item.get('mode') or '').strip()
        if mode:
            logs.append(self._log_entry(kind='mode', content=mode, meta={'synthetic': True}))
        prompt_preview = str(work_item.get('prompt_preview') or '').strip()
        if prompt_preview:
            logs.append(self._log_entry(kind='prompt', content=prompt_preview, meta={'synthetic': True}))
        validation_tools = [str(item or '').strip() for item in list(work_item.get('validation_tools') or []) if str(item or '').strip()]
        if validation_tools:
            logs.append(self._log_entry(kind='validation_tools', content=', '.join(validation_tools), meta={'synthetic': True}))
        child_unit_id = str(work_item.get('child_unit_id') or '').strip()
        if child_unit_id:
            logs.append(self._log_entry(kind='delegate', content=child_unit_id, meta={'synthetic': True}))
        return logs

    def get_tree_text(self, project_id: str) -> str:
        return self._tree_builder.to_text(root=self.get_tree(project_id))

    def summary(self, session_id: str) -> dict[str, Any]:
        projects = self._store.list_projects(session_id)
        unread_progress = sum(1 for item in projects if int(item.latest_progress_rev or 0) > int(item.read_progress_rev or 0))
        unread_engineering = sum(1 for item in projects if int(item.latest_engineering_rev or 0) > int(item.read_engineering_rev or 0))
        total = len([item for item in projects if self._service.get_project(item.project_id) is not None and self._service.get_project(item.project_id).status != 'archived'])
        in_progress = sum(1 for item in projects if item.task_state == 'in_progress')
        failed = sum(1 for item in projects if item.task_state == 'failed')
        text = f'未读的任务进展：{unread_progress}个，未读的工程异常：{unread_engineering}个，总任务：{total}个，进行中任务：{in_progress}个，失败任务：{failed}个。'
        return {
            'text': text,
            'unread_task_progress': unread_progress,
            'unread_engineering_exceptions': unread_engineering,
            'total_tasks': total,
            'in_progress_tasks': in_progress,
            'failed_tasks': failed,
        }

    def list_tasks(self, session_id: str, scope: int, *, mark_read: bool = True) -> dict[str, Any]:
        items = self._store.list_projects(session_id)
        if int(scope) == 2:
            items = [item for item in items if item.task_state == 'in_progress']
        elif int(scope) == 3:
            items = [item for item in items if item.task_state == 'failed']
        elif int(scope) == 4:
            items = [item for item in items if int(item.latest_progress_rev or 0) > int(item.read_progress_rev or 0)]
        tasks: list[dict[str, Any]] = []
        for item in items:
            project = self._service.get_project(item.project_id)
            if project is None or project.status == 'archived':
                continue
            brief = item.latest_engineering_text or item.latest_progress_text or project.summary or project.title
            tasks.append(
                {
                    'task_id': item.project_id,
                    'brief': brief,
                    'task_state': item.task_state,
                    'unread_progress': int(item.latest_progress_rev or 0) > int(item.read_progress_rev or 0),
                }
            )
        if int(scope) == 4 and mark_read:
            for item in items:
                self.mark_progress_read(item.project_id)
        return {'scope': int(scope), 'tasks': tasks, 'text': '\n'.join(f"- {item['task_id']}: {item['brief']}" for item in tasks) or 'No matching tasks.'}

    def progress(self, task_id: str, *, mark_read: bool = True) -> dict[str, Any] | None:
        project = self._service.get_project(task_id)
        record = self._store.get_project(task_id)
        if project is None or record is None:
            return None
        root = self.get_tree(task_id)
        if mark_read:
            self.mark_progress_read(task_id)
        return {
            'task_id': task_id,
            'task_state': record.task_state,
            'tree_text': self._tree_builder.to_text(root=root),
            'root_node': root.model_dump(mode='json') if root is not None else None,
            'nodes': [item.model_dump(mode='json') for item in self._store.list_nodes(task_id)],
            'text': f"任务状态：{record.task_state}\n{self._tree_builder.to_text(root=root)}",
        }

    def engineering_exceptions(self, session_id: str, *, mark_read: bool = True) -> dict[str, Any]:
        items = [item for item in self._store.list_projects(session_id) if item.has_active_engineering_exception]
        task_ids = [item.project_id for item in items if self._service.get_project(item.project_id) is not None]
        if mark_read:
            for task_id in task_ids:
                self.mark_engineering_read(task_id)
        text = '\n'.join(task_ids) if task_ids else 'No engineering exceptions.'
        return {'task_ids': task_ids, 'text': text}

    def _root_node_state(self, project_id: str, *, nodes: list[TaskMonitorNodeRecord] | None = None) -> str:
        project = self._service.get_project(project_id)
        if project is None:
            return 'in_progress'
        entries = nodes if nodes is not None else self._store.list_nodes(project_id)
        index = {item.node_id: item for item in entries}
        root = index.get(project.root_unit_id)
        if root is None:
            return 'in_progress'
        if root.state == 'waiting':
            return 'waiting'
        for node in entries:
            if node.state != 'waiting':
                continue
            current = node
            while current.parent_node_id:
                parent = index.get(current.parent_node_id)
                if parent is None:
                    break
                current = parent
            if current.node_id == root.node_id:
                return 'waiting'
        return root.state

