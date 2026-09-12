"""子树定向通知（subtree_barrier）核心行为回归。

覆盖：定向校验（终态/验收拒绝、嵌套合并、不相交并集）、任务不暂停、
延迟启动（任务/目标人工暂停）、波次状态分支、上传播收件人、验收打断
不消耗拒绝预算、dispatcher hold 语义（含根）、消息三态。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.errors import DistributionHoldError, NodePausedError
from main.models import NodeFinalResult, NodeRecord, SpawnChildSpec
from main.protocol import now_iso
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
)
from main.runtime.subtree_hold import NOTICE_INTERRUPT_REASON
from main.runtime.task_actor_service import TaskNodeDispatcher
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be used in this test: {kwargs!r}")


async def _noop_async(*args, **kwargs):
    _ = args, kwargs
    return None


def _build_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_async
    service.global_scheduler.cancel_task = _noop_async
    service.global_scheduler.wait = _noop_async
    # 后台驱动器默认关闭：波次由测试显式驱动，避免竞态。
    service.task_actor_service.ensure_scoped_epoch_driver = lambda task_id: None
    return service


def _set_spawn_operations(service: MainRuntimeService, *, node_id: str, payload: dict[str, object]) -> None:
    def _mutate(metadata: dict[str, object]) -> dict[str, object]:
        metadata["spawn_operations"] = payload
        return metadata

    service.log_service.update_node_metadata(node_id, _mutate)


async def _seed_root_with_two_live_children(service: MainRuntimeService):
    """root（等子节点）+ 两个存活执行子节点 a/b（叶子）。"""
    record = await service.create_task("子树定向通知回归", session_id="web:ceo-demo")
    task = service.get_task(record.task_id)
    root = service.store.get_node(record.root_node_id)
    assert task is not None and root is not None
    spec_a = SpawnChildSpec(goal="branch a", prompt="pa", execution_policy={"mode": "focus"})
    spec_b = SpawnChildSpec(goal="branch b", prompt="pb", execution_policy={"mode": "focus"})
    child_a = service.node_runner._create_execution_child(
        task=task, parent=root, spec=spec_a, owner_round_id="round-live", owner_entry_index=0,
    )
    child_b = service.node_runner._create_execution_child(
        task=task, parent=root, spec=spec_b, owner_round_id="round-live", owner_entry_index=1,
    )
    _set_spawn_operations(
        service,
        node_id=root.node_id,
        payload={
            "round-live": {
                "specs": [spec_a.model_dump(mode="json"), spec_b.model_dump(mode="json")],
                "entries": [
                    {"index": 0, "goal": "branch a", "child_node_id": child_a.node_id},
                    {"index": 1, "goal": "branch b", "child_node_id": child_b.node_id},
                ],
                "completed": False,
            },
        },
    )
    return record, root, child_a, child_b


async def _drive_waves(service: MainRuntimeService, task_id: str, *, max_waves: int = 8) -> str:
    outcome = "idle"
    for _ in range(max_waves):
        outcome = await service.task_actor_service._run_distribution_epoch(task_id)
        if outcome in {"completed", "failed", "idle", "deferred"}:
            return outcome
    return outcome


@pytest.mark.asyncio
async def test_targeted_notice_freezes_only_target_subtree_without_task_pause(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, root, child_a, child_b = await _seed_root_with_two_live_children(service)

        await service.task_append_notice(
            task_ids=None,
            node_ids=[child_a.node_id],
            message="仅调整 a 子树的验收口径",
            session_id="web:ceo-demo",
        )

        task = service.get_task(record.task_id)
        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        meta = dict((service.log_service.read_task_runtime_meta(record.task_id) or {}).get("distribution") or {})

        # 不再任务级暂停：冻结范围由 hold 谓词按子树强制。
        assert task is not None
        assert task.pause_requested is False
        assert task.is_paused is False
        # 屏障 = 目标子树（a 是叶子：仅 a 自身），root/b 不在其中。
        assert epoch.payload.get("target_node_ids") == [child_a.node_id]
        assert epoch.payload.get("barrier_node_ids") == [child_a.node_id]
        assert meta.get("mode") == "subtree_barrier"
        assert meta.get("target_node_ids") == [child_a.node_id]
        assert meta.get("blocked_node_ids") == [child_a.node_id]
        assert root.node_id not in (meta.get("blocked_node_ids") or [])
        assert child_b.node_id not in (meta.get("blocked_node_ids") or [])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_target_validation_rejects_terminal_and_acceptance_nodes(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, root, child_a, _child_b = await _seed_root_with_two_live_children(service)
        # 终态目标拒绝
        service.log_service.update_node_status(record.task_id, child_a.node_id, status="success", final_output="done")
        with pytest.raises(ValueError) as terminal_exc:
            await service.task_append_notice(
                task_ids=None, node_ids=[child_a.node_id], message="m", session_id="web:ceo-demo",
            )
        assert str(terminal_exc.value) == "append_notice_target_terminal"
        # 验收节点目标拒绝
        acceptance = NodeRecord(
            node_id="node:acc-1",
            task_id=record.task_id,
            parent_node_id=root.node_id,
            root_node_id=root.node_id,
            depth=1,
            node_kind="acceptance",
            status="in_progress",
            goal="acc",
            prompt="acc",
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        service.store.upsert_node(acceptance)
        with pytest.raises(ValueError) as acceptance_exc:
            await service.task_append_notice(
                task_ids=None, node_ids=[acceptance.node_id], message="m", session_id="web:ceo-demo",
            )
        assert str(acceptance_exc.value) == "append_notice_invalid_node_target"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_nested_targets_merge_and_disjoint_targets_union_barrier(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, root, child_a, child_b = await _seed_root_with_two_live_children(service)
        # 嵌套：root 覆盖 a → 合并为 root（等价原全局模式）
        await service.task_append_notice(
            task_ids=None, node_ids=[root.node_id, child_a.node_id], message="全树口径更新", session_id="web:ceo-demo",
        )
        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        assert epoch.payload.get("target_node_ids") == [root.node_id]
        assert set(epoch.payload.get("barrier_node_ids") or []) == {root.node_id, child_a.node_id, child_b.node_id}

        # 同 epoch 处于 pause_requested 时再追加：coalesce 合并消息与不相交目标
        await service.task_append_notice(
            task_ids=None, node_ids=[child_b.node_id], message="补充 b 的要求", session_id="web:ceo-demo",
        )
        epochs = service.store.list_active_task_message_distribution_epochs(record.task_id)
        assert len(epochs) == 1
        merged_payload = dict(epochs[0].payload or {})
        assert merged_payload.get("target_node_ids") == [root.node_id]
        assert merged_payload.get("queued_root_messages") == ["全树口径更新", "补充 b 的要求"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_paused_task_defers_distribution_without_auto_resume(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, _root, child_a, _child_b = await _seed_root_with_two_live_children(service)
        service.log_service.set_pause_state(record.task_id, pause_requested=True, is_paused=True)
        await service.task_append_notice(
            task_ids=None, node_ids=[child_a.node_id], message="延迟分发", session_id="web:ceo-demo",
        )
        outcome = await service.task_actor_service._run_distribution_epoch(record.task_id)
        assert outcome == "deferred"
        task = service.get_task(record.task_id)
        assert task is not None and task.pause_requested is True  # 未被自动恢复
        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        assert epoch.state == "pause_requested"  # 未推进

        service.log_service.set_pause_state(record.task_id, pause_requested=False, is_paused=False)
        outcome = await service.task_actor_service._run_distribution_epoch(record.task_id)
        assert outcome != "deferred"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_paused_target_node_defers_until_resumed(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, _root, child_a, _child_b = await _seed_root_with_two_live_children(service)
        service.log_service.set_node_pause_state(record.task_id, child_a.node_id, pause_requested=True, is_paused=True)
        await service.task_append_notice(
            task_ids=None, node_ids=[child_a.node_id], message="等待节点恢复", session_id="web:ceo-demo",
        )
        outcome = await service.task_actor_service._run_distribution_epoch(record.task_id)
        assert outcome == "deferred"
        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        assert epoch.payload.get("deferred_frontier_node_ids") == [child_a.node_id]

        service.log_service.set_node_pause_state(
            record.task_id, child_a.node_id, pause_requested=False, is_paused=False, pause_reason="", remark="",
        )
        outcome = await _drive_waves(service, record.task_id)
        assert outcome == "completed"
        # 目标自己的通知以本地待处理记录落盘（叶子=直接并入分支）
        refreshed_a = service.store.get_node(child_a.node_id)
        pending_records = list((refreshed_a.metadata or {}).get("pending_append_notice_records") or [])
        assert [item["message"] for item in pending_records] == ["等待节点恢复"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_completion_propagates_upward_to_ancestors_and_final_acceptance(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, root, child_a, _child_b = await _seed_root_with_two_live_children(service)
        await service.task_append_notice(
            task_ids=None, node_ids=[child_a.node_id], message="a 子树新约束", session_id="web:ceo-demo",
        )
        outcome = await _drive_waves(service, record.task_id)
        assert outcome == "completed"

        # 祖先（root）信箱收到转述+原文
        root_notifications = service.store.list_task_node_notifications(record.task_id, root.node_id)
        relayed = [item for item in root_notifications if "后代节点收到用户定向通知" in str(item.message or "")]
        assert len(relayed) == 1
        assert relayed[0].source_node_id == child_a.node_id
        assert "a 子树新约束" in relayed[0].message
        assert relayed[0].status == "delivered"
        # 兄弟节点 b 不会收到任何投递
        assert service.store.list_task_node_notifications(record.task_id, _child_b.node_id) == []
        # meta 清空、epoch 完成
        meta = dict((service.log_service.read_task_runtime_meta(record.task_id) or {}).get("distribution") or {})
        assert meta.get("state") == ""
        assert meta.get("blocked_node_ids") == []
        assert child_a.node_id in (meta.get("pending_notice_node_ids") or [])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_upward_propagation_informs_ancestor_acceptance_companion(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, root, child_a, _child_b = await _seed_root_with_two_live_children(service)
        # root 有一个存活的验收子节点（handshake 关联）
        acceptance = NodeRecord(
            node_id="node:acc-root",
            task_id=record.task_id,
            parent_node_id=root.node_id,
            root_node_id=root.node_id,
            depth=1,
            node_kind="acceptance",
            status="in_progress",
            goal="root acceptance",
            prompt="acc",
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        service.store.upsert_node(acceptance)

        def _set_handshake(metadata: dict) -> dict:
            metadata[ACCEPTANCE_HANDSHAKE_KEY] = {
                "state": ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
                "acceptance_node_id": acceptance.node_id,
            }
            return metadata

        service.log_service.update_node_metadata(root.node_id, _set_handshake)

        await service.task_append_notice(
            task_ids=None, node_ids=[child_a.node_id], message="a 子树新约束", session_id="web:ceo-demo",
        )
        assert await _drive_waves(service, record.task_id) == "completed"

        acc_notifications = service.store.list_task_node_notifications(record.task_id, acceptance.node_id)
        informed = [item for item in acc_notifications if "被检验节点收到用户定向通知" in str(item.message or "")]
        assert len(informed) == 1
        assert "a 子树新约束" in informed[0].message
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_acceptance_interrupt_result_does_not_consume_rejection_budget(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, root, child_a, _child_b = await _seed_root_with_two_live_children(service)
        acceptance = NodeRecord(
            node_id="node:acc-a",
            task_id=record.task_id,
            parent_node_id=child_a.node_id,
            root_node_id=root.node_id,
            depth=2,
            node_kind="acceptance",
            status="in_progress",
            goal="a acceptance",
            prompt="acc",
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        service.store.upsert_node(acceptance)

        def _set_handshake(metadata: dict) -> dict:
            metadata[ACCEPTANCE_HANDSHAKE_KEY] = {
                "state": ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
                "acceptance_node_id": acceptance.node_id,
                "rejection_count": 1,
                "max_rejections": 3,
            }
            return metadata

        service.log_service.update_node_metadata(child_a.node_id, _set_handshake)
        task = service.get_task(record.task_id)
        assert task is not None

        synthetic = NodeFinalResult(
            status="failed",
            delivery_status="blocked",
            summary="acceptance interrupted by user notice",
            answer="",
            evidence=[],
            remaining_work=[],
            blocking_reason=NOTICE_INTERRUPT_REASON,
        )
        acceptance_node = service.store.get_node(acceptance.node_id)
        result = service.node_runner._handle_acceptance_node_result(
            task=task, acceptance=acceptance_node, result=synthetic,
        )
        assert result.delivery_status == "partial"
        refreshed = service.store.get_node(child_a.node_id)
        handshake = dict((refreshed.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) or {})
        # 拒绝预算不消耗、状态回到等待执行重试、执行节点被重新激活
        assert int(handshake.get("rejection_count") or 0) == 1
        assert handshake.get("state") == "waiting_execution_retry"
        assert refreshed.status == "in_progress"
        # 不发验收→执行的交接反馈消息（用户通知本身就是消息）
        feedback = [
            item
            for item in service.store.list_task_node_notifications(record.task_id, child_a.node_id)
            if str(item.source_node_id or "") == acceptance.node_id
        ]
        assert feedback == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_dispatcher_hold_leaves_future_pending_including_root(tmp_path: Path) -> None:
    store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(root_node_id="node:root"),
        get_node=lambda node_id: SimpleNamespace(node_id=node_id, node_kind="execution", status="in_progress"),
    )

    class _HoldRunner:
        def __init__(self, exc):
            self._exc = exc

        async def run_node(self, task_id, node_id):
            raise self._exc

        def fail_paused_node(self, task_id, node_id, reason):
            return NodeFinalResult(status="failed", summary=reason)

    log_service = SimpleNamespace(update_task_runtime_meta=lambda *a, **k: None)

    # hold：根节点也保持 pending（不 set_exception）
    dispatcher = TaskNodeDispatcher(
        task_id="task:hold", store=store, log_service=log_service,
        node_runner=_HoldRunner(DistributionHoldError("task:hold", "node:root", "epoch:1")),
    )
    waiter = asyncio.create_task(dispatcher.execute_node("task:hold", "node:root"))
    await asyncio.sleep(0.05)
    assert not waiter.done(), "hold 不得解析 future（根也一样），run_task 等释放"
    # 人工暂停：根节点仍然 set_exception（旧契约不变）
    paused_dispatcher = TaskNodeDispatcher(
        task_id="task:hold", store=store, log_service=log_service,
        node_runner=_HoldRunner(NodePausedError("task:hold", "node:root")),
    )
    paused_waiter = asyncio.create_task(paused_dispatcher.execute_node("task:hold", "node:root"))
    with pytest.raises(NodePausedError):
        await asyncio.wait_for(paused_waiter, timeout=1)
    waiter.cancel()
    await dispatcher.close()
    await paused_dispatcher.close()


@pytest.mark.asyncio
async def test_message_list_three_states(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, root, child_a, _child_b = await _seed_root_with_two_live_children(service)
        # 直接构造一条投递到 child_a 的信箱通知
        service.node_runner._persist_node_notification_direct(
            task_id=record.task_id,
            epoch_id="epoch:t",
            source_node_id=root.node_id,
            target_node_id=child_a.node_id,
            message="转发给 a 的补充要求",
        )

        def _statuses() -> list[str]:
            detail = service.query_service.get_node_detail(record.task_id, child_a.node_id, detail_level="full")
            return [str(item.get("status") or "") for item in list(detail.message_list or [])]

        # 1) delivered = 待处理
        assert _statuses() == ["pending"]
        # 2) 控制/决策回合处理过 = 已消费（未并入）
        service.node_runner._mark_incoming_distribution_notifications_processed(
            task_id=record.task_id, node_id=child_a.node_id, epoch_id="epoch:t",
        )
        assert _statuses() == ["consumed"]
        # 3) 恢复路径真正注入后 = 已并入上下文
        pending = service.node_runner._pending_node_notifications(task_id=record.task_id, node_id=child_a.node_id)
        assert [item.status for item in pending] == ["consumed"]  # 未并入的仍待注入
        service.node_runner._consume_node_notifications(
            task_id=record.task_id,
            node_id=child_a.node_id,
            notification_ids=[item.notification_id for item in pending],
        )
        assert _statuses() == ["merged"]
        assert service.node_runner._pending_node_notifications(task_id=record.task_id, node_id=child_a.node_id) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_web_entry_skips_session_ownership_validation(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, _root, child_a, _child_b = await _seed_root_with_two_live_children(service)
        # 会话不匹配（web:shared vs web:ceo-demo）：工具路径拒绝、网页路径放行
        with pytest.raises(ValueError):
            await service.append_notice_to_targets(
                task_ids=None, node_ids=[child_a.node_id], message="跨会话",
                session_id="web:shared", require_session_ownership=True,
            )
        result = await service.append_notice_to_targets(
            task_ids=None, node_ids=[child_a.node_id], message="跨会话",
            session_id="", require_session_ownership=False,
        )
        assert record.task_id in result
    finally:
        await service.close()
