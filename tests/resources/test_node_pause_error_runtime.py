from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest

from g3ku.heartbeat.node_error_scanner import NodeErrorScanner
from main.errors import NodePausedError
from main.monitoring.query_service import TaskQueryService
from main.monitoring.models import TaskProjectionNodeRecord
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
        workspace_root=tmp_path,
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


def test_sqlite_node_error_logs_filter_by_node(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        task_id = "task:node-error-filter"
        node_a = "node:error-a"
        node_b = "node:error-b"
        store.upsert_task(_task_record(task_id, node_a))
        store.upsert_node(_node_record(task_id, node_a))
        store.upsert_node(_node_record(task_id, node_b))
        store.append_task_error_log(
            task_id=task_id,
            node_id=node_a,
            node_title="error-a",
            error_text="provider unavailable a",
            created_at="2026-03-29T00:00:01+08:00",
        )
        store.append_task_error_log(
            task_id=task_id,
            node_id=node_b,
            node_title="error-b",
            error_text="provider unavailable b",
            created_at="2026-03-29T00:00:02+08:00",
        )

        assert len(store.list_task_error_logs(task_id)) == 2
        node_logs = store.list_task_node_error_logs(task_id, node_a)
        assert len(node_logs) == 1
        assert node_logs[0].node_id == node_a
        assert node_logs[0].error_text == "provider unavailable a"
        assert store.list_task_node_error_logs(task_id, "node:missing") == []

        store.delete_task(task_id)
        assert store.list_task_node_error_logs(task_id, node_a) == []
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
async def test_task_node_error_log_payload_is_scoped_to_node(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("node error payload", session_id="web:shared")
        task_id = record.task_id
        service.log_service.append_task_error_log(
            task_id,
            record.root_node_id,
            error_text="first boom",
            node_title="payload node",
        )
        service.log_service.append_task_error_log(
            task_id,
            "node:other",
            error_text="other boom",
            node_title="other node",
        )

        payload = service.get_task_node_error_log_payload(task_id, record.root_node_id)
        assert payload is not None
        assert payload["ok"] is True
        assert payload["node_id"] == record.root_node_id
        assert len(payload["items"]) == 1
        assert payload["items"][0]["error_text"] == "first boom"

        other_payload = service.get_task_node_error_log_payload(task_id, "node:other")
        assert other_payload is not None
        assert len(other_payload["items"]) == 1
        assert other_payload["items"][0]["error_text"] == "other boom"

        assert service.get_task_node_error_log_payload("task:missing", record.root_node_id) is None
    finally:
        await service.close()


def _paused_node_record(task_id: str, node_id: str) -> NodeRecord:
    node = _node_record(task_id, node_id)
    node.status = "in_progress"
    node.is_paused = True
    node.pause_requested = True
    node.pause_reason = "error"
    return node


def test_projection_record_carries_top_level_pause_fields(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    node = _paused_node_record("task:pause-render", "node:pause-render")
    projection = service.log_service._task_projection_node_record(node)
    assert projection.is_paused is True
    assert projection.pause_reason == "error"


def test_tree_text_label_renders_paused_status() -> None:
    node = _paused_node_record("task:pause-render", "node:pause-render")
    projection = TaskProjectionNodeRecord(
        node_id=node.node_id,
        task_id=node.task_id,
        status=node.status,
        is_paused=node.is_paused,
        pause_reason=node.pause_reason,
        payload={},
    )
    label = TaskQueryService._tree_text_label(projection, {})
    assert "paused(error)" in label
    assert "in_progress" not in label


def test_tree_text_label_falls_back_to_payload_pause_flag() -> None:
    # 历史投影未即时刷新时，顶层 is_paused 仍为 False，但 payload 已带暂停信息。
    projection = TaskProjectionNodeRecord(
        node_id="node:stale",
        task_id="task:stale",
        status="in_progress",
        is_paused=False,
        pause_reason="",
        payload={"is_paused": True, "pause_reason": "error"},
    )
    label = TaskQueryService._tree_text_label(projection, {})
    assert "paused(error)" in label


def test_tree_text_label_keeps_status_when_not_paused() -> None:
    projection = TaskProjectionNodeRecord(
        node_id="node:ok",
        task_id="task:ok",
        status="success",
        is_paused=False,
        pause_reason="",
        payload={},
    )
    label = TaskQueryService._tree_text_label(projection, {})
    assert ",success," in label
    assert "paused" not in label


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
async def test_control_nodes_keep_paused_in_web_mode_enqueues_no_fail_command(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("keep paused web", session_id="web:shared")
        root = service.get_node(record.root_node_id)
        assert root is not None
        await service.pause_node(record.task_id, root.node_id, reason="manual")

        before = service.store.list_unfinished_task_commands(task_id=record.task_id)
        kept = await service.control_nodes(record.task_id, [root.node_id], "keep_paused", remark="等待用户决策")
        after = service.store.list_unfinished_task_commands(task_id=record.task_id)
        assert kept["items"][0]["result"] == "kept_paused"
        assert len(after) == len(before)
        assert all(command["command_type"] != "fail_node" for command in after)
        latest = service.get_node(root.node_id)
        assert latest is not None and latest.status == "in_progress" and latest.pause_requested is True
        pause_row = service.store.get_task_node_pause(root.node_id)
        assert pause_row is not None and pause_row.remark == "等待用户决策"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_fail_node_command_falls_back_to_remark_reason(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("worker fail fallback", session_id="web:shared")
        node = service.get_node(record.root_node_id)
        assert node is not None
        service.log_service.set_node_pause_state(
            record.task_id,
            node.node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="error",
            remark="provider unavailable",
        )
        # The web leader enqueues fail_node with remark but an empty reason; the
        # worker must fall back to the remark instead of stamping the generic
        # "failed by operator" default.
        await service._process_worker_command(
            {
                "command_id": "command:keep-paused-regression",
                "command_type": "fail_node",
                "task_id": record.task_id,
                "payload": {"node_ids": [node.node_id], "reason": "", "remark": "401 认证失败不可自动恢复"},
            }
        )
        latest = service.get_node(node.node_id)
        assert latest is not None and latest.status == "failed"
        assert latest.failure_reason == "401 认证失败不可自动恢复"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_fail_in_web_mode_enqueues_remark_as_reason(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("fail web payload", session_id="web:shared")
        root = service.get_node(record.root_node_id)
        assert root is not None
        await service.pause_node(record.task_id, root.node_id, reason="manual")
        await service.control_nodes(record.task_id, [root.node_id], "fail", remark="认证类错误不可自动恢复")
        commands = service.store.list_unfinished_task_commands(command_type="fail_node", task_id=record.task_id)
        assert len(commands) == 1
        row = service.store.get_task_command(commands[0]["command_id"])
        assert row is not None
        payload = row["payload"]
        assert payload.get("reason") == "认证类错误不可自动恢复"
        assert payload.get("remark") == "认证类错误不可自动恢复"
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


def _load_manage_task_nodes_tool_module():
    tool_path = Path(__file__).resolve().parents[2] / "tools" / "manage_task_nodes_cn" / "main" / "tool.py"
    spec = importlib.util.spec_from_file_location("manage_task_nodes_tool_under_test", tool_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pause_node_state(service: MainRuntimeService, task_id: str, node, *, reason: str = "manual", remark: str = "") -> None:
    service.log_service.set_node_pause_state(
        task_id,
        node.node_id,
        pause_requested=True,
        is_paused=True,
        pause_reason=reason,
        remark=remark,
    )


@pytest.mark.asyncio
async def test_control_nodes_targets_mix_cascade_pause_and_fail_on_disjoint_subtrees(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("targets mixed", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child_a = _execution_child(service, task=task, parent=root, name="a")
        grandchild_a = _execution_child(service, task=task, parent=child_a, name="a1")
        child_b = _execution_child(service, task=task, parent=root, name="b")
        grandchild_b = _execution_child(service, task=task, parent=child_b, name="b1")
        for node in (child_b, grandchild_b):
            _pause_node_state(service, record.task_id, node)

        result = await service.control_nodes(
            record.task_id,
            [],
            "",
            targets=[
                {"node_id": child_a.node_id, "action": "pause", "cascade": True},
                {"node_id": child_b.node_id, "action": "fail", "cascade": True},
            ],
            remark="b branch invalidated",
        )
        assert result["ok"] is True
        for node_id in (child_a.node_id, grandchild_a.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.pause_requested is True
            assert node.pause_reason == "agent" and node.status == "in_progress"
        for node_id in (child_b.node_id, grandchild_b.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.status == "failed"
            assert node.failure_reason == "b branch invalidated"
        latest_root = service.get_node(root.node_id)
        assert latest_root is not None
        assert latest_root.status == "in_progress" and not latest_root.pause_requested
        summaries = {item["node_id"]: item for item in result["targets"]}
        assert summaries[child_a.node_id]["applied"] == 2 and summaries[child_a.node_id]["skipped"] == 0
        assert summaries[child_b.node_id]["applied"] == 2 and summaries[child_b.node_id]["skipped"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_targets_subtree_overlap_rejects_whole_batch(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("targets overlap", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        grandchild = _execution_child(service, task=task, parent=child, name="grandchild")
        for node in (child, grandchild):
            _pause_node_state(service, record.task_id, node)
        before = service.store.list_unfinished_task_commands(task_id=record.task_id)

        result = await service.control_nodes(
            record.task_id,
            [],
            "",
            targets=[
                {"node_id": root.node_id, "action": "pause", "cascade": True},
                {"node_id": child.node_id, "action": "fail", "cascade": True},
            ],
            remark="overlap batch",
        )
        assert result["ok"] is False
        assert result["error"] == "subtree_overlap"
        assert result["items"] == []
        covered = {item["node_id"]: item["covered_by"] for item in result["conflicts"]}
        assert set(covered) == {child.node_id, grandchild.node_id}
        for owners in covered.values():
            assert sorted(owner["node_id"] for owner in owners) == sorted([root.node_id, child.node_id])
            assert sorted(owner["index"] for owner in owners) == [0, 1]
        latest_root = service.get_node(root.node_id)
        assert latest_root is not None and not latest_root.pause_requested and not latest_root.is_paused
        for node_id in (child.node_id, grandchild.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.status == "in_progress" and node.pause_requested is True
            row = service.store.get_task_node_pause(node_id)
            assert row is not None and row.pause_reason == "manual"
        after = service.store.list_unfinished_task_commands(task_id=record.task_id)
        assert len(after) == len(before)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_same_action_overlap_merges_into_covering_target(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("same action merge", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        grandchild = _execution_child(service, task=task, parent=child, name="grandchild")

        result = await service.control_nodes(
            record.task_id,
            [],
            "",
            targets=[
                {"node_id": root.node_id, "action": "pause", "cascade": True},
                {"node_id": child.node_id, "action": "pause", "cascade": True},
            ],
        )
        assert result["ok"] is True
        assert result["merged"] == [{
            "index": 1,
            "node_id": child.node_id,
            "action": "pause",
            "into_index": 0,
            "into_node_id": root.node_id,
        }]
        assert [item["node_id"] for item in result["targets"]] == [root.node_id]
        assert result["targets"][0]["applied"] == 3
        for node_id in (root.node_id, child.node_id, grandchild.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.pause_requested is True and node.pause_reason == "agent"

        # 级联/非级联混合同样合并：非级联子条目被根的级联恢复条目吸收。
        resumed = await service.control_nodes(
            record.task_id,
            [],
            "",
            targets=[
                {"node_id": child.node_id, "action": "resume"},
                {"node_id": root.node_id, "action": "resume", "cascade": True},
            ],
        )
        assert resumed["ok"] is True
        assert resumed["merged"] == [{
            "index": 0,
            "node_id": child.node_id,
            "action": "resume",
            "into_index": 1,
            "into_node_id": root.node_id,
        }]
        assert [item["node_id"] for item in resumed["targets"]] == [root.node_id]
        for node_id in (root.node_id, child.node_id, grandchild.node_id):
            node = service.get_node(node_id)
            assert node is not None and not node.pause_requested and not node.is_paused
            assert service.store.get_task_node_pause(node_id) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_cascade_fail_requires_fully_paused_subtree(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("cascade fail gate", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        grandchild = _execution_child(service, task=task, parent=child, name="grandchild")
        _pause_node_state(service, record.task_id, root)

        blocked = await service.control_nodes(record.task_id, [root.node_id], "fail", remark="fail whole tree", cascade=True)
        assert blocked["ok"] is False
        assert blocked["error"] == "subtree_not_fully_paused"
        assert sorted(blocked["blocking_node_ids"]) == sorted([child.node_id, grandchild.node_id])
        latest_root = service.get_node(root.node_id)
        assert latest_root is not None and latest_root.status == "in_progress"

        paused = await service.control_nodes(record.task_id, [root.node_id], "pause", cascade=True)
        assert paused["ok"] is True
        root_item = next(item for item in paused["items"] if item["node_id"] == root.node_id)
        assert root_item["result"] == "conflict" and root_item["reason"] == "node_already_paused"
        for node_id in (child.node_id, grandchild.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.pause_requested is True and node.pause_reason == "agent"
        assert latest_root.pause_reason == "manual"

        failed = await service.control_nodes(record.task_id, [root.node_id], "fail", remark="fail whole tree", cascade=True)
        assert failed["ok"] is True
        for node_id in (root.node_id, child.node_id, grandchild.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.status == "failed"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_cascade_pause_skips_error_paused_descendants(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("cascade pause skip", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        grandchild = _execution_child(service, task=task, parent=child, name="grandchild")
        _pause_node_state(service, record.task_id, child, reason="error", remark="provider unavailable")

        result = await service.control_nodes(record.task_id, [root.node_id], "pause", remark="cascade pause", cascade=True)
        assert result["ok"] is True
        latest_root = service.get_node(root.node_id)
        assert latest_root is not None and latest_root.pause_requested is True and latest_root.pause_reason == "agent"
        latest_child = service.get_node(child.node_id)
        assert latest_child is not None and latest_child.pause_reason == "error"
        child_row = service.store.get_task_node_pause(child.node_id)
        assert child_row is not None and child_row.remark == "provider unavailable"
        latest_grandchild = service.get_node(grandchild.node_id)
        assert latest_grandchild is not None and latest_grandchild.pause_requested is True
        assert latest_grandchild.pause_reason == "agent"
        child_item = next(item for item in result["items"] if item["node_id"] == child.node_id)
        assert child_item["result"] == "conflict" and child_item["reason"] == "node_already_paused"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_cascade_resume_clears_subtree_and_skips_conflicts(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("cascade resume", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        grandchild = _execution_child(service, task=task, parent=child, name="grandchild")
        finished = _execution_child(service, task=task, parent=child, name="finished")
        service.store.update_node(finished.node_id, lambda node: node.model_copy(update={"status": "success"}))
        _pause_node_state(service, record.task_id, root)
        _pause_node_state(service, record.task_id, child, reason="error", remark="provider unavailable")

        result = await service.control_nodes(record.task_id, [root.node_id], "resume", cascade=True)
        assert result["ok"] is True
        for node_id in (root.node_id, child.node_id):
            node = service.get_node(node_id)
            assert node is not None and not node.pause_requested and not node.is_paused
            assert service.store.get_task_node_pause(node_id) is None
        latest_grandchild = service.get_node(grandchild.node_id)
        assert latest_grandchild is not None and not latest_grandchild.pause_requested
        skipped = {(item["node_id"], item.get("reason")) for item in result["items"] if item["result"] == "conflict"}
        assert (grandchild.node_id, "node_not_paused") in skipped
        assert (finished.node_id, "node_terminal") in skipped
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_root_cascade_targets_whole_tree(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("root cascade", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        grandchild = _execution_child(service, task=task, parent=child, name="grandchild")

        # legacy 形态重复 node_id 去重后不得误判为子树重叠。
        result = await service.control_nodes(record.task_id, [root.node_id, root.node_id], "pause", cascade=True)
        assert result["ok"] is True
        assert len(result["targets"]) == 1 and result["targets"][0]["applied"] == 3
        for node_id in (root.node_id, child.node_id, grandchild.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.pause_requested is True and node.pause_reason == "agent"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_control_nodes_scoped_web_mode_enqueues_explicit_ids_per_entry(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("scoped web enqueue", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child_a = _execution_child(service, task=task, parent=root, name="a")
        grandchild_a = _execution_child(service, task=task, parent=child_a, name="a1")
        child_b = _execution_child(service, task=task, parent=root, name="b")
        child_c = _execution_child(service, task=task, parent=root, name="c")
        for node in (child_b, child_c):
            _pause_node_state(service, record.task_id, node)
        before = service.store.list_unfinished_task_commands(task_id=record.task_id)

        result = await service.control_nodes(
            record.task_id,
            [],
            "",
            targets=[
                {"node_id": child_a.node_id, "action": "pause", "cascade": True},
                {"node_id": child_b.node_id, "action": "fail"},
                {"node_id": child_c.node_id, "action": "keep_paused"},
            ],
            remark="waiting for user decision",
        )
        assert result["ok"] is True
        after = service.store.list_unfinished_task_commands(task_id=record.task_id)
        before_ids = {item["command_id"] for item in before}
        new_commands = [command for command in after if command["command_id"] not in before_ids]
        assert [command["command_type"] for command in new_commands] == ["pause_node", "fail_node"]
        payloads = {}
        for command in new_commands:
            row = service.store.get_task_command(command["command_id"])
            assert row is not None
            payloads[command["command_type"]] = row["payload"]
        pause_payload = payloads["pause_node"]
        assert sorted(pause_payload["node_ids"]) == sorted([child_a.node_id, grandchild_a.node_id])
        assert pause_payload["cascade"] is False and pause_payload["reason"] == "agent"
        fail_payload = payloads["fail_node"]
        assert fail_payload["node_ids"] == [child_b.node_id]
        assert fail_payload["cascade"] is False
        assert fail_payload["reason"] == "waiting for user decision"
        assert fail_payload["remark"] == "waiting for user decision"
        for payload in payloads.values():
            assert child_c.node_id not in list(payload.get("node_ids") or [])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_replay_of_scoped_commands_is_idempotent(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("scoped replay", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child_a = _execution_child(service, task=task, parent=root, name="a")
        child_b = _execution_child(service, task=task, parent=root, name="b")
        _pause_node_state(service, record.task_id, child_b)

        result = await service.control_nodes(
            record.task_id,
            [],
            "",
            targets=[
                {"node_id": child_a.node_id, "action": "pause", "cascade": True},
                {"node_id": child_b.node_id, "action": "fail"},
            ],
            remark="replay test",
        )
        assert result["ok"] is True
        commands = service.store.list_unfinished_task_commands(task_id=record.task_id)
        assert commands
        for command in commands:
            row = service.store.get_task_command(command["command_id"])
            assert row is not None
            await service._process_worker_command(
                {
                    "command_id": command["command_id"],
                    "command_type": row["command_type"],
                    "task_id": record.task_id,
                    "payload": row["payload"],
                }
            )
        latest_a = service.get_node(child_a.node_id)
        assert latest_a is not None and latest_a.pause_requested is True and latest_a.pause_reason == "agent"
        latest_b = service.get_node(child_b.node_id)
        assert latest_b is not None and latest_b.status == "failed"
        assert service.store.list_unfinished_task_commands(task_id=record.task_id) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_manage_task_nodes_tool_param_shapes_and_targets_passthrough(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("tool shapes", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        module = _load_manage_task_nodes_tool_module()
        tool = module._ManageTaskNodesHandler(service)

        both = json.loads(await tool.execute(
            task_id=record.task_id,
            node_ids=[record.root_node_id],
            action="pause",
            targets=[{"node_id": record.root_node_id, "action": "pause"}],
        ))
        assert both["ok"] is False and both["error"] == "invalid_param"
        neither = json.loads(await tool.execute(task_id=record.task_id))
        assert neither["ok"] is False and neither["error"] == "invalid_param"
        missing_action = json.loads(await tool.execute(task_id=record.task_id, targets=[{"node_id": record.root_node_id}]))
        assert missing_action["ok"] is False and missing_action["error"] == "invalid_param"

        # 条目未声明 cascade 时继承顶层 cascade（显式参数不得被静默忽略）。
        paused = json.loads(await tool.execute(
            task_id=record.task_id,
            targets=[{"node_id": record.root_node_id, "action": "pause"}],
            cascade=True,
        ))
        assert paused["ok"] is True
        for node_id in (root.node_id, child.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.pause_requested is True
    finally:
        await service.close()
