"""web 模式删除任务前必须等待暂停排空结束的回归测试。

修复缺陷：web 模式下 pause_task 只置持久标志并投递命令，不等待 worker
确认；delete_task 随即删除文件，而 worker actor 可能仍在写，造成半写
残留/记录损坏。现在删除前轮询等待 pause_task 命令被消费完（排空结束），
到期未排空按 task_still_stopping 拒绝。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from main.service import runtime_service as runtime_service_module
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _make_web_service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )


def _paused_task(task_id: str = "task-drain", status: str = "in_progress") -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        session_id="web:demo",
        status=status,
        is_paused=True,
        pause_requested=True,
    )


def _wire_drain(service: MainRuntimeService, *, worker_state: str, unfinished: bool) -> None:
    service.worker_state = lambda: worker_state  # type: ignore[method-assign]
    # 返回归一化与未归一化两种 id 形态，覆盖直接调用与经
    # delete_task（先做 normalize_task_id）两条路径。
    service.store.list_unfinished_task_commands = (  # type: ignore[method-assign]
        lambda command_type="": (
            [{"task_id": "task-drain"}, {"task_id": "task:task-drain"}]
            if unfinished and command_type == "pause_task"
            else []
        )
    )


def test_drain_requires_all_conditions(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task = _paused_task()

    _wire_drain(service, worker_state="online", unfinished=True)
    assert service._task_pause_drain_active("task-drain", task=task) is True

    _wire_drain(service, worker_state="online", unfinished=False)
    assert service._task_pause_drain_active("task-drain", task=task) is False, "命令已消费则排空结束"

    _wire_drain(service, worker_state="offline", unfinished=True)
    assert service._task_pause_drain_active("task-drain", task=task) is False, "worker 离线时持久标志即权威"

    _wire_drain(service, worker_state="online", unfinished=True)
    assert service._task_pause_drain_active("task-drain", task=_paused_task(status="success")) is False

    not_paused = _paused_task()
    not_paused.is_paused = False
    not_paused.pause_requested = False
    assert service._task_pause_drain_active("task-drain", task=not_paused) is False


def test_drain_never_active_outside_web_mode(tmp_path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.worker_state = lambda: "online"  # type: ignore[method-assign]
    service.store.list_unfinished_task_commands = lambda command_type="": [{"task_id": "task-drain"}]  # type: ignore[method-assign]
    assert service._task_pause_drain_active("task-drain", task=_paused_task()) is False


async def test_await_drain_times_out_when_worker_never_consumes_command(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    service.get_task = lambda task_id: _paused_task(task_id)  # type: ignore[method-assign]
    _wire_drain(service, worker_state="online", unfinished=True)

    started = time.monotonic()
    drained = await service._await_task_pause_drain("task-drain", timeout_seconds=0.6, poll_interval_seconds=0.05)
    assert drained is False
    assert time.monotonic() - started >= 0.5


async def test_await_drain_returns_true_once_command_consumed(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    service.get_task = lambda task_id: _paused_task(task_id)  # type: ignore[method-assign]
    consumed = {"done": False}

    def _unfinished(command_type="") -> list:
        if command_type == "pause_task" and not consumed["done"]:
            return [{"task_id": "task-drain"}]
        return []

    service.worker_state = lambda: "online"  # type: ignore[method-assign]
    service.store.list_unfinished_task_commands = _unfinished  # type: ignore[method-assign]

    async def _consume_soon() -> None:
        await asyncio.sleep(0.15)
        consumed["done"] = True

    consumer = asyncio.create_task(_consume_soon())
    drained = await service._await_task_pause_drain("task-drain", timeout_seconds=5.0, poll_interval_seconds=0.05)
    await consumer
    assert drained is True


async def test_delete_task_refused_while_pause_drain_active(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_service_module, "_DELETE_PAUSE_DRAIN_TIMEOUT_SECONDS", 0.6)
    service = _make_web_service(tmp_path)
    service.get_task = lambda task_id: _paused_task(task_id)  # type: ignore[method-assign]
    # 排空门之前的分发清理协作者依赖真实任务读模型，非本用例被测对象。
    service._cancel_distribution_for_force_delete = lambda **kwargs: None  # type: ignore[method-assign]
    deleted: list[str] = []
    _wire_drain(service, worker_state="online", unfinished=True)
    service.store.delete_task = deleted.append  # type: ignore[method-assign]

    with pytest.raises(ValueError) as excinfo:
        await service.delete_task("task-drain")

    assert str(excinfo.value) == "task_still_stopping"
    assert deleted == [], "排空未完成前绝不允许删除任务记录/文件"
