"""task_progress 文本真化回归测试。

锁定三条契约(对应 task_progress 误导性状态治理):
1. 未获得调度证据的验收节点渲染为「待检验」,绝不渲染为「检验中」;
2. 「运行中」只授予新鲜的 active 帧,陈旧 active 帧降级为「疑似中断」;
3. 恢复路径不再凭空制造 active 帧(只标记 runnable),活动/验收状态进入文本头部。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from main.models import TaskRecord
from main.monitoring.query_service import TaskQueryService
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _mark_worker_online(service: MainRuntimeService) -> None:
    updated_at = now_iso()
    item = {
        "worker_id": "worker:test",
        "role": "task_worker",
        "status": "running",
        "updated_at": updated_at,
        "payload": {"execution_mode": "worker", "active_task_count": 0},
    }
    service.store.upsert_worker_status(
        worker_id=str(item["worker_id"]),
        role=str(item["role"]),
        status=str(item["status"]),
        updated_at=str(item["updated_at"]),
        payload=dict(item["payload"]),
    )
    service.publish_worker_status_event(item=item)


def _build_service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )


def _create_web_task(service: MainRuntimeService):
    _mark_worker_online(service)
    return service.create_task("test task", session_id="web:shared")


def _stale_iso(minutes: float = 11.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_progress_undispatched_acceptance_shows_waiting_not_verifying(tmp_path: Path):
    service = _build_service(tmp_path)
    record = asyncio.run(_create_web_task(service))
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=root,
        goal="最终验收:测试",
        acceptance_prompt="检查结果是否满足要求。",
        parent_node_id=root.node_id,
        metadata={"final_acceptance": True},
    )

    text = service.view_progress(record.task_id, mark_read=False)

    assert text.startswith("Task status: in_progress\n")
    # 未派发任何检验调度 → 验收节点只能显示等待态,禁止「检验中」。
    assert "待检验" in text
    assert "检验中" not in text
    # 头部必须携带活动时间与调度计数,给模型判读留出证据。
    assert "最近活动:" in text
    assert "调度: execution(运行" in text
    assert "inspection(运行" in text


def test_progress_fresh_active_acceptance_frame_shows_verifying(tmp_path: Path):
    service = _build_service(tmp_path)
    record = asyncio.run(_create_web_task(service))
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=root,
        goal="最终验收:测试",
        acceptance_prompt="检查结果是否满足要求。",
        parent_node_id=root.node_id,
        metadata={"final_acceptance": True},
    )

    service.log_service.replace_runtime_frames(
        record.task_id,
        frames=[
            service.log_service._default_frame(
                node_id=acceptance.node_id,
                depth=int(acceptance.depth or 0),
                node_kind="acceptance",
                phase="in_model_round",
            )
        ],
        active_node_ids=[acceptance.node_id],
    )

    text = service.view_progress(record.task_id, mark_read=False)

    assert acceptance.node_id in text
    assert "检验中" in text
    assert "运行中" in text
    assert "待检验" not in text
    assert "疑似中断" not in text


def test_progress_stale_active_frame_shows_suspected_interruption(tmp_path: Path):
    service = _build_service(tmp_path)
    record = asyncio.run(_create_web_task(service))
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    acceptance = service.node_runner.create_acceptance_node(
        task=task,
        accepted_node=root,
        goal="最终验收:测试",
        acceptance_prompt="检查结果是否满足要求。",
        parent_node_id=root.node_id,
        metadata={"final_acceptance": True},
    )

    service.log_service.replace_runtime_frames(
        record.task_id,
        frames=[
            service.log_service._default_frame(
                node_id=acceptance.node_id,
                depth=int(acceptance.depth or 0),
                node_kind="acceptance",
                phase="in_model_round",
            )
        ],
        active_node_ids=[acceptance.node_id],
    )
    # 把活跃帧的时间戳改旧:超过陈旧阈值后必须降级,不得再声称「运行中/检验中」。
    current = service.store.get_task_runtime_frame(record.task_id, acceptance.node_id)
    assert current is not None
    service.store.upsert_task_runtime_frame(
        current.model_copy(update={"updated_at": _stale_iso(minutes=11.0)})
    )

    text = service.view_progress(record.task_id, mark_read=False)

    assert acceptance.node_id in text
    assert "疑似中断" in text
    assert "运行中" not in text
    assert "检验中" not in text
    assert "待检验" in text


def test_progress_header_shows_final_acceptance_waiting_state(tmp_path: Path):
    service = _build_service(tmp_path)
    record = asyncio.run(_create_web_task(service))
    task = service.get_task(record.task_id)
    assert task is not None

    def _mutate(metadata):
        metadata["final_acceptance"] = {
            "required": True,
            "prompt": "检查最终结果。",
            "status": "waiting_acceptance",
        }
        return metadata

    service.log_service.update_task_metadata(record.task_id, _mutate, mark_unread=False)

    text = service.view_progress(record.task_id, mark_read=False)

    assert "验收: 等待验收" in text


class _StubLogService:
    def read_task_runtime_meta(self, task_id: str):
        return {}


def test_progress_recent_activity_ignores_ledger_bumped_task_updated_at():
    """mark_task_read 等账本动作会刷新 task.updated_at;「最近活动」不得取它,
    否则「刚查看过」会被误当成「有真实执行活动」。"""
    query_service = TaskQueryService(
        store=None,
        file_store=None,
        log_service=_StubLogService(),
    )
    task = TaskRecord(
        task_id="task:dated",
        title="t",
        user_request="u",
        root_node_id="node:root",
        created_at=_stale_iso(minutes=60 * 24),
        updated_at=now_iso(),
    )
    lines = query_service._task_progress_activity_lines(
        task,
        live_state=None,
        final_acceptance_label="",
        fallback_activity_at="",
    )
    joined = " | ".join(lines)
    # 回退到创建时间(1 天前),而不是被账本刷新的 updated_at(刚刚)。
    assert "最近活动: 1 天前" in joined
    assert "刚刚" not in joined


def test_recover_interrupted_task_fabricates_runnable_not_active_frame(tmp_path: Path):
    """恢复路径契约:frames 为空时制造的帧只表示「可运行、待调度」,不得标 active。"""
    service = _build_service(tmp_path)
    record = asyncio.run(_create_web_task(service))
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None

    # 清空全部运行时帧,模拟「干净重启、无遗留执行状态」。
    service.log_service.replace_runtime_frames(
        record.task_id,
        frames=[],
        active_node_ids=[],
        runnable_node_ids=[],
        waiting_node_ids=[],
    )
    assert list(service.store.list_task_runtime_frames(record.task_id)) == []

    service._recover_interrupted_task(record.task_id)

    records = list(service.store.list_task_runtime_frames(record.task_id))
    assert len(records) == 1
    assert records[0].node_id == root.node_id
    assert records[0].active is False
    assert records[0].runnable is True

    runtime_state = service.log_service.read_runtime_state(record.task_id) or {}
    assert runtime_state.get("active_node_ids") == []
    assert runtime_state.get("runnable_node_ids") == [root.node_id]