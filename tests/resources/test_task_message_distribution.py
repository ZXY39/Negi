from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.tool_visibility import CEO_FIXED_BUILTIN_TOOL_NAMES, NODE_FIXED_BUILTIN_TOOL_NAMES
from main.models import NodeFinalResult, NodeRecord, SpawnChildSpec, TaskMessageDistributionEpoch, TaskNodeNotification, TokenUsageSummary
from main.protocol import now_iso
from main.runtime.pending_notice_state import (
    PENDING_NOTICE_STATE_KEY,
    RESUME_MODE_ORDINARY,
    RESUME_MODE_WAIT_FOR_CHILDREN,
)
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore


@pytest.fixture(autouse=True)
def _default_node_send_preflight_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    def _resolve(**kwargs) -> SendModelContextWindowInfo:
        refs = list(kwargs.get("model_refs") or [])
        model_key = str(refs[0] or "").strip() if refs else ""
        return SendModelContextWindowInfo(
            model_key=model_key,
            provider_id="test",
            provider_model=f"test:{model_key}" if model_key else "test",
            resolved_model=model_key,
            context_window_tokens=32000,
            resolution_error="",
        )

    monkeypatch.setattr(
        react_loop_module,
        "get_runtime_config",
        lambda **_: (SimpleNamespace(), 0, False),
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.runtime_chat_backend,
        "resolve_send_model_context_window_info",
        _resolve,
        raising=False,
    )


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


def build_service(tmp_path: Path) -> MainRuntimeService:
    return _build_service(tmp_path)


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
        assert epochs[0].payload.get("barrier_root_node_id") == record.root_node_id
        assert epochs[0].payload.get("barrier_node_ids") == [record.root_node_id]
        assert epochs[0].root_message == "新增董事会验收格式"
        assert distribution == {
            "active_epoch_id": epochs[0].epoch_id,
            "state": "barrier_requested",
            "mode": "task_wide_barrier",
            "frontier_node_ids": [],
            "blocked_node_ids": [record.root_node_id],
            "pending_notice_node_ids": [record.root_node_id],
            "queued_epoch_count": 1,
            "pending_mailbox_count": 0,
        }
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_node_detail_exposes_consumed_append_notice_messages(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        resumed_task_ids: list[str] = []
        service.task_actor_service.distribution_resume_callback = (
            lambda task_id: resumed_task_ids.append(str(task_id or "").strip())
        )
        record = await service.create_task("鏁寸悊閲嶇偣瀹㈡埛娴佸け淇″彿", session_id="web:ceo-demo")
        root = service.store.get_node(record.root_node_id)
        assert root is not None

        service.log_service.update_node_metadata(
            root.node_id,
            lambda metadata: {
                **metadata,
                "append_notice_context": {
                    "notice_records": [
                        {
                            "notification_id": "notif:notice-1",
                            "epoch_id": "epoch:notice",
                            "source_node_id": "node:source",
                            "message": "改成男性角色Top20",
                            "consumed_at": "2026-04-19T15:26:11+08:00",
                            "compression_stage_id": "",
                        }
                    ],
                    "compression_segments": [],
                },
            },
        )

        detail = service.query_service.get_node_detail(record.task_id, root.node_id, detail_level="full")

        assert detail is not None
        assert detail.append_notice_messages == [
            {
                "notification_id": "notif:notice-1",
                "epoch_id": "epoch:notice",
                "source_node_id": "node:source",
                "message": "改成男性角色Top20",
                "consumed_at": "2026-04-19T15:26:11+08:00",
                "compression_stage_id": "",
            }
        ]
        assert detail.message_list == [
            {
                "notification_id": "notif:notice-1",
                "epoch_id": "epoch:notice",
                "source_node_id": "node:source",
                "message": "改成男性角色Top20",
                "received_at": "2026-04-19T15:26:11+08:00",
                "consumed_at": "2026-04-19T15:26:11+08:00",
                "status": "consumed",
                "compression_stage_id": "",
                "deliveries": [],
            }
        ]
    finally:
        await service.close()



@pytest.mark.asyncio
async def test_compression_notice_tail_block_precedes_externalized_stage_block(tmp_path: Path) -> None:
    from main.runtime.append_notice_context import APPEND_NOTICE_TAIL_PREFIX

    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        service.log_service.update_node_metadata(
            root.node_id,
            lambda metadata: {
                **metadata,
                "append_notice_context": {
                    "notice_records": [
                        {
                            "notification_id": "notif:compression-1",
                            "epoch_id": "epoch:compression",
                            "source_node_id": root.node_id,
                            "message": "必须按董事会模板输出",
                            "consumed_at": "2026-04-19T10:00:00+08:00",
                            "compression_stage_id": "",
                        },
                        {
                            "notification_id": "notif:compression-2",
                            "epoch_id": "epoch:compression",
                            "source_node_id": root.node_id,
                            "message": "必须补充风险分层",
                            "consumed_at": "2026-04-19T10:05:00+08:00",
                            "compression_stage_id": "",
                        },
                    ],
                    "compression_segments": [],
                },
            },
        )
        service.log_service.update_node_metadata(
            root.node_id,
            lambda metadata: {
                **metadata,
                "execution_stages": {
                    "active_stage_id": "stage-14",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "stage-compression-1",
                            "stage_index": 10,
                            "stage_kind": "compression",
                            "system_generated": True,
                            "mode": "\u81ea\u4e3b\u6267\u884c",
                            "status": "\u5b8c\u6210",
                            "stage_goal": "Archive completed stage history 1-10",
                            "completed_stage_summary": "archived old stages",
                            "key_refs": [],
                            "archive_ref": "artifact:artifact:stage-archive-1",
                            "archive_stage_index_start": 1,
                            "archive_stage_index_end": 10,
                            "tool_round_budget": 0,
                            "tool_rounds_used": 0,
                            "created_at": "2026-04-19T10:10:00+08:00",
                            "finished_at": "2026-04-19T10:10:00+08:00",
                            "rounds": [],
                        },
                        {
                            "stage_id": "stage-11",
                            "stage_index": 11,
                            "stage_kind": "normal",
                            "system_generated": False,
                            "mode": "\u81ea\u4e3b\u6267\u884c",
                            "status": "\u5b8c\u6210",
                            "stage_goal": "completed stage eleven",
                            "completed_stage_summary": "finished stage eleven",
                            "key_refs": [],
                            "tool_round_budget": 3,
                            "tool_rounds_used": 1,
                            "created_at": "2026-04-19T10:11:00+08:00",
                            "finished_at": "2026-04-19T10:11:30+08:00",
                            "rounds": [],
                        },
                        {
                            "stage_id": "stage-12",
                            "stage_index": 12,
                            "stage_kind": "normal",
                            "system_generated": False,
                            "mode": "\u81ea\u4e3b\u6267\u884c",
                            "status": "\u5b8c\u6210",
                            "stage_goal": "completed stage twelve",
                            "completed_stage_summary": "finished stage twelve",
                            "key_refs": [],
                            "tool_round_budget": 3,
                            "tool_rounds_used": 1,
                            "created_at": "2026-04-19T10:12:00+08:00",
                            "finished_at": "2026-04-19T10:12:30+08:00",
                            "rounds": [],
                        },
                        {
                            "stage_id": "stage-13",
                            "stage_index": 13,
                            "stage_kind": "normal",
                            "system_generated": False,
                            "mode": "\u81ea\u4e3b\u6267\u884c",
                            "status": "\u5b8c\u6210",
                            "stage_goal": "completed stage thirteen",
                            "completed_stage_summary": "finished stage thirteen",
                            "key_refs": [],
                            "tool_round_budget": 3,
                            "tool_rounds_used": 1,
                            "created_at": "2026-04-19T10:13:00+08:00",
                            "finished_at": "2026-04-19T10:13:30+08:00",
                            "rounds": [],
                        },
                        {
                            "stage_id": "stage-14",
                            "stage_index": 14,
                            "stage_kind": "normal",
                            "system_generated": False,
                            "mode": "\u81ea\u4e3b\u6267\u884c",
                            "status": "\u8fdb\u884c\u4e2d",
                            "stage_goal": "current stage",
                            "completed_stage_summary": "",
                            "key_refs": [],
                            "tool_round_budget": 3,
                            "tool_rounds_used": 0,
                            "created_at": "2026-04-19T10:14:00+08:00",
                            "finished_at": "",
                            "rounds": [],
                        },
                    ],
                },
            },
        )

        # Persist once so the append notice interval is rolled into a compression segment.
        stage_payload = (service.store.get_node(root.node_id).metadata or {}).get("execution_stages")
        from main.models import normalize_execution_stage_metadata
        service.log_service._persist_execution_stage_state_locked(
            task=task,
            node_id=root.node_id,
            state=normalize_execution_stage_metadata(stage_payload),
        )

        prepared = service._react_loop._prepare_messages(
            [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}],
            runtime_context={"task_id": record.task_id, "node_id": root.node_id},
        )
        contents = [str(item.get("content") or "") for item in prepared]
        notice_index = next(index for index, content in enumerate(contents) if content.startswith(APPEND_NOTICE_TAIL_PREFIX))
        compression_index = next(index for index, content in enumerate(contents) if content.startswith("[G3KU_STAGE_EXTERNALIZED_V1]"))

        assert notice_index < compression_index
        assert "必须按董事会模板输出" in contents[notice_index]
        assert "必须补充风险分层" in contents[notice_index]
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
        epochs_by_state = {item.state: item for item in epochs}

        assert len(epochs) == 2
        assert epochs_by_state["distributing"].epoch_id == active_epoch.epoch_id
        assert epochs_by_state["queued"].root_message == "必须补充风险分层"
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


