from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest

from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.tool_visibility import CEO_FIXED_BUILTIN_TOOL_NAMES, NODE_FIXED_BUILTIN_TOOL_NAMES
from main.models import SpawnChildSpec, TaskMessageDistributionEpoch, TaskNodeNotification
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
    return _build_service_with_backend(tmp_path, chat_backend=_DummyChatBackend())


def _build_service_with_backend(tmp_path: Path, *, chat_backend) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=chat_backend,
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


def _set_spawn_operations(service: MainRuntimeService, *, root_node_id: str, payload: dict[str, object]) -> None:
    def _mutate(metadata: dict[str, object]) -> dict[str, object]:
        metadata["spawn_operations"] = payload
        return metadata

    service.log_service.update_node_metadata(root_node_id, _mutate)


class _QueuedChatBackend:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self._responses:
            raise AssertionError(f"unexpected chat call: {kwargs!r}")
        return self._responses.pop(0)


async def _seed_distributing_epoch(
    service: MainRuntimeService,
    *,
    task_id: str,
    message: str,
    frontier_node_ids: list[str],
) -> TaskMessageDistributionEpoch:
    task = service.get_task(task_id)
    assert task is not None
    await service.task_append_notice(
        task_ids=[task_id],
        node_ids=[],
        message=message,
        session_id=task.session_id,
    )
    epoch = service.store.list_active_task_message_distribution_epochs(task_id)[0]
    updated = epoch.model_copy(
        update={
            "state": "distributing",
            "payload": {
                **dict(epoch.payload or {}),
                "frontier_node_ids": list(frontier_node_ids),
                "distributed_node_ids": [],
            },
        }
    )
    service.store.upsert_task_message_distribution_epoch(updated)
    service.log_service.update_task_runtime_meta(
        task_id,
        distribution={
            "active_epoch_id": updated.epoch_id,
            "state": "distributing",
            "frontier_node_ids": list(frontier_node_ids),
            "queued_epoch_count": 0,
            "pending_mailbox_count": 0,
        },
    )
    return updated


