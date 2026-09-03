from __future__ import annotations

from pathlib import Path

import pytest

from main.models import NodeFinalResult, SpawnChildSpec
from main.prompts import load_prompt
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_REJECTED_TERMINAL,
    ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
    normalize_acceptance_handshake,
)
from main.runtime.node_runner import (
    _BLOCKED_VERIFICATION_LOG_KEY,
    _BLOCKED_VERIFICATION_ONLY_KEY,
    _REJECTION_HISTORY_KEY,
)
from main.runtime.react_loop import ReActToolLoop
from main.runtime.stage_budget import STAGE_TOOL_ROUND_BUDGET_MIN
from main.service.runtime_service import MainRuntimeService
from main.types import KIND_ACCEPTANCE, STATUS_FAILED, STATUS_SUCCESS


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in blocked-verification tests: {kwargs!r}")


def _make_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None
    return service


async def _create_task(service: MainRuntimeService):
    return await service.create_task("blocked gate task", session_id="web:shared")


def _create_execution_child(service: MainRuntimeService, *, task, parent, name: str = "child"):
    return service.node_runner._create_execution_child(
        task=task,
        parent=parent,
        spec=SpawnChildSpec(
            goal=f"{name} goal",
            prompt=f"{name} prompt",
            execution_policy={"mode": "focus"},
        ),
    )


def _blocked_result(*, summary: str = "占位", blocking_reason: str = "占位，避免误结束") -> NodeFinalResult:
    return NodeFinalResult(
        status="failed",
        delivery_status="blocked",
        summary=summary,
        answer=summary,
        evidence=[],
        remaining_work=["需要实际落盘 child_c.md 后重新提交"],
        blocking_reason=blocking_reason,
    )


def _verdict(
    status: str,
    *,
    delivery: str = "final",
    summary: str = "",
    blocking_reason: str = "",
    evidence: list | None = None,
) -> NodeFinalResult:
    return NodeFinalResult(
        status=status,
        delivery_status=delivery,
        summary=summary,
        answer=summary,
        evidence=list(evidence or []),
        remaining_work=[],
        blocking_reason=blocking_reason,
    )


def _install_fake_verifier(service: MainRuntimeService, verdicts: list[NodeFinalResult]) -> list[str]:
    """Patch _run_nested_node: hand back scripted verdicts and mark the verifier terminal."""
    calls: list[str] = []
    remaining = list(verdicts)

    async def fake_run_nested(task_id: str, node_id: str) -> NodeFinalResult:
        calls.append(node_id)
        verdict = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        service.log_service.update_node_status(
            task_id,
            node_id,
            status=verdict.status,
            final_output=str(verdict.answer or verdict.summary or ""),
            failure_reason="" if verdict.status == STATUS_SUCCESS else str(verdict.blocking_reason or verdict.summary or ""),
        )
        return verdict

    service.node_runner._run_nested_node = fake_run_nested
    return calls


def _install_fake_rerun(service: MainRuntimeService, result: NodeFinalResult) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        calls.append((task_id, node_id))
        return result

    service.node_runner.run_node = fake_run_node
    return calls


def _install_forbidden_verifier(service: MainRuntimeService) -> None:
    async def fake_run_nested(task_id: str, node_id: str) -> NodeFinalResult:
        raise AssertionError(f"verifier should not run: {node_id}")

    service.node_runner._run_nested_node = fake_run_nested


def _notifications_for(service: MainRuntimeService, task_id: str, node_id: str) -> list[str]:
    return [
        str(getattr(item, "message", "") or "")
        for item in list(service.store.list_task_node_notifications(task_id, node_id) or [])
    ]


def _acceptance_children(service: MainRuntimeService, node_id: str):
    return [
        item
        for item in list(service.store.list_children(node_id) or [])
        if str(getattr(item, "node_kind", "") or "").strip().lower() == KIND_ACCEPTANCE
    ]


