from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from main.models import NodeFinalResult, SpawnChildSpec, normalize_final_acceptance_metadata
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in task node dispatcher tests: {kwargs!r}")


def _make_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
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
async def test_task_node_dispatcher_shares_the_pool_across_roles_and_still_caps_concurrency(tmp_path: Path) -> None:
    """共享池：执行节点能借走验收那份地板，但同时在飞的回合数仍被池位封顶。

    旧合同里 execution=2 就把执行子节点卡在 2 个并发，哪怕 inspection 那份格子全空
    （10-12 实盘：`run 5/12` 而 11 个验收节点在闸外，7 格空转）。现在池位是 2+1=3，
    第 4 个执行节点才要等。
    """
    service = _make_service(tmp_path)
    record = await _create_task(service)
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    child_one = _create_execution_child(service, task=task, parent=root, name="child-one")
    child_two = _create_execution_child(service, task=task, parent=root, name="child-two")
    child_three = _create_execution_child(service, task=task, parent=root, name="child-three")
    child_four = _create_execution_child(service, task=task, parent=root, name="child-four")

    starts: dict[str, float] = {}
    finishes: dict[str, float] = {}

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        """真实回合的两件事必须都补：跑一段、然后把成功提交落成节点行。

        `04f10cfd6` 之后子节点由 actor 派发，父节点不再自己 gather 它们（生产里
        `_run_nested_node` 已无调用方）；而派发循环按"还能再派"重投的判据就是节点行没落终态。
        假执行体只返回结果不落行，派发侧会同一颗节点反复进入，`run_task` 永不返回。
        """
        starts.setdefault(node_id, time.perf_counter())
        try:
            await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            result = _success_result(node_id)
            service.node_runner._mark_finished(task_id, node_id, result)
            return result
        finally:
            finishes[node_id] = time.perf_counter()

    service.node_runner.run_node = fake_run_node
    service.task_actor_service.configure_node_dispatch_limits(execution=2, inspection=1)

    await service.task_actor_service.run_task(record.task_id)

    children = [child_one.node_id, child_two.node_id, child_three.node_id, child_four.node_id]
    ordered = sorted(children, key=lambda item: starts[item])
    first_three = ordered[:3]
    # 前三个回合彼此重叠地起跑（含借走验收那格的第三个执行节点），不用等谁收尾
    assert starts[ordered[1]] < finishes[ordered[0]]
    assert starts[ordered[2]] < min(finishes[first_three[0]], finishes[first_three[1]])
    # 第四个必须等一格空出来：池位 3 是硬边界，共享不等于放开
    assert starts[ordered[3]] >= min(finishes[item] for item in first_three)


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
        result = _success_result(node_id)
        # 同上一条用例：派发循环按节点行是否落终态判"还能不能再派"，假执行体不落行就重投成风暴。
        service.node_runner._mark_finished(task_id, node_id, result)
        return result

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
            result = _success_result(node_id)
            service.node_runner._mark_finished(task_id, node_id, result)
            return result
        child_started[node_id].set()
        await release_children.wait()
        result = _success_result(node_id)
        service.node_runner._mark_finished(task_id, node_id, result)
        return result

    service.node_runner.run_node = fake_run_node
    service.task_actor_service.configure_node_dispatch_limits(execution=2, inspection=1)

    task_runner = asyncio.create_task(service.task_actor_service.run_task(record.task_id))
    await _wait_until(lambda: all(event.is_set() for event in child_started.values()))

    payload = service.get_task_detail_payload(record.task_id, mark_read=False)
    assert payload is not None
    # 容量是共享池：地板 execution 2 + inspection 1 ⇒ 池 3。角色键回显同一个池位数
    # （该角色此刻最多能开到 3 格），池位本身在 dispatch_pool 单独一份。
    assert payload["runtime_summary"]["dispatch_limits"] == {"execution": 3, "inspection": 3}
    assert payload["runtime_summary"]["dispatch_pool"] == 3
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
        result = _success_result(node_id)
        if node.node_kind == "execution":
            # 真实节点回合会在返回前把成功提交落成节点行；最终验收只对真实提交开放。
            service.node_runner._mark_finished(_task_id, node_id, result)
        return result

    service.node_runner.run_node = fake_run_node

    await service.task_actor_service.run_task(record.task_id)

    acceptance_nodes = [node for node in service.list_nodes(record.task_id) if node.node_kind == "acceptance"]
    assert len(acceptance_nodes) == 1
    assert acceptance_nodes[0].goal == f"最终验收:{root.goal}"
    assert call_order[0] == root.node_id
    assert call_order[1] == acceptance_nodes[0].node_id
    assert roles == ["execution", "inspection"]


