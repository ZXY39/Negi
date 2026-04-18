from __future__ import annotations

from pathlib import Path
import pytest

from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.tool_visibility import CEO_FIXED_BUILTIN_TOOL_NAMES, NODE_FIXED_BUILTIN_TOOL_NAMES
from main.models import TaskMessageDistributionEpoch, TaskNodeNotification
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore


def test_task_message_distribution_storage_round_trip(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        record = TaskMessageDistributionEpoch(
            epoch_id="epoch:001",
            task_id="task:demo",
            root_node_id="node:root",
            root_message="CEO notice",
            state="queued",
            created_at="2026-04-18T10:00:00+08:00",
            paused_at="",
            distributed_at="2026-04-18T10:00:05+08:00",
            completed_at="",
            error_text="",
            payload={
                "frontier_node_ids": ["node:root", "node:child"],
                "metadata": {"reason": "append_notice"},
            },
        )

        stored = store.upsert_task_message_distribution_epoch(record)
        fetched = store.get_task_message_distribution_epoch(record.task_id, record.epoch_id)
        active = store.list_active_task_message_distribution_epochs(record.task_id)

        assert stored == record
        assert fetched is not None
        assert fetched.epoch_id == record.epoch_id
        assert fetched.root_message == "CEO notice"
        assert fetched.state == "queued"
        assert fetched.payload == {
            "frontier_node_ids": ["node:root", "node:child"],
            "metadata": {"reason": "append_notice"},
        }
        assert [item.epoch_id for item in active] == [record.epoch_id]
    finally:
        store.close()


def test_task_node_notification_storage_round_trip(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        epoch = TaskMessageDistributionEpoch(
            epoch_id="epoch:002",
            task_id="task:demo",
            root_node_id="node:root",
            root_message="CEO notice",
            state="queued",
            created_at="2026-04-18T10:01:00+08:00",
            paused_at="",
            distributed_at="",
            completed_at="",
            error_text="",
            payload={"frontier_node_ids": ["node:child"]},
        )
        store.upsert_task_message_distribution_epoch(epoch)

        notification = TaskNodeNotification(
            notification_id="notif:001",
            task_id="task:demo",
            node_id="node:child",
            epoch_id="epoch:002",
            source_node_id="node:root",
            message="Please review the new notice.",
            status="delivered",
            created_at="2026-04-18T10:01:05+08:00",
            delivered_at="2026-04-18T10:01:06+08:00",
            consumed_at="",
            payload={
                "channel": "mailbox",
                "priority": "normal",
            },
        )

        stored = store.upsert_task_node_notification(notification)
        task_notifications = store.list_task_node_notifications(notification.task_id, notification.node_id)
        epoch_notifications = store.list_task_epoch_notifications(notification.task_id, notification.epoch_id)

        assert stored == notification
        assert [item.notification_id for item in task_notifications] == [notification.notification_id]
        assert [item.notification_id for item in epoch_notifications] == [notification.notification_id]
        assert task_notifications[0].message == "Please review the new notice."
        assert task_notifications[0].status == "delivered"
        assert task_notifications[0].payload == {
            "channel": "mailbox",
            "priority": "normal",
        }
    finally:
        store.close()


def test_task_append_notice_contract_supports_task_ids_and_node_ids() -> None:
    from main.service.task_append_notice_contract import build_task_append_notice_parameters

    schema = build_task_append_notice_parameters()
    props = dict(schema.get("properties") or {})

    assert "task_ids" in props
    assert "node_ids" in props
    assert "message" in props


def test_ceo_fixed_builtin_tools_include_task_append_notice() -> None:
    assert "task_append_notice" in set(CEO_FIXED_BUILTIN_TOOL_NAMES)
    assert "task_append_notice" not in set(NODE_FIXED_BUILTIN_TOOL_NAMES)


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be used in this test: {kwargs!r}")


async def _noop_async(*args, **kwargs):
    _ = args, kwargs
    return None


def _build_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_async
    service.global_scheduler.cancel_task = _noop_async
    service.global_scheduler.wait = _noop_async
    return service


@pytest.mark.asyncio
async def test_task_append_notice_requests_pause_then_creates_distribution_epoch(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")

        result = await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="新增董事会验收格式",
            session_id="web:ceo-demo",
        )

        task = service.get_task(record.task_id)
        epochs = service.store.list_active_task_message_distribution_epochs(record.task_id)
        runtime_meta = service.log_service.read_task_runtime_meta(record.task_id) or {}
        distribution = dict(runtime_meta.get("distribution") or {})

        assert isinstance(result, str)
        assert result.startswith(f"已向任务 {record.task_id} 追加通知")
        assert "创建任务成功" not in result
        assert task is not None
        assert task.pause_requested is True
        assert task.is_paused is True
        assert len(epochs) == 1
        assert epochs[0].state == "pause_requested"
        assert epochs[0].root_node_id == record.root_node_id
        assert epochs[0].root_message == "新增董事会验收格式"
        assert distribution == {
            "active_epoch_id": epochs[0].epoch_id,
            "state": "pause_requested",
            "frontier_node_ids": [],
            "queued_epoch_count": 1,
            "pending_mailbox_count": 0,
        }
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_append_notice_coalesces_root_messages_before_distribution_starts(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="新增董事会验收格式",
            session_id="web:ceo-demo",
        )
        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="必须补充风险分层",
            session_id="web:ceo-demo",
        )

        epochs = service.store.list_active_task_message_distribution_epochs(record.task_id)

        assert len(epochs) == 1
        assert epochs[0].state == "pause_requested"
        assert epochs[0].root_message == "新增董事会验收格式"
        assert epochs[0].payload.get("queued_root_messages") == [
            "新增董事会验收格式",
            "必须补充风险分层",
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_append_notice_queues_next_epoch_while_distribution_is_active(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="新增董事会验收格式",
            session_id="web:ceo-demo",
        )
        epochs = service.store.list_active_task_message_distribution_epochs(record.task_id)
        active_epoch = epochs[0].model_copy(update={"state": "distributing"})
        service.store.upsert_task_message_distribution_epoch(active_epoch)
        service.log_service.update_task_runtime_meta(
            record.task_id,
            distribution={
                "active_epoch_id": active_epoch.epoch_id,
                "state": "distributing",
                "frontier_node_ids": [record.root_node_id],
                "queued_epoch_count": 0,
                "pending_mailbox_count": 0,
            },
        )

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="必须补充风险分层",
            session_id="web:ceo-demo",
        )

        epochs = service.store.list_active_task_message_distribution_epochs(record.task_id)
        runtime_meta = service.log_service.read_task_runtime_meta(record.task_id) or {}
        distribution = dict(runtime_meta.get("distribution") or {})

        assert len(epochs) == 2
        assert epochs[0].epoch_id == active_epoch.epoch_id
        assert epochs[0].state == "distributing"
        assert epochs[1].state == "queued"
        assert epochs[1].root_message == "必须补充风险分层"
        assert distribution["active_epoch_id"] == active_epoch.epoch_id
        assert distribution["state"] == "distributing"
        assert distribution["queued_epoch_count"] == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_append_notice_rejects_cross_session_or_terminal_targets(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        current = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        other = await service.create_task("整理北美重点客户风险", session_id="web:other-demo")

        with pytest.raises(ValueError, match="append_notice_invalid_task_target"):
            await service.task_append_notice(
                task_ids=[other.task_id],
                node_ids=[],
                message="新增董事会验收格式",
                session_id="web:ceo-demo",
            )

        service.store.update_task(
            current.task_id,
            lambda record: record.model_copy(update={"status": "success", "updated_at": now_iso()}),
        )

        with pytest.raises(ValueError, match="append_notice_invalid_task_target"):
            await service.task_append_notice(
                task_ids=[current.task_id],
                node_ids=[],
                message="必须补充风险分层",
                session_id="web:ceo-demo",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_append_notice_success_does_not_produce_verified_task_ids_or_task_dispatch(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")

        result = await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="新增董事会验收格式",
            session_id="web:ceo-demo",
        )

        assert isinstance(result, str)
        assert result.startswith(f"已向任务 {record.task_id} 追加通知")
        assert "创建任务成功" not in result
        assert ceo_runtime_ops.CeoFrontDoorRuntimeOps._looks_like_task_dispatch_claim(result) is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_force_delete_during_distribution_cancels_epoch_and_stops_further_notice_delivery(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="新增董事会验收格式",
            session_id="web:ceo-demo",
        )
        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        service.store.upsert_task_message_distribution_epoch(epoch.model_copy(update={"state": "distributing"}))
        service.store.upsert_task_node_notification(
            TaskNodeNotification(
                notification_id="notif:force-delete",
                task_id=record.task_id,
                node_id=record.root_node_id,
                epoch_id=epoch.epoch_id,
                source_node_id=record.root_node_id,
                message="新增董事会验收格式",
                status="delivered",
                created_at=now_iso(),
                delivered_at=now_iso(),
                consumed_at="",
                payload={},
            )
        )

        deleted = await service.delete_task(record.task_id)

        assert deleted is not None
        assert service.get_task(record.task_id) is None
        assert service.store.list_active_task_message_distribution_epochs(record.task_id) == []
        assert service.store.list_task_epoch_notifications(record.task_id, epoch.epoch_id) == []
    finally:
        await service.close()
