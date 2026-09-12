"""run_task 分发门控与子树 hold 谓词回归。

历史缺陷（ce8fd928 修复）：run_task 的分发状态集合漏掉 'barrier_draining'，
该状态期间调度重入会跳过 epoch 直接 execute_node(root)（屏障被绕过），且
离开该态的唯一路径被状态门挡住，任务可永久半卡死。

现契约（子树屏障统一分发）：run_task 不再同步驱动分发 epoch——检测到活跃
分发状态时确保单飞驱动器（ensure_scoped_epoch_driver）后继续普通执行路径；
"不得绕过屏障"的保证改由 hold 谓词在 run_node 入口与 react_loop 各安全
检查点强制（DistributionHoldError → dispatcher 保持 future pending）。
"""

from __future__ import annotations

from types import SimpleNamespace

from main.runtime.subtree_hold import resolve_subtree_hold_epoch_id
from main.runtime.task_actor_service import TaskActorService


def _make_service(state: str) -> TaskActorService:
    service = object.__new__(TaskActorService)
    calls = {"epoch": 0, "execute_root": 0, "ensure_driver": 0}
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
    service._epoch_drivers = {}  # type: ignore[attr-defined]
    service._log_service = SimpleNamespace(  # type: ignore[attr-defined]
        update_node_status=lambda *args, **kwargs: None,
        refresh_task_view=lambda *args, **kwargs: None,
        set_pause_state=lambda *args, **kwargs: None,
        set_node_pause_state=lambda *args, **kwargs: None,
    )
    service._distribution_runtime_state = lambda task_id: {"state": state}  # type: ignore[attr-defined]

    def _ensure_driver(task_id: str) -> None:
        calls["ensure_driver"] += 1

    service.ensure_scoped_epoch_driver = _ensure_driver  # type: ignore[attr-defined]

    async def _epoch(task_id: str):
        calls["epoch"] += 1
        return "completed"

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


async def test_active_distribution_states_ensure_driver_without_inline_epoch() -> None:
    for state in ("pause_requested", "barrier_requested", "paused", "barrier_draining", "distributing"):
        service = _make_service(state)
        await service.run_task("task-1")
        calls = service._calls  # type: ignore[attr-defined]
        assert calls["ensure_driver"] == 1, f"{state} 必须唤醒分发驱动器"
        assert calls["epoch"] == 0, f"{state} 不得在 run_task 内同步驱动 epoch"


async def test_normal_state_still_executes_root_without_driver() -> None:
    service = _make_service("running")
    await service.run_task("task-1")
    calls = service._calls  # type: ignore[attr-defined]
    assert calls["ensure_driver"] == 0
    assert calls["epoch"] == 0
    assert calls["execute_root"] == 1


def _node(parent: str = "") -> SimpleNamespace:
    return SimpleNamespace(parent_node_id=parent)


def test_hold_predicate_blocked_set_and_state_gates() -> None:
    nodes = {"child": _node("target"), "target": _node("root"), "root": _node(""), "outside": _node("root")}
    get_node = lambda node_id: nodes.get(node_id)  # noqa: E731
    base = {
        "active_epoch_id": "epoch:1",
        "state": "barrier_draining",
        "mode": "subtree_barrier",
        "target_node_ids": ["target"],
        "blocked_node_ids": ["target", "child"],
        "frontier_node_ids": [],
    }
    # 快照屏障命中
    assert resolve_subtree_hold_epoch_id(distribution=base, get_node=get_node, node_id="child") == "epoch:1"
    # 祖先链实时重推：不在快照里的目标子孙同样冻结
    late = dict(base, blocked_node_ids=[])
    assert resolve_subtree_hold_epoch_id(distribution=late, get_node=get_node, node_id="child") == "epoch:1"
    # 目标自身（非 distributing 态）也冻结
    assert resolve_subtree_hold_epoch_id(distribution=base, get_node=get_node, node_id="target") == "epoch:1"
    # 子树外节点与祖先不受影响
    assert resolve_subtree_hold_epoch_id(distribution=base, get_node=get_node, node_id="outside") == ""
    assert resolve_subtree_hold_epoch_id(distribution=base, get_node=get_node, node_id="root") == ""
    # 非活动状态不冻结
    for state in ("", "resume_ready", "completed"):
        assert resolve_subtree_hold_epoch_id(distribution=dict(base, state=state), get_node=get_node, node_id="child") == ""
    # failed 保持冻结（显式恢复才降级）
    assert resolve_subtree_hold_epoch_id(distribution=dict(base, state="failed"), get_node=get_node, node_id="child") == "epoch:1"


def test_hold_predicate_frontier_exemption_only_while_distributing() -> None:
    nodes = {"target": _node("root"), "root": _node("")}
    get_node = lambda node_id: nodes.get(node_id)  # noqa: E731
    base = {
        "active_epoch_id": "epoch:1",
        "state": "distributing",
        "mode": "subtree_barrier",
        "target_node_ids": ["target"],
        "blocked_node_ids": ["target"],
        "frontier_node_ids": ["target"],
    }
    # distributing 态的 frontier 成员走控制回合而非 hold
    assert resolve_subtree_hold_epoch_id(distribution=base, get_node=get_node, node_id="target") == ""
    # drain 阶段即使误入 frontier 也保持冻结
    draining = dict(base, state="barrier_draining")
    assert resolve_subtree_hold_epoch_id(distribution=draining, get_node=get_node, node_id="target") == "epoch:1"
