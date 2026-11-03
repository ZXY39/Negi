"""恢复拾取不得点亮停靠根节点；最终验收只对真实提交开放。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

import g3ku.web.launcher as launcher
from main.errors import NodePausedError
from main.models import NodeFinalResult, SpawnChildSpec, normalize_final_acceptance_metadata
from main.runtime.task_actor_service import TaskActorService, TaskNodeDispatcher
from main.service.runtime_service import MainRuntimeService
from main.types import KIND_ACCEPTANCE


class _DummyChatBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        raise AssertionError(f"chat backend should not be called: {kwargs!r}")


def _build_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )

    async def _noop_enqueue(_task_id: str) -> None:
        return None

    service.global_scheduler.enqueue_task = _noop_enqueue
    return service


def _final_acceptance_nodes(service: MainRuntimeService, task_id: str, root_id: str) -> list[object]:
    return [
        node
        for node in service.store.list_nodes(task_id) or []
        if str(getattr(node, "node_kind", "") or "").strip().lower() == KIND_ACCEPTANCE
        and str(getattr(node, "parent_node_id", "") or "").strip() == root_id
    ]


async def _create_task_with_acceptance(service: MainRuntimeService, *, prompt: str):
    record = await service.create_task(
        prompt,
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None
    node_id = str(
        normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance")).node_id or ""
    ).strip()
    acceptance = service.store.get_node(node_id)
    assert acceptance is not None
    return record.task_id, root, acceptance


@pytest.mark.asyncio
async def test_recovery_preserves_live_final_acceptance_pointer(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, _root, acceptance = await _create_task_with_acceptance(service, prompt="recovery pointer")
        service._recover_interrupted_task(task_id)
        task = service.get_task(task_id)
        assert task is not None
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance"))
        assert final_acceptance.node_id == acceptance.node_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_final_acceptance_node_is_adopted_not_duplicated(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root, acceptance = await _create_task_with_acceptance(service, prompt="adopt acceptance")
        before = _final_acceptance_nodes(service, task_id, root.node_id)
        assert [node.node_id for node in before] == [acceptance.node_id]

        def _clear_pointer(metadata):
            current = normalize_final_acceptance_metadata(metadata.get("final_acceptance"))
            payload = current.model_dump(mode="json")
            payload["node_id"] = ""
            metadata["final_acceptance"] = payload
            return metadata

        service.log_service.update_task_metadata(task_id, _clear_pointer)
        task = service.get_task(task_id)
        assert task is not None
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance"))
        adopted = await service.task_actor_service._get_or_create_final_acceptance_node(
            task_id=task_id,
            task=task,
            root=root,
            final_acceptance=final_acceptance,
        )
        assert adopted.node_id == acceptance.node_id
        after = _final_acceptance_nodes(service, task_id, root.node_id)
        assert [node.node_id for node in after] == [acceptance.node_id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_run_task_does_not_execute_parked_root(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root, _acceptance = await _create_task_with_acceptance(service, prompt="parked root")
        service.log_service.replace_runtime_frames(
            task_id,
            frames=[
                {
                    "node_id": root.node_id,
                    "depth": root.depth,
                    "node_kind": root.node_kind,
                    "phase": "waiting_explicit",
                }
            ],
        )
        await service.task_actor_service.run_task(task_id)
        assert service._chat_backend.calls == 0
    finally:
        await service.close()


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


@pytest.mark.asyncio
async def test_interrupted_task_recovery_keeps_root_pause(tmp_path: Path) -> None:
    """启动恢复只重置任务行；根节点的独立暂停保留，交给下一次 resume。"""
    service = _build_service(tmp_path)
    try:
        task_id, root, _acceptance = await _create_task_with_acceptance(
            service, prompt="recovery keeps root pause"
        )
        service.log_service.set_node_pause_state(
            task_id,
            root.node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="error",
            remark="provider unavailable",
        )

        service._recover_interrupted_task(task_id)

        task = service.get_task(task_id)
        assert task is not None and task.status == "in_progress"
        assert task.is_paused is False and task.pause_requested is False
        latest_root = service.get_node(root.node_id)
        assert latest_root is not None and latest_root.is_paused is True
        assert latest_root.pause_reason == "error"
        pause_row = service.store.get_task_node_pause(root.node_id)
        assert pause_row is not None and pause_row.remark == "provider unavailable"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_root_error_pause_keeps_actor_alive_until_children_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """根回合因暂停退出时，actor 继续驱动非根节点，dispatcher 最后才关闭。"""
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("root pause", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child_a = _execution_child(service, task=task, parent=root, name="a")
        child_b = _execution_child(service, task=task, parent=root, name="b")

        original_execute = TaskNodeDispatcher.execute_node

        async def execute_node(self, task_id: str, node_id: str):
            if node_id == root.node_id:
                raise NodePausedError(task_id, node_id)
            return await original_execute(self, task_id, node_id)

        monkeypatch.setattr(TaskNodeDispatcher, "execute_node", execute_node)

        async def fake_run_node(task_id: str, node_id: str):
            await asyncio.sleep(0.02)
            service.log_service.update_node_status(
                task_id, node_id, status="success", final_output="done"
            )
            return _success_result(node_id)

        monkeypatch.setattr(service.node_runner, "run_node", fake_run_node)
        closed_snapshot: dict[str, str] = {}
        original_close = TaskNodeDispatcher.close

        async def close(self):
            for node_id in (child_a.node_id, child_b.node_id):
                node = service.get_node(node_id)
                closed_snapshot[node_id] = str(getattr(node, "status", "") or "")
            return await original_close(self)

        monkeypatch.setattr(TaskNodeDispatcher, "close", close)

        await service.task_actor_service.run_task(record.task_id)

        # 子节点跑到终态，没有被 dispatcher.close 取消。
        for node_id in (child_a.node_id, child_b.node_id):
            node = service.get_node(node_id)
            assert node is not None and node.status == "success"
            assert closed_snapshot.get(node_id) == "success"
        # 根保持暂停，任务旗标不因根暂停而移动。
        latest_root = service.get_node(root.node_id)
        assert latest_root is not None and latest_root.is_paused is True
        latest_task = service.get_task(record.task_id)
        assert latest_task is not None and latest_task.status == "in_progress"
        assert latest_task.is_paused is False and latest_task.pause_requested is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_paused_root_skips_the_turn_and_still_drives_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("paused root pickup", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = _execution_child(service, task=task, parent=root, name="child")
        service.log_service.set_node_pause_state(
            record.task_id,
            root.node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="manual",
        )

        async def execute_node(self, task_id: str, node_id: str):
            if node_id == root.node_id:
                raise AssertionError("a paused root must not start a turn")
            raise AssertionError(f"unexpected direct dispatch: {node_id}")

        monkeypatch.setattr(TaskNodeDispatcher, "execute_node", execute_node)

        async def fake_run_node(task_id: str, node_id: str):
            service.log_service.update_node_status(
                task_id, node_id, status="success", final_output="done"
            )
            return _success_result(node_id)

        monkeypatch.setattr(service.node_runner, "run_node", fake_run_node)

        await service.task_actor_service.run_task(record.task_id)

        latest_child = service.get_node(child.node_id)
        assert latest_child is not None and latest_child.status == "success"
        latest_root = service.get_node(root.node_id)
        assert latest_root is not None and latest_root.is_paused is True
        latest_task = service.get_task(record.task_id)
        assert latest_task is not None and latest_task.is_paused is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_final_acceptance_is_deferred_until_real_submission(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root, acceptance = await _create_task_with_acceptance(service, prompt="no submission")
        before = [node.node_id for node in _final_acceptance_nodes(service, task_id, root.node_id)]
        result = await service.task_actor_service._run_final_acceptance_if_needed(task_id)
        assert result.delivery_status == "partial"
        after = [node.node_id for node in _final_acceptance_nodes(service, task_id, root.node_id)]
        assert after == before == [acceptance.node_id]
        assert service._chat_backend.calls == 0
    finally:
        await service.close()


def test_only_final_submission_enters_final_acceptance() -> None:
    assert TaskActorService._is_final_submission_result(
        NodeFinalResult(status="success", delivery_status="final")
    )
    assert not TaskActorService._is_final_submission_result(
        NodeFinalResult(status="success", delivery_status="partial")
    )
    assert not TaskActorService._is_final_submission_result(
        NodeFinalResult(status="failed", delivery_status="blocked")
    )


def test_launcher_requests_graceful_exit_before_force_kill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    killed: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.info = {
                "pid": pid,
                "exe": str(tmp_path / ".venv" / "Scripts" / "python.exe"),
                "cmdline": ["python", "-m", "g3ku", "web"],
            }

        def cwd(self) -> str:
            return str(tmp_path)

        def kill(self) -> None:
            killed.append(self.pid)

        def parent(self):
            return None

    class _FakePsutil:
        @staticmethod
        def Process(_pid: int):  # noqa: N802 - mirrors the psutil API name
            return _FakeProcess(99)

        @staticmethod
        def process_iter(attrs=None):
            return [_FakeProcess(1001)]

        @staticmethod
        def wait_procs(procs, timeout):
            return (list(procs), [])

    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    monkeypatch.setattr(launcher, "_resolve_project_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "_web_runtime_cmdline_matches", lambda cmdline: True)
    monkeypatch.setattr(launcher, "_request_runtime_exit", lambda port, **kwargs: True)

    assert launcher._terminate_stale_web_runtime_processes(port=18790) == []
    assert killed == []
