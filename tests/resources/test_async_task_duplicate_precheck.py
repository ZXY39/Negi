from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be used in this test: {kwargs!r}")


class _ReviewBackend:
    def __init__(self, response):
        self._response = response

    async def chat(self, **kwargs):
        _ = kwargs
        return self._response


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
async def test_precheck_allows_new_task_when_session_has_no_unfinished_tasks(tmp_path: Path):
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


@pytest.mark.asyncio
async def test_precheck_uses_llm_to_allow_distinct_new_task(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_ReviewBackend(SimpleNamespace(tool_calls=[], content="")),
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
        service._chat_backend = _ReviewBackend(
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "review_async_task_duplicate_precheck",
                        "arguments": {
                            "decision": "approve_new",
                            "reason": "the candidate task has a different goal and deliverable",
                        },
                    }
                ],
                content="",
            )
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
        assert decision["decision_source"] == "llm"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_uses_llm_to_reject_fuzzy_duplicate(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_ReviewBackend(SimpleNamespace(tool_calls=[], content="")),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        first = await service.create_task(
            "整理重点客户流失信号",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理重点客户流失信号",
                "execution_policy": {"mode": "focus"},
            },
        )
        service._chat_backend = _ReviewBackend(
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "review_async_task_duplicate_precheck",
                        "arguments": {
                            "decision": "reject_duplicate",
                            "matched_task_id": first.task_id,
                            "reason": "same goal and deliverable as existing task",
                        },
                    }
                ],
                content="",
            )
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="汇总重点客户流失预警并整理结论",
            core_requirement="汇总重点客户流失预警并整理结论",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "reject_duplicate"
        assert decision["matched_task_id"] == first.task_id
        assert decision["decision_source"] == "llm"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_returns_append_notice_decision_when_old_task_needs_new_constraints(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_ReviewBackend(SimpleNamespace(tool_calls=[], content="")),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        first = await service.create_task(
            "整理重点客户流失信号",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理重点客户流失信号",
                "execution_policy": {"mode": "focus"},
            },
        )
        service._chat_backend = _ReviewBackend(
            SimpleNamespace(
                tool_calls=[
                    {
                        "name": "review_async_task_duplicate_precheck",
                        "arguments": {
                            "decision": "reject_use_append_notice",
                            "matched_task_id": first.task_id,
                            "reason": "existing task only needs the new acceptance constraint",
                        },
                    }
                ],
                content="",
            )
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="整理重点客户流失信号并新增董事会验收格式",
            core_requirement="整理重点客户流失信号并新增董事会验收格式",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=True,
            final_acceptance_prompt="必须按董事会模板输出",
        )

        assert decision["decision"] == "reject_use_append_notice"
        assert decision["matched_task_id"] == first.task_id
        assert decision["decision_source"] == "llm"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_falls_back_to_approve_when_llm_review_is_unavailable(tmp_path: Path):
    class _BrokenBackend:
        async def chat(self, **kwargs):
            raise RuntimeError(f"inspection backend unavailable: {kwargs!r}")

    service = MainRuntimeService(
        chat_backend=_BrokenBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        await service.create_task(
            "整理重点客户流失信号",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理重点客户流失信号",
                "execution_policy": {"mode": "focus"},
            },
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="汇总重点客户流失预警并整理结论",
            core_requirement="汇总重点客户流失预警并整理结论",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "approve_new"
        assert decision["matched_task_id"] == ""
        assert decision["decision_source"] == "fallback"
    finally:
        await service.close()
