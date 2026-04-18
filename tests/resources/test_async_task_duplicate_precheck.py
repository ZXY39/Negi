from __future__ import annotations

from pathlib import Path

import pytest

from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be used in this test: {kwargs!r}")


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


@pytest.mark.asyncio
async def test_precheck_rejects_exact_core_requirement_duplicate(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        first = await service.create_task(
            "整理本周用户投诉并给出处理建议",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理本周用户投诉并给出处理建议",
                "execution_policy": {"mode": "focus"},
            },
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="整理本周用户投诉并给出处理建议",
            core_requirement="整理本周用户投诉并给出处理建议",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "reject_duplicate"
        assert decision["matched_task_id"] == first.task_id
        assert decision["decision_source"] == "rule"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_includes_paused_tasks_in_duplicate_pool(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        first = await service.create_task(
            "整理北美客户续费流失原因",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理北美客户续费流失原因",
                "execution_policy": {"mode": "focus"},
            },
        )
        await service.pause_task(first.task_id)

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="整理北美客户续费流失原因",
            core_requirement="整理北美客户续费流失原因",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "reject_duplicate"
        assert decision["matched_task_id"] == first.task_id
        assert decision["decision_source"] == "rule"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_allows_distinct_new_task(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        await service.create_task(
            "整理北美客户续费流失原因",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理北美客户续费流失原因",
                "execution_policy": {"mode": "focus"},
            },
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="设计续费流失挽回实验方案",
            core_requirement="设计续费流失挽回实验方案",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "approve_new"
        assert decision["matched_task_id"] == ""
        assert decision["decision_source"] == "rule"
    finally:
        await service.close()
