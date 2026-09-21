"""帧活性判据的单一来源与下发回归。

节点树的状态徽标要区分「此刻真在执行」和「留下一帧没人再写」，而前端拿不到帧的
时间戳，也无法自己算阈值。契约因此是：后端在两份 runtime_summary 帧载荷上都带
``stale``，阈值与 ``task_progress`` 的活性标注共用 main.monitoring.frame_liveness。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from main.monitoring.frame_liveness import STALE_FRAME_MINUTES, frame_is_stale
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _build_service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
        execution_model_refs=["fake"],
        acceptance_model_refs=["fake"],
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_frame_is_stale_only_proves_age_is_known() -> None:
    reference = _now()
    assert frame_is_stale((reference - timedelta(minutes=1)).isoformat(), now=reference) is False
    assert frame_is_stale(
        (reference - timedelta(minutes=STALE_FRAME_MINUTES + 1)).isoformat(), now=reference
    ) is True
    # 时间戳读不出来时按「新鲜」处理：年龄未知不等于年龄已证明。
    assert frame_is_stale("", now=reference) is False
    assert frame_is_stale("not-a-timestamp", now=reference) is False


@pytest.mark.asyncio
async def test_runtime_summary_frames_carry_staleness_flag(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("frame liveness", session_id="web:ceo-demo")
        task_id = record.task_id
        service.log_service.upsert_frame(
            task_id,
            {
                "node_id": record.root_node_id,
                "depth": 0,
                "node_kind": "execution",
                "phase": "before_model",
                "tool_calls": [],
                "child_pipelines": [],
            },
            publish_snapshot=False,
        )

        def _flagged_frames() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
            state = service.log_service.read_runtime_state(task_id) or {}
            summary = service.log_service._runtime_summary_payload(task_id)
            return (
                list(state.get("frames") or []),
                list(summary.get("frames") or []),
            )

        for frames in _flagged_frames():
            assert [str(item.get("node_id")) for item in frames] == [record.root_node_id]
            assert frames[0]["stale"] is False

        stored = service.store.get_task_runtime_frame(task_id, record.root_node_id)
        assert stored is not None
        aged = stored.model_copy(
            update={"updated_at": (_now() - timedelta(minutes=STALE_FRAME_MINUTES + 5)).isoformat()}
        )
        service.store.replace_task_runtime_frames(task_id, [aged])

        state_frames, summary_frames = _flagged_frames()
        assert state_frames[0]["stale"] is True
        assert summary_frames[0]["stale"] is True

        snapshot = service.query_service.get_task_snapshot(task_id, mark_read=False)
        assert snapshot is not None
        frames = list((snapshot.get("runtime_summary") or {}).get("frames") or [])
        assert [item["node_id"] for item in frames] == [record.root_node_id]
        assert frames[0]["stale"] is True
    finally:
        await service.close()
