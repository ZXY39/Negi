"""验收车道的状态翻转必须同时落到任务树的两份读数。

节点树徽标只按节点自己的状态判读：`in_progress` 画等待中/运行中，终态才画 SUCCESS/FAILED。
验收打回把执行节点与验收节点一起摘回非终态、执行节点真失败时把验收节点判成终局 failed——
这两类写点都绕过 `update_node_status`，少发一帧就会让浏览器整段返工期间挂着上一次裁定的
FAILED，或让一个终局 failed 在快照里永远读成非终态。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from main.models import NodeFinalResult, normalize_final_acceptance_metadata
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in acceptance publish tests: {kwargs!r}")


def _build_service(tmp_path: Path) -> MainRuntimeService:
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


def _capture_node_patches(service: MainRuntimeService) -> list[dict]:
    captured: list[dict] = []
    original = service.log_service._dispatch_live_event_locked
    service.log_service._dispatch_live_event_locked = (
        lambda **kwargs: (captured.append(kwargs), original(**kwargs))[1]
    )
    return captured


def _published_statuses(captured: list[dict], node_id: str, mark: int) -> list[str]:
    statuses: list[str] = []
    for kwargs in captured[mark:]:
        if kwargs.get("event_type") != "task.node.patch":
            continue
        node = (kwargs.get("data") or {}).get("node") or {}
        if str(node.get("node_id") or "").strip() != node_id:
            continue
        statuses.append(str(node.get("status") or ""))
    return statuses


def _served_statuses(service: MainRuntimeService, task_id: str) -> dict[str, str]:
    payload = service.get_task_tree_snapshot_payload(task_id) or {}
    nodes = (payload.get("nodes_by_id") or {}).values()
    return {str(item.get("node_kind") or ""): str(item.get("status") or "") for item in nodes}


async def _final_acceptance_pair(service: MainRuntimeService):
    record = await service.create_task(
        "acceptance publish",
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    acceptance_id = normalize_final_acceptance_metadata(
        (task.metadata or {}).get("final_acceptance")
    ).node_id
    acceptance = service.store.get_node(acceptance_id)
    service.node_runner._set_execution_waiting_acceptance_state(
        task_id=task.task_id,
        execution_node_id=root.node_id,
        acceptance_node_id=acceptance.node_id,
        result_ref="artifact:result",
        result_summary="draft answer",
    )
    # 交付已提交、验收回合已下裁定：两侧都由 `_mark_finished` 落成终态。
    service.log_service.update_node_status(
        task.task_id, root.node_id, status="success", final_output="draft answer",
    )
    service.log_service.update_node_status(
        task.task_id, acceptance.node_id, status="failed", failure_reason="missing board format",
    )
    return record, task, root, acceptance


@pytest.mark.asyncio
async def test_rejection_reactivates_both_nodes_on_the_tree_patch_lane(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    captured = _capture_node_patches(service)
    try:
        record, task, root, acceptance = await _final_acceptance_pair(service)
        mark = len(captured)

        handled = service.node_runner._handle_acceptance_node_result(
            task=task,
            acceptance=service.store.get_node(acceptance.node_id),
            result=NodeFinalResult(
                status="failed",
                delivery_status="final",
                summary="missing board format",
                answer="missing board format",
                evidence=[],
                remaining_work=[],
                blocking_reason="missing board format",
            ),
        )

        assert handled.delivery_status == "partial"
        assert _served_statuses(service, record.task_id) == {
            "execution": "in_progress",
            "acceptance": "in_progress",
        }
        # 返工期间树上不许留 FAILED：两侧各收到一帧非终态补丁。
        assert _published_statuses(captured, acceptance.node_id, mark) == ["in_progress"]
        assert _published_statuses(captured, root.node_id, mark) == ["in_progress"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_execution_failure_cancelled_acceptance_shows_terminal_failure(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    captured = _capture_node_patches(service)
    try:
        record, task, root, acceptance = await _final_acceptance_pair(service)
        service.node_runner._handle_acceptance_node_result(
            task=task,
            acceptance=service.store.get_node(acceptance.node_id),
            result=NodeFinalResult(
                status="failed",
                delivery_status="final",
                summary="missing board format",
                answer="missing board format",
                evidence=[],
                remaining_work=[],
                blocking_reason="missing board format",
            ),
        )
        mark = len(captured)

        service.log_service.update_node_status(
            task.task_id, root.node_id, status="failed", failure_reason="execution node crashed",
        )
        service.node_runner._cancel_waiting_acceptance_for_execution_failure(
            task_id=task.task_id,
            execution_node_id=root.node_id,
            reason="execution node crashed",
        )

        assert service.store.get_node(acceptance.node_id).status == "failed"
        # 终局失败必须读得到：只写存储会让它在快照里一直停在非终态。
        assert _served_statuses(service, record.task_id)["acceptance"] == "failed"
        assert _published_statuses(captured, acceptance.node_id, mark) == ["failed"]
    finally:
        await service.close()
