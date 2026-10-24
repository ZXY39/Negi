from __future__ import annotations

from main.monitoring.models import (
    TaskProjectionNodeDetailRecord,
    TaskProjectionNodeRecord,
    TaskProjectionRuntimeFrameRecord,
)


class TaskProjector:
    # 轮次投影（task_node_rounds）已停止写入：前端节点树不再按派生轮次分组，
    # 子节点关系由投影节点的 parent_node_id 直接表达。表定义保留以兼容旧库。
    def __init__(self, *, store) -> None:
        self._store = store

    def sync_node(self, node_record: TaskProjectionNodeRecord, detail_record: TaskProjectionNodeDetailRecord) -> None:
        self._store.upsert_task_node(node_record)
        self._store.upsert_task_node_detail(detail_record)

    def replace_runtime_frames(self, task_id: str, frames: list[TaskProjectionRuntimeFrameRecord]) -> None:
        self._store.replace_task_runtime_frames(task_id, frames)
