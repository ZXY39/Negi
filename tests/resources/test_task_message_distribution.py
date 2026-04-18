from __future__ import annotations

from pathlib import Path

from g3ku.runtime.tool_visibility import CEO_FIXED_BUILTIN_TOOL_NAMES, NODE_FIXED_BUILTIN_TOOL_NAMES
from main.models import TaskMessageDistributionEpoch, TaskNodeNotification
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
