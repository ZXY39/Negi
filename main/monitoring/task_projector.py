from __future__ import annotations

from main.monitoring.models import (
    TaskProjectionNodeDetailRecord,
    TaskProjectionNodeRecord,
    TaskProjectionRoundRecord,
    TaskProjectionRuntimeFrameRecord,
)


class TaskProjector:
    def __init__(self, *, store) -> None:
        self._store = store

    def sync_node(self, node_record: TaskProjectionNodeRecord, detail_record: TaskProjectionNodeDetailRecord) -> None:
        self._store.upsert_task_node(node_record)
        self._store.upsert_task_node_detail(detail_record)

    def sync_rounds_for_parent(self, task_id: str, parent_node_id: str, rounds: list[TaskProjectionRoundRecord]) -> None:
        self._store.replace_task_node_rounds_for_parent(task_id, parent_node_id, rounds)

    def replace_runtime_frames(self, task_id: str, frames: list[TaskProjectionRuntimeFrameRecord]) -> None:
        self._store.replace_task_runtime_frames(task_id, frames)