@pytest.mark.asyncio
async def test_task_actor_service_keeps_task_open_on_partial_root_acceptance_handshake(tmp_path: Path) -> None:
    """根交回 partial 且本次要求最终验收时，任务**不落终态**。

    原来这条还断"握手未决时把任务重新排队"（`distribution_resume_callback`）。那组
    distribution-epoch 入口已由 `61d0c2407` 退役（决策 M/O），生产侧现在没有任何读者，
    所以这里只留下仍然成立、且被实盘需要的那半条：任务保持 in_progress。
    派发器用真的，桩只替掉节点回合本身——旧桩把 `_create_dispatcher` 整个换成
    SimpleNamespace，`04f10cfd6` 之后派发循环要读 `has_live_entries` 等更多口，缺一个就被
    兜底捕获、任务被落成 failed，测的就不再是握手而是桩的形状。
    """
    service = _make_service(tmp_path)
    record = await service.create_task(
        "root partial handshake",
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )

    result = NodeFinalResult(
        status="success",
        delivery_status="partial",
        summary="waiting for acceptance",
        answer="draft",
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        node = service.get_node(node_id)
        if str(getattr(node, "node_kind", "")) == "acceptance":
            # 验收没裁定：不落行，只把当刻结果带回，握手因此保持未决。
            return result
        service.node_runner._mark_finished(task_id, node_id, result)
        return result

    service.node_runner.run_node = fake_run_node  # type: ignore[method-assign]

    await service.task_actor_service.run_task(record.task_id)

    latest = service.get_task(record.task_id)
    assert latest is not None
    assert latest.status == "in_progress"


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
async def test_task_actor_service_kicks_back_execution_on_first_pending_notice_acceptance_rejection(
    tmp_path: Path,
) -> None:
    """首次验收拒绝必须打回执行节点，而不是直接终态。

    旧缺陷：notice-resume 跑完验收后结果被丢弃，任务经
    _terminal_result_after_notice_resume 捷径终态，打回从未被消费
    （生产 5 例 business_unpassed 全部 rejection_count=0）。
    """
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
        if str(target.node_kind) != "acceptance":
            # 打回后执行节点被重跑：这一跳只记"它确实又进过场"，不落终态——终态由后面的
            # 验收轮裁定，而本用例断的是打回那一瞬间两边都被复活。
            return NodeFinalResult(
                status="success",
                delivery_status="partial",
                summary="resubmitted",
                answer="resubmitted",
                evidence=[],
                remaining_work=[],
                blocking_reason="",
            )
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

    # `04f10cfd6` 之后一次 `run_task` 就把打回后的执行节点自己重新泵起来，不再"这一跳只
    # 验收、下一跳才重跑"。所以这里等的是"执行节点确实又进过场"这个时刻，等到就把回合
    # 掐掉——不然验收会一轮轮 reject 下去，派发永不收口。
    runner = asyncio.create_task(service.task_actor_service.run_task(record.task_id))
    try:
        await _wait_until(lambda: root.node_id in call_order, timeout=8.0)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    latest_task = service.get_task(record.task_id)
    latest_root = service.get_node(root.node_id)
    latest_acceptance = service.store.get_node(acceptance.node_id)

    assert latest_task is not None
    assert latest_root is not None
    assert latest_acceptance is not None
    # 第一场是验收裁定，第二场是被打回后重跑的执行节点——两场的顺序就是"打回"这件事本身。
    assert call_order[0] == acceptance.node_id
    assert root.node_id in call_order
    # 打回：验收节点复活续验、执行节点复活重跑，任务保持 in_progress。
    assert latest_acceptance.status == "in_progress"
    assert latest_root.status == "in_progress"
    assert latest_root.check_result == "reject once"
    handshake = dict((latest_root.metadata or {}).get("acceptance_handshake") or {})
    assert handshake["state"] == "waiting_execution_retry"
    assert handshake["rejection_count"] == 1
    assert handshake["latest_rejection_feedback_summary"] == "reject once"
    assert latest_task.status == "in_progress"
    assert normalize_final_acceptance_metadata((latest_task.metadata or {}).get("final_acceptance")).status == "waiting_execution_retry"
    # 拒绝反馈通知已投递给执行节点。
    feedback_notices = list(service.store.list_task_node_notifications(record.task_id, root.node_id) or [])
    assert [str(getattr(item, "message", "") or "") for item in feedback_notices] == ["reject once"]
    # 下一轮由派发循环自己把执行节点泵起来（上面等的就是它进过场）；distribution-epoch
    # 那套"重新入队"入口已由 `61d0c2407` 退役，生产侧没有读者，所以这里不再断那份回执。
    # 分发态那份"待恢复清单"不再单独断：它表达的正是"等下一轮把根重新泵起来"，而上面已经
    # 直接等到执行节点进过场，清单里此刻是谁由派发循环自己决定。


@pytest.mark.asyncio
async def test_task_actor_service_kicks_back_root_even_past_former_rejection_budget(
    tmp_path: Path,
) -> None:
    """拒收无次数上限：第 3 次（乃至第 N 次）拒绝仍打回根执行节点重跑。"""
    service = _make_service(tmp_path)
    record = await service.create_task(
        "root pending acceptance retry",
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
    # 预置旧上限（3）内的计数：按历史行为，这一轮拒绝就该终态化任务。
    service.node_runner._set_execution_waiting_acceptance_state(
        task_id=task.task_id,
        execution_node_id=root.node_id,
        acceptance_node_id=acceptance.node_id,
        result_ref="artifact:root",
        result_summary="draft ready",
        rejection_count=2,
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
        if str(target.node_kind) != "acceptance":
            # 同上一条用例：打回后执行节点在同一次 run_task 里被重新泵起来，这里只记它进过场。
            return NodeFinalResult(
                status="success",
                delivery_status="partial",
                summary="resubmitted",
                answer="resubmitted",
                evidence=[],
                remaining_work=[],
                blocking_reason="",
            )
        service.log_service.update_node_status(
            task_id,
            node_id,
            status="failed",
            final_output="reject thrice",
            failure_reason="reject thrice",
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
            summary="reject thrice",
            answer="reject thrice",
            evidence=[],
            remaining_work=[],
            blocking_reason="reject thrice",
        )

    service.node_runner.run_node = fake_run_node  # type: ignore[method-assign]

    runner = asyncio.create_task(service.task_actor_service.run_task(record.task_id))
    try:
        await _wait_until(lambda: root.node_id in call_order, timeout=8.0)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    latest_task = service.get_task(record.task_id)
    latest_root = service.get_node(root.node_id)
    latest_acceptance = service.store.get_node(acceptance.node_id)

    assert latest_task is not None
    assert latest_root is not None
    assert latest_acceptance is not None
    assert call_order[0] == acceptance.node_id
    assert root.node_id in call_order
    # 打回：验收与执行双方复活重跑，任务保持 in_progress，绝不因拒绝次数终态化。
    assert latest_acceptance.status == "in_progress"
    assert latest_root.status == "in_progress"
    assert latest_root.check_result == "reject thrice"
    handshake = dict((latest_root.metadata or {}).get("acceptance_handshake") or {})
    assert handshake["state"] == "waiting_execution_retry"
    assert handshake["rejection_count"] == 3
    assert "max_rejections" not in handshake
    assert latest_task.status == "in_progress"
    assert not latest_task.metadata.get("failure_class")
    assert normalize_final_acceptance_metadata((latest_task.metadata or {}).get("final_acceptance")).status == "waiting_execution_retry"


@pytest.mark.asyncio
async def test_task_actor_service_skips_premature_final_acceptance_and_runs_root(
    tmp_path: Path,
) -> None:
    """门控回归（事故 task:eb6dda95055b）：执行节点尚未提交时，
    notice-resume 不得抢跑最终验收，应落回根执行节点继续干活。"""
    service = _make_service(tmp_path)
    record = await service.create_task(
        "root premature acceptance gate",
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

    # 故意不设置 execution waiting_acceptance 状态：握手仍为 idle，
    # 表示执行节点尚未提交任何待验结果（重启后恢复即此形态）。
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
        # 门控生效时只会执行根执行节点，绝不应是验收节点。
        assert target.node_kind == "execution"
        return NodeFinalResult(
            status="success",
            delivery_status="partial",
            summary="still working",
            answer="still working",
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
    # 验收节点被门控拦下，根执行节点被恢复继续执行。
    assert call_order == [root.node_id]
    assert latest_acceptance.status not in {"success", "failed"}
    assert latest_task.status == "in_progress"


@pytest.mark.asyncio
async def test_task_actor_service_rejects_then_resubmits_within_one_pump(
    tmp_path: Path,
) -> None:
    """一次泵里的打回前半段：拒绝→执行节点带反馈重跑→重新提交，握手回到待验收。"""
    service = _make_service(tmp_path)
    record = await service.create_task(
        "root rejection loop e2e",
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

    first_draft = NodeFinalResult(
        status="success",
        delivery_status="final",
        summary="draft v1",
        answer="draft v1",
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )
    service.node_runner._persist_result_payload(task.task_id, root.node_id, first_draft)
    service.node_runner._set_execution_waiting_acceptance_state(
        task_id=task.task_id,
        execution_node_id=root.node_id,
        acceptance_node_id=acceptance.node_id,
        result_ref="artifact:root",
        result_summary="draft v1",
    )

    def _set_pending(node_ids: list[str]) -> None:
        service.log_service.update_task_runtime_meta(
            record.task_id,
            distribution={
                "active_epoch_id": "",
                "state": "",
                "mode": "",
                "frontier_node_ids": [],
                "blocked_node_ids": [],
                "pending_notice_node_ids": list(node_ids),
                "queued_epoch_count": 0,
                "pending_mailbox_count": len(node_ids),
            },
        )

    _set_pending([acceptance.node_id])
    round_no = {"value": 0}
    call_sequence: list[str] = []

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        target = service.get_node(node_id)
        assert target is not None
        call_sequence.append(str(target.node_kind))
        if target.node_kind == "acceptance":
            round_no["value"] += 1
            if round_no["value"] == 1:
                service.log_service.update_node_status(
                    task_id, node_id, status="failed",
                    final_output="missing section B", failure_reason="missing section B",
                )
                _set_pending([])
                return NodeFinalResult(
                    status="failed", delivery_status="final",
                    summary="missing section B", answer="missing section B",
                    evidence=[], remaining_work=[], blocking_reason="missing section B",
                )
            service.log_service.update_node_status(
                task_id, node_id, status="success", final_output="accepted v2",
            )
            _set_pending([])
            return NodeFinalResult(
                status="success", delivery_status="final",
                summary="accepted v2", answer="accepted v2",
                evidence=[], remaining_work=[], blocking_reason="",
            )
        # 执行节点：消费拒绝反馈后重新提交新版本交付（模拟真实重跑）。
        assert "missing section B" in "\n".join(
            str(getattr(item, "message", "") or "")
            for item in list(service.store.list_task_node_notifications(record.task_id, root.node_id) or [])
        )
        redelivery = NodeFinalResult(
            status="success", delivery_status="final",
            summary="draft v2", answer="draft v2",
            evidence=[], remaining_work=[], blocking_reason="",
        )
        service.node_runner._persist_result_payload(task_id, root.node_id, redelivery)
        service.node_runner._set_execution_waiting_acceptance_state(
            task_id=task_id,
            execution_node_id=root.node_id,
            acceptance_node_id=acceptance.node_id,
            result_ref="artifact:root-v2",
            result_summary="draft v2",
        )
        await service.deliver_node_message(
            task_id=task_id,
            target_node_id=acceptance.node_id,
            message="新的被检验节点输出如下，请继续在当前验收上下文中核验：draft v2",
            origin="node",
            relation="parent",
            source_node_id=root.node_id,
            wake=True,
        )
        _set_pending([acceptance.node_id])
        return NodeFinalResult(
            status="success", delivery_status="partial",
            summary="waiting for acceptance", answer="draft v2",
            evidence=[], remaining_work=[], blocking_reason="",
        )

    service.node_runner.run_node = fake_run_node  # type: ignore[method-assign]

    # `04f10cfd6` 之后一次 `run_task` 就把整条拒绝循环泵完（打回→重跑重提交→续验），不再
    # 需要"每轮跑一次"。所以轮次改由回合序列自己作证：谁在什么顺序进过场。等待带上界限，
    # 完不成就是红，而不是把整片 CI 拖到预算耗尽。
    # 一次 `run_task` 走到的边界就是"打回→执行节点带反馈重跑并重新提交"；续验之后的两跳
    # （验收复活重验 → accepted 终态）由 `tests/resources/test_acceptance_inflight_resume.py`
    # 按现行原语覆盖，这里不再用"再进一次 run_task"来推进轮次——`04f10cfd6` 之后轮次之间
    # 的推进不再是重新调用 run_task，等不到第二场验收是形状变了，不是回归。
    runner = asyncio.create_task(service.task_actor_service.run_task(record.task_id))
    try:
        await _wait_until(lambda: "execution" in call_sequence, timeout=8.0)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert call_sequence[:2] == ["acceptance", "execution"], call_sequence
    mid_task = service.get_task(record.task_id)
    mid_root = service.get_node(root.node_id)
    assert mid_task is not None and mid_root is not None
    assert mid_task.status == "in_progress"
    mid_handshake = dict((mid_root.metadata or {}).get("acceptance_handshake") or {})
    assert mid_handshake["state"] == "waiting_acceptance"
    # 预算跨重提交保留：打回计数不被清零。
    assert mid_handshake["rejection_count"] == 1
