"""最终验收回合的可恢复性回归（task:c7f1dbfae6e2，2026-09-19）。

事故链：验收节点进入回合第一步就消费并合并了自己的交接通知 → 进程在 429 退避中
收尾，半截回合的协程与 dispatcher entry 一起消失 → 重启后通知账本里没有它、孤儿
收尸又不认验收节点 → `run_task` 落回 `dispatcher.execute_node(root)`，被检验的
执行节点白跑三小时，而它等的那份裁定一直挂在握手状态里。

本文件锁定修复契约：
- B1：握手处于 waiting_acceptance 且验收节点非终态、
  未被冻结时，该节点是一个可重派发的持久承诺；
- B2：冻结、点名他人、终态、人工暂停一律不算（否则会凭空多出一轮打回）；
- B3：`run_task` 在通知账本无人可派发时重派发验收回合，且同一次遍历不再跑根节点；
- B4：裁定落地后握手离开等待态，谓词随即失效——重派发不会变成循环。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from g3ku.providers.base import LLMResponse, ToolCallRequest
from main.models import NodeFinalResult, TaskNodeNotification, normalize_final_acceptance_metadata
from main.protocol import now_iso
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
    normalize_acceptance_handshake,
    set_acceptance_handshake_state,
)
from main.service.runtime_service import MainRuntimeService


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


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


class _QueuedChatBackend:
    def __init__(self, responses: list[object]) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = list(responses)

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self._responses:
            raise AssertionError("queued chat backend exhausted")
        return self._responses.pop(0)


def _build_service(tmp_path: Path, *, chat_backend=None) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=chat_backend or _DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
        execution_model_refs=["fake"],
        acceptance_model_refs=["fake"],
    )

    async def _noop_enqueue_task(_task_id: str) -> None:
        return None

    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


def _submit_final_response(*, call_id: str, status: str, delivery_status: str, text: str):
    return LLMResponse(
        tool_calls=[
            ToolCallRequest(
                id=call_id,
                name="submit_final_result",
                arguments={
                    "status": status,
                    "delivery_status": delivery_status,
                    "summary": text,
                    "answer": text,
                    "evidence": [],
                    "remaining_work": [],
                    "blocking_reason": "" if status == "success" else text,
                },
            )
        ],
        content="",
        finish_reason="tool_calls",
    )


async def _waiting_acceptance_task(
    service: MainRuntimeService,
    *,
    prompt: str,
) -> tuple[str, str, str]:
    """建一个「根已提交、验收节点在飞、通知账本已清空」的现场，返回 (task, root, acceptance)。"""
    record = await service.create_task(
        prompt,
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None
    acceptance_id = str(
        normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance")).node_id or ""
    ).strip()
    acceptance = service.store.get_node(acceptance_id)
    assert acceptance is not None

    service.node_runner._set_execution_waiting_acceptance_state(
        task_id=record.task_id,
        execution_node_id=root.node_id,
        acceptance_node_id=acceptance.node_id,
        result_ref="artifact:result",
        result_summary="draft answer",
    )
    # 交接通知已被验收节点消费并合并：这正是它从通知账本里消失的时刻。
    return record.task_id, root.node_id, acceptance.node_id


def _handshake(service: MainRuntimeService, node_id: str) -> dict[str, object]:
    node = service.store.get_node(node_id)
    assert node is not None
    return normalize_acceptance_handshake((node.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY))


def _set_handshake(
    service: MainRuntimeService,
    *,
    node_id: str,
    state: str,
    acceptance_node_id: str,
    submission_id: str | None = None,
) -> None:
    def _mutate(metadata: dict[str, object]) -> dict[str, object]:
        current = normalize_acceptance_handshake(metadata.get(ACCEPTANCE_HANDSHAKE_KEY))
        metadata[ACCEPTANCE_HANDSHAKE_KEY] = set_acceptance_handshake_state(
            current,
            state=state,
            acceptance_node_id=acceptance_node_id,
            rejection_count=int(current.get("rejection_count") or 0),
            latest_execution_result_ref=str(current.get("latest_execution_result_ref") or ""),
            latest_execution_result_summary=str(current.get("latest_execution_result_summary") or ""),
            latest_rejection_feedback_ref="",
            latest_rejection_feedback_summary="",
            updated_at=now_iso(),
            latest_execution_submission_id=submission_id,
        )
        return metadata

    service.log_service.update_node_metadata(node_id, _mutate)


def _give_node_pending_notice(service: MainRuntimeService, *, node_id: str, notification_id: str) -> None:
    service.store.upsert_task_node_notification(
        TaskNodeNotification(
            notification_id=notification_id,
            task_id=str(service.store.get_node(node_id).task_id),
            node_id=node_id,
            epoch_id="",
            source_node_id=node_id,
            message="追加要求：还差一项",
            status="delivered",
            created_at=now_iso(),
            delivered_at=now_iso(),
            consumed_at="",
            payload={},
        )
    )


# ---------------------------------------------------------------------------
# B1：握手等待验收 = 可重派发的持久承诺
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_acceptance_handshake_makes_acceptance_node_resumable(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, _root_id, acceptance_id = await _waiting_acceptance_task(service, prompt="resume candidate")

        assert service.node_runner.resumable_final_acceptance_node_id(task_id) == acceptance_id
    finally:
        await service.close()


# ---------------------------------------------------------------------------
# B2：不该重派发的形态一律不匹配
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frozen_acceptance_is_not_resumable(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root_id, _acceptance_id = await _waiting_acceptance_task(service, prompt="frozen acceptance")
        # 被检验的执行节点仍有未消费通知：此时派发验收会立刻产出 partial，
        # 被当成一次凭空打回。
        _give_node_pending_notice(service, node_id=root_id, notification_id="notif:root-pending")

        assert service.node_runner.resumable_final_acceptance_node_id(task_id) == ""
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_handshake_that_no_longer_waits_is_not_resumable(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root_id, acceptance_id = await _waiting_acceptance_task(service, prompt="idle handshake")

        for state in (ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY, "accepted", "idle"):
            _set_handshake(service, node_id=root_id, state=state, acceptance_node_id=acceptance_id)
            assert service.node_runner.resumable_final_acceptance_node_id(task_id) == ""

        # 握手点名另一份验收节点（已被重建）：旧节点不得复活成一次假裁定。
        _set_handshake(service, node_id=root_id, state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE, acceptance_node_id="node:other")
        assert service.node_runner.resumable_final_acceptance_node_id(task_id) == ""
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_or_paused_acceptance_node_is_not_resumable(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, _root_id, acceptance_id = await _waiting_acceptance_task(service, prompt="paused acceptance")

        service.log_service.set_node_pause_state(
            task_id,
            acceptance_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="manual",
        )
        assert service.node_runner.resumable_final_acceptance_node_id(task_id) == ""

        service.log_service.set_node_pause_state(
            task_id,
            acceptance_id,
            pause_requested=False,
            is_paused=False,
        )
        service.log_service.update_node_status(task_id, acceptance_id, status="failed", failure_reason="gone")
        assert service.node_runner.resumable_final_acceptance_node_id(task_id) == ""
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_root_is_not_resumable(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root_id, _acceptance_id = await _waiting_acceptance_task(service, prompt="terminal root")
        service.log_service.update_node_status(
            task_id,
            root_id,
            status="success",
            final_output="already delivered",
        )

        assert service.node_runner.resumable_final_acceptance_node_id(task_id) == ""
    finally:
        await service.close()


# ---------------------------------------------------------------------------
# B3：run_task 重派发在飞回合，且不再同时跑根节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_redispatches_inflight_acceptance_round_without_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账本为空、只靠握手承诺时，恢复选择器把半截验收回合重派起来。

    本用例专门隔离「通知账本之外的握手承诺」这条路：派发扫描先被短路，证明
    `_resume_inflight_final_acceptance` 自己就能把回合拉起来（扫描路径由
    `test_node_acceptance_scheduler.py` 覆盖）。
    """
    backend = _QueuedChatBackend(
        [_submit_final_response(call_id="call:accept-pass", status="success", delivery_status="final", text="验收通过")]
    )
    service = _build_service(tmp_path, chat_backend=backend)
    fake_logger = MagicMock()
    monkeypatch.setattr("main.runtime.task_actor_service.logger", fake_logger)

    async def _noop_dispatch(*args, **kwargs) -> bool:
        return False

    async def _noop_drive_scheduler_loop(*, task_id: str, dispatcher, root_node_id: str, current_result):
        # 用例的主体是 run_task 的重派发决策；验收回合跑完后的调度循环不在断言范围内。
        return current_result, False

    monkeypatch.setattr(service.task_actor_service, "_dispatch_runnable_nodes", _noop_dispatch)
    monkeypatch.setattr(
        service.task_actor_service,
        "_drive_scheduler_loop",
        _noop_drive_scheduler_loop,
    )
    try:
        task_id, root_id, acceptance_id = await _waiting_acceptance_task(service, prompt="redispatch round")
        # 通知账本必须真的是空的：本用例要证明的就是「账本之外还有一份承诺」。
        # 握手是唯一凭据（旧的 distribution 投影已删除）。
        assert service.node_runner.resumable_final_acceptance_node_id(task_id) == acceptance_id

        await service.task_actor_service.run_task(task_id)

        acceptance = service.store.get_node(acceptance_id)
        root = service.store.get_node(root_id)
        assert acceptance is not None and root is not None
        # 验收节点跑完并裁定，根节点没有白跑一轮新回合。
        assert len(backend.calls) == 1
        assert acceptance.status == "success"
        assert root.status == "success"
        assert _handshake(service, root_id).get("state") == "accepted"
        warnings = [str(call) for call in fake_logger.warning.call_args_list]
        assert any("final acceptance round re-dispatched" in text for text in warnings)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_inflight_acceptance_resume_skipped_when_entry_is_live(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, _root_id, acceptance_id = await _waiting_acceptance_task(service, prompt="live entry guard")
        dispatcher = service.task_actor_service._create_dispatcher(task_id)
        entry = MagicMock()
        entry.task = MagicMock()
        entry.task.done.return_value = False
        dispatcher._entries[acceptance_id] = entry
        service.task_actor_service._dispatchers[task_id] = dispatcher

        assert await service.task_actor_service._resume_inflight_final_acceptance(task_id) is False
    finally:
        await service.close()


# ---------------------------------------------------------------------------
# B4：裁定落地后谓词失效——重派发不会变成循环
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejection_after_resume_clears_resumable_candidate(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root_id, acceptance_id = await _waiting_acceptance_task(service, prompt="kickback clears")
        acceptance = service.store.get_node(acceptance_id)
        assert acceptance is not None

        rejection = NodeFinalResult(
            status="failed",
            delivery_status="final",
            summary="缺少验收项",
            answer="缺少验收项",
            evidence=[],
            remaining_work=[],
            blocking_reason="缺少验收项",
        )
        handled = await service.node_runner._handle_acceptance_node_result(
            task=service.store.get_task(task_id),
            acceptance=acceptance,
            result=rejection,
        )

        assert handled.delivery_status == "partial"
        # 验收节点按设计仍在飞（下一轮还要复验），但握手已把驱动权交还执行节点。
        assert service.store.get_node(acceptance_id).status == "in_progress"
        assert _handshake(service, root_id).get("state") == ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY
        assert service.node_runner.resumable_final_acceptance_node_id(task_id) == ""
    finally:
        await service.close()


# ---------------------------------------------------------------------------
# I1：accepted 关闭本轮分派——重放终态根不得再开验收轮
# ---------------------------------------------------------------------------


def _seed_booked_pass(
    service: MainRuntimeService,
    *,
    task_id: str,
    root_id: str,
    acceptance_id: str,
) -> None:
    """落一个「裁决已记账」的现场：根 success、握手 accepted、验收节点 success。"""
    service.log_service.update_node_status(
        task_id,
        root_id,
        status="success",
        final_output="booked answer",
    )
    service.node_runner._update_execution_acceptance_handshake(
        node_id=root_id,
        state=ACCEPTANCE_STATE_ACCEPTED,
        acceptance_node_id=acceptance_id,
        rejection_count=0,
        latest_execution_result_ref="artifact:booked",
        latest_execution_result_summary="booked answer",
        latest_rejection_feedback_ref="",
        latest_rejection_feedback_summary="",
        latest_execution_submission_id="submission:booked",
    )
    service.log_service.update_node_status(
        task_id,
        acceptance_id,
        status="success",
        final_output="verdict: pass",
    )


def _acceptance_round_stub(service: MainRuntimeService, runs: list[str], *, submission_id: str = ""):
    async def _fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
        node = service.store.get_node(node_id)
        assert node is not None
        if str(node.node_kind or "").strip().lower() != "acceptance":
            return service.node_runner._result_from_record(node)
        runs.append(node_id)
        service.log_service.update_node_status(
            task_id,
            node_id,
            status="success",
            final_output="verdict: pass again",
        )
        return NodeFinalResult(
            status="success",
            delivery_status="final",
            summary="verdict: pass again",
            answer="verdict: pass again",
            evidence=[],
            remaining_work=[],
            blocking_reason="",
            submission_id=submission_id,
        )

    return _fake_run_node


@pytest.mark.asyncio
async def test_replayed_terminal_root_does_not_open_a_second_acceptance_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1：裁决落账后重复驱动 run_task，不得重置验收节点、重开模型回合。"""
    service = _build_service(tmp_path)
    terminal_events: list[str] = []

    def _count_terminal(*, task) -> None:
        terminal_events.append(str(getattr(task, "task_id", "") or ""))

    try:
        record = await service.create_task(
            "booked pass replay",
            session_id="web:shared",
            metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
        )
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        acceptance_id = str(
            normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance")).node_id or ""
        ).strip()
        acceptance = service.store.get_node(acceptance_id)
        assert acceptance is not None

        monkeypatch.setattr(service.log_service, "_publish_task_terminal_locked", _count_terminal)

        _seed_booked_pass(
            service,
            task_id=record.task_id,
            root_id=root.node_id,
            acceptance_id=acceptance_id,
        )
        seeded_task = service.get_task(record.task_id)
        seeded_acceptance = service.store.get_node(acceptance_id)
        seeded_root = service.store.get_node(root.node_id)
        assert seeded_task is not None and seeded_acceptance is not None and seeded_root is not None
        assert seeded_task.status == "success"
        assert seeded_acceptance.status == "success"
        assert seeded_root.check_result
        assert (
            normalize_final_acceptance_metadata((seeded_task.metadata or {}).get("final_acceptance")).status
            == "passed"
        )
        assert len(terminal_events) == 1

        runs: list[str] = []
        monkeypatch.setattr(service.node_runner, "run_node", _acceptance_round_stub(service, runs))

        await service.task_actor_service.run_task(record.task_id)

        latest_task = service.get_task(record.task_id)
        latest_acceptance = service.store.get_node(acceptance_id)
        latest_root = service.store.get_node(root.node_id)
        assert latest_task is not None and latest_acceptance is not None and latest_root is not None
        # 没有第二轮模型回合；那一份已记账的 pass 没有被塞进 rejection_history。
        assert runs == []
        assert list((latest_acceptance.metadata or {}).get("rejection_history") or []) == []
        assert latest_acceptance.status == "success"
        assert (
            normalize_final_acceptance_metadata((latest_task.metadata or {}).get("final_acceptance")).status
            == "passed"
        )
        assert latest_root.check_result
        assert len(terminal_events) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_new_submission_after_booked_pass_still_reverifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2：正向控制——新提交（新 submission id + waiting_acceptance）仍要作废旧裁决重验。"""
    service = _build_service(tmp_path)
    try:
        record = await service.create_task(
            "new submission after pass",
            session_id="web:shared",
            metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
        )
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        acceptance_id = str(
            normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance")).node_id or ""
        ).strip()

        _seed_booked_pass(
            service,
            task_id=record.task_id,
            root_id=root.node_id,
            acceptance_id=acceptance_id,
        )
        _set_handshake(
            service,
            node_id=root.node_id,
            state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
            acceptance_node_id=acceptance_id,
            submission_id="submission:new",
        )

        runs: list[str] = []
        monkeypatch.setattr(
            service.node_runner,
            "run_node",
            _acceptance_round_stub(service, runs, submission_id="submission:new"),
        )

        await service.task_actor_service.run_task(record.task_id)

        latest_task = service.get_task(record.task_id)
        latest_acceptance = service.store.get_node(acceptance_id)
        assert latest_task is not None and latest_acceptance is not None
        assert runs == [acceptance_id]
        assert list((latest_acceptance.metadata or {}).get("rejection_history") or [])
        handshake = _handshake(service, root.node_id)
        assert handshake.get("state") == ACCEPTANCE_STATE_ACCEPTED
        assert handshake.get("latest_execution_submission_id") == "submission:new"
        assert (
            normalize_final_acceptance_metadata((latest_task.metadata or {}).get("final_acceptance")).status
            == "passed"
        )
    finally:
        await service.close()


