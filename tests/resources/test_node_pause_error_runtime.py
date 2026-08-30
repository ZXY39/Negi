from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from g3ku.heartbeat.node_error_scanner import NodeErrorScanner
from main.errors import NodePausedError
from main.models import NodeFinalResult, NodeRecord, SpawnChildSpec, TaskRecord, TokenUsageSummary
from main.runtime.react_loop import ReActToolLoop
from main.runtime.task_actor_service import TaskNodeDispatcher
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in node pause tests: {kwargs!r}")


def _task_record(task_id: str, root_node_id: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="web:shared",
        title="pause test",
        user_request="pause test",
        status="in_progress",
        root_node_id=root_node_id,
        max_depth=2,
        created_at="2026-03-29T00:00:00+08:00",
        updated_at="2026-03-29T00:00:00+08:00",
        token_usage=TokenUsageSummary(tracked=True),
        metadata={},
    )


def _node_record(task_id: str, node_id: str) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=task_id,
        parent_node_id=None,
        root_node_id=node_id,
        depth=0,
        node_kind="execution",
        status="in_progress",
        goal="pause test",
        prompt="pause test",
        input="pause test",
        output=[],
        check_result="",
        final_output="",
        can_spawn_children=False,
        created_at="2026-03-29T00:00:00+08:00",
        updated_at="2026-03-29T00:00:00+08:00",
        token_usage=TokenUsageSummary(tracked=True),
        token_usage_by_model=[],
        metadata={},
    )


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


def _success_result(node_id: str) -> NodeFinalResult:
    text = f"{node_id} complete"
    return NodeFinalResult(
        status="success",
        delivery_status="final",
        summary=text,
        answer=text,
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )


def _execution_child(service: MainRuntimeService, *, task, parent, name: str):
    return service.node_runner._create_execution_child(
        task=task,
        parent=parent,
        spec=SpawnChildSpec(
            goal=f"{name} goal",
            prompt=f"{name} prompt",
            execution_policy={"mode": "focus"},
        ),
    )


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def test_sqlite_pause_and_error_logs_are_crud_and_task_scoped(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        task_id = "task:pause-crud"
        node_id = "node:pause-crud"
        store.upsert_task(_task_record(task_id, node_id))
        store.upsert_node(_node_record(task_id, node_id))
        store.update_node(node_id, lambda node: node.model_copy(update={"is_paused": True, "pause_requested": True, "pause_reason": "error"}))

        pause = store.upsert_task_node_pause(
            task_id=task_id,
            node_id=node_id,
            pause_reason="error",
            remark="provider unavailable",
            created_at="2026-03-29T00:00:01+08:00",
            updated_at="2026-03-29T00:00:01+08:00",
        )
        error = store.append_task_error_log(
            task_id=task_id,
            node_id=node_id,
            node_title="pause-crud",
            error_text="provider unavailable",
            created_at="2026-03-29T00:00:01+08:00",
        )

        assert store.get_task_node_pause(node_id) == pause
        assert store.list_task_node_pauses(task_id)[0].remark == "provider unavailable"
        assert store.list_task_error_logs(task_id)[0] == error
        pending = store.list_new_error_pauses()
        assert len(pending) == 1
        assert pending[0]["node_id"] == node_id

        store.mark_task_node_pause_delivered(pause.id)
        assert store.list_new_error_pauses() == []

        store.delete_task(task_id)
        assert store.get_task(task_id) is None
        assert store.list_task_node_pauses(task_id) == []
        assert store.list_task_error_logs(task_id) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_node_error_becomes_error_pause_and_keeps_node_non_terminal(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("node error", session_id="web:shared")

        async def fail_context(**kwargs):
            raise RuntimeError("provider unavailable")

        service.node_runner._context_preparer = fail_context
        with pytest.raises(NodePausedError):
            await service.node_runner.run_node(record.task_id, record.root_node_id)

        node = service.get_node(record.root_node_id)
        assert node is not None
        assert node.status == "in_progress"
        assert node.pause_requested is True
        assert node.is_paused is True
        assert node.pause_reason == "error"
        assert service.log_service.list_task_error_logs(record.task_id)[0].error_text == "RuntimeError: provider unavailable"
        assert service.store.get_task_node_pause(record.root_node_id) is not None
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result_factory",
    [
        lambda: ReActToolLoop._invalid_final_submission_failure(reason="missing required status", count=1),
        lambda: ReActToolLoop._invalid_stage_submission_failure(reason="invalid stage", count=1, stage_goal="stage"),
        lambda: ReActToolLoop._read_only_repeat_failure(signature="same-call", count=1, repair_text="change the query"),
        lambda: ReActToolLoop._stage_only_transition_failure(count=1, stage_goal="stage"),
        lambda: ReActToolLoop._xml_repair_failure(count=1, tool_names=["exec"], content_excerpt="<tool>"),
        lambda: ReActToolLoop._orphan_tool_result_failure(call_ids=["call:orphan"], strike_count=1),
    ],
)
async def test_react_circuit_breakers_become_resumable_error_pauses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_factory,
) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("protocol circuit breaker", session_id="web:shared")
        guarded_result = result_factory()
        assert guarded_result.failure_disposition == "pause"
        assert "failure_disposition" not in guarded_result.payload_dict()

        async def return_guarded_result(**_kwargs) -> NodeFinalResult:
            return guarded_result

        monkeypatch.setattr(service.node_runner._react_loop, "run", return_guarded_result)
        with pytest.raises(NodePausedError):
            await service.node_runner.run_node(record.task_id, record.root_node_id)

        paused = service.get_node(record.root_node_id)
        assert paused is not None
        assert paused.status == "in_progress"
        assert paused.pause_requested is True
        assert paused.is_paused is True
        assert paused.pause_reason == "error"
        assert service.log_service.list_task_error_logs(record.task_id)[0].error_text == guarded_result.failure_text
        assert (paused.metadata or {}).get("result_payload") is None

        resumed = await service.resume_node(record.task_id, record.root_node_id)
        assert resumed is not None
        assert resumed.pause_requested is False
        assert resumed.is_paused is False

        async def return_success(**_kwargs) -> NodeFinalResult:
            return _success_result(record.root_node_id)

        monkeypatch.setattr(service.node_runner._react_loop, "run", return_success)
        completed = await service.node_runner.run_node(record.task_id, record.root_node_id)
        assert completed.status == "success"
        latest = service.get_node(record.root_node_id)
        assert latest is not None and latest.status == "success"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_kind", ["canceled", "task_failed"])
