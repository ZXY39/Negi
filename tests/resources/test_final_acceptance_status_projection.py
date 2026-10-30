"""I2：`task.metadata.final_acceptance.status` 是握手的投影，不是独立副本。

事故 task:fb87a120fbe9：01:15:19 验收节点落 pass、任务终态；01:15:20 重复分派把
验收节点重置回 in_progress，同时分派路径把任务级字段从 passed 改写成 running。
本文件锁定三条契约：

- 映射函数覆盖握手的每个状态，且 accepted 无论节点行是什么都读 passed；
- 分派路径只写 node_id，不再产生 running；
- 节点终态传播按握手投影，而不是按节点行 success/failed 猜。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from main.models import normalize_final_acceptance_metadata
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
    ACCEPTANCE_STATE_IDLE,
    ACCEPTANCE_STATE_REJECTED_TERMINAL,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
    derive_final_acceptance_status,
)
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _build_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
        execution_model_refs=["fake"],
        acceptance_model_refs=["fake"],
    )

    async def _noop_enqueue_task(_task_id: str) -> None:
        return None

    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


async def _task_with_acceptance(service: MainRuntimeService) -> tuple[str, str, str]:
    record = await service.create_task(
        "projection matrix",
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None
    acceptance_id = str(
        normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance")).node_id or ""
    ).strip()
    assert acceptance_id
    return record.task_id, root.node_id, acceptance_id


def _set_handshake_state(service: MainRuntimeService, *, node_id: str, state: str, acceptance_node_id: str) -> None:
    service.node_runner._update_execution_acceptance_handshake(
        node_id=node_id,
        state=state,
        acceptance_node_id=acceptance_node_id,
        rejection_count=0,
        latest_execution_result_ref="artifact:root",
        latest_execution_result_summary="draft",
        latest_rejection_feedback_ref="",
        latest_rejection_feedback_summary="",
    )


def _stored_status(service: MainRuntimeService, task_id: str) -> str:
    task = service.get_task(task_id)
    assert task is not None
    return normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance")).status


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ACCEPTANCE_STATE_ACCEPTED, "passed"),
        (ACCEPTANCE_STATE_REJECTED_TERMINAL, "failed"),
        (ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE, "canceled_by_execution_failure"),
        (ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY, "waiting_execution_retry"),
        (ACCEPTANCE_STATE_WAITING_ACCEPTANCE, "waiting_acceptance"),
        (ACCEPTANCE_STATE_IDLE, "pending"),
        ("unknown-state", "pending"),
    ],
)
def test_derive_final_acceptance_status_projects_every_handshake_state(state: str, expected: str) -> None:
    assert derive_final_acceptance_status(state) == expected


@pytest.mark.asyncio
async def test_task_state_projects_the_handshake_including_the_incident_shape(tmp_path: Path) -> None:
    """accepted + 验收节点仍在 in_progress → passed；绝不能按节点行读成 running。"""
    service = _build_service(tmp_path)
    try:
        task_id, root_id, acceptance_id = await _task_with_acceptance(service)
        acceptance = service.store.get_node(acceptance_id)
        assert acceptance is not None and acceptance.status == "in_progress"

        for state in (
            ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
            ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
            ACCEPTANCE_STATE_ACCEPTED,
            ACCEPTANCE_STATE_REJECTED_TERMINAL,
            ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
            ACCEPTANCE_STATE_IDLE,
        ):
            _set_handshake_state(
                service,
                node_id=root_id,
                state=state,
                acceptance_node_id=acceptance_id,
            )
            service.node_runner._set_task_final_acceptance_state(
                task_id=task_id,
                acceptance_node_id=acceptance_id,
            )
            assert _stored_status(service, task_id) == derive_final_acceptance_status(state)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_dispatch_path_writes_no_status_and_never_produces_running(tmp_path: Path) -> None:
    """T4：分派决策只登记 node_id；等待验收时字段保持 waiting_acceptance。"""
    service = _build_service(tmp_path)
    try:
        task_id, root_id, acceptance_id = await _task_with_acceptance(service)
        service.node_runner._set_execution_waiting_acceptance_state(
            task_id=task_id,
            execution_node_id=root_id,
            acceptance_node_id=acceptance_id,
            result_ref="artifact:root",
            result_summary="draft",
        )
        assert _stored_status(service, task_id) == ACCEPTANCE_STATE_WAITING_ACCEPTANCE

        task = service.get_task(task_id)
        root = service.get_node(root_id)
        assert task is not None and root is not None
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance"))
        await service.task_actor_service._get_or_create_final_acceptance_node(
            task_id=task_id,
            task=task,
            root=root,
            final_acceptance=final_acceptance,
        )

        assert _stored_status(service, task_id) == ACCEPTANCE_STATE_WAITING_ACCEPTANCE
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_propagation_projects_the_handshake_not_the_node_row(tmp_path: Path) -> None:
    """验收节点落了 success 时，任务级字段跟随握手：accepted → passed。"""
    service = _build_service(tmp_path)
    try:
        task_id, root_id, acceptance_id = await _task_with_acceptance(service)
        _set_handshake_state(
            service,
            node_id=root_id,
            state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
            acceptance_node_id=acceptance_id,
        )
        service.log_service.update_node_status(
            task_id,
            acceptance_id,
            status="success",
            final_output="verdict: pass",
        )
        assert _stored_status(service, task_id) == ACCEPTANCE_STATE_WAITING_ACCEPTANCE

        root = service.store.get_node(root_id)
        acceptance = service.store.get_node(acceptance_id)
        assert root is not None and acceptance is not None
        service.log_service._sync_final_acceptance_state(
            task_id=task_id,
            acceptance_node=acceptance,
            accepted_node=root,
            status="passed",
        )
        _set_handshake_state(
            service,
            node_id=root_id,
            state=ACCEPTANCE_STATE_ACCEPTED,
            acceptance_node_id=acceptance_id,
        )
        root = service.store.get_node(root_id)
        assert root is not None
        service.log_service._sync_final_acceptance_state(
            task_id=task_id,
            acceptance_node=acceptance,
            accepted_node=root,
            status="passed",
        )
        assert _stored_status(service, task_id) == "passed"
        assert service.store.get_node(root_id).check_result
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_rejected_terminal_projection_reads_failed_without_passing_through_the_node_row(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root_id, acceptance_id = await _task_with_acceptance(service)
        _set_handshake_state(
            service,
            node_id=root_id,
            state=ACCEPTANCE_STATE_REJECTED_TERMINAL,
            acceptance_node_id=acceptance_id,
        )
        service.node_runner._set_task_final_acceptance_state(
            task_id=task_id,
            acceptance_node_id=acceptance_id,
        )
        assert _stored_status(service, task_id) == "failed"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_terminal_propagation_keeps_its_blocked_failure_early_return(tmp_path: Path) -> None:
    """普通验收拒绝没有次数上限：节点 failed 不得借投影把任务级字段写成 failed。"""
    service = _build_service(tmp_path)
    try:
        task_id, root_id, acceptance_id = await _task_with_acceptance(service)
        _set_handshake_state(
            service,
            node_id=root_id,
            state=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
            acceptance_node_id=acceptance_id,
        )
        service.node_runner._set_task_final_acceptance_state(
            task_id=task_id,
            acceptance_node_id=acceptance_id,
        )
        assert _stored_status(service, task_id) == ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY

        root = service.store.get_node(root_id)
        acceptance = service.store.get_node(acceptance_id)
        assert root is not None and acceptance is not None
        service.log_service._sync_final_acceptance_state(
            task_id=task_id,
            acceptance_node=acceptance.model_copy(update={"status": "failed"}),
            accepted_node=root,
            status="failed",
        )
        assert _stored_status(service, task_id) == ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_setter_keeps_final_execution_output_ownership(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root_id, acceptance_id = await _task_with_acceptance(service)
        _set_handshake_state(
            service,
            node_id=root_id,
            state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
            acceptance_node_id=acceptance_id,
        )
        service.node_runner._set_task_final_acceptance_state(
            task_id=task_id,
            acceptance_node_id=acceptance_id,
            final_execution_output="final board",
        )
        task = service.get_task(task_id)
        assert task is not None
        assert task.metadata.get("final_execution_output") == "final board"
        assert _stored_status(service, task_id) == ACCEPTANCE_STATE_WAITING_ACCEPTANCE
    finally:
        await service.close()