async def seed_live_root_with_two_running_children(service: MainRuntimeService):
    record = await service.create_task("鏁寸悊閲嶇偣瀹㈡埛娴佸け淇″彿", session_id="web:ceo-demo")
    task = service.get_task(record.task_id)
    root = service.store.get_node(record.root_node_id)
    assert task is not None
    assert root is not None

    branch_a_spec = SpawnChildSpec(goal="branch a", prompt="branch a prompt", execution_policy={"mode": "focus"})
    branch_b_spec = SpawnChildSpec(goal="branch b", prompt="branch b prompt", execution_policy={"mode": "focus"})
    branch_a = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=branch_a_spec,
        owner_round_id="round-live",
        owner_entry_index=0,
    )
    branch_b = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=branch_b_spec,
        owner_round_id="round-live",
        owner_entry_index=1,
    )
    _set_spawn_operations(
        service,
        root_node_id=root.node_id,
        payload={
            "round-live": {
                "specs": [
                    branch_a_spec.model_dump(mode="json"),
                    branch_b_spec.model_dump(mode="json"),
                ],
                "entries": [
                    {"index": 0, "goal": "branch a", "child_node_id": branch_a.node_id},
                    {"index": 1, "goal": "branch b", "child_node_id": branch_b.node_id},
                ],
                "completed": False,
            }
        },
    )
    return record, root, branch_a, branch_b


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
    assert "should_distribute" in item["properties"]
    assert "message" in item["properties"]
    assert "reason" in item["properties"]
    assert "should_distribute" in item["required"]
    assert "reason" in item["required"]


