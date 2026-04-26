from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main.models import NodeFinalResult, SpawnChildSpec, normalize_final_acceptance_metadata
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
async def test_task_node_dispatcher_cancel_nodes_only_stops_targeted_child_and_preserves_sibling(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    record = await _create_task(service)
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    child_one = _create_execution_child(service, task=task, parent=root, name="cancel-target")
    child_two = _create_execution_child(service, task=task, parent=root, name="cancel-sibling")
    child_one_started = asyncio.Event()
    child_two_started = asyncio.Event()
    child_one_canceled = asyncio.Event()
    child_two_release = asyncio.Event()
    child_two_finished = asyncio.Event()

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        if node_id == root.node_id:
            await asyncio.gather(
                service.node_runner._run_nested_node(task_id, child_one.node_id),
                service.node_runner._run_nested_node(task_id, child_two.node_id),
            )
            return service.node_runner._mark_finished(task_id, node_id, _success_result(node_id))
        if node_id == child_one.node_id:
            child_one_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_one_canceled.set()
                return service.node_runner._mark_finished(
                    task_id,
                    node_id,
                    NodeFinalResult(
                        status="failed",
                        delivery_status="blocked",
                        summary="canceled",
                        answer="",
                        evidence=[],
                        remaining_work=[],
                        blocking_reason="canceled",
                    ),
                )
        if node_id == child_two.node_id:
            child_two_started.set()
            await child_two_release.wait()
            child_two_finished.set()
            return service.node_runner._mark_finished(task_id, node_id, _success_result(node_id))
        return service.node_runner._mark_finished(task_id, node_id, _success_result(node_id))

    service.node_runner.run_node = fake_run_node
    dispatcher = service.task_actor_service._create_dispatcher(record.task_id)
    service.task_actor_service._dispatchers[record.task_id] = dispatcher
    try:
        root_task = asyncio.create_task(dispatcher.execute_node(record.task_id, root.node_id))
        await _wait_until(lambda: child_one_started.is_set() and child_two_started.is_set())

        await dispatcher.cancel_nodes([child_one.node_id])

        child_two_release.set()
        root_result = await root_task
        payload = service.get_task_detail_payload(record.task_id, mark_read=False)

        assert child_one_canceled.is_set()
        assert child_two_finished.is_set()
        assert root_result.status == "success"
        assert payload is not None
        child_one_after = service.get_node(child_one.node_id)
        child_two_after = service.get_node(child_two.node_id)
        assert child_one_after is not None and child_two_after is not None
        assert child_one_after.status == "failed"
        assert child_two_after.status == "success"
        assert payload["runtime_summary"]["dispatch_running"] == {"execution": 0, "inspection": 0}
    finally:
        await dispatcher.close()
        service.task_actor_service._dispatchers.pop(record.task_id, None)


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
    assert acceptance_nodes[0].goal == f"最终验收:{root.goal}"
    assert call_order[0] == root.node_id
    assert call_order[1] == acceptance_nodes[0].node_id
    assert roles == ["execution", "inspection"]


@pytest.mark.asyncio
async def test_task_actor_service_requeues_partial_root_acceptance_handshake(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    record = await service.create_task(
        "root partial handshake",
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )
    scheduled: list[str] = []

    result = NodeFinalResult(
        status="success",
        delivery_status="partial",
        summary="waiting for acceptance",
        answer="draft",
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )

    service.task_actor_service._create_dispatcher = lambda task_id: SimpleNamespace(  # type: ignore[method-assign]
        execute_node=AsyncMock(return_value=result),
        close=AsyncMock(return_value=None),
    )
    service.task_actor_service.distribution_resume_callback = lambda task_id: scheduled.append(str(task_id))

    await service.task_actor_service.run_task(record.task_id)

    latest = service.get_task(record.task_id)
    assert latest is not None
    assert latest.status == "in_progress"
    assert scheduled == [record.task_id]


@pytest.mark.asyncio
async def test_task_actor_service_terminalizes_root_after_pending_notice_acceptance_success(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    record = await service.create_task(
        "root pending acceptance terminalize",
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance"))
    acceptance = service.store.get_node(final_acceptance.node_id)

    assert acceptance is not None

    root_result = NodeFinalResult(
        status="success",
        delivery_status="final",
        summary="draft ready",
        answer="draft ready",
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )
    service.node_runner._persist_result_payload(task.task_id, root.node_id, root_result)
    service.node_runner._set_execution_waiting_acceptance_state(
        task_id=task.task_id,
        execution_node_id=root.node_id,
        acceptance_node_id=acceptance.node_id,
        result_ref="artifact:root",
        result_summary="draft ready",
    )
    service.log_service.update_task_runtime_meta(
        task.task_id,
        distribution={
            "active_epoch_id": "",
            "state": "",
            "mode": "",
            "frontier_node_ids": [],
            "blocked_node_ids": [],
            "pending_notice_node_ids": [acceptance.node_id],
            "queued_epoch_count": 0,
            "pending_mailbox_count": 1,
        },
    )

    call_order: list[str] = []

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        call_order.append(node_id)
        target = service.get_node(node_id)
        assert target is not None
        assert target.node_kind == "acceptance"
        service.log_service.update_node_status(
            task_id,
            node_id,
            status="success",
            final_output="accepted",
        )
        service.log_service.update_task_runtime_meta(
            task_id,
            distribution={
                "active_epoch_id": "",
                "state": "",
                "mode": "",
                "frontier_node_ids": [],
                "blocked_node_ids": [],
                "pending_notice_node_ids": [],
                "queued_epoch_count": 0,
                "pending_mailbox_count": 0,
            },
        )
        return NodeFinalResult(
            status="success",
            delivery_status="final",
            summary="accepted",
            answer="accepted",
            evidence=[],
            remaining_work=[],
            blocking_reason="",
        )

    service.node_runner.run_node = fake_run_node  # type: ignore[method-assign]

    await service.task_actor_service.run_task(record.task_id)

    latest_task = service.get_task(record.task_id)
    latest_root = service.get_node(root.node_id)
    latest_acceptance = service.store.get_node(acceptance.node_id)

    assert latest_task is not None
    assert latest_root is not None
    assert latest_acceptance is not None
    assert call_order == [acceptance.node_id]
    assert latest_acceptance.status == "success"
    assert latest_root.status == "success"
    assert latest_root.final_output == "draft ready"
    assert latest_root.check_result == "accepted"
    assert dict((latest_root.metadata or {}).get("acceptance_handshake") or {})["state"] == "accepted"
    assert latest_task.status == "success"
    assert normalize_final_acceptance_metadata((latest_task.metadata or {}).get("final_acceptance")).status == "passed"
    assert list(service.store.list_task_runtime_frames(record.task_id) or []) == []


@pytest.mark.asyncio
async def test_task_actor_service_terminalizes_root_after_pending_notice_acceptance_failure(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    record = await service.create_task(
        "root pending acceptance reject",
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)

    assert task is not None
    assert root is not None

    final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance"))
    acceptance = service.store.get_node(final_acceptance.node_id)

    assert acceptance is not None

    root_result = NodeFinalResult(
        status="success",
        delivery_status="final",
        summary="draft ready",
        answer="draft ready",
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )
    service.node_runner._persist_result_payload(task.task_id, root.node_id, root_result)
    service.node_runner._set_execution_waiting_acceptance_state(
        task_id=task.task_id,
        execution_node_id=root.node_id,
        acceptance_node_id=acceptance.node_id,
        result_ref="artifact:root",
        result_summary="draft ready",
    )
    service.log_service.update_task_runtime_meta(
        task.task_id,
        distribution={
            "active_epoch_id": "",
            "state": "",
            "mode": "",
            "frontier_node_ids": [],
            "blocked_node_ids": [],
            "pending_notice_node_ids": [acceptance.node_id],
            "queued_epoch_count": 0,
            "pending_mailbox_count": 1,
        },
    )

    call_order: list[str] = []

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        call_order.append(node_id)
        target = service.get_node(node_id)
        assert target is not None
        assert target.node_kind == "acceptance"
        service.log_service.update_node_status(
            task_id,
            node_id,
            status="failed",
            final_output="reject once",
            failure_reason="reject once",
        )
        service.log_service.update_task_runtime_meta(
            task_id,
            distribution={
                "active_epoch_id": "",
                "state": "",
                "mode": "",
                "frontier_node_ids": [],
                "blocked_node_ids": [],
                "pending_notice_node_ids": [],
                "queued_epoch_count": 0,
                "pending_mailbox_count": 0,
            },
        )
        return NodeFinalResult(
            status="failed",
            delivery_status="final",
            summary="reject once",
            answer="reject once",
            evidence=[],
            remaining_work=[],
            blocking_reason="reject once",
        )

    service.node_runner.run_node = fake_run_node  # type: ignore[method-assign]

    await service.task_actor_service.run_task(record.task_id)

    latest_task = service.get_task(record.task_id)
    latest_root = service.get_node(root.node_id)
    latest_acceptance = service.store.get_node(acceptance.node_id)

    assert latest_task is not None
    assert latest_root is not None
    assert latest_acceptance is not None
    assert call_order == [acceptance.node_id]
    assert latest_acceptance.status == "failed"
    assert latest_root.status == "success"
    assert latest_root.final_output == "draft ready"
    assert latest_root.check_result == "reject once"
    assert dict((latest_root.metadata or {}).get("acceptance_handshake") or {})["state"] == "rejected_terminal"
    assert latest_task.status == "success"
    assert latest_task.failure_reason == "reject once"
    assert latest_task.metadata.get("failure_class") == "business_unpassed"
    assert normalize_final_acceptance_metadata((latest_task.metadata or {}).get("final_acceptance")).status == "failed"
    assert list(service.store.list_task_runtime_frames(record.task_id) or []) == []
