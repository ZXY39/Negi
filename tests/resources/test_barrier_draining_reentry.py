"""barrier_draining 重入回归测试。

修复缺陷：run_task 的分发状态集合漏了 'barrier_draining'，该状态期间重入
会跳过 epoch 直接 execute_node(root)（屏障被绕过）或任务永久半卡死
（离开该态的唯一路径被状态门挡住）。与 _resume_pending_notice_nodes 的
集合保持一致。
"""

from __future__ import annotations

from types import SimpleNamespace

from main.runtime.task_actor_service import TaskActorService


def _make_service(state: str) -> TaskActorService:
    service = object.__new__(TaskActorService)
    calls = {"epoch": 0, "execute_root": 0}
    service._calls = calls  # type: ignore[attr-defined]

    task = SimpleNamespace(
        task_id="task-1",
        root_node_id="root-1",
        is_paused=False,
        pause_requested=False,
        status="running",
    )
    root = SimpleNamespace(node_id="root-1", status="in_progress")

    class _StoreStub:
        def get_task(self, task_id: str):
            return task

        def get_node(self, node_id: str):
            return root

    class _DispatcherStub:
        async def execute_node(self, task_id: str, node_id: str):
            calls["execute_root"] += 1
            return SimpleNamespace(status="success", delivery_status="final")

        async def close(self) -> None:
            return None

    service._store = _StoreStub()  # type: ignore[attr-defined]
    service._stall_notifier = None  # type: ignore[attr-defined]
    service._dispatchers = {}  # type: ignore[attr-defined]
    service._log_service = SimpleNamespace(  # type: ignore[attr-defined]
        update_node_status=lambda *args, **kwargs: None,
        refresh_task_view=lambda *args, **kwargs: None,
        set_pause_state=lambda *args, **kwargs: None,
        set_node_pause_state=lambda *args, **kwargs: None,
    )
    service._distribution_runtime_state = lambda task_id: {"state": state}  # type: ignore[attr-defined]

    async def _epoch(task_id: str):
        calls["epoch"] += 1
        return {"completed": True}

    async def _resume_pending_notice_nodes(task_id: str) -> bool:
        return False

    async def _run_final_acceptance_if_needed(task_id: str):
        return SimpleNamespace(
            status="success",
            delivery_status="final",
            output="",
            failure_text="",
        )

    service._run_distribution_epoch = _epoch  # type: ignore[attr-defined]
    service._resume_pending_notice_nodes = _resume_pending_notice_nodes  # type: ignore[attr-defined]
    service._run_final_acceptance_if_needed = _run_final_acceptance_if_needed  # type: ignore[attr-defined]
    service._create_dispatcher = lambda task_id: _DispatcherStub()  # type: ignore[attr-defined]
    return service


async def test_barrier_draining_reentry_routes_to_distribution_epoch() -> None:
    service = _make_service("barrier_draining")
    await service.run_task("task-1")
    calls = service._calls  # type: ignore[attr-defined]
    assert calls["epoch"] == 1, "barrier_draining 重入必须走分发 epoch"
    assert calls["execute_root"] == 0, "不得绕过屏障直接执行根节点"


async def test_normal_state_still_executes_root() -> None:
    service = _make_service("running")
    await service.run_task("task-1")
    calls = service._calls  # type: ignore[attr-defined]
    assert calls["epoch"] == 0
    assert calls["execute_root"] == 1
