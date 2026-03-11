from __future__ import annotations

import re

from g3ku.org_graph.models import ProjectCreateRequest


class CEORunner:
    def __init__(self, service):
        self._service = service

    async def handle_message(self, session_id: str, text: str) -> str:
        message = str(text or '').strip()
        lower = message.lower()
        if not message:
            return 'Empty request ignored.'

        project_id = self._extract_project_id(message)
        notice_id = self._extract_notice_id(message)

        if project_id and any(token in lower for token in ['pause', '暂停']):
            project = await self._service.pause_project(project_id)
            return self._render_control_result(project_id, project, action='paused')
        if project_id and any(token in lower for token in ['resume', '恢复', '继续项目']):
            project = await self._service.resume_project(project_id)
            return self._render_control_result(project_id, project, action='resumed')
        if project_id and any(token in lower for token in ['cancel', '取消']):
            project = await self._service.cancel_project(project_id)
            return self._render_control_result(project_id, project, action='canceled')
        if project_id and any(token in lower for token in ['archive', '归档']):
            project = await self._service.archive_project(project_id)
            return self._render_control_result(project_id, project, action='archived')
        if notice_id and any(token in lower for token in ['ack', '确认通知', '已读通知', 'acknowledge']):
            notice = self._service.ack_notice(notice_id)
            if notice is None:
                return f'Notice not found: {notice_id}'
            return f'Notice acknowledged: {notice.notice_id} ({notice.project_id})'

        if any(token in lower for token in ['list projects', '项目列表', '有哪些项目', '全部项目']):
            return self._render_project_list(session_id)
        if any(token in lower for token in ['status', '进度', '状态']) and project_id:
            return self._render_project_status(project_id)
        if any(token in lower for token in ['notice', '通知']):
            notices = self._service.list_notices(session_id)
            if not notices:
                return 'No pending project notices.'
            return '\n'.join(f'- {notice.notice_id} | {notice.project_id}: {notice.text}' for notice in notices)

        requested_depth = self._extract_depth(message)
        request = ProjectCreateRequest(session_id=session_id, prompt=message, max_depth=requested_depth)
        project = await self._service.create_project(request)
        return (
            f'Project created: {project.project_id}\n'
            f'- title: {project.title}\n'
            f'- status: {project.status}\n'
            f'- max_depth: {project.effective_max_depth}\n'
            'The CEO remains available while the project runs in the background.'
        )

    def _render_project_list(self, session_id: str) -> str:
        projects = self._service.list_projects(session_id)
        if not projects:
            return 'No projects found for this session.'
        return '\n'.join(f'- {project.project_id} [{project.status}] {project.title}' for project in projects)

    def _render_project_status(self, project_id: str) -> str:
        project = self._service.get_project(project_id)
        if project is None:
            return f'Project not found: {project_id}'
        return f'{project.project_id} [{project.status}] {project.summary or project.title}'

    @staticmethod
    def _render_control_result(project_id: str, project, *, action: str) -> str:
        if project is None:
            return f'Project not found: {project_id}'
        return f'Project {action}: {project.project_id} [{project.status}] {project.summary}'

    @staticmethod
    def _extract_project_id(text: str) -> str | None:
        match = re.search(r'(proj:[A-Za-z0-9:._-]+)', text)
        return match.group(1) if match else None

    @staticmethod
    def _extract_notice_id(text: str) -> str | None:
        match = re.search(r'(notice:[A-Za-z0-9:._-]+)', text)
        return match.group(1) if match else None

    @staticmethod
    def _extract_depth(text: str) -> int | None:
        match = re.search(r'(\d+)\s*层', text)
        return int(match.group(1)) if match else None