@pytest.mark.asyncio
async def test_spawned_child_nodes_record_owner_round_metadata(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        spec = SpawnChildSpec(
            goal="child goal",
            prompt="child prompt",
            execution_policy={"mode": "focus"},
            acceptance_prompt="check child",
        )
        cached_payload = {
            "specs": [spec.model_dump(mode="json")],
            "entries": [service.node_runner._normalize_spawn_entry(index=0, spec=spec, entry={})],
            "completed": False,
        }

        service.node_runner._materialize_spawn_batch_children(
            task=task,
            parent=root,
            specs=[spec],
            allowed_indexes=[0],
            cache_key="round-1",
            cached_payload=cached_payload,
        )

        root_after = service.store.get_node(root.node_id)
        entry = dict((((root_after.metadata or {}).get("spawn_operations") or {}).get("round-1") or {}).get("entries")[0])
        child = service.store.get_node(entry["child_node_id"])
        assert child is not None

        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=child,
            goal="accept:child goal",
            acceptance_prompt="check child",
            parent_node_id=child.node_id,
            owner_parent_node_id=root.node_id,
            owner_round_id="round-1",
            owner_entry_index=0,
        )

        child_detail = service.get_node_detail_payload(record.task_id, child.node_id)
        assert child.metadata["spawn_owner_parent_node_id"] == root.node_id
        assert child.metadata["spawn_owner_round_id"] == "round-1"
        assert child.metadata["spawn_owner_entry_index"] == 0
        assert child.metadata["spawn_owner_kind"] == "child"
        assert acceptance.metadata["accepted_node_id"] == child.node_id
        assert acceptance.metadata["spawn_owner_parent_node_id"] == root.node_id
        assert acceptance.metadata["spawn_owner_round_id"] == "round-1"
        assert acceptance.metadata["spawn_owner_entry_index"] == 0
        assert acceptance.metadata["spawn_owner_kind"] == "acceptance"
        assert child_detail is not None
        assert child_detail["item"]["spawn_owner_parent_node_id"] == root.node_id
        assert child_detail["item"]["spawn_owner_round_id"] == "round-1"
        assert child_detail["item"]["latest_live_distribution_round_id"] == "round-1"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_live_distribution_children_use_latest_incomplete_spawn_round_only(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        first_spec = SpawnChildSpec(goal="first child", prompt="first prompt", execution_policy={"mode": "focus"})
        second_spec = SpawnChildSpec(goal="second child", prompt="second prompt", execution_policy={"mode": "focus"})
        first_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=first_spec,
            owner_round_id="round-1",
            owner_entry_index=0,
        )
        second_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=second_spec,
            owner_round_id="round-2",
            owner_entry_index=0,
        )
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-1": {
                    "specs": [first_spec.model_dump(mode="json")],
                    "entries": [{"index": 0, "goal": "first child", "child_node_id": first_child.node_id}],
                    "completed": False,
                },
                "round-2": {
                    "specs": [second_spec.model_dump(mode="json")],
                    "entries": [{"index": 0, "goal": "second child", "child_node_id": second_child.node_id}],
                    "completed": False,
                },
            },
        )

        assert service.node_runner.live_distribution_child_node_ids(
            task_id=record.task_id,
            parent_node_id=root.node_id,
        ) == [second_child.node_id]
        assert service.node_runner.node_is_in_live_distribution_tree(task_id=record.task_id, node_id=root.node_id) is True
        assert service.node_runner.node_is_in_live_distribution_tree(task_id=record.task_id, node_id=first_child.node_id) is False
        assert service.node_runner.node_is_in_live_distribution_tree(task_id=record.task_id, node_id=second_child.node_id) is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_completed_spawn_round_children_are_not_distribution_targets(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        spec = SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy={"mode": "focus"})
        child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=spec,
            owner_round_id="round-1",
            owner_entry_index=0,
        )
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-1": {
                    "specs": [spec.model_dump(mode="json")],
                    "entries": [{"index": 0, "goal": "child goal", "child_node_id": child.node_id}],
                    "completed": True,
                },
            },
        )

        assert service.node_runner.live_distribution_child_node_ids(
            task_id=record.task_id,
            parent_node_id=root.node_id,
        ) == []
        assert service.node_runner.node_is_in_live_distribution_tree(task_id=record.task_id, node_id=child.node_id) is False
    finally:
        await service.close()


def test_submit_message_distribution_tool_schema_uses_explicit_child_targets() -> None:
    from main.runtime.internal_tools import SubmitMessageDistributionTool

    tool = SubmitMessageDistributionTool(lambda payload: payload)
    schema = tool.parameters
    item = schema["properties"]["children"]["items"]

    assert "target_node_id" in item["properties"]
    assert "message" in item["properties"]
    assert "reason" in item["properties"]


