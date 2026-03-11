from __future__ import annotations

from g3ku.org_graph.ids import new_notice_id
from g3ku.org_graph.models import PendingProjectNotice
from g3ku.org_graph.protocol import now_iso
from g3ku.org_graph.storage.project_store import ProjectStore


class NoticeService:
    def __init__(self, store: ProjectStore):
        self._store = store

    def create(self, *, session_id: str, project_id: str, kind: str, title: str, text: str) -> PendingProjectNotice:
        record = PendingProjectNotice(
            notice_id=new_notice_id(),
            session_id=session_id,
            project_id=project_id,
            kind=kind,
            title=title,
            text=text,
            created_at=now_iso(),
            acknowledged=False,
        )
        return self._store.upsert_notice(record)

    def list(self, session_id: str, *, include_acknowledged: bool = False) -> list[PendingProjectNotice]:
        return self._store.list_notices(session_id, include_acknowledged=include_acknowledged)

    def ack(self, notice_id: str) -> PendingProjectNotice | None:
        return self._store.acknowledge_notice(notice_id)