@pytest.mark.asyncio
async def test_distribution_turn_requires_explicit_decision_for_each_live_child(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [
                                {
                                    "target_node_id": "CHILD_ONE",
                                    "should_distribute": True,
                                    "message": "branch-a update",
                                    "reason": "affected",
                                }
                            ],
                            "notes": "missed branch b",
                        },
                    }
                ],
                content="",
            )
        ]
    )
    service = _build_service_with_backend(tmp_path, chat_backend=backend)
    try:
        record, root, branch_a, branch_b = await seed_live_root_with_two_running_children(service)
        epoch = await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="new global constraint",
            frontier_node_ids=[root.node_id],
        )
        backend._responses[0].tool_calls[0]["arguments"]["children"][0]["target_node_id"] = branch_a.node_id

        task = service.get_task(record.task_id)
        assert task is not None

        result = await service.node_runner._run_distribution_node(task=task, node=root)

        refreshed_epoch = service.store.get_task_message_distribution_epoch(record.task_id, epoch.epoch_id)
        assert result.status == "failed"
        assert result.blocking_reason == "distribution_decision_missing_child_decisions"
        assert service.store.list_task_node_notifications(record.task_id, branch_a.node_id) == []
        assert service.store.list_task_node_notifications(record.task_id, branch_b.node_id) == []
        assert refreshed_epoch is not None
        assert refreshed_epoch.payload.get("distributed_node_ids") == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_response_tool_calls_normalize_tool_call_request_objects(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        response = LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call:distribution",
                    name="submit_message_distribution",
                    arguments={"children": [], "notes": "normalized"},
                )
            ],
        )

        normalized = service.node_runner._distribution_response_tool_calls(response)

        assert normalized == [
            {
                "id": "call:distribution",
                "name": "submit_message_distribution",
                "arguments": {"children": [], "notes": "normalized"},
            }
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_turn_accepts_tool_call_request_objects(tmp_path: Path) -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id="call:distribution",
                name="submit_message_distribution",
                arguments={
                    "children": [
                        {
                            "target_node_id": "CHILD_ONE",
                            "should_distribute": True,
                            "message": "branch-a update",
                            "reason": "candidate-pool branch must switch to male characters",
                        },
                        {
                            "target_node_id": "CHILD_TWO",
                            "should_distribute": False,
                            "message": "",
                            "reason": "heat-source design branch stays reusable without gender-specific assumptions",
                        },
                    ],
                    "notes": "only the candidate-pool branch needs the requirement change",
                },
            )
        ],
    )
    backend = _QueuedChatBackend([response])
    service = _build_service_with_backend(tmp_path, chat_backend=backend)
    try:
        record, root, branch_a, branch_b = await seed_live_root_with_two_running_children(service)
        await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="榜单对象改成二次元男角色Top20",
            frontier_node_ids=[root.node_id],
        )
        response.tool_calls[0].arguments["children"][0]["target_node_id"] = branch_a.node_id
        response.tool_calls[0].arguments["children"][1]["target_node_id"] = branch_b.node_id

        task = service.get_task(record.task_id)
        assert task is not None

        result = await service.node_runner._run_distribution_node(task=task, node=root)
        refreshed_epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]

        assert result.status == "success"
        assert [item.message for item in service.store.list_task_node_notifications(record.task_id, branch_a.node_id)] == [
            "branch-a update"
        ]
        assert service.store.list_task_node_notifications(record.task_id, branch_b.node_id) == []
        assert refreshed_epoch.payload["decision_records"][0]["delivered_child_ids"] == [branch_a.node_id]
        assert refreshed_epoch.payload["decision_records"][0]["skipped_child_decisions"] == [
            {
                "target_node_id": branch_b.node_id,
                "reason": "heat-source design branch stays reusable without gender-specific assumptions",
            }
        ]
        assert refreshed_epoch.payload["debug_trace"][2]["tool_call_names"] == ["submit_message_distribution"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_turn_uses_responses_compatible_flat_function_tool_choice(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [],
                            "notes": "no child receives the notice in this turn",
                        },
                    }
                ],
                content="",
            )
        ]
    )
    service = _build_service_with_backend(tmp_path, chat_backend=backend)
    try:
        record = await service.create_task("distribution tool choice test", session_id="web:ceo-demo")
        await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="append notice for distribution tool choice",
            frontier_node_ids=[record.root_node_id],
        )

        await service.task_actor_service.run_task(record.task_id)

        assert len(backend.calls) == 1
        assert backend.calls[0]["tool_choice"] == {
            "type": "function",
            "name": "submit_message_distribution",
        }
    finally:
        await service.close()


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
                                {
                                    "target_node_id": "CHILD_ONE",
                                    "should_distribute": True,
                                    "message": "first child update",
                                    "reason": "focus",
                                },
                                {
                                    "target_node_id": "CHILD_TWO",
                                    "should_distribute": True,
                                    "message": "second child update",
                                    "reason": "coverage",
                                },
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
async def test_distribution_turn_requeues_task_when_next_frontier_exists(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [
                                {
                                    "target_node_id": "CHILD_ONE",
                                    "should_distribute": True,
                                    "message": "first child update",
                                    "reason": "focus",
                                },
                                {
                                    "target_node_id": "CHILD_TWO",
                                    "should_distribute": True,
                                    "message": "second child update",
                                    "reason": "coverage",
                                },
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
        resumed_task_ids: list[str] = []
        service.task_actor_service.distribution_resume_callback = (
            lambda task_id: resumed_task_ids.append(str(task_id or "").strip())
        )
        record = await service.create_task("distribution requeue", session_id="web:ceo-demo")
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
            message="new requirement",
            frontier_node_ids=[root.node_id],
        )
        backend._responses[0].tool_calls[0]["arguments"]["children"][0]["target_node_id"] = first_child.node_id
        backend._responses[0].tool_calls[0]["arguments"]["children"][1]["target_node_id"] = second_child.node_id

        await service.task_actor_service.run_task(record.task_id)

        assert resumed_task_ids == [record.task_id]
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
async def test_distribution_turn_runs_node_send_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="新增董事会验收格式",
            frontier_node_ids=[record.root_node_id],
        )

        observed: dict[str, object] = {}

        def _fake_preflight(**kwargs):
            observed.update(dict(kwargs))
            return (
                [
                    *list(kwargs.get("request_messages") or []),
                    {"role": "assistant", "content": "[G3KU_TOKEN_COMPACT_V2]\n{\"kind\":\"distribution-test\"}"},
                ],
                {"applied": True, "mode": "marker"},
                "token_compression",
                "",
            )

        monkeypatch.setattr(
            service._react_loop,
            "run_node_send_preflight_for_control_turn",
            _fake_preflight,
        )

        await service.task_actor_service.run_task(record.task_id)

        assert observed["task_id"] == record.task_id
        assert observed["node_id"] == record.root_node_id
        assert observed["tool_choice"] == {
            "type": "function",
            "name": "submit_message_distribution",
        }
        assert observed["parallel_tool_calls"] is False
        assert len(backend.calls) == 1
        rendered = "\n".join(str(item.get("content") or "") for item in list(backend.calls[0].get("messages") or []))
        assert "[G3KU_TOKEN_COMPACT_V2]" in rendered
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
                                {
                                    "target_node_id": "CHILD_ONE",
                                    "should_distribute": False,
                                    "message": "",
                                    "reason": "this child is not affected by the new constraint",
                                },
                                {
                                    "target_node_id": "CHILD_TWO",
                                    "should_distribute": True,
                                    "message": "second child update",
                                    "reason": "only this child needs the new constraint",
                                },
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
        backend._responses[0].tool_calls[0]["arguments"]["children"][0]["target_node_id"] = first_child.node_id
        backend._responses[0].tool_calls[0]["arguments"]["children"][1]["target_node_id"] = second_child.node_id

        await service.task_actor_service.run_task(record.task_id)

        assert service.store.list_task_node_notifications(record.task_id, first_child.node_id) == []
        second_notifications = service.store.list_task_node_notifications(record.task_id, second_child.node_id)
        assert len(second_notifications) == 1
        assert second_notifications[0].message == "second child update"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_turn_uses_runtime_child_snapshot_and_persists_decisions(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [
                                {
                                    "target_node_id": "CHILD_ONE",
                                    "should_distribute": True,
                                    "message": "branch-a specific notice",
                                    "reason": "branch a is affected",
                                },
                                {
                                    "target_node_id": "CHILD_TWO",
                                    "should_distribute": False,
                                    "message": "",
                                    "reason": "branch b can continue unchanged",
                                },
                            ],
                            "notes": "keep one local notice on root",
                        },
                    }
                ],
                content="",
            )
        ]
    )
    service = _build_service_with_backend(tmp_path, chat_backend=backend)
    try:
        record, root, branch_a, branch_b = await seed_live_root_with_two_running_children(service)
        epoch = await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="new constraint",
            frontier_node_ids=[root.node_id],
        )
        backend._responses[0].tool_calls[0]["arguments"]["children"][0]["target_node_id"] = branch_a.node_id
        backend._responses[0].tool_calls[0]["arguments"]["children"][1]["target_node_id"] = branch_b.node_id

        task = service.get_task(record.task_id)
        assert task is not None

        await service.node_runner._run_distribution_node(task=task, node=root)

        assert len(backend.calls) == 1
        prompt_payload = json.loads(str(backend.calls[0]["messages"][1]["content"] or "{}"))
        first_child_payload = next(
            item for item in list(prompt_payload.get("live_children") or [])
            if item["node_id"] == branch_a.node_id
        )
        assert first_child_payload["is_live_in_current_tree"] is True

        refreshed_epoch = service.store.get_task_message_distribution_epoch(record.task_id, epoch.epoch_id)
        assert refreshed_epoch is not None
        decision_records = list((refreshed_epoch.payload or {}).get("decision_records") or [])
        assert decision_records[0]["source_node_id"] == root.node_id
        assert decision_records[0]["local_notice_kept"] is True
        assert decision_records[0]["delivered_child_ids"] == [branch_a.node_id]
        assert decision_records[0]["skipped_child_decisions"] == [
            {
                "target_node_id": branch_b.node_id,
                "reason": "branch b can continue unchanged",
            }
        ]

        detail = service.query_service.get_node_detail(record.task_id, root.node_id, detail_level="full")
        assert detail is not None
        assert detail.message_list[0]["status"] == "pending"
        assert detail.message_list[0]["deliveries"][0]["target_node_id"] == branch_a.node_id
        assert detail.message_list[0]["deliveries"][0]["decision"] == "distributed"
        assert detail.message_list[0]["deliveries"][1]["target_node_id"] == branch_b.node_id
        assert detail.message_list[0]["deliveries"][1]["decision"] == "skipped"
        assert detail.message_list[0]["deliveries"][1]["reason"] == "branch b can continue unchanged"
        assert detail.message_list[0]["deliveries"][1]["status"] == ""
        assert detail.message_list[0]["deliveries"][1]["message"] == ""
        assert detail.message_list[0]["deliveries"][1]["received_at"] == ""
        assert detail.message_list[0]["deliveries"][1]["notification_id"] == ""
        assert branch_b.node_id not in {
            item["target_node_id"]
            for item in list(detail.message_list[0]["deliveries"] or [])
            if item.get("decision") == "distributed"
        }
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_success_execution_node_with_acceptance_is_reactivated_and_acceptance_invalidated_on_delivery(tmp_path: Path) -> None:
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
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-1": {
                    "specs": [spec.model_dump(mode="json")],
                    "entries": [
                        {
                            "index": 0,
                            "goal": "child goal",
                            "child_node_id": child.node_id,
                            "acceptance_node_id": acceptance.node_id,
                            "check_status": "passed",
                        }
                    ],
                    "completed": False,
                },
            },
        )
        service.store.update_node(
            child.node_id,
            lambda record: record.model_copy(
                update={
                    "status": "success",
                    "updated_at": now_iso(),
                    "final_output": "child succeeded",
                    "check_result": "acceptance passed",
                }
            ),
        )
        service.store.update_node(
            acceptance.node_id,
            lambda record: record.model_copy(
                update={
                    "status": "success",
                    "updated_at": now_iso(),
                    "final_output": "acceptance passed",
                }
            ),
        )

        service._deliver_distribution_message(
            task_id=record.task_id,
            epoch_id="epoch:reactivate-success",
            source_node_id=root.node_id,
            target_node_id=child.node_id,
            message="新增董事会验收格式",
        )

        updated_child = service.store.get_node(child.node_id)
        updated_acceptance = service.store.get_node(acceptance.node_id)
        updated_root = service.store.get_node(root.node_id)
        notifications = service.store.list_task_node_notifications(record.task_id, child.node_id)
        entry = dict((((updated_root.metadata or {}).get("spawn_operations") or {}).get("round-1") or {}).get("entries")[0])

        assert updated_child is not None
        assert updated_child.status == "in_progress"
        assert updated_child.final_output == ""
        assert updated_child.failure_reason == ""
        assert updated_acceptance is not None
        assert updated_acceptance.metadata["invalidated"] is True
        assert updated_acceptance.metadata["invalidated_by_epoch_id"] == "epoch:reactivate-success"
        assert entry["check_status"] not in {"passed", "failed"}
        assert len(notifications) == 1
        assert notifications[0].status == "delivered"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_execution_node_is_reactivated_on_delivery(tmp_path: Path) -> None:
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
        service.store.update_node(
            child.node_id,
            lambda record: record.model_copy(
                update={
                    "status": "failed",
                    "updated_at": now_iso(),
                    "failure_reason": "child failed",
                }
            ),
        )

        service._deliver_distribution_message(
            task_id=record.task_id,
            epoch_id="epoch:reactivate-failed",
            source_node_id=root.node_id,
            target_node_id=child.node_id,
            message="必须补充风险分层",
        )

        updated_child = service.store.get_node(child.node_id)
        notifications = service.store.list_task_node_notifications(record.task_id, child.node_id)

        assert updated_child is not None
        assert updated_child.status == "in_progress"
        assert updated_child.failure_reason == ""
        assert len(notifications) == 1
        assert notifications[0].message == "必须补充风险分层"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_acceptance_nodes_are_never_direct_distribution_targets(tmp_path: Path) -> None:
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

        with pytest.raises(ValueError, match="distribution_target_must_be_execution_node"):
            service._deliver_distribution_message(
                task_id=record.task_id,
                epoch_id="epoch:reject-acceptance",
                source_node_id=root.node_id,
                target_node_id=acceptance.node_id,
                message="新增董事会验收格式",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_delivered_mailbox_message_is_appended_at_next_safe_boundary(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        service.log_service.upsert_frame(
            record.task_id,
            {
                "node_id": root.node_id,
                "depth": root.depth,
                "node_kind": root.node_kind,
                "phase": "before_model",
                "stage_status": "active",
                "stage_goal": "old stage",
                "messages": [
                    {"role": "system", "content": "system seed"},
                    {"role": "user", "content": "historic request"},
                ],
            },
        )
        service.store.upsert_task_node_notification(
            TaskNodeNotification(
                notification_id="notif:safe-boundary",
                task_id=record.task_id,
                node_id=root.node_id,
                epoch_id="epoch:safe-boundary",
                source_node_id=root.node_id,
                message="新增董事会验收格式",
                status="delivered",
                created_at=now_iso(),
                delivered_at=now_iso(),
                consumed_at="",
                payload={},
            )
        )

        resumed = await service.node_runner._resume_react_state(task=task, node=root)

        notifications = service.store.list_task_node_notifications(record.task_id, root.node_id)
        frame = service.log_service.read_runtime_frame(record.task_id, root.node_id) or {}

        assert resumed["messages"][-1]["role"] == "user"
        assert resumed["messages"][-1]["content"] == "新增董事会验收格式"
        assert notifications[0].status == "delivered"
        assert frame.get("stage_status") == ""
        assert frame.get("stage_goal") == ""
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_node_rebuilds_history_from_latest_runtime_refs_before_consuming_message(tmp_path: Path) -> None:
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
        service.log_service.upsert_frame(
            record.task_id,
            {
                "node_id": child.node_id,
                "depth": child.depth,
                "node_kind": child.node_kind,
                "phase": "before_model",
                "messages": [
                    {"role": "system", "content": "system seed"},
                    {"role": "user", "content": "historic child request"},
                ],
            },
        )
        service.log_service.remove_frame(record.task_id, child.node_id)
        service.store.update_node(
            child.node_id,
            lambda current: current.model_copy(
                update={
                    "status": "success",
                    "updated_at": now_iso(),
                    "final_output": "child succeeded",
                }
            ),
        )
        service._deliver_distribution_message(
            task_id=record.task_id,
            epoch_id="epoch:rebuild-history",
            source_node_id=root.node_id,
            target_node_id=child.node_id,
            message="必须补充风险分层",
        )
        updated_child = service.store.get_node(child.node_id)
        assert updated_child is not None

        resumed = await service.node_runner._resume_react_state(task=task, node=updated_child)

        assert resumed["messages"][1]["content"] == "historic child request"
        assert resumed["messages"][-1]["content"] == "必须补充风险分层"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_root_distribution_notice_uses_wait_for_children_resume_mode_when_active_round_exists(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("鏁寸悊閲嶇偣瀹㈡埛娴佸け淇″彿", session_id="web:ceo-demo")
        root = service.store.get_node(record.root_node_id)
        assert root is not None

        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-live": {
                    "specs": [],
                    "entries": [],
                    "completed": False,
                }
            },
        )
        epoch = await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="鏂板钁ｄ簨浼氶獙鏀舵牸寮?",
            frontier_node_ids=[record.root_node_id],
        )
        created_at = "2026-04-24T12:00:00+08:00"

        service.node_runner._queue_pending_root_distribution_notices(epoch=epoch, created_at=created_at)

        updated_root = service.store.get_node(record.root_node_id)
        pending_notice_state = dict((updated_root.metadata or {}).get(PENDING_NOTICE_STATE_KEY) or {})

        assert updated_root is not None
        assert pending_notice_state == {
            "resume_mode": RESUME_MODE_WAIT_FOR_CHILDREN,
            "epoch_id": epoch.epoch_id,
            "holding_round_id": "round-live",
            "updated_at": created_at,
        }
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_root_distribution_notice_uses_ordinary_resume_mode_without_active_round(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("鏁寸悊閲嶇偣瀹㈡埛娴佸け淇″彿", session_id="web:ceo-demo")
        epoch = await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="鏂板钁ｄ簨浼氶獙鏀舵牸寮?",
            frontier_node_ids=[record.root_node_id],
        )
        created_at = "2026-04-24T12:05:00+08:00"

        service.node_runner._queue_pending_root_distribution_notices(epoch=epoch, created_at=created_at)

        updated_root = service.store.get_node(record.root_node_id)
        pending_notice_state = dict((updated_root.metadata or {}).get(PENDING_NOTICE_STATE_KEY) or {})

        assert updated_root is not None
        assert pending_notice_state == {
            "resume_mode": RESUME_MODE_ORDINARY,
            "epoch_id": epoch.epoch_id,
            "holding_round_id": "",
            "updated_at": created_at,
        }
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_epoch_completes_and_task_resumes_ordinary_execution(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [],
                            "notes": "leaf node finished distribution",
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

        updated_task = service.get_task(record.task_id)
        updated_root = service.store.get_node(record.root_node_id)
        updated_epoch = service.store.get_task_message_distribution_epoch(record.task_id, epoch.epoch_id)
        runtime_meta = service.log_service.read_task_runtime_meta(record.task_id) or {}
        distribution = dict(runtime_meta.get("distribution") or {})

        assert updated_task is not None
        assert updated_root is not None
        assert updated_task.status == "in_progress"
        assert updated_task.pause_requested is False
        assert updated_task.is_paused is False
        assert updated_root.status == "in_progress"
        assert updated_root.failure_reason == ""
        assert updated_epoch is not None
        assert updated_epoch.state == "completed"
        assert distribution["state"] == ""
        assert distribution["mode"] == ""
        assert distribution["frontier_node_ids"] == []
        assert distribution["pending_notice_node_ids"] == [record.root_node_id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_epoch_keeps_node_level_pending_notice_after_global_distribution_finishes(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [],
                            "notes": "root node keeps the new requirement",
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

        updated_epoch = service.store.get_task_message_distribution_epoch(record.task_id, epoch.epoch_id)
        updated_root = service.store.get_node(record.root_node_id)
        progress = service.query_service.view_progress(record.task_id, mark_read=False)

        assert updated_epoch is not None
        assert updated_epoch.state == "completed"
        assert updated_root is not None
        assert list((updated_root.metadata or {}).get("pending_append_notice_records") or [])
        assert progress is not None
        assert progress.live_state is not None
        assert progress.live_state.distribution.state == ""
        assert progress.live_state.distribution.mode == ""
        assert progress.live_state.distribution.pending_notice_node_ids == [record.root_node_id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_root_distribution_message_is_consumed_into_append_notice_context_at_next_safe_boundary(tmp_path: Path) -> None:
    from main.runtime.append_notice_context import APPEND_NOTICE_TAIL_PREFIX

    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [],
                            "notes": "root node keeps the new requirement",
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

        service.log_service.upsert_frame(
            record.task_id,
            {
                "node_id": root.node_id,
                "depth": root.depth,
                "node_kind": root.node_kind,
                "phase": "before_model",
                "stage_status": "active",
                "stage_goal": "old stage",
                "callable_tool_names": [
                    "submit_next_stage",
                    "submit_final_result",
                    "spawn_child_nodes",
                    "content_describe",
                    "content_open",
                    "content_search",
                    "exec",
                    "load_skill_context",
                    "load_tool_context",
                ],
                "provider_tool_names": [
                    "submit_next_stage",
                    "submit_final_result",
                    "spawn_child_nodes",
                    "content_describe",
                    "content_open",
                    "content_search",
                    "exec",
                    "load_skill_context",
                    "load_tool_context",
                ],
                "messages": [
                    {"role": "system", "content": "system seed"},
                    {"role": "user", "content": "historic request"},
                ],
            },
        )
        epoch = await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="改成男性角色Top20并停止女性候选收集",
            frontier_node_ids=[record.root_node_id],
        )

        await service.task_actor_service.run_task(record.task_id)

        updated_root = service.store.get_node(record.root_node_id)
        pending_root_notices = list((updated_root.metadata or {}).get("pending_append_notice_records") or [])

        assert updated_root is not None
        assert len(pending_root_notices) == 1
        assert pending_root_notices[0]["notification_id"] == f"root-notice:{epoch.epoch_id}:1"
        assert pending_root_notices[0]["epoch_id"] == epoch.epoch_id
        assert pending_root_notices[0]["source_node_id"] == record.root_node_id
        assert pending_root_notices[0]["message"] == "改成男性角色Top20并停止女性候选收集"
        assert pending_root_notices[0]["created_at"]
        assert pending_root_notices[0]["order_index"] == 1

        resumed = await service.node_runner._resume_react_state(task=task, node=updated_root)
        pending_root_notice_ids = list(resumed.get("pending_root_notice_ids") or [])
        frame = service.log_service.read_runtime_frame(record.task_id, record.root_node_id) or {}

        assert resumed["messages"][-1]["role"] == "user"
        assert resumed["messages"][-1]["content"] == "改成男性角色Top20并停止女性候选收集"
        assert pending_root_notice_ids == [f"root-notice:{epoch.epoch_id}:1"]
        assert frame.get("stage_status") == ""
        assert frame.get("stage_goal") == ""

        service.node_runner._record_consumed_notice_context(
            node_id=record.root_node_id,
            notifications=service.node_runner._pending_root_notice_records_by_ids(
                node_id=record.root_node_id,
                notification_ids=pending_root_notice_ids,
            ),
        )
        service.node_runner._consume_pending_root_notice_records(
            node_id=record.root_node_id,
            notification_ids=pending_root_notice_ids,
        )

        consumed_root = service.store.get_node(record.root_node_id)
        prepared = service._react_loop._prepare_messages(
            list(resumed["messages"]),
            runtime_context={"task_id": record.task_id, "node_id": record.root_node_id},
        )
        prepared_contents = [str(item.get("content") or "") for item in prepared]
        detail = service.query_service.get_node_detail(record.task_id, record.root_node_id, detail_level="full")

        assert consumed_root is not None
        assert list((consumed_root.metadata or {}).get("pending_append_notice_records") or []) == []
        assert sum(1 for content in prepared_contents if "改成男性角色Top20并停止女性候选收集" in content) == 1
        assert all(
            not (content.startswith(APPEND_NOTICE_TAIL_PREFIX) and "改成男性角色Top20并停止女性候选收集" in content)
            for content in prepared_contents
        )
        assert detail is not None
        assert detail.append_notice_messages == [
            {
                "notification_id": f"root-notice:{epoch.epoch_id}:1",
                "epoch_id": epoch.epoch_id,
                "source_node_id": record.root_node_id,
                "message": "改成男性角色Top20并停止女性候选收集",
                "consumed_at": detail.append_notice_messages[0]["consumed_at"],
                "compression_stage_id": "",
            }
        ]
        assert detail.append_notice_messages[0]["consumed_at"]
        assert detail.message_list == [
            {
                "notification_id": f"root-notice:{epoch.epoch_id}:1",
                "epoch_id": epoch.epoch_id,
                "source_node_id": record.root_node_id,
                "message": "改成男性角色Top20并停止女性候选收集",
                "received_at": pending_root_notices[0]["created_at"],
                "consumed_at": detail.message_list[0]["consumed_at"],
                "status": "consumed",
                "compression_stage_id": "",
                "deliveries": [],
            }
        ]
        assert detail.message_list[0]["consumed_at"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_tree_snapshot_exposes_pending_notice_count_for_root(tmp_path: Path) -> None:
    backend = _QueuedChatBackend(
        [
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "submit_message_distribution",
                        "arguments": {
                            "children": [],
                            "notes": "root node keeps the new requirement",
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
        await _seed_distributing_epoch(
            service,
            task_id=record.task_id,
            message="改成男性角色Top20并停止女性候选收集",
            frontier_node_ids=[record.root_node_id],
        )

        await service.task_actor_service.run_task(record.task_id)

        snapshot = service.query_service.get_tree_snapshot(record.task_id)

        assert snapshot is not None
        assert snapshot.nodes_by_id[record.root_node_id].pending_notice_count == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_root_distribution_message_is_consumed_immediately_after_first_model_response(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        service.log_service.upsert_frame(
            record.task_id,
            {
                "node_id": root.node_id,
                "depth": root.depth,
                "node_kind": root.node_kind,
                "phase": "before_model",
                "stage_status": "active",
                "stage_goal": "old stage",
                "callable_tool_names": [
                    "submit_next_stage",
                    "submit_final_result",
                    "spawn_child_nodes",
                    "content_describe",
                    "content_open",
                    "content_search",
                    "exec",
                    "load_skill_context",
                    "load_tool_context",
                ],
                "provider_tool_names": [
                    "submit_next_stage",
                    "submit_final_result",
                    "spawn_child_nodes",
                    "content_describe",
                    "content_open",
                    "content_search",
                    "exec",
                    "load_skill_context",
                    "load_tool_context",
                ],
                "messages": [
                    {"role": "system", "content": "system seed"},
                    {"role": "user", "content": "historic request"},
                ],
            },
        )
        service.log_service.update_node_metadata(
            record.root_node_id,
            lambda metadata: {
                **metadata,
                "pending_append_notice_records": [
                    {
                        "notification_id": "root-notice:epoch:test:1",
                        "epoch_id": "epoch:test",
                        "source_node_id": record.root_node_id,
                        "message": "改成男性角色Top20并停止女性候选收集",
                        "created_at": "2026-04-19T10:00:00+08:00",
                        "order_index": 1,
                    }
                ],
            },
        )

        observed_pending_notice_count = None

        async def fake_react_run(**kwargs):
            nonlocal observed_pending_notice_count
            consume_callback = kwargs.get("runtime_context", {}).get("consume_inflight_notice_callback")
            assert callable(consume_callback)
            consume_callback()
            current = service.store.get_node(record.root_node_id)
            observed_pending_notice_count = len(list((current.metadata or {}).get("pending_append_notice_records") or []))
            return NodeFinalResult(
                status="success",
                delivery_status="final",
                summary="ordinary execution resumed",
                answer="ordinary execution resumed",
                evidence=[],
                remaining_work=[],
                blocking_reason="",
            )

        service.node_runner._react_loop.run = fake_react_run

        await service.node_runner.run_node(record.task_id, record.root_node_id)
        assert observed_pending_notice_count == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_progress_exposes_distribution_runtime_state(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        service.log_service.update_task_runtime_meta(
            record.task_id,
            distribution={
                "active_epoch_id": "epoch:summary",
                "state": "distributing",
                "mode": "task_wide_barrier",
                "frontier_node_ids": ["node:root", "node:child"],
                "blocked_node_ids": ["node:root"],
                "pending_notice_node_ids": ["node:child"],
                "queued_epoch_count": 1,
                "pending_mailbox_count": 2,
            },
        )

        progress = service.query_service.view_progress(record.task_id, mark_read=False)

        assert progress is not None
        assert progress.live_state is not None
        assert progress.live_state.distribution.active_epoch_id == "epoch:summary"
        assert progress.live_state.distribution.state == "distributing"
        assert progress.live_state.distribution.mode == "task_wide_barrier"
        assert progress.live_state.distribution.frontier_node_ids == ["node:root", "node:child"]
        assert progress.live_state.distribution.blocked_node_ids == ["node:root"]
        assert progress.live_state.distribution.pending_notice_node_ids == ["node:child"]
        assert progress.live_state.distribution.pending_mailbox_count == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_append_notice_creates_barrier_state_for_live_tree(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    try:
        record, root, branch_a, branch_b = await seed_live_root_with_two_running_children(service)

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="new constraint",
            session_id=record.session_id,
        )

        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        assert epoch.state == "pause_requested"
        assert set(epoch.payload["barrier_node_ids"]) == {root.node_id, branch_a.node_id, branch_b.node_id}

        progress = service.query_service.view_progress(record.task_id, mark_read=False)
        assert progress is not None
        assert progress.live_state is not None
        assert progress.live_state.distribution.state == "barrier_requested"
        assert set(progress.live_state.distribution.blocked_node_ids) == {root.node_id, branch_a.node_id, branch_b.node_id}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_tree_snapshot_marks_nodes_waiting_for_distribution_barrier(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    try:
        record, root, branch_a, branch_b = await seed_live_root_with_two_running_children(service)

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="new constraint",
            session_id=record.session_id,
        )

        snapshot = service.query_service.get_tree_snapshot(record.task_id)
        assert snapshot is not None
        assert snapshot.nodes_by_id[root.node_id].distribution_status == "barrier_blocked"
        assert snapshot.nodes_by_id[branch_a.node_id].distribution_status == "barrier_blocked"
        assert snapshot.nodes_by_id[branch_b.node_id].distribution_status == "barrier_blocked"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_barrier_drains_running_tree_before_frontier_propagation(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record, root, branch_a, branch_b = await seed_live_root_with_two_running_children(service)

        service.log_service.replace_runtime_frames(
            record.task_id,
            active_node_ids=[branch_a.node_id, branch_b.node_id],
            runnable_node_ids=[],
            waiting_node_ids=[],
            frames=[
                {
                    **service.log_service._default_frame(
                        node_id=branch_a.node_id,
                        depth=branch_a.depth,
                        node_kind=branch_a.node_kind,
                        phase="model.chat.await_response",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                },
                {
                    **service.log_service._default_frame(
                        node_id=branch_b.node_id,
                        depth=branch_b.depth,
                        node_kind=branch_b.node_kind,
                        phase="model.chat.await_response",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                },
            ],
            publish_snapshot=False,
        )

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="new requirement",
            session_id=record.session_id,
        )

        progress = service.query_service.view_progress(record.task_id, mark_read=False)
        assert progress is not None
        assert progress.live_state is not None
        assert progress.live_state.distribution.state == "barrier_requested"

        await service.task_actor_service._run_distribution_epoch(record.task_id)

        refreshed = service.query_service.view_progress(record.task_id, mark_read=False)
        assert refreshed is not None
        assert refreshed.live_state is not None
        assert refreshed.live_state.distribution.state == "barrier_draining"
        assert root.node_id in refreshed.live_state.distribution.blocked_node_ids
        assert branch_a.node_id in refreshed.live_state.distribution.blocked_node_ids
        assert branch_b.node_id in refreshed.live_state.distribution.blocked_node_ids
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_barrier_waits_for_unmaterialized_spawn_children(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("distribution barrier", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        first_spec = SpawnChildSpec(goal="first child", prompt="first prompt", execution_policy={"mode": "focus"})
        second_spec = SpawnChildSpec(goal="second child", prompt="second prompt", execution_policy={"mode": "focus"})
        materialized_child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=first_spec,
            owner_round_id="round-live",
            owner_entry_index=0,
        )
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-live": {
                    "specs": [first_spec.model_dump(mode="json"), second_spec.model_dump(mode="json")],
                    "entries": [
                        service.node_runner._normalize_spawn_entry(
                            index=0,
                            spec=first_spec,
                            entry={
                                "child_node_id": materialized_child.node_id,
                                "review_decision": "allowed",
                                "status": "running",
                            },
                        ),
                        service.node_runner._normalize_spawn_entry(
                            index=1,
                            spec=second_spec,
                            entry={
                                "review_decision": "allowed",
                                "status": "queued",
                            },
                        ),
                    ],
                    "completed": False,
                }
            },
        )
        service.log_service.replace_runtime_frames(
            record.task_id,
            active_node_ids=[root.node_id, materialized_child.node_id],
            runnable_node_ids=[materialized_child.node_id],
            waiting_node_ids=[root.node_id],
            frames=[
                {
                    **service.log_service._default_frame(
                        node_id=root.node_id,
                        depth=root.depth,
                        node_kind=root.node_kind,
                        phase="waiting_children",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                },
                {
                    **service.log_service._default_frame(
                        node_id=materialized_child.node_id,
                        depth=materialized_child.depth,
                        node_kind=materialized_child.node_kind,
                        phase="before_model",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                },
            ],
            publish_snapshot=False,
        )

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="new requirement",
            session_id=record.session_id,
        )

        await service.task_actor_service._run_distribution_epoch(record.task_id)

        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        progress = service.query_service.view_progress(record.task_id, mark_read=False)

        assert epoch.state == "barrier_draining"
        assert root.node_id in list(epoch.payload.get("drain_pending_node_ids") or [])
        assert epoch.payload.get("materialize_pending_entries") == [
            {
                "parent_node_id": root.node_id,
                "round_id": "round-live",
                "entry_index": 1,
                "goal": "second child",
                "status": "queued",
            }
        ]
        assert progress is not None
        assert progress.live_state is not None
        assert progress.live_state.distribution.state == "barrier_draining"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_barrier_waits_when_spawn_entry_references_missing_child_row(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("distribution barrier missing child row", session_id="web:ceo-demo")
        root = service.store.get_node(record.root_node_id)
        assert root is not None

        spec = SpawnChildSpec(goal="late child", prompt="late prompt", execution_policy={"mode": "focus"})
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-live": {
                    "specs": [spec.model_dump(mode="json")],
                    "entries": [
                        service.node_runner._normalize_spawn_entry(
                            index=0,
                            spec=spec,
                            entry={
                                "child_node_id": "node:missing-child",
                                "review_decision": "allowed",
                                "status": "running",
                            },
                        ),
                    ],
                    "completed": False,
                }
            },
        )
        service.log_service.replace_runtime_frames(
            record.task_id,
            active_node_ids=[root.node_id],
            runnable_node_ids=[],
            waiting_node_ids=[root.node_id],
            frames=[
                {
                    **service.log_service._default_frame(
                        node_id=root.node_id,
                        depth=root.depth,
                        node_kind=root.node_kind,
                        phase="waiting_children",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                }
            ],
            publish_snapshot=False,
        )

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="new requirement",
            session_id=record.session_id,
        )

        await service.task_actor_service._run_distribution_epoch(record.task_id)

        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        assert epoch.state == "barrier_draining"
        assert epoch.payload.get("materialize_pending_entries") == [
            {
                "parent_node_id": root.node_id,
                "round_id": "round-live",
                "entry_index": 0,
                "goal": "late child",
                "status": "running",
            }
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_reconcile_spawn_entry_bindings_marks_duplicate_children_and_unblocks_barrier(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("distribution duplicate child reconcile", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        first_spec = SpawnChildSpec(goal="first child", prompt="first prompt", execution_policy={"mode": "focus"})
        second_spec = SpawnChildSpec(goal="second child", prompt="second prompt", execution_policy={"mode": "focus"})
        older_first = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=first_spec,
            owner_round_id="round-live",
            owner_entry_index=0,
        )
        canonical_first = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=first_spec,
            owner_round_id="round-live",
            owner_entry_index=0,
        )
        older_second = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=second_spec,
            owner_round_id="round-live",
            owner_entry_index=1,
        )
        canonical_second = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=second_spec,
            owner_round_id="round-live",
            owner_entry_index=1,
        )
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-live": {
                    "specs": [first_spec.model_dump(mode="json"), second_spec.model_dump(mode="json")],
                    "entries": [
                        service.node_runner._normalize_spawn_entry(
                            index=0,
                            spec=first_spec,
                            entry={"review_decision": "allowed", "status": "running"},
                        ),
                        service.node_runner._normalize_spawn_entry(
                            index=1,
                            spec=second_spec,
                            entry={
                                "child_node_id": canonical_second.node_id,
                                "review_decision": "allowed",
                                "status": "running",
                            },
                        ),
                    ],
                    "completed": False,
                }
            },
        )
        service.log_service.replace_runtime_frames(
            record.task_id,
            active_node_ids=[root.node_id, canonical_first.node_id, canonical_second.node_id],
            runnable_node_ids=[canonical_first.node_id, canonical_second.node_id],
            waiting_node_ids=[root.node_id],
            frames=[
                {
                    **service.log_service._default_frame(
                        node_id=root.node_id,
                        depth=root.depth,
                        node_kind=root.node_kind,
                        phase="waiting_children",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                },
                {
                    **service.log_service._default_frame(
                        node_id=canonical_first.node_id,
                        depth=canonical_first.depth,
                        node_kind=canonical_first.node_kind,
                        phase="before_model",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                },
                {
                    **service.log_service._default_frame(
                        node_id=canonical_second.node_id,
                        depth=canonical_second.depth,
                        node_kind=canonical_second.node_kind,
                        phase="before_model",
                    ),
                    "pending_tool_calls": [],
                    "tool_calls": [],
                    "child_pipelines": [],
                    "pending_child_specs": [],
                    "partial_child_results": [],
                    "last_error": "",
                },
            ],
            publish_snapshot=False,
        )

        changed = service.node_runner.reconcile_spawn_entry_child_bindings(
            task_id=record.task_id,
            parent_node_id=root.node_id,
        )

        refreshed_root = service.store.get_node(root.node_id)
        assert refreshed_root is not None
        entries = refreshed_root.metadata["spawn_operations"]["round-live"]["entries"]
        assert changed is True
        assert entries[0]["child_node_id"] == canonical_first.node_id
        assert entries[1]["child_node_id"] == canonical_second.node_id
        assert service.store.get_node(older_first.node_id).metadata["duplicate_spawn_child"] is True
        assert service.store.get_node(older_second.node_id).metadata["duplicate_spawn_child"] is True
        assert service.node_runner.live_distribution_child_node_ids(
            task_id=record.task_id,
            parent_node_id=root.node_id,
        ) == [canonical_first.node_id, canonical_second.node_id]
        assert service.task_actor_service._barrier_materialize_pending_entries(
            task_id=record.task_id,
            barrier_node_ids=[root.node_id],
        ) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_barrier_waits_when_child_owner_metadata_does_not_match_spawn_entry(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("distribution barrier owner mismatch", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        spec = SpawnChildSpec(goal="late child", prompt="late prompt", execution_policy={"mode": "focus"})
        child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=spec,
            owner_round_id="different-round",
            owner_entry_index=0,
        )
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-live": {
                    "specs": [spec.model_dump(mode="json")],
                    "entries": [
                        service.node_runner._normalize_spawn_entry(
                            index=0,
                            spec=spec,
                            entry={
                                "child_node_id": child.node_id,
                                "review_decision": "allowed",
                                "status": "running",
                            },
                        ),
                    ],
                    "completed": False,
                }
            },
        )

        await service.task_append_notice(
            task_ids=[record.task_id],
            node_ids=[],
            message="new requirement",
            session_id=record.session_id,
        )

        await service.task_actor_service._run_distribution_epoch(record.task_id)

        epoch = service.store.list_active_task_message_distribution_epochs(record.task_id)[0]
        assert epoch.state == "barrier_draining"
        assert epoch.payload.get("materialize_pending_entries") == [
            {
                "parent_node_id": root.node_id,
                "round_id": "round-live",
                "entry_index": 0,
                "goal": "late child",
                "status": "running",
            }
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_distribution_delivery_sets_wait_for_children_resume_mode_for_child_target(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("child mailbox resume mode", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        parent_spec = SpawnChildSpec(goal="branch", prompt="branch prompt", execution_policy={"mode": "focus"})
        branch = service.node_runner._create_execution_child(task=task, parent=root, spec=parent_spec)
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "root-round": {
                    "specs": [parent_spec.model_dump(mode="json")],
                    "entries": [
                        service.node_runner._normalize_spawn_entry(
                            index=0,
                            spec=parent_spec,
                            entry={
                                "child_node_id": branch.node_id,
                                "review_decision": "allowed",
                                "status": "running",
                            },
                        ),
                    ],
                    "completed": False,
                }
            },
        )

        child_spec = SpawnChildSpec(goal="leaf", prompt="leaf prompt", execution_policy={"mode": "focus"})
        leaf = service.node_runner._create_execution_child(
            task=task,
            parent=branch,
            spec=child_spec,
            owner_round_id="branch-round",
            owner_entry_index=0,
        )
        _set_spawn_operations(
            service,
            root_node_id=branch.node_id,
            payload={
                "branch-round": {
                    "specs": [child_spec.model_dump(mode="json")],
                    "entries": [
                        service.node_runner._normalize_spawn_entry(
                            index=0,
                            spec=child_spec,
                            entry={
                                "child_node_id": leaf.node_id,
                                "review_decision": "allowed",
                                "status": "running",
                            },
                        ),
                    ],
                    "completed": False,
                }
            },
        )

        service._deliver_distribution_message(
            task_id=record.task_id,
            epoch_id="epoch:demo",
            source_node_id=root.node_id,
            target_node_id=branch.node_id,
            message="new requirement",
        )

        branch_after = service.get_node(branch.node_id)
        assert branch_after is not None
        pending_notice_state = dict((branch_after.metadata or {}).get(PENDING_NOTICE_STATE_KEY) or {})
        assert pending_notice_state["resume_mode"] == RESUME_MODE_WAIT_FOR_CHILDREN
        assert pending_notice_state["holding_round_id"] == "branch-round"
        assert pending_notice_state["epoch_id"] == "epoch:demo"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resume_ready_recovery_dispatch_targets_pending_notice_nodes_not_root_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("resume ready recovery fanout", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        spec = SpawnChildSpec(goal="branch", prompt="branch prompt", execution_policy={"mode": "focus"})
        branch = service.node_runner._create_execution_child(task=task, parent=root, spec=spec)

        service.log_service.update_task_runtime_meta(
            record.task_id,
            distribution={
                "active_epoch_id": "epoch:demo",
                "state": "resume_ready",
                "mode": "task_wide_barrier",
                "frontier_node_ids": [],
                "blocked_node_ids": [],
                "pending_notice_node_ids": [branch.node_id],
                "queued_epoch_count": 0,
                "pending_mailbox_count": 1,
            },
        )

        dispatched: list[str] = []

        async def _capture_run_node(task_id: str, node_id: str):
            dispatched.append(node_id)
            return NodeFinalResult(
                status="success",
                delivery_status="blocked",
                summary=f"captured {node_id}",
                answer="",
                evidence=[],
                remaining_work=[],
                blocking_reason="",
            )

        monkeypatch.setattr(service.node_runner, "run_node", _capture_run_node)

        await service.task_actor_service.run_task(record.task_id)

        assert dispatched == [branch.node_id]
    finally:
        await service.close()


def test_atomic_child_bind_creates_node_and_updates_spawn_entry_under_same_task_lock(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    record = asyncio.run(service.create_task("atomic bind", session_id="web:ceo-demo"))
    try:
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        spec = SpawnChildSpec(goal="child", prompt="prompt", execution_policy={"mode": "focus"})
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "call:spawn-round": {
                    "specs": [spec.model_dump(mode="json")],
                    "entries": [
                        service.node_runner._normalize_spawn_entry(
                            index=0,
                            spec=spec,
                            entry={"status": "queued"},
                        )
                    ],
                    "completed": False,
                }
            },
        )

        child = NodeRecord(
            node_id="node:atomic-child",
            task_id=task.task_id,
            parent_node_id=root.node_id,
            root_node_id=root.root_node_id,
            depth=root.depth + 1,
            node_kind="execution",
            status="in_progress",
            goal="child",
            prompt="prompt",
            input="prompt",
            output=[],
            check_result="",
            final_output="",
            can_spawn_children=True,
            created_at=now_iso(),
            updated_at=now_iso(),
            token_usage=TokenUsageSummary(tracked=False),
            token_usage_by_model=[],
            metadata={},
        )

        service.log_service.create_child_and_bind_spawn_entry(
            task_id=task.task_id,
            parent_node_id=root.node_id,
            cache_key="call:spawn-round",
            entry_index=0,
            child=child,
        )

        child_after = service.get_node("node:atomic-child")
        parent_after = service.get_node(root.node_id)
        assert child_after is not None
        assert parent_after is not None
        operation = dict(((parent_after.metadata or {}).get("spawn_operations") or {}).get("call:spawn-round") or {})
        entries = list(operation.get("entries") or [])
        assert entries[0]["child_node_id"] == "node:atomic-child"
    finally:
        asyncio.run(service.close())


@pytest.mark.asyncio
async def test_prepare_messages_keeps_append_notice_tail_before_stage_compaction_block(tmp_path: Path) -> None:
    from main.runtime.append_notice_context import APPEND_NOTICE_TAIL_PREFIX

    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        root = service.store.get_node(record.root_node_id)
        assert root is not None

        stage_state = {
            "active_stage_id": "stage-5",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "stage-1",
                    "stage_index": 1,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "完成",
                    "stage_goal": "inspect stage one",
                    "completed_stage_summary": "finished stage one",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                },
                {
                    "stage_id": "stage-2",
                    "stage_index": 2,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "完成",
                    "stage_goal": "inspect stage two",
                    "completed_stage_summary": "finished stage two",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                },
                {
                    "stage_id": "stage-3",
                    "stage_index": 3,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "完成",
                    "stage_goal": "inspect stage three",
                    "completed_stage_summary": "finished stage three",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                },
                {
                    "stage_id": "stage-4",
                    "stage_index": 4,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "完成",
                    "stage_goal": "inspect stage four",
                    "completed_stage_summary": "finished stage four",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                },
                {
                    "stage_id": "stage-5",
                    "stage_index": 5,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "进行中",
                    "stage_goal": "inspect stage five",
                    "completed_stage_summary": "",
                    "key_refs": [],
                    "tool_round_budget": 3,
                    "tool_rounds_used": 0,
                },
            ],
        }
        service.log_service.update_node_metadata(
            root.node_id,
            lambda metadata: {
                **metadata,
                "execution_stages": stage_state,
                "append_notice_context": {
                    "notice_records": [
                        {
                            "notification_id": "notif:tail-1",
                            "epoch_id": "epoch:tail",
                            "source_node_id": root.node_id,
                            "message": "必须按董事会模板输出",
                            "consumed_at": "2026-04-19T10:00:00+08:00",
                            "compression_stage_id": "",
                        }
                    ],
                    "compression_segments": [],
                },
            },
        )

        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "{\"task_id\":\"task-1\",\"goal\":\"demo\"}"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-stage-1", "type": "function", "function": {"name": "submit_next_stage", "arguments": "{}"}}]},
            {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": "{\"ok\": true}"},
            {"role": "assistant", "content": "stage one raw detail"},
            {"role": "user", "content": "必须按董事会模板输出"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-stage-2", "type": "function", "function": {"name": "submit_next_stage", "arguments": "{}"}}]},
            {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-2", "content": "{\"ok\": true}"},
            {"role": "assistant", "content": "stage two raw detail"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-stage-3", "type": "function", "function": {"name": "submit_next_stage", "arguments": "{}"}}]},
            {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-3", "content": "{\"ok\": true}"},
            {"role": "assistant", "content": "stage three raw detail"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-stage-4", "type": "function", "function": {"name": "submit_next_stage", "arguments": "{}"}}]},
            {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-4", "content": "{\"ok\": true}"},
            {"role": "assistant", "content": "stage four raw detail"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-stage-5", "type": "function", "function": {"name": "submit_next_stage", "arguments": "{}"}}]},
            {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-5", "content": "{\"ok\": true}"},
            {"role": "assistant", "content": "current stage assistant detail"},
        ]

        prepared = service._react_loop._prepare_messages(
            messages,
            runtime_context={"task_id": record.task_id, "node_id": root.node_id},
        )

        contents = [str(item.get("content") or "") for item in prepared]
        notice_index = next(index for index, content in enumerate(contents) if content.startswith(APPEND_NOTICE_TAIL_PREFIX))
        compact_index = next(index for index, content in enumerate(contents) if content.startswith("[G3KU_STAGE_COMPACT_V1]"))

        assert notice_index < compact_index
        assert "必须按董事会模板输出" in contents[notice_index]
    finally:
        await service.close()


@pytest.mark.skip(reason="covered by unicode-safe replacement below")
@pytest.mark.asyncio
async def test_new_compression_stage_rolls_append_notices_into_compressed_tail_block(tmp_path: Path) -> None:
    from main.models import ExecutionStageRecord, ExecutionStageState
    from main.runtime.append_notice_context import APPEND_NOTICE_TAIL_PREFIX

    service = _build_service(tmp_path)
    try:
        record = await service.create_task("整理重点客户流失信号", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None

        service.log_service.update_node_metadata(
            root.node_id,
            lambda metadata: {
                **metadata,
                "append_notice_context": {
                    "notice_records": [
                        {
                            "notification_id": "notif:compression-1",
                            "epoch_id": "epoch:compression",
                            "source_node_id": root.node_id,
                            "message": "必须按董事会模板输出",
                            "consumed_at": "2026-04-19T10:00:00+08:00",
                            "compression_stage_id": "",
                        },
                        {
                            "notification_id": "notif:compression-2",
                            "epoch_id": "epoch:compression",
                            "source_node_id": root.node_id,
                            "message": "必须补充风险分层",
                            "consumed_at": "2026-04-19T10:05:00+08:00",
                            "compression_stage_id": "",
                        },
                    ],
                    "compression_segments": [],
                },
            },
        )

        stage_state = ExecutionStageState(
                active_stage_id="stage-14",
                transition_required=False,
                stages=[
                    ExecutionStageRecord(
                        stage_id="stage-compression-1",
                        stage_index=10,
                    stage_kind="compression",
                    system_generated=True,
                    mode="自主执行",
                    status="完成",
                    stage_goal="Archive completed stage history 1-10",
                    completed_stage_summary="archived old stages",
                    key_refs=[],
                    archive_ref="artifact:artifact:stage-archive-1",
                    archive_stage_index_start=1,
                    archive_stage_index_end=10,
                    tool_round_budget=0,
                    tool_rounds_used=0,
                        created_at="2026-04-19T10:10:00+08:00",
                        finished_at="2026-04-19T10:10:00+08:00",
                        rounds=[],
                    ),
                    ExecutionStageRecord(
                        stage_id="stage-11",
                        stage_index=11,
                        stage_kind="normal",
                        system_generated=False,
                        mode="自主执行",
                        status="完成",
                        stage_goal="completed stage eleven",
                        completed_stage_summary="finished stage eleven",
                        key_refs=[],
                        tool_round_budget=3,
                        tool_rounds_used=1,
                        created_at="2026-04-19T10:11:00+08:00",
                        finished_at="2026-04-19T10:11:30+08:00",
                        rounds=[],
                    ),
                    ExecutionStageRecord(
                        stage_id="stage-12",
                        stage_index=12,
                        stage_kind="normal",
                        system_generated=False,
                        mode="自主执行",
                        status="完成",
                        stage_goal="completed stage twelve",
                        completed_stage_summary="finished stage twelve",
                        key_refs=[],
                        tool_round_budget=3,
                        tool_rounds_used=1,
                        created_at="2026-04-19T10:12:00+08:00",
                        finished_at="2026-04-19T10:12:30+08:00",
                        rounds=[],
                    ),
                    ExecutionStageRecord(
                        stage_id="stage-13",
                        stage_index=13,
                        stage_kind="normal",
                        system_generated=False,
                        mode="自主执行",
                        status="完成",
                        stage_goal="completed stage thirteen",
                        completed_stage_summary="finished stage thirteen",
                        key_refs=[],
                        tool_round_budget=3,
                        tool_rounds_used=1,
                        created_at="2026-04-19T10:13:00+08:00",
                        finished_at="2026-04-19T10:13:30+08:00",
                        rounds=[],
                    ),
                    ExecutionStageRecord(
                        stage_id="stage-14",
                        stage_index=14,
                        stage_kind="normal",
                        system_generated=False,
                        mode="自主执行",
                        status="进行中",
                        stage_goal="current stage",
                        completed_stage_summary="",
                        key_refs=[],
                        tool_round_budget=3,
                        tool_rounds_used=0,
                        created_at="2026-04-19T10:14:00+08:00",
                        finished_at="",
                        rounds=[],
                    ),
                ],
            )

        service.log_service._persist_execution_stage_state_locked(task=task, node_id=root.node_id, state=stage_state)

        updated_root = service.store.get_node(root.node_id)
        append_notice_context = dict((updated_root.metadata or {}).get("append_notice_context") or {})
        assert append_notice_context["compression_segments"][0]["compression_stage_id"] == "stage-compression-1"

        prepared = service._react_loop._prepare_messages(
            [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}],
            runtime_context={"task_id": record.task_id, "node_id": root.node_id},
        )
        contents = [str(item.get("content") or "") for item in prepared]
        notice_index = next(index for index, content in enumerate(contents) if content.startswith(APPEND_NOTICE_TAIL_PREFIX))
        compression_index = next(index for index, content in enumerate(contents) if content.startswith("[G3KU_STAGE_EXTERNALIZED_V1]"))

        assert notice_index < compression_index
        assert "必须按董事会模板输出" in contents[notice_index]
        assert "必须补充风险分层" in contents[notice_index]
    finally:
        await service.close()
