"""任务级暂停不能把子节点判成孤儿（实盘 task:11267bed647f，2026-10-25）。

事故链：任务暂停只终止在飞协程，节点行仍是 `in_progress`、没有暂停标记、帧停在
在飞相位；下一次 `run_task` 拾取的孤儿收尸把这 9 个节点判成
`spawn_orphan_candidate` 并 park 成 `pause_reason='error'`。恢复任务时只清任务标志，
这些节点再也不会被派发（暂停节点被派发扫描跳过，有帧的非根节点也不在扫描范围内）。

本文件锁定四条契约：
- 任务级暂停给在飞节点落 `pause_reason='task'`，且不覆盖人工/故障暂停；
- 任务暂停期不做孤儿收尸；
- 恢复任务时清 `'task'` 位并重建执行器，停靠相位只清位不点火；
- `manual` / `error` 的暂停不被任务恢复代清。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import SpawnChildSpec
from main.service.runtime_service import MainRuntimeService


@pytest.fixture(autouse=True)
def _default_node_send_preflight_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    def _resolve(**kwargs) -> SendModelContextWindowInfo:
        refs = list(kwargs.get("model_refs") or [])
        model_key = str(refs[0]).strip() if refs else ""
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
        raise AssertionError(f"chat backend should not be called: {kwargs!r}")


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


async def _task_with_child(service: MainRuntimeService):
    record = await service.create_task("pause marker task", session_id="web:shared")
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None
    child = service.node_runner._create_execution_child(
        task=task,
        parent=root,
        spec=SpawnChildSpec(goal="child goal", prompt="child prompt", execution_policy={"mode": "focus"}),
    )
    # 孤儿收尸只认带 spawn_owner_round_id 的派生节点；不写这一位就进不了候选集，
    # 用例会在"收尸根本没看它"的假绿下通过。
    service.log_service.update_node_metadata(
        child.node_id,
        lambda md: {
            **md,
            "spawn_owner_kind": "child",
            "spawn_owner_round_id": "round:test-pause",
            "spawn_owner_parent_node_id": root.node_id,
        },
    )
    return task, root, child


def _write_inflight_frame(service: MainRuntimeService, task_id: str, node_id: str, phase: str) -> None:
    call_id = f"call_{node_id.split(':')[-1]}"
    service.log_service.update_frame(
        task_id,
        node_id,
        lambda frame: {
            **frame,
            "phase": phase,
            "pending_tool_calls": [{"id": call_id, "name": "exec", "arguments": {"command": "echo hi"}}],
            "active_round_id": f"round:{node_id}",
            "active_round_tool_call_ids": [call_id],
            "tool_calls": [
                {
                    "tool_call_id": call_id,
                    "tool_name": "exec",
                    "status": "running",
                    "started_at": "2026-10-25T16:01:04+08:00",
                    "finished_at": "",
                    "elapsed_seconds": None,
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_task_pause_marks_inflight_node_with_task_reason(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task, _root, child = await _task_with_child(service)
        service.node_runner.mark_task_paused_node(task.task_id, child.node_id)

        node = service.store.get_node(child.node_id)
        assert node is not None
        assert node.is_paused is True
        assert node.pause_reason == "task"
        pause_row = service.store.get_task_node_pause(child.node_id)
        assert pause_row is not None and str(pause_row.pause_reason) == "task"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_pause_does_not_overwrite_operator_pause(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task, _root, child = await _task_with_child(service)
        service.node_runner._mark_node_paused(task.task_id, child.node_id, reason="manual", remark="人工暂停")
        service.node_runner.mark_task_paused_node(task.task_id, child.node_id)

        node = service.store.get_node(child.node_id)
        assert node is not None and node.pause_reason == "manual"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_orphan_reconcile_is_skipped_while_task_paused(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task, _root, child = await _task_with_child(service)
        _write_inflight_frame(service, task.task_id, child.node_id, "waiting_tool_results")
        service.log_service.set_pause_state(task.task_id, pause_requested=True, is_paused=True)
        dispatcher = service.task_actor_service._create_dispatcher(task.task_id)

        await service.task_actor_service._reconcile_orphan_in_progress_nodes(task.task_id, dispatcher)

        node = service.store.get_node(child.node_id)
        assert node is not None
        assert node.is_paused is False, "暂停期不得把在飞节点 park 成孤儿"
        assert not list(service.store.list_task_error_logs(task.task_id))
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resume_task_clears_task_pause_marker_and_revives_node(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task, _root, child = await _task_with_child(service)
        _write_inflight_frame(service, task.task_id, child.node_id, "waiting_tool_results")
        service.node_runner.mark_task_paused_node(task.task_id, child.node_id)

        calls: list[str] = []

        async def _fake_resume_entry(task_id: str, node_id: str, *, arm_verification: bool = True) -> str:
            calls.append(node_id)
            return "resumed"

        service.task_actor_service.resume_node_entry = _fake_resume_entry

        resumed = await service.task_actor_service._resume_task_paused_nodes(task.task_id)

        node = service.store.get_node(child.node_id)
        assert node is not None
        assert node.is_paused is False and node.pause_reason == ""
        assert resumed == [child.node_id]
        assert calls == [child.node_id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resume_task_does_not_revive_parked_node(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task, _root, child = await _task_with_child(service)
        _write_inflight_frame(service, task.task_id, child.node_id, "waiting_explicit")
        service.node_runner.mark_task_paused_node(task.task_id, child.node_id)

        calls: list[str] = []
        async def _fake_resume_entry(task_id: str, node_id: str, *, arm_verification: bool = True) -> str:
            calls.append(node_id)
            return "resumed"

        service.task_actor_service.resume_node_entry = _fake_resume_entry

        resumed = await service.task_actor_service._resume_task_paused_nodes(task.task_id)

        node = service.store.get_node(child.node_id)
        assert node is not None and node.is_paused is False
        assert resumed == [], "停靠节点只清位，不点火"
        assert calls == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resume_task_leaves_operator_pause_alone(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task, _root, child = await _task_with_child(service)
        _write_inflight_frame(service, task.task_id, child.node_id, "waiting_tool_results")
        service.node_runner._mark_node_paused(task.task_id, child.node_id, reason="manual", remark="人工暂停")

        resumed = await service.task_actor_service._resume_task_paused_nodes(task.task_id)

        node = service.store.get_node(child.node_id)
        assert node is not None and node.is_paused is True and node.pause_reason == "manual"
        assert resumed == []
    finally:
        await service.close()