def _handshake(service: MainRuntimeService, node_id: str) -> dict:
    node = service.get_node(node_id)
    return normalize_acceptance_handshake((node.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)) if node else {}


@pytest.mark.asyncio
async def test_gate_allows_justified_block_and_creates_adhoc_verifier(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        service.log_service.submit_next_stage(
            record.task_id,
            child.node_id,
            stage_goal="从已落盘的 batch 文件汇总生成 child_c.md",
            tool_round_budget=STAGE_TOOL_ROUND_BUDGET_MIN,
        )
        verifier_calls = _install_fake_verifier(service, [
            _verdict(
                "success",
                summary="阻塞成立：目标依赖缺失已核验",
                evidence=[{"kind": "file", "path": "temp/missing.txt", "note": "确认文件不存在"}],
            ),
        ])

        child = service.get_node(child.node_id)
        result = await service.node_runner._maybe_gate_blocked_submission(
            task=task,
            node=child,
            result=_blocked_result(),
        )

        assert result is not None
        assert result.status == STATUS_FAILED
        assert len(verifier_calls) == 1

        verifiers = _acceptance_children(service, child.node_id)
        assert len(verifiers) == 1
        verifier = verifiers[0]
        assert str(verifier.goal or "").startswith("blocked-check:")
        assert bool((verifier.metadata or {}).get(_BLOCKED_VERIFICATION_ONLY_KEY))
        assert service.get_node(verifier.node_id).status == STATUS_SUCCESS

        failed_child = service.get_node(child.node_id)
        assert failed_child.status == STATUS_FAILED
        assert "阻塞成立" in str(failed_child.failure_reason or "")
        assert "[blocked核验]" in str(failed_child.failure_reason or "")

        handshake = _handshake(service, child.node_id)
        assert handshake.get("state") == ACCEPTANCE_STATE_REJECTED_TERMINAL
        assert handshake.get("acceptance_node_id") == verifier.node_id

        log = list((failed_child.metadata or {}).get(_BLOCKED_VERIFICATION_LOG_KEY) or [])
        assert log and log[-1]["decision"] == "allowed:justified"

        messages = _notifications_for(service, record.task_id, verifier.node_id)
        activation = next(item for item in messages if "failed+blocked" in item)
        assert "执行节点提交了 failed+blocked" in activation
        assert "占位，避免误结束" in activation
        assert "机械信号" in activation
        assert "tool_round_budget" in activation
        assert "判定契约" in activation
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gate_rejects_unjustified_block_and_resumes_execution(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        _install_fake_verifier(service, [
            _verdict("failed", summary="阻塞不成立", blocking_reason="预算未用尽，请继续写入 child_c.md 后重新提交"),
        ])
        rerun_result = NodeFinalResult(
            status="success", delivery_status="final", summary="done", answer="done",
            evidence=[], remaining_work=[], blocking_reason="",
        )
        rerun_calls = _install_fake_rerun(service, rerun_result)

        result = await service.node_runner._maybe_gate_blocked_submission(
            task=task,
            node=service.get_node(child.node_id),
            result=_blocked_result(),
        )

        assert result is rerun_result
        assert rerun_calls == [(record.task_id, child.node_id)]

        handshake = _handshake(service, child.node_id)
        assert handshake.get("state") == ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY
        assert int(handshake.get("rejection_count") or 0) == 1

        feedback = _notifications_for(service, record.task_id, child.node_id)
        assert any("预算未用尽" in item for item in feedback)

        verifier = _acceptance_children(service, child.node_id)[0]
        assert service.get_node(verifier.node_id).status == "in_progress"

        log = list((service.get_node(child.node_id).metadata or {}).get(_BLOCKED_VERIFICATION_LOG_KEY) or [])
        assert log and log[-1]["decision"] == "rejected"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gate_reuses_waiting_acceptance_and_preserves_original_rejection(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=child,
            goal=f"accept:{child.goal}",
            acceptance_prompt="verify child output",
            parent_node_id=child.node_id,
        )
        original_reason = "child_c.md 未落盘，且主输出虚假声称已写入，需执行节点补写后重新提交"
        service.log_service.update_node_status(
            record.task_id,
            acceptance.node_id,
            status=STATUS_FAILED,
            final_output="验收未通过（拒绝交付）",
            failure_reason=original_reason,
        )
        service.node_runner._update_execution_acceptance_handshake(
            node_id=child.node_id,
            state=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
            acceptance_node_id=acceptance.node_id,
            rejection_count=1,
            max_rejections=3,
            latest_execution_result_ref="",
            latest_execution_result_summary="",
            latest_rejection_feedback_ref="",
            latest_rejection_feedback_summary=original_reason,
        )
        _install_fake_verifier(service, [
            _verdict("failed", summary="阻塞不成立", blocking_reason="仍有预算，去合并 batch 文件落盘 child_c.md"),
        ])
        rerun_calls = _install_fake_rerun(service, NodeFinalResult(
            status="success", delivery_status="final", summary="done", answer="done",
            evidence=[], remaining_work=[], blocking_reason="",
        ))

        result = await service.node_runner._maybe_gate_blocked_submission(
            task=service.get_task(record.task_id),
            node=service.get_node(child.node_id),
            result=_blocked_result(),
        )

        assert result.status == STATUS_SUCCESS
        assert len(rerun_calls) == 1
        # 复用既有验收节点，不新建
        verifiers = _acceptance_children(service, child.node_id)
        assert [item.node_id for item in verifiers] == [acceptance.node_id]
        # 原始拒绝理由必须保留在 rejection_history
        refreshed = service.get_node(acceptance.node_id)
        history = list((refreshed.metadata or {}).get(_REJECTION_HISTORY_KEY) or [])
        assert any(original_reason in str(entry.get("failure_reason") or "") for entry in history)
        handshake = _handshake(service, child.node_id)
        assert int(handshake.get("rejection_count") or 0) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gate_exhausted_budget_allows_failure_with_marker(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        service.node_runner._update_execution_acceptance_handshake(
            node_id=child.node_id,
            state=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
            acceptance_node_id="",
            rejection_count=2,
            max_rejections=3,
            latest_execution_result_ref="",
            latest_execution_result_summary="",
            latest_rejection_feedback_ref="",
            latest_rejection_feedback_summary="",
        )
        _install_fake_verifier(service, [
            _verdict("failed", summary="阻塞不成立", blocking_reason="继续干活"),
        ])
        rerun_calls = _install_fake_rerun(service, NodeFinalResult(status="success", summary="never"))

        result = await service.node_runner._maybe_gate_blocked_submission(
            task=service.get_task(record.task_id),
            node=service.get_node(child.node_id),
            result=_blocked_result(),
        )

        assert result.status == STATUS_FAILED
        assert rerun_calls == []
        failed_child = service.get_node(child.node_id)
        assert "额度耗尽" in str(failed_child.failure_reason or "")
        assert "2/3" in str(failed_child.failure_reason or "") or "3/3" in str(failed_child.failure_reason or "")
        log = list((failed_child.metadata or {}).get(_BLOCKED_VERIFICATION_LOG_KEY) or [])
        assert log and log[-1]["decision"] == "allowed:exhausted"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gate_entry_exhausted_skips_verification_entirely(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        service.node_runner._update_execution_acceptance_handshake(
            node_id=child.node_id,
            state=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
            acceptance_node_id="",
            rejection_count=3,
            max_rejections=3,
            latest_execution_result_ref="",
            latest_execution_result_summary="",
            latest_rejection_feedback_ref="",
            latest_rejection_feedback_summary="",
        )
        _install_forbidden_verifier(service)

        result = await service.node_runner._maybe_gate_blocked_submission(
            task=task,
            node=service.get_node(child.node_id),
            result=_blocked_result(),
        )

        assert result.status == STATUS_FAILED
        assert "额度耗尽" in str(service.get_node(child.node_id).failure_reason or "")
        assert _acceptance_children(service, child.node_id) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gate_invalid_verdict_without_evidence_is_repaired_then_rejected(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        verifier_calls = _install_fake_verifier(service, [
            _verdict("success", summary="阻塞成立"),  # 无 evidence → 无效裁决
        ])
        rerun_calls = _install_fake_rerun(service, NodeFinalResult(
            status="success", delivery_status="final", summary="done", answer="done",
            evidence=[], remaining_work=[], blocking_reason="",
        ))

        result = await service.node_runner._maybe_gate_blocked_submission(
            task=task,
            node=service.get_node(child.node_id),
            result=_blocked_result(),
        )

        assert result.status == STATUS_SUCCESS
        assert len(verifier_calls) == 2
        assert len(rerun_calls) == 1
        verifier_id = verifier_calls[0]
        repair = _notifications_for(service, record.task_id, verifier_id)
        assert any("没有附带任何 evidence" in item for item in repair)
        assert int(_handshake(service, child.node_id).get("rejection_count") or 0) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gate_unverifiable_verdict_allows_failure_after_retry(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        verifier_calls = _install_fake_verifier(service, [
            _verdict("failed", delivery="blocked", summary="核验无法完成", blocking_reason="artifact 不可读"),
        ])

        result = await service.node_runner._maybe_gate_blocked_submission(
            task=task,
            node=service.get_node(child.node_id),
            result=_blocked_result(),
        )

        assert result.status == STATUS_FAILED
        assert len(verifier_calls) == 2
        failed_child = service.get_node(child.node_id)
        assert "核验无法完成" in str(failed_child.failure_reason or "")
        log = list((failed_child.metadata or {}).get(_BLOCKED_VERIFICATION_LOG_KEY) or [])
        assert log and log[-1]["decision"] == "allowed:unverifiable"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gate_ignores_non_execution_and_non_blocked_results(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=child,
            goal=f"accept:{child.goal}",
            acceptance_prompt="verify",
            parent_node_id=child.node_id,
        )
        _install_forbidden_verifier(service)

        # 验收节点自身的 failed+blocked 不受门禁
        assert await service.node_runner._maybe_gate_blocked_submission(
            task=task, node=acceptance, result=_blocked_result(),
        ) is None
        # 执行节点 failed+final 不受门禁
        assert await service.node_runner._maybe_gate_blocked_submission(
            task=task,
            node=child,
            result=_verdict("failed", summary="x", blocking_reason="y"),
        ) is None
        # 执行节点 success+final 不受门禁
        assert await service.node_runner._maybe_gate_blocked_submission(
            task=task,
            node=child,
            result=NodeFinalResult(
                status="success", delivery_status="final", summary="ok", answer="ok",
                evidence=[], remaining_work=[], blocking_reason="",
            ),
        ) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_waiting_acceptance_preserves_original_verdict(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=child,
            goal=f"accept:{child.goal}",
            acceptance_prompt="verify",
            parent_node_id=child.node_id,
        )
        original_reason = "原始拒绝理由：child_c.md 未落盘"
        service.store.update_node(
            acceptance.node_id,
            lambda item: item.model_copy(update={"failure_reason": original_reason}),
        )
        for state in (ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY, "waiting_block_verification"):
            service.node_runner._update_execution_acceptance_handshake(
                node_id=child.node_id,
                state=state,
                acceptance_node_id=acceptance.node_id,
                rejection_count=1,
                max_rejections=3,
                latest_execution_result_ref="",
                latest_execution_result_summary="",
                latest_rejection_feedback_ref="",
                latest_rejection_feedback_summary="",
            )
            # 复位为非终态以便再次触发级联
            service.store.update_node(
                acceptance.node_id,
                lambda item: item.model_copy(update={"status": "in_progress", "finished_at": ""}),
            )
            service.node_runner._cancel_waiting_acceptance_for_execution_failure(
                task_id=record.task_id,
                execution_node_id=child.node_id,
                reason="占位失败原因",
            )
            refreshed = service.get_node(acceptance.node_id)
            assert refreshed.status == STATUS_FAILED
            # 原有非空结论不被覆盖
            assert str(refreshed.failure_reason or "") == original_reason
            cancel_note = (refreshed.metadata or {}).get("canceled_by_execution_failure")
            assert isinstance(cancel_note, dict)
            assert cancel_note.get("reason") == "占位失败原因"
            assert cancel_note.get("execution_node_id") == child.node_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_reactivate_node_stashes_rejection_history(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        service.store.update_node(
            child.node_id,
            lambda item: item.model_copy(update={
                "status": STATUS_FAILED,
                "failure_reason": "旧失败理由",
                "final_output": "旧输出",
            }),
        )
        updated = service.node_runner._reactivate_node_for_retry(task_id=record.task_id, node_id=child.node_id)
        assert updated is not None
        assert updated.status == "in_progress"
        assert str(updated.failure_reason or "") == ""
        history = list((updated.metadata or {}).get(_REJECTION_HISTORY_KEY) or [])
        assert history and history[-1]["failure_reason"] == "旧失败理由"
        assert history[-1]["final_output"] == "旧输出"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_adhoc_verifier_finalized_when_execution_succeeds(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)
        verifier = service.node_runner._blocked_verification_node(task=task, node=child)
        assert verifier is not None
        service.node_runner._update_execution_acceptance_handshake(
            node_id=child.node_id,
            state=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
            acceptance_node_id=verifier.node_id,
            rejection_count=1,
            max_rejections=3,
            latest_execution_result_ref="",
            latest_execution_result_summary="",
            latest_rejection_feedback_ref="",
            latest_rejection_feedback_summary="",
        )
        service.node_runner._finalize_adhoc_blocked_verifier(record.task_id, child.node_id)
        refreshed = service.get_node(verifier.node_id)
        assert refreshed.status == STATUS_SUCCESS
        assert "阻塞核验结束" in str(refreshed.final_output or "")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_run_node_wires_blocked_gate_before_marking_finished(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await _create_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        child = _create_execution_child(service, task=task, parent=root)

        class _FakeReactLoop:
            async def run(self, **kwargs) -> NodeFinalResult:
                return _blocked_result()

        service.node_runner._react_loop = _FakeReactLoop()
        _install_fake_verifier(service, [
            _verdict("failed", summary="阻塞不成立", blocking_reason="继续干活"),
        ])
        rerun_result = NodeFinalResult(
            status="success", delivery_status="final", summary="done", answer="done",
            evidence=[], remaining_work=[], blocking_reason="",
        )
        rerun_calls = _install_fake_rerun(service, rerun_result)

        result = await service.node_runner.run_node(record.task_id, child.node_id)

        assert result is rerun_result
        assert rerun_calls == [(record.task_id, child.node_id)]
        # 门禁生效时执行节点不应被直接标记为失败
        assert service.get_node(child.node_id).status == "in_progress"
    finally:
        await service.close()


def test_prompts_and_repair_guidance_cover_blocked_verification() -> None:
    blocked_template = load_prompt("blocked_verification.md")
    assert "阻塞成立" in blocked_template
    assert "占位式" in blocked_template
    assert "evidence" in blocked_template

    acceptance_prompt = load_prompt("acceptance_execution.md")
    assert "阻塞核验模式" in acceptance_prompt
    assert "占位式裁决" in acceptance_prompt

    execution_prompt = load_prompt("node_execution.md")
    assert "会被验收节点独立核验" in execution_prompt
    assert "禁止把 `failed` 结果" in execution_prompt

    execution_guidance = ReActToolLoop._result_repair_guidance(node_kind="execution")
    assert "independently verified" in execution_guidance
    assert "placeholder" in execution_guidance
    acceptance_guidance = ReActToolLoop._result_repair_guidance(node_kind="acceptance")
    assert "placeholder" in acceptance_guidance
