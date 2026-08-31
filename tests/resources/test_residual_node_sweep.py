from __future__ import annotations

from pathlib import Path
from typing import Any

from main.ids import new_node_id
from main.models import NodeRecord
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


def _build_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
        execution_model_refs=["fake"],
        acceptance_model_refs=["fake"],
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


def _add_node(
    service: MainRuntimeService,
    task_id: str,
    root_node_id: str,
    *,
    status: str = "in_progress",
    metadata: dict[str, Any] | None = None,
    node_id: str | None = None,
) -> NodeRecord:
    now = now_iso()
    node = NodeRecord(
        node_id=node_id or new_node_id(),
        task_id=task_id,
        parent_node_id=root_node_id,
        root_node_id=root_node_id,
        depth=1,
        node_kind="execution",
        status=status,
        goal="residual node goal",
        prompt="residual node prompt",
        input="",
        output=[],
        check_result="",
        final_output="",
        can_spawn_children=False,
        created_at=now,
        updated_at=now,
        metadata=dict(metadata or {}),
    )
    service.store.upsert_node(node)
    return node


def _mark_task_terminal(
    service: MainRuntimeService,
    task_id: str,
    root_node_id: str,
    *,
    status: str = "success",
) -> None:
    """Mark both the root node and the task terminal, mirroring the real terminal flow."""
    now = now_iso()
    service.store.update_node(
        root_node_id,
        lambda record: record.model_copy(
            update={"status": status, "finished_at": now, "updated_at": now}
        ),
    )
    service.store.update_task(
        task_id,
        lambda record: record.model_copy(
            update={"status": status, "finished_at": now, "updated_at": now}
        ),
    )


async def test_sweep_residual_nodes_only_for_terminal_tasks(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    task = await service.create_task("sweep test", session_id="web:shared")

    residual = _add_node(service, task.task_id, task.root_node_id, status="in_progress")
    done = _add_node(service, task.task_id, task.root_node_id, status="success")

    # Task still in progress -> sweep must not touch any node.
    assert service.log_service.sweep_residual_nodes(task.task_id) == []
    assert service.store.get_node(residual.node_id).status == "in_progress"

    # Terminal task -> residual in_progress node is swept to failed; success node untouched.
    _mark_task_terminal(service, task.task_id, task.root_node_id)
    swept = service.log_service.sweep_residual_nodes(task.task_id)
    assert {swept_node.node_id for swept_node in swept} == {residual.node_id}
    assert service.store.get_node(residual.node_id).status == "failed"
    assert service.store.get_node(done.node_id).status == "success"

    # Idempotent: no residual nodes remain.
    assert service.log_service.sweep_residual_nodes(task.task_id) == []


async def test_sweep_residual_nodes_preserves_content_locator(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    task = await service.create_task("sweep ref test", session_id="web:shared")

    residual = _add_node(
        service,
        task.task_id,
        task.root_node_id,
        status="in_progress",
        metadata={"execution_trace_ref": "artifact:trace1", "result_payload_ref": "artifact:payload1"},
    )
    _mark_task_terminal(service, task.task_id, task.root_node_id)
    service.log_service.sweep_residual_nodes(task.task_id)

    updated = service.store.get_node(residual.node_id)
    assert updated.status == "failed"
    assert "task_terminal_cleanup" in updated.failure_reason
    assert "task already success" in updated.failure_reason
    assert "preserved at" in updated.failure_reason
    assert "artifact:trace1" in updated.failure_reason
    assert "artifact:payload1" in updated.failure_reason
    # The node's transcript/artifact locators are never deleted — only status changes.
    assert updated.metadata.get("execution_trace_ref") == "artifact:trace1"
    assert updated.metadata.get("result_payload_ref") == "artifact:payload1"


async def test_startup_self_heals_terminal_tasks(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    task = await service.create_task("self heal test", session_id="web:shared")

    residual = _add_node(service, task.task_id, task.root_node_id, status="in_progress")
    _mark_task_terminal(service, task.task_id, task.root_node_id)

    # Simulate a fresh worker restart re-running the startup bootstrap loop.
    service._started = False
    await service.startup()

    assert service.store.get_node(residual.node_id).status == "failed"