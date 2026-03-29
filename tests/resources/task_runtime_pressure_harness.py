from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from main.models import TaskRecord, TokenUsageSummary
from main.monitoring.models import (
    TaskProjectionNodeDetailRecord,
    TaskProjectionNodeRecord,
    TaskProjectionRoundRecord,
    TaskProjectionRuntimeFrameRecord,
)
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in pressure harness seed phase: {kwargs!r}")


def _iso_at(index: int) -> str:
    normalized = max(0, int(index or 0))
    hours = (normalized // 3600) % 24
    minutes = (normalized // 60) % 60
    seconds = normalized % 60
    return f"2026-03-29T{hours:02d}:{minutes:02d}:{seconds:02d}+08:00"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _measure_sync(fn, *, repeats: int) -> tuple[dict[str, object], object]:
    samples: list[float] = []
    last_result = None
    for _ in range(max(1, int(repeats or 1))):
        started = time.perf_counter()
        last_result = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p50_index = max(0, math.ceil(0.50 * len(ordered)) - 1)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return (
        {
            "runs": len(ordered),
            "p50_ms": round(ordered[p50_index], 3),
            "p95_ms": round(ordered[p95_index], 3),
            "max_ms": round(max(ordered), 3),
        },
        last_result,
    )


async def seed_runtime(
    *,
    workspace: Path,
    task_count: int,
    nodes_per_task: int,
    root_children_per_task: int,
) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=workspace / "runtime.sqlite3",
        files_base_dir=workspace / "tasks",
        artifact_dir=workspace / "artifacts",
        governance_store_path=workspace / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None
    for task_index in range(task_count):
        task_id = f"task:pressure:{task_index:04d}"
        root_node_id = f"node:pressure:{task_index:04d}:root"
        created_at = _iso_at(task_index)
        task = TaskRecord(
            task_id=task_id,
            session_id="web:shared",
            title=f"Pressure Task {task_index}",
            user_request=f"pressure task {task_index}",
            status="in_progress",
            root_node_id=root_node_id,
            max_depth=max(1, int(nodes_per_task)),
            is_unread=False,
            brief_text=f"pressure task {task_index}",
            created_at=created_at,
            updated_at=created_at,
            token_usage=TokenUsageSummary(tracked=True),
            metadata={"seed_mode": "direct_v2"},
        )
        service.store.upsert_task(task)
        service.store.upsert_task_runtime_meta(
            task_id=task_id,
            updated_at=created_at,
            payload={
                "updated_at": created_at,
                "last_visible_output_at": created_at,
                "last_stall_notice_bucket_minutes": 0,
            },
        )

        node_records: list[TaskProjectionNodeRecord] = []
        detail_records: list[TaskProjectionNodeDetailRecord] = []
        round_records: list[TaskProjectionRoundRecord] = []
        runtime_frames: list[TaskProjectionRuntimeFrameRecord] = []

        def add_node(
            *,
            node_id: str,
            parent_node_id: str | None,
            depth: int,
            sort_key: str,
            title: str,
            goal: str,
            output_text: str = "",
        ) -> None:
            updated_at = _iso_at(task_index * max(1, nodes_per_task) + len(node_records))
            node_records.append(
                TaskProjectionNodeRecord(
                    node_id=node_id,
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    root_node_id=root_node_id,
                    depth=depth,
                    node_kind="execution",
                    status="in_progress",
                    title=title,
                    updated_at=updated_at,
                    default_round_id="round:root" if node_id == root_node_id else "",
                    selected_round_id="round:root" if node_id == root_node_id else "",
                    round_options_count=1 if node_id == root_node_id else 0,
                    sort_key=sort_key,
                    payload={
                        "node_id": node_id,
                        "task_id": task_id,
                        "parent_node_id": parent_node_id,
                        "depth": depth,
                        "node_kind": "execution",
                        "status": "in_progress",
                        "title": title,
                        "updated_at": updated_at,
                    },
                )
            )
            detail_records.append(
                TaskProjectionNodeDetailRecord(
                    node_id=node_id,
                    task_id=task_id,
                    updated_at=updated_at,
                    input_text=f"input {goal}",
                    input_ref="",
                    output_text=output_text,
                    output_ref="",
                    check_result="",
                    check_result_ref="",
                    final_output="",
                    final_output_ref="",
                    failure_reason="",
                    prompt_summary=f"prompt {goal}",
                    execution_trace_ref="",
                    payload={
                        "node_id": node_id,
                        "task_id": task_id,
                        "parent_node_id": parent_node_id,
                        "depth": depth,
                        "node_kind": "execution",
                        "status": "in_progress",
                        "goal": goal,
                        "prompt_summary": f"prompt {goal}",
                        "input_text": f"input {goal}",
                        "output_text": output_text,
                        "updated_at": updated_at,
                        "token_usage": {"tracked": True},
                        "token_usage_by_model": [],
                        "execution_trace": {
                            "tool_steps": [],
                        },
                    },
                )
            )

        add_node(
            node_id=root_node_id,
            parent_node_id=None,
            depth=0,
            sort_key="0000",
            title=f"Root {task_index}",
            goal=f"root goal {task_index}",
            output_text=f"root output {task_index}",
        )

        remaining_nodes = max(0, nodes_per_task - 1)
        direct_children = min(max(0, int(root_children_per_task or 0)), remaining_nodes)
        root_child_ids: list[str] = []
        for node_index in range(direct_children):
            child_id = f"node:pressure:{task_index:04d}:root-child:{node_index:03d}"
            root_child_ids.append(child_id)
            add_node(
                node_id=child_id,
                parent_node_id=root_node_id,
                depth=1,
                sort_key=f"{node_index + 1:04d}",
                title=f"Root Child {task_index}-{node_index}",
                goal=f"root child goal {task_index}-{node_index}",
                output_text=f"root child output {task_index}-{node_index}",
            )

        parent_node_id = root_child_ids[-1] if root_child_ids else root_node_id
        parent_depth = 1 if root_child_ids else 0
        for node_index in range(direct_children, remaining_nodes):
            child_id = f"node:pressure:{task_index:04d}:chain:{node_index:03d}"
            parent_depth += 1
            add_node(
                node_id=child_id,
                parent_node_id=parent_node_id,
                depth=parent_depth,
                sort_key=f"{node_index + 1:04d}",
                title=f"Chain Node {task_index}-{node_index}",
                goal=f"chain goal {task_index}-{node_index}",
                output_text="",
            )
            parent_node_id = child_id

        if root_child_ids:
            round_records.append(
                TaskProjectionRoundRecord(
                    task_id=task_id,
                    parent_node_id=root_node_id,
                    round_id="round:root",
                    round_index=0,
                    label="Round 1",
                    is_latest=True,
                    created_at=created_at,
                    source="explicit",
                    total_children=len(root_child_ids),
                    completed_children=0,
                    running_children=0,
                    failed_children=0,
                    child_node_ids=root_child_ids,
                )
            )

        runtime_frames.append(
            TaskProjectionRuntimeFrameRecord(
                task_id=task_id,
                node_id=root_node_id,
                depth=0,
                node_kind="execution",
                phase="before_model",
                active=True,
                runnable=True,
                waiting=False,
                updated_at=created_at,
                payload={
                    "node_id": root_node_id,
                    "depth": 0,
                    "node_kind": "execution",
                    "phase": "before_model",
                    "stage_goal": f"root goal {task_index}",
                    "tool_calls": [],
                    "child_pipelines": [],
                },
            )
        )

        service.store.replace_task_nodes(task_id, node_records)
        service.store.replace_task_node_details(task_id, detail_records)
        service.store.replace_task_node_rounds(task_id, round_records)
        service.store.replace_task_runtime_frames(task_id, runtime_frames)
        service.store.append_task_model_call(
            task_id=task_id,
            node_id=root_node_id,
            created_at=created_at,
            payload={
                "call_index": 1,
                "prepared_message_count": 8,
                "prepared_message_chars": 512,
                "response_tool_call_count": 0,
                "delta_usage": {"tracked": True, "input_tokens": 100, "output_tokens": 40, "call_count": 1, "calls_with_usage": 1},
                "delta_usage_by_model": [],
            },
        )
    return service


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed and probe Task Runtime V2 pressure dataset")
    parser.add_argument("--workspace", type=Path, default=Path(".g3ku") / "pressure-runtime")
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--nodes-per-task", type=int, default=200)
    parser.add_argument("--active-tasks", type=int, default=20)
    parser.add_argument("--root-children", type=int, default=50)
    parser.add_argument("--probes", type=int, default=50)
    args = parser.parse_args()

    shutil.rmtree(args.workspace, ignore_errors=True)
    args.workspace.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    service = await seed_runtime(
        workspace=args.workspace,
        task_count=max(1, int(args.tasks)),
        nodes_per_task=max(1, int(args.nodes_per_task)),
        root_children_per_task=max(0, int(args.root_children or 0)),
    )
    try:
        all_tasks = service.store.list_tasks()
        sample_task = all_tasks[0]
        active_task_count = min(max(0, int(args.active_tasks or 0)), len(all_tasks))
        original_run_task = service.task_actor_service.run_task
        block_execution = asyncio.Event()

        async def blocking_run_task(_task_id: str) -> None:
            await block_execution.wait()

        service.task_actor_service.run_task = blocking_run_task
        service.global_scheduler._max_concurrent_tasks = max(1, active_task_count)
        for task in all_tasks[:active_task_count]:
            await service.global_scheduler.enqueue_task(task.task_id)

        deadline = time.perf_counter() + 5.0
        while service.global_scheduler.active_task_count() < active_task_count and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)

        worker_updated_at = _now_iso()
        service.store.upsert_worker_status(
            worker_id=service.worker_id or "worker:pressure",
            role="task_worker",
            status="running",
            updated_at=worker_updated_at,
            payload={
                "execution_mode": service.execution_mode,
                "active_task_count": active_task_count,
            },
        )
        latest_worker = service.latest_worker_status()

        list_stats, list_payload = _measure_sync(
            lambda: service.query_service.get_tasks("web:shared", 1),
            repeats=max(1, int(args.probes or 1)),
        )
        detail_stats, detail_payload = _measure_sync(
            lambda: service.get_task_detail_payload(sample_task.task_id, mark_read=False),
            repeats=max(1, int(args.probes or 1)),
        )
        children_stats, children_payload = _measure_sync(
            lambda: service.get_node_children_payload(sample_task.task_id, sample_task.root_node_id, limit=50),
            repeats=max(1, int(args.probes or 1)),
        )
        worker_stats, worker_payload = _measure_sync(
            lambda: service.worker_status_payload(stale_after_seconds=60.0),
            repeats=max(1, int(args.probes or 1)),
        )

        result = {
            "seed_seconds": round(time.perf_counter() - started, 3),
            "task_count": len(all_tasks),
            "nodes_per_task": max(1, int(args.nodes_per_task)),
            "active_task_count": service.global_scheduler.active_task_count(),
            "queued_task_count": service.global_scheduler.queued_task_count(),
            "probe_runs": max(1, int(args.probes or 1)),
            "list": {
                **list_stats,
                "response_bytes": len(json.dumps(_jsonable(list_payload), ensure_ascii=False).encode("utf-8")),
            },
            "detail": {
                **detail_stats,
                "response_bytes": len(json.dumps(_jsonable(detail_payload), ensure_ascii=False).encode("utf-8")),
            },
            "children": {
                **children_stats,
                "response_bytes": len(json.dumps(_jsonable(children_payload), ensure_ascii=False).encode("utf-8")),
                "items_returned": len(list((children_payload or {}).get("items") or [])),
            },
            "worker_status": {
                **worker_stats,
                "response_bytes": len(json.dumps(_jsonable(worker_payload), ensure_ascii=False).encode("utf-8")),
                "worker_state": (worker_payload or {}).get("worker_state"),
                "worker_online": bool((worker_payload or {}).get("worker_online")),
            },
            "worker_snapshot": _jsonable(latest_worker),
            "acceptance": {
                "list_p95_lt_150ms": bool(float(list_stats["p95_ms"]) < 150.0),
                "detail_p95_lt_200ms": bool(float(detail_stats["p95_ms"]) < 200.0),
                "detail_payload_lt_100kb": bool(len(json.dumps(_jsonable(detail_payload), ensure_ascii=False).encode("utf-8")) < 100 * 1024),
                "children_p95_lt_120ms": bool(float(children_stats["p95_ms"]) < 120.0),
                "children_returned_50": len(list((children_payload or {}).get("items") or [])) == 50,
                "worker_online": bool((worker_payload or {}).get("worker_online")),
                "worker_active_count_matches_target": int(((latest_worker or {}).get("payload") or {}).get("active_task_count") or 0) >= active_task_count,
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        block_execution.set()
        await asyncio.gather(*(service.global_scheduler.wait(task.task_id) for task in all_tasks[:active_task_count]), return_exceptions=True)
        service.task_actor_service.run_task = original_run_task
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
