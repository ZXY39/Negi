from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from main.models import SpawnChildSpec
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in pressure harness seed phase: {kwargs!r}")


def _execution_policy() -> dict[str, str]:
    return {"mode": "focus"}


async def seed_runtime(*, workspace: Path, task_count: int, nodes_per_task: int) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=workspace / "runtime.sqlite3",
        files_base_dir=workspace / "tasks",
        artifact_dir=workspace / "artifacts",
        governance_store_path=workspace / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None
    service.global_scheduler.enqueue_task = lambda _task_id: asyncio.sleep(0)
    for task_index in range(task_count):
        record = await service.create_task(f"pressure task {task_index}", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        parent = root
        for node_index in range(max(0, nodes_per_task - 1)):
            parent = service.node_runner._create_execution_child(
                task=task,
                parent=parent,
                spec=SpawnChildSpec(
                    goal=f"node {task_index}-{node_index}",
                    prompt=f"prompt {task_index}-{node_index}",
                    execution_policy=_execution_policy(),
                ),
            )
    return service


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed and probe Task Runtime V2 pressure dataset")
    parser.add_argument("--workspace", type=Path, default=Path(".g3ku") / "pressure-runtime")
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--nodes-per-task", type=int, default=200)
    args = parser.parse_args()

    args.workspace.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    service = await seed_runtime(
        workspace=args.workspace,
        task_count=max(1, int(args.tasks)),
        nodes_per_task=max(1, int(args.nodes_per_task)),
    )
    try:
        all_tasks = service.store.list_tasks()
        sample_task = all_tasks[0]
        t0 = time.perf_counter()
        service.query_service.get_tasks("web:shared", 1)
        t1 = time.perf_counter()
        service.get_task_detail_payload(sample_task.task_id, mark_read=False)
        t2 = time.perf_counter()
        service.get_node_children_payload(sample_task.task_id, sample_task.root_node_id, limit=50)
        t3 = time.perf_counter()
        print(
            {
                "seed_seconds": round(t1 - started, 3),
                "task_count": len(all_tasks),
                "list_seconds": round(t1 - t0, 4),
                "detail_seconds": round(t2 - t1, 4),
                "children_seconds": round(t3 - t2, 4),
            }
        )
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
