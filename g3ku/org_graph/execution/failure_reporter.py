from __future__ import annotations

from g3ku.org_graph.execution.escalation import EscalationHelper


class FailureReporter:
    def __init__(self, service):
        self._service = service

    async def record_unit_failure(self, *, project, unit, error: Exception | str, failure_kind: str) -> None:
        error_text = str(error)
        failure_kind = str(failure_kind or 'engineering')
        metadata = dict(unit.metadata or {})
        metadata['failure_kind'] = failure_kind
        updated_unit = unit.model_copy(
            update={
                'status': 'failed',
                'error_summary': error_text,
                'current_action': '',
                'updated_at': self._service.now(),
                'finished_at': self._service.now(),
                'metadata': metadata,
            }
        )
        self._service.store.upsert_unit(updated_unit)
        if updated_unit.current_stage_id:
            stage = self._service.get_stage(updated_unit.current_stage_id)
            if stage is not None and stage.status not in {'completed', 'failed', 'canceled'}:
                failed_stage = stage.model_copy(
                    update={
                        'status': 'failed',
                        'error_summary': error_text,
                        'finished_at': self._service.now(),
                    }
                )
                self._service.store.upsert_stage(failed_stage)
                await self._service.emit_event(
                    project=project,
                    scope='stage',
                    event_name='stage.failed',
                    text=f'{failed_stage.title} failed: {error_text}',
                    unit_id=updated_unit.unit_id,
                    stage_id=failed_stage.stage_id,
                    level='error',
                    data={'failure_kind': failure_kind},
                )
        await self._service.emit_unit_event(
            project=project,
            unit=updated_unit,
            event_name='unit.failed',
            text=f'{unit.role_title} failed: {error_text}',
            level='error',
            extra_data={'failure_kind': failure_kind},
        )
        if failure_kind != 'engineering':
            return
        await self._service.emit_event(
            project=project,
            scope='project',
            event_name='notice.raised',
            text=f'Failure escalated from unit {unit.role_title}',
            unit_id=unit.unit_id,
            level='error',
            data={'error': error_text, 'code': 'unit_failed', 'failure_kind': failure_kind},
        )
        notice = self._service.notice_service.create(
            session_id=project.session_id,
            project_id=project.project_id,
            kind='project_blocked',
            title='Project unit failed',
            text=EscalationHelper.unit_failure_text(project_title=project.title, role_title=unit.role_title, error=error_text),
        )
        await self._service.publish_notice(project.session_id, notice)

    async def report_unit_failure(self, *, project, unit, error: Exception | str) -> None:
        await self.record_unit_failure(project=project, unit=unit, error=error, failure_kind='engineering')

    async def record_project_failure(self, *, project, error: Exception | str, failure_kind: str) -> None:
        error_text = str(error)
        failure_kind = str(failure_kind or 'engineering')
        updated = project.model_copy(
            update={
                'status': 'failed',
                'error_summary': error_text,
                'updated_at': self._service.now(),
                'finished_at': self._service.now(),
                'summary': error_text,
            }
        )
        self._service.store.upsert_project(updated)
        await self._service.emit_event(
            project=updated,
            scope='project',
            event_name='project.failed',
            text=error_text,
            level='error',
            data={'failure_kind': failure_kind},
        )
        if failure_kind == 'engineering':
            await self._service.emit_event(
                project=updated,
                scope='project',
                event_name='notice.raised',
                text='Project failure escalated to CEO',
                level='error',
                data={'error': error_text, 'code': 'project_failed', 'failure_kind': failure_kind},
            )
            notice = self._service.notice_service.create(
                session_id=project.session_id,
                project_id=project.project_id,
                kind='project_failed',
                title='Project failed',
                text=EscalationHelper.project_failed_text(project_title=project.title, error=error_text),
            )
            await self._service.publish_notice(project.session_id, notice)
        await self._service.publish_summary(updated)

    async def report_project_failure(self, *, project, error: Exception | str) -> None:
        await self.record_project_failure(project=project, error=error, failure_kind='engineering')
