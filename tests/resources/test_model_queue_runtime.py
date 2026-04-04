from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from main.protocol import now_iso
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.node_turn_controller import NodeTurnController
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def test_model_key_concurrency_controller_scopes_limits_by_model_ref() -> None:
    controller = ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {"key_count": 2, "per_key_limit": 1} if model_ref in {"model:a", "model:b"} else None
    )

    a0 = controller.try_acquire_first_available(model_ref="model:a")
    a1 = controller.try_acquire_first_available(model_ref="model:a")
    a2 = controller.try_acquire_first_available(model_ref="model:a")
    b0 = controller.try_acquire_first_available(model_ref="model:b")

    assert a0 is not None
    assert a1 is not None
    assert a0.key_index == 0
    assert a1.key_index == 1
    assert a2 is None
    assert b0 is not None
    assert b0.key_index == 0

    controller.release(a0)
    a3 = controller.try_acquire_first_available(model_ref="model:a")
    assert a3 is not None
    assert a3.key_index == 0


def test_model_key_concurrency_controller_uses_per_key_limits_and_skips_disabled_keys() -> None:
    controller = ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: (
            {"key_indexes": [0, 1], "per_key_limits": {0: 3, 1: 5, 2: 0}}
            if model_ref == "model:a"
            else None
        )
    )

    leases = [controller.try_acquire_first_available(model_ref="model:a") for _ in range(9)]

    assert [lease.key_index for lease in leases if lease is not None] == [0, 0, 0, 1, 1, 1, 1, 1]
    assert leases[-1] is None


@pytest.mark.asyncio
async def test_node_turn_controller_enforces_strict_fifo_head_blocking() -> None:
    model_controller = ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {"key_count": 1, "per_key_limit": 1}
    )
    node_controller = NodeTurnController(
        model_concurrency_controller=model_controller,
        gate_supplier=lambda: True,
        poll_interval_seconds=0.05,
    )
    model_controller.configure(on_availability_changed=node_controller.poke)
    try:
        head = await node_controller.acquire_turn(task_id="task:one", node_id="node:one", model_ref="model:a")

        second_task = asyncio.create_task(
            node_controller.acquire_turn(task_id="task:two", node_id="node:two", model_ref="model:a")
        )
        third_task = asyncio.create_task(
            node_controller.acquire_turn(task_id="task:three", node_id="node:three", model_ref="model:b")
        )
        await asyncio.sleep(0.15)

        snapshot = node_controller.snapshot()
        assert second_task.done() is False
        assert third_task.done() is False
        assert snapshot["node_queue_running_count"] == 1
        assert snapshot["node_queue_waiting_count"] == 2

        model_controller.release(head.initial_model_permit)
        head.initial_model_permit = None
        node_controller.release_turn(head)

        second = await asyncio.wait_for(second_task, timeout=1.0)
        assert second.node_id == "node:two"

        model_controller.release(second.initial_model_permit)
        second.initial_model_permit = None
        node_controller.release_turn(second)

        third = await asyncio.wait_for(third_task, timeout=1.0)
        assert third.node_id == "node:three"
    finally:
        await node_controller.close()


@pytest.mark.asyncio
async def test_node_turn_controller_holds_frozen_task_requests_in_separate_fifo_until_thawed() -> None:
    model_controller = ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {"key_count": 1, "per_key_limit": 1}
    )
    frozen_tasks: set[str] = {"task:frozen"}
    node_controller = NodeTurnController(
        model_concurrency_controller=model_controller,
        gate_supplier=lambda: True,
        freeze_supplier=lambda task_id: str(task_id or "").strip() in frozen_tasks,
        poll_interval_seconds=0.05,
    )
    model_controller.configure(on_availability_changed=node_controller.poke)
    try:
        head = await node_controller.acquire_turn(task_id="task:head", node_id="node:head", model_ref="model:a")

        frozen_waiter = asyncio.create_task(
            node_controller.acquire_turn(task_id="task:frozen", node_id="node:frozen-1", model_ref="model:a")
        )
        unfrozen_waiter = asyncio.create_task(
            node_controller.acquire_turn(task_id="task:next", node_id="node:next-1", model_ref="model:a")
        )
        await asyncio.sleep(0.15)

        snapshot = node_controller.snapshot()
        assert frozen_waiter.done() is False
        assert unfrozen_waiter.done() is False
        assert snapshot["node_queue_running_count"] == 1
        assert snapshot["node_queue_waiting_count"] == 2
        assert snapshot["node_queue_frozen_count"] == 1

        model_controller.release(head.initial_model_permit)
        head.initial_model_permit = None
        node_controller.release_turn(head)

        next_lease = await asyncio.wait_for(unfrozen_waiter, timeout=1.0)
        assert next_lease.node_id == "node:next-1"

        model_controller.release(next_lease.initial_model_permit)
        next_lease.initial_model_permit = None
        node_controller.release_turn(next_lease)

        await asyncio.sleep(0.1)
        assert frozen_waiter.done() is False

        frozen_tasks.clear()
        node_controller.poke()

        thawed = await asyncio.wait_for(frozen_waiter, timeout=1.0)
        assert thawed.node_id == "node:frozen-1"
    finally:
        await node_controller.close()


def test_worker_status_payload_exposes_tool_and_node_queue_metrics(tmp_path: Path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    try:
        payload = service.worker_status_payload(
            item={
                "worker_id": "worker:test",
                "role": "task_worker",
                "status": "running",
                "updated_at": now_iso(),
                "payload": {
                    "tool_pressure_state": "normal",
                    "tool_queue_running_count": 2,
                    "tool_queue_waiting_count": 5,
                    "node_queue_running_count": 3,
                    "node_queue_waiting_count": 7,
                    "machine_pressure_available": True,
                    "worker_heartbeat_at": now_iso(),
                },
            }
        )

        assert payload["tool_queue_running_count"] == 2
        assert payload["tool_queue_waiting_count"] == 5
        assert payload["node_queue_running_count"] == 3
        assert payload["node_queue_waiting_count"] == 7
    finally:
        service.store.close()
        service.governance_store.close()
