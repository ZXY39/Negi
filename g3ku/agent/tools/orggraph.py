from __future__ import annotations

import json
from typing import Any, Callable

from g3ku.agent.tools.base import Tool
from g3ku.org_graph.models import ProjectCreateRequest


def _runtime_session_key(runtime: dict[str, Any] | None) -> str:
    payload = runtime if isinstance(runtime, dict) else {}
    return str(payload.get('session_key') or 'web:shared').strip() or 'web:shared'


def _consume_project_dispatch_slot(runtime: dict[str, Any] | None) -> None:
    payload = runtime if isinstance(runtime, dict) else None
    if payload is None:
        return
    used = int(payload.get('main_agent_project_dispatch_count') or 0)
    if used >= 1:
        raise ValueError('main_agent_single_project_dispatch_only')
    payload['main_agent_project_dispatch_count'] = used + 1


def _single_line_text(value: Any, *, max_chars: int = 120) -> str:
    text = ' '.join(str(value or '').split())
    if len(text) <= max_chars:
        return text
    return f'{text[:max_chars - 3].rstrip()}...'


def _compact_task_list_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tasks = [
        {
            'task_id': str(item.get('task_id') or '').strip(),
            'brief': _single_line_text(item.get('brief') or ''),
        }
        for item in list(payload.get('tasks') or [])
        if str(item.get('task_id') or '').strip()
    ]
    text = '\n'.join(f"- {item['task_id']}: {item['brief']}" for item in tasks) or 'No matching tasks.'
    return {
        'scope': int(payload.get('scope') or 0),
        'tasks': tasks,
        'text': text,
    }


def _compact_task_progress_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = str(payload.get('task_id') or '').strip()
    task_state = str(payload.get('task_state') or '').strip()
    tree_text = str(payload.get('tree_text') or '').strip()
    text = f'任务状态：{task_state}'
    if tree_text:
        text = f'{text}\n{tree_text}'
    return {
        'task_id': task_id,
        'task_state': task_state,
        'tree_text': tree_text,
        'text': text,
    }


def _compact_engineering_exceptions_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task_ids = [str(item or '').strip() for item in list(payload.get('task_ids') or []) if str(item or '').strip()]
    text = '\n'.join(task_ids) if task_ids else 'No engineering exceptions.'
    return {
        'task_ids': task_ids,
        'text': text,
    }


class _ProjectServiceTool(Tool):
    def __init__(self, service_getter: Callable[[], Any]):
        self._service_getter = service_getter

    async def _service(self):
        service = self._service_getter()
        await service.startup()
        return service


class OrgGraphCreateProjectTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'orggraph_create_project'

    @property
    def description(self) -> str:
        return 'Create a new org-graph background project for long-running or multi-step work handled by one execution lead.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string', 'description': 'The project request to execute in the background.'},
                'preferred_title': {'type': 'string', 'description': 'Optional short project title.'},
                'output_target': {'type': 'string', 'description': 'Optional output target label.'},
            },
            'required': ['prompt'],
        }

    async def execute(self, prompt: str, preferred_title: str | None = None, output_target: str = 'chat', __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        try:
            _consume_project_dispatch_slot(__g3ku_runtime)
        except ValueError as exc:
            return f'Error: {exc}'
        service = await self._service()
        project = await service.create_project(
            ProjectCreateRequest(
                session_id=_runtime_session_key(__g3ku_runtime),
                prompt=str(prompt or ''),
                preferred_title=str(preferred_title or '').strip() or None,
                output_target=str(output_target or 'chat').strip() or 'chat',
                metadata={},
            )
        )
        payload = {
            'project_id': project.project_id,
            'title': project.title,
            'status': project.status,
            'summary': project.summary,
            'text': f'Created background project {project.project_id} ({project.title}); the main agent handed this request to the project lead execution node.',
        }
        return json.dumps(payload, ensure_ascii=False)


class OrgGraphControlProjectTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'orggraph_control_project'

    @property
    def description(self) -> str:
        return 'Pause, resume, cancel, or archive an existing org-graph project.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'project_id': {'type': 'string', 'description': 'The project id to control.'},
                'action': {'type': 'string', 'enum': ['pause', 'resume', 'cancel', 'archive'], 'description': 'Control action to perform.'},
            },
            'required': ['project_id', 'action'],
        }

    async def execute(self, project_id: str, action: str, **kwargs: Any) -> str:
        service = await self._service()
        normalized = str(action or '').strip().lower()
        if normalized == 'pause':
            project = await service.pause_project(project_id)
        elif normalized == 'resume':
            project = await service.resume_project(project_id)
        elif normalized == 'cancel':
            project = await service.cancel_project(project_id)
        elif normalized == 'archive':
            project = await service.archive_project(project_id)
        else:
            return json.dumps({'ok': False, 'error': f'Unknown action: {action}'}, ensure_ascii=False)
        if project is None:
            return json.dumps({'ok': False, 'error': f'Project not found: {project_id}'}, ensure_ascii=False)
        return json.dumps({'ok': True, 'project_id': project.project_id, 'status': project.status, 'summary': project.summary}, ensure_ascii=False)


class TaskMonitorSummaryTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'task_monitor_summary'

    @property
    def description(self) -> str:
        return 'Summarize unread progress, unread engineering exceptions, total tasks, active tasks, and failed tasks.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {}}

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        service = await self._service()
        payload = service.monitor_service.summary(_runtime_session_key(__g3ku_runtime))
        return json.dumps(payload, ensure_ascii=False)


class TaskMonitorListTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'task_monitor_list'

    @property
    def description(self) -> str:
        return 'Return task ids and short descriptions for all, in-progress, failed, or unread tasks.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'scope': {'type': 'integer', 'enum': [1, 2, 3, 4], 'description': '1=all, 2=in_progress, 3=failed, 4=unread_progress'},
            },
            'required': ['scope'],
        }

    async def execute(self, scope: int, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        service = await self._service()
        payload = service.monitor_service.list_tasks(_runtime_session_key(__g3ku_runtime), int(scope), mark_read=False)
        return json.dumps(_compact_task_list_payload(payload), ensure_ascii=False)


class TaskMonitorProgressTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'task_monitor_progress'

    @property
    def description(self) -> str:
        return 'Return one task status and a text tree showing each node id and state.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'task_id': {'type': 'string', 'description': 'The target project/task id.'},
            },
            'required': ['task_id'],
        }

    async def execute(self, task_id: str, **kwargs: Any) -> str:
        service = await self._service()
        payload = service.monitor_service.progress(task_id, mark_read=False)
        if payload is None:
            return json.dumps({'ok': False, 'error': f'Task not found: {task_id}'}, ensure_ascii=False)
        return json.dumps(_compact_task_progress_payload(payload), ensure_ascii=False)


class TaskMonitorEngineeringExceptionsTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'task_monitor_engineering_exceptions'

    @property
    def description(self) -> str:
        return 'Return task ids that currently have unresolved engineering exceptions.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {}}

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        service = await self._service()
        payload = service.monitor_service.engineering_exceptions(_runtime_session_key(__g3ku_runtime), mark_read=False)
        return json.dumps(_compact_engineering_exceptions_payload(payload), ensure_ascii=False)


class LoadSkillContextTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'load_skill_context'

    @property
    def description(self) -> str:
        return 'Load the detailed body of a currently visible skill so the 主Agent can use it.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'skill_id': {'type': 'string', 'description': 'The skill id to load.'},
            },
            'required': ['skill_id'],
        }

    async def execute(self, skill_id: str, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        service = await self._service()
        session_id = _runtime_session_key(__g3ku_runtime)
        visible = {item.skill_id: item for item in service.list_visible_skill_resources(actor_role='ceo', session_id=session_id)}
        record = visible.get(str(skill_id or '').strip())
        if record is None:
            return json.dumps({'ok': False, 'error': f'Skill not visible for 主Agent: {skill_id}'}, ensure_ascii=False)
        content = ''
        if record.skill_doc_path:
            content = __import__('pathlib').Path(record.skill_doc_path).read_text(encoding='utf-8')
        return json.dumps({'ok': True, 'skill_id': record.skill_id, 'content': content}, ensure_ascii=False)


class LoadToolContextTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'load_tool_context'

    @property
    def description(self) -> str:
        return 'Load the detailed usage guide for a currently visible tool so the 主Agent can use it correctly.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'tool_id': {'type': 'string', 'description': 'The tool id to load.'},
            },
            'required': ['tool_id'],
        }

    async def execute(self, tool_id: str, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        service = await self._service()
        session_id = _runtime_session_key(__g3ku_runtime)
        tool_name = str(tool_id or '').strip()
        visible = set(service.list_effective_tool_names(session_id=session_id, actor_role='ceo'))
        if tool_name not in visible:
            return json.dumps({'ok': False, 'error': f'Tool not visible for 主Agent: {tool_id}'}, ensure_ascii=False)
        manager = getattr(service, 'resource_manager', None)
        if manager is None:
            return json.dumps({'ok': False, 'error': 'Resource manager unavailable'}, ensure_ascii=False)
        try:
            content = manager.load_toolskill_body(tool_name)
        except FileNotFoundError:
            content = ''
        return json.dumps({'ok': True, 'tool_id': tool_name, 'content': content}, ensure_ascii=False)


