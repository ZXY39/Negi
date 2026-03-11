from __future__ import annotations

from typing import Any

from g3ku.org_graph.ids import new_event_id
from g3ku.org_graph.models import ProjectEventRecord
from g3ku.org_graph.protocol import build_envelope, now_iso
from g3ku.org_graph.service.project_registry import ProjectRegistry
from g3ku.org_graph.storage.event_store import EventStore


class TraceEmitter:
    def __init__(self, *, event_store: EventStore, registry: ProjectRegistry):
        self._event_store = event_store
        self._registry = registry

    async def emit_event(
        self,
        *,
        session_id: str,
        project_id: str,
        scope: str,
        event_name: str,
        text: str,
        unit_id: str | None = None,
        stage_id: str | None = None,
        level: str = 'info',
        data: dict[str, Any] | None = None,
    ) -> ProjectEventRecord:
        stored = self._event_store.append(
            ProjectEventRecord(
                event_id=new_event_id(),
                project_id=project_id,
                seq=0,
                session_id=session_id,
                unit_id=unit_id,
                stage_id=stage_id,
                scope=scope,
                event_name=event_name,
                level=level,
                text=text,
                data=data or {},
                created_at=now_iso(),
            )
        )
        await self._registry.publish_project(
            session_id,
            project_id,
            build_envelope(
                channel='project',
                session_id=session_id,
                project_id=project_id,
                seq=stored.seq,
                type='project.event',
                event_name=stored.event_name,
                data=stored.model_dump(mode='json'),
            ),
        )
        return stored

    async def emit_terminal(self, *, session_id: str, project_id: str, payload: dict[str, Any]) -> None:
        await self._registry.publish_project(
            session_id,
            project_id,
            build_envelope(
                channel='project',
                session_id=session_id,
                project_id=project_id,
                seq=self._event_store.latest_seq(project_id),
                type='project.finished',
                data=payload,
            ),
        )

