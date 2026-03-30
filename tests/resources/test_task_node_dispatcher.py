from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from main.models import NodeFinalResult, SpawnChildSpec
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in task node dispatcher tests: {kwargs!r}")


def _make_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None
    return service


async def _create_task(service: MainRuntimeService):
    return await service.create_task("dispatcher task", session_id="web:shared")


def _execution_policy() -> dict[str, str]:
    return {"mode": "focus"}


def _create_execution_child(service: MainRuntimeService, *, task, parent, name: str):
    return service.node_runner._create_execution_child(
        task=task,
        parent=parent,
        spec=SpawnChildSpec(
            goal=f"{name} goal",
            prompt=f"{name} prompt",
            execution_policy=_execution_policy(),
        ),
    )


def _success_result(node_id: str) -> NodeFinalResult:
    text = f"{node_id} complete"
    return NodeFinalResult(
        status="success",
        delivery_status="final",
        summary=text,
        answer=text,
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.perf_counter() + max(0.1, float(timeout))
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


@pytest.mark.asyncio
async def test_task_node_dispatcher_runs_execution_children_in_parallel_and_respects_limit(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    record = await _create_task(service)
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    child_one = _create_execution_child(service, task=task, parent=root, name="child-one")
    child_two = _create_execution_child(service, task=task, parent=root, name="child-two")
    child_three = _create_execution_child(service, task=task, parent=root, name="child-three")

    dependencies = {
        root.node_id: [child_one.node_id, child_two.node_id, child_three.node_id],
    }
    starts: dict[str, float] = {}
    finishes: dict[str, float] = {}

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        starts.setdefault(node_id, time.perf_counter())
        try:
            await asyncio.sleep(0.02)
            child_ids = list(dependencies.get(node_id) or [])
            if child_ids:
                await asyncio.gather(
                    *(service.node_runner._run_nested_node(task_id, child_id) for child_id in child_ids)
                )
            await asyncio.sleep(0.05)
            return _success_result(node_id)
        finally:
            finishes[node_id] = time.perf_counter()

    service.node_runner.run_node = fake_run_node
    service.task_actor_service.configure_node_dispatch_limits(execution=2, inspection=1)

    await service.task_actor_service.run_task(record.task_id)

    ordered_children = sorted(
        [child_one.node_id, child_two.node_id, child_three.node_id],
        key=lambda item: starts[item],
    )
    first_child, second_child, third_child = ordered_children
    assert starts[second_child] < finishes[first_child]
    assert starts[third_child] >= min(finishes[first_child], finishes[second_child])


@pytest.mark.asyncio
async def test_task_node_dispatcher_deduplicates_duplicate_nested_waiters(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    record = await _create_task(service)
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    child = _create_execution_child(service, task=task, parent=root, name="shared-child")
    call_counts: dict[str, int] = {}

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        call_counts[node_id] = int(call_counts.get(node_id) or 0) + 1
        if node_id == root.node_id:
            await asyncio.gather(
                service.node_runner._run_nested_node(task_id, child.node_id),
                service.node_runner._run_nested_node(task_id, child.node_id),
            )
        await asyncio.sleep(0.02)
        return _success_result(node_id)

    service.node_runner.run_node = fake_run_node

    await service.task_actor_service.run_task(record.task_id)

    assert call_counts[root.node_id] == 1
    assert call_counts[child.node_id] == 1


@pytest.mark.asyncio
async def test_task_node_dispatcher_exposes_dispatch_metrics_in_task_detail_payload(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    record = await _create_task(service)
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    child_one = _create_execution_child(service, task=task, parent=root, name="detail-child-one")
    child_two = _create_execution_child(service, task=task, parent=root, name="detail-child-two")
    release_children = asyncio.Event()
    child_started = {
        child_one.node_id: asyncio.Event(),
        child_two.node_id: asyncio.Event(),
    }

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        if node_id == root.node_id:
            await asyncio.gather(
                service.node_runner._run_nested_node(task_id, child_one.node_id),
                service.node_runner._run_nested_node(task_id, child_two.node_id),
            )
            return _success_result(node_id)
        child_started[node_id].set()
        await release_children.wait()
        return _success_result(node_id)

    service.node_runner.run_node = fake_run_node
    service.task_actor_service.configure_node_dispatch_limits(execution=2, inspection=1)

    task_runner = asyncio.create_task(service.task_actor_service.run_task(record.task_id))
    await _wait_until(lambda: all(event.is_set() for event in child_started.values()))

    payload = service.get_task_detail_payload(record.task_id, mark_read=False)
    assert payload is not None
    assert payload["runtime_summary"]["dispatch_limits"] == {"execution": 2, "inspection": 1}
    assert payload["runtime_summary"]["dispatch_running"] == {"execution": 2, "inspection": 0}
    assert payload["runtime_summary"]["dispatch_queued"] == {"execution": 0, "inspection": 0}

    release_children.set()
    await task_runner


@pytest.mark.asyncio
async def test_task_node_dispatcher_cleans_up_running_children_when_task_is_canceled(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    record = await _create_task(service)
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    child = _create_execution_child(service, task=task, parent=root, name="cancel-child")
    child_started = asyncio.Event()
    child_canceled = asyncio.Event()

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        if node_id == root.node_id:
            await service.node_runner._run_nested_node(task_id, child.node_id)
            return _success_result(node_id)
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_canceled.set()
            raise

    service.node_runner.run_node = fake_run_node

    task_runner = asyncio.create_task(service.task_actor_service.run_task(record.task_id))
    await _wait_until(child_started.is_set)
    task_runner.cancel()
    await task_runner

    assert child_canceled.is_set()
    payload = service.get_task_detail_payload(record.task_id, mark_read=False)
    assert payload is not None
    assert payload["runtime_summary"]["dispatch_running"] == {"execution": 0, "inspection": 0}
    assert payload["runtime_summary"]["dispatch_queued"] == {"execution": 0, "inspection": 0}


@pytest.mark.asyncio
async def test_task_node_dispatcher_runs_final_acceptance_via_inspection_role(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    record = await _create_task(service)
    root = service.get_node(record.root_node_id)
    assert root is not None

    service.log_service.update_task_metadata(
        record.task_id,
        lambda metadata: {
            **dict(metadata or {}),
            "final_acceptance": {"required": True, "prompt": "verify final output"},
        },
        mark_unread=False,
    )

    call_order: list[str] = []
    roles: list[str] = []

    async def fake_run_node(_task_id: str, node_id: str) -> NodeFinalResult:
        node = service.get_node(node_id)
        assert node is not None
        call_order.append(node_id)
        roles.append("inspection" if node.node_kind == "acceptance" else "execution")
        await asyncio.sleep(0.01)
        return _success_result(node_id)

    service.node_runner.run_node = fake_run_node

    await service.task_actor_service.run_task(record.task_id)

    acceptance_nodes = [node for node in service.list_nodes(record.task_id) if node.node_kind == "acceptance"]
    assert len(acceptance_nodes) == 1
    assert call_order[0] == root.node_id
    assert call_order[1] == acceptance_nodes[0].node_id
    assert roles == ["execution", "inspection"]