async def test_circuit_breaker_result_respects_cancellation_and_task_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_kind: str,
) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task(f"terminal priority {terminal_kind}", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="priority child")
        guarded_result = ReActToolLoop._invalid_final_submission_failure(
            reason="missing required status",
            count=1,
        )

        async def return_guarded_result(**_kwargs) -> NodeFinalResult:
            if terminal_kind == "canceled":
                service.log_service.request_cancel(record.task_id)
            else:
                service.log_service.mark_task_failed(record.task_id, reason="task terminal")
            return guarded_result

        monkeypatch.setattr(service.node_runner._react_loop, "run", return_guarded_result)
        result = await service.node_runner.run_node(record.task_id, child.node_id)
        assert result.status == "failed"
        node = service.get_node(child.node_id)
        assert node is not None and node.status == "failed"
        assert node.is_paused is False
        assert service.log_service.list_task_error_logs(record.task_id) == []
        assert service.store.get_task_node_pause(child.node_id) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_valid_failed_final_result_remains_terminal_not_error_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("business failure", session_id="web:shared")

        async def return_business_failure(**_kwargs) -> NodeFinalResult:
            return NodeFinalResult(
                status="failed",
                delivery_status="blocked",
                summary="business validation failed",
                answer="",
                evidence=[],
                remaining_work=[],
                blocking_reason="business validation failed",
            )

        monkeypatch.setattr(service.node_runner._react_loop, "run", return_business_failure)
        result = await service.node_runner.run_node(record.task_id, record.root_node_id)
        assert result.status == "failed"
        node = service.get_node(record.root_node_id)
        assert node is not None and node.status == "failed"
        assert node.is_paused is False
        assert service.log_service.list_task_error_logs(record.task_id) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_dispatcher_keeps_paused_child_waiter_pending_and_resume_resolves_it(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("resume child", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        service.log_service.set_node_pause_state(
            record.task_id,
            child.node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="manual",
        )

        calls = 0

        async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise NodePausedError(task_id, node_id)
            return _success_result(node_id)

        service.node_runner.run_node = fake_run_node  # type: ignore[method-assign]
        dispatcher = TaskNodeDispatcher(
            task_id=record.task_id,
            store=service.store,
            log_service=service.log_service,
            node_runner=service.node_runner,
        )
        service.task_actor_service._dispatchers[record.task_id] = dispatcher
        waiter = asyncio.create_task(dispatcher.execute_node(record.task_id, child.node_id))
        await _wait_until(lambda: calls == 1)
        assert not waiter.done()

        await service.resume_node(record.task_id, child.node_id)
        result = await asyncio.wait_for(waiter, timeout=2.0)
        assert result.status == "success"
        assert calls == 2
        resumed = service.get_node(child.node_id)
        assert resumed is not None and resumed.is_paused is False and resumed.pause_requested is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failing_paused_child_resolves_parent_waiter_and_parent_continues(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("fail child", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        service.log_service.set_node_pause_state(
            record.task_id,
            child.node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="error",
            remark="provider unavailable",
        )

        dispatcher = TaskNodeDispatcher(
            task_id=record.task_id,
            store=service.store,
            log_service=service.log_service,
            node_runner=service.node_runner,
        )
        service.task_actor_service._dispatchers[record.task_id] = dispatcher

        async def fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
            if node_id == child.node_id:
                raise NodePausedError(task_id, node_id)
            child_result = await dispatcher.execute_node(task_id, child.node_id)
            assert child_result.status == "failed"
            return _success_result(node_id)

        service.node_runner.run_node = fake_run_node  # type: ignore[method-assign]
        parent_waiter = asyncio.create_task(dispatcher.execute_node(record.task_id, root.node_id))
        await _wait_until(lambda: child.node_id in dispatcher._entries and not dispatcher._entries[child.node_id].future.done())

        await service.fail_node(record.task_id, child.node_id, "operator rejected recovery")
        result = await asyncio.wait_for(parent_waiter, timeout=2.0)
        assert result.status == "success"
        failed_child = service.get_node(child.node_id)
        assert failed_child is not None and failed_child.status == "failed"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_support_cascade_actions_and_keep_paused_remark(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("control nodes", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        grandchild = _execution_child(service, task=task, parent=child, name="grandchild")

        paused = await service.pause_node(record.task_id, root.node_id, cascade=True, reason="manual")
        assert paused is not None
        for node_id in (root.node_id, child.node_id, grandchild.node_id):
            node = service.get_node(node_id)
            assert node is not None
            assert node.pause_requested is True
            assert node.pause_reason == "manual"
        with pytest.raises(ValueError, match="remark_required_for_keep_paused"):
            await service.control_nodes(record.task_id, [root.node_id], "keep_paused")

        kept = await service.control_nodes(record.task_id, [root.node_id], "keep_paused", remark="等待人工确认")
        assert kept["items"][0]["result"] == "kept_paused"
        assert service.store.get_task_node_pause(root.node_id).remark == "等待人工确认"

        resumed = await service.control_nodes(record.task_id, [root.node_id], "resume")
        assert resumed["items"][0]["result"] == "resumed"
        assert service.store.get_task_node_pause(root.node_id) is None

        await service.control_nodes(record.task_id, [root.node_id], "pause")
        failed = await service.control_nodes(record.task_id, [root.node_id], "fail", remark="等待人工确认??")
        assert failed["items"][0]["result"] == "failed"
        latest_root = service.get_node(root.node_id)
        assert latest_root is not None and latest_root.status == "failed"
        assert service.store.get_task_node_pause(root.node_id) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_node_error_scanner_delivers_once_and_only_marks_successful_enqueue(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("scanner", session_id="web:scanner")
        service.log_service.set_node_pause_state(
            record.task_id,
            record.root_node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="error",
            remark="provider unavailable",
        )
        service.log_service.append_task_error_log(
            record.task_id,
            record.root_node_id,
            error_text="provider unavailable",
            node_title="root",
        )

        events: list[tuple[str, list[dict]]] = []

        class _Heartbeat:
            def enqueue_task_node_error_payload(self, session_id: str, items: list[dict]) -> bool:
                events.append((session_id, items))
                return True

        scanner = NodeErrorScanner(main_task_service=service, heartbeat=_Heartbeat())
        assert await scanner.scan_once() == 1
        assert await scanner.scan_once() == 0
        assert len(events) == 1
        session_id, items = events[0]
        assert session_id == "web:scanner"
        assert items[0]["dedupe_key"].startswith(f"node-error:{record.task_id}:{record.root_node_id}:")
        assert service.store.get_task_node_pause(record.root_node_id).delivered is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_takes_priority_over_node_error_pause(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("cancel priority", session_id="web:shared")
        service.log_service.set_node_pause_state(
            record.task_id,
            record.root_node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="manual",
        )
        service.log_service.request_cancel(record.task_id)

        async def fail_context(**kwargs):
            raise RuntimeError("provider unavailable")

        service.node_runner._context_preparer = fail_context
        result = await service.node_runner.run_node(record.task_id, record.root_node_id)
        node = service.get_node(record.root_node_id)
        assert result.status == "failed"
        assert node is not None and node.status == "failed"
        assert service.log_service.list_task_error_logs(record.task_id) == []
        assert service.store.get_task_node_pause(record.root_node_id) is None
    finally:
        await service.close()