@pytest.mark.asyncio
async def test_distribution_turn_runs_through_task_dispatcher_and_persists_child_deliveries(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [
                                {"target_node_id": "CHILD_ONE", "message": "first child update", "reason": "focus"},
                                {"target_node_id": "CHILD_TWO", "message": "second child update", "reason": "coverage"},
                            ],
                            "notes": "forward to both active children",
                        },
                    }
                ],
                content="",
            )
        ]
    )
    service = _build_service_with_backend(tmp_path, chat_backend=backend)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        first_spec = SpawnChildSpec(goal="first child", prompt="first prompt", execution_policy={"mode": "focus"})
        second_spec = SpawnChildSpec(goal="second child", prompt="second prompt", execution_policy={"mode": "focus"})
        first_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=first_spec,
            owner_round_id="round-1",
            owner_entry_index=0,
        )
        second_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=second_spec,
            owner_round_id="round-1",
            owner_entry_index=1,
        )
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-1": {
                    "specs": [first_spec.model_dump(mode="json"), second_spec.model_dump(mode="json")],
                    "entries": [
                        {"index": 0, "goal": "first child", "child_node_id": first_child.node_id},
                        {"index": 1, "goal": "second child", "child_node_id": second_child.node_id},
                    ],
                    "completed": False,
                },
            },
        )
        epoch = await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="新增董事会验收格式",
            frontier_node_ids=[root.node_id],
        )
        backend._responses[0].tool_calls[0]["arguments"]["children"][0]["target_node_id"] = first_child.node_id
        backend._responses[0].tool_calls[0]["arguments"]["children"][1]["target_node_id"] = second_child.node_id

        await service.task_actor_service.run_task(record.task_id)

        first_notifications = service.store.list_task_node_notifications(record.task_id, first_child.node_id)
        second_notifications = service.store.list_task_node_notifications(record.task_id, second_child.node_id)
        runtime_meta = service.log_service.read_task_runtime_meta(record.task_id) or {}
        distribution = dict(runtime_meta.get("distribution") or {})
        frame = service.log_service.read_runtime_frame(record.task_id, root.node_id) or {}

        assert len(backend.calls) == 1
        assert frame.get("phase") == "message_distribution"
        assert len(first_notifications) == 1
        assert first_notifications[0].epoch_id == epoch.epoch_id
        assert first_notifications[0].message == "first child update"
        assert len(second_notifications) == 1
        assert second_notifications[0].message == "second child update"
        assert distribution["frontier_node_ids"] == [first_child.node_id, second_child.node_id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_leaf_node_does_not_spawn_further_distribution_turns(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [],
                            "notes": "leaf node has no current live execution children",
                        },
                    }
                ],
                content="",
            )
        ]
    )
    service = _build_service_with_backend(tmp_path, chat_backend=backend)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        epoch = await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="新增董事会验收格式",
            frontier_node_ids=[record.root_node_id],
        )

        await service.task_actor_service.run_task(record.task_id)

        runtime_meta = service.log_service.read_task_runtime_meta(record.task_id) or {}
        distribution = dict(runtime_meta.get("distribution") or {})
        refreshed_epoch = service.store.get_task_message_distribution_epoch(record.task_id, epoch.epoch_id)

        assert len(backend.calls) == 1
        assert distribution["frontier_node_ids"] == []
        assert refreshed_epoch is not None
        assert refreshed_epoch.payload.get("distributed_node_ids") == [record.root_node_id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_turn_omits_non_targeted_children(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [
                                {"target_node_id": "CHILD_TWO", "message": "second child update", "reason": "only this child needs the new constraint"},
                            ],
                            "notes": "leave the other child unchanged",
                        },
                    }
                ],
                content="",
            )
        ]
    )
    service = _build_service_with_backend(tmp_path, chat_backend=backend)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        first_spec = SpawnChildSpec(goal="first child", prompt="first prompt", execution_policy={"mode": "focus"})
        second_spec = SpawnChildSpec(goal="second child", prompt="second prompt", execution_policy={"mode": "focus"})
        first_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=first_spec,
            owner_round_id="round-1",
            owner_entry_index=0,
        )
        second_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=second_spec,
            owner_round_id="round-1",
            owner_entry_index=1,
        )
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-1": {
                    "specs": [first_spec.model_dump(mode="json"), second_spec.model_dump(mode="json")],
                    "entries": [
                        {"index": 0, "goal": "first child", "child_node_id": first_child.node_id},
                        {"index": 1, "goal": "second child", "child_node_id": second_child.node_id},
                    ],
                    "completed": False,
                },
            },
        )
        await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="新增董事会验收格式",
            frontier_node_ids=[root.node_id],
        )
        backend._responses[0].tool_calls[0]["arguments"]["children"][0]["target_node_id"] = second_child.node_id

        await service.task_actor_service.run_task(record.task_id)

        assert service.store.list_task_node_notifications(record.task_id, first_child.node_id) == []
        second_notifications = service.store.list_task_node_notifications(record.task_id, second_child.node_id)
        assert len(second_notifications) == 1
        assert second_notifications[0].message == "second child update"
    finally:
        await service.close()
