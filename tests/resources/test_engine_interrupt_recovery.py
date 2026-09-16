"""引擎中断不得把任务终态化——必须留给启动恢复（2026-09-16 事故）。

事故链（task:eacd0f0467b7）：worker 进程被外部信号终止 → 有序退出收尾取消
在飞执行器 → run_node/run_task 的 CancelledError 分支在「无取消/暂停标志」
时仍落 failed/'canceled' 终态 → 任务在下一个 worker 启动前已被终态化，
`_recover_interrupted_task` 启动恢复永远没有机会接管 → 215 份简历任务
对用户呈现为「已取消」，工作整体丢失。

本文件锁定修复契约：
- E1：无 cancel/pause 标志的 CancelledError（进程退出收尾、杂散取消）——
  节点保持 in_progress、任务保持 in_progress，绝不落 canceled 终态；
- E2：尽力即时重排钩子被触发（进程仍活着时任务不悬空）；
- E3：用户主动取消（cancel_requested=True）语义不变——仍终态为
  failed/'canceled'（引擎中断豁免不得侵蚀用户取消）；
- E4：服务层重排守护——只重排「存活、无标志、仍 in_progress」的任务，
  调度器关闭（进程退出）后不再重排。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import main.runtime.react_loop as react_loop_module
from main.service.runtime_service import MainRuntimeService


@pytest.fixture(autouse=True)
def _default_node_send_preflight_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
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


class _GatedBackend:
    """首个模型调用被 gate 挡住，制造「执行器在飞」窗口。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        self.entered.set()
        await self.gate.wait()
        raise AssertionError("gated chat should be cancelled, never completed")


def _build_service(tmp_path: Path, backend) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=backend,
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )

    async def _noop_enqueue(task_id: str) -> None:
        _ = task_id
        return None

    async def _noop_cancel(task_id: str) -> None:
        _ = task_id
        return None

    async def _noop_wait(task_id: str) -> None:
        _ = task_id
        return None

    service.global_scheduler.enqueue_task = _noop_enqueue
    service.global_scheduler.cancel_task = _noop_cancel
    service.global_scheduler.wait = _noop_wait
    return service


async def _start_gated_task(tmp_path: Path):
    backend = _GatedBackend()
    service = _build_service(tmp_path, backend)
    record = await service.create_task("engine interrupt recovery probe", session_id="web:ceo-demo")
    runner = asyncio.create_task(
        service.task_actor_service.run_task(record.task_id),
        name=f"test-runner:{record.task_id}",
    )
    await asyncio.wait_for(backend.entered.wait(), timeout=15)
    return service, backend, record, runner


# ---------------------------------------------------------------------------
# E1 + E2：引擎级中断——节点/任务保持 in_progress，重排钩子被触发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_interrupt_keeps_task_recoverable(tmp_path: Path) -> None:
    service, backend, record, runner = await _start_gated_task(tmp_path)
    try:
        enqueued: list[str] = []
        requeue_requests: list[str] = []

        async def _recording_enqueue(task_id: str) -> None:
            enqueued.append(str(task_id or "").strip())

        def _recording_requeue_callback(task_id: str) -> None:
            requeue_requests.append(str(task_id or "").strip())
            loop = asyncio.get_running_loop()
            loop.call_soon(
                lambda normalized=str(task_id or "").strip(): asyncio.create_task(
                    _recording_enqueue(normalized)
                )
            )

        service.global_scheduler.enqueue_task = _recording_enqueue
        service.task_actor_service.interrupted_task_requeue_callback = _recording_requeue_callback

        # 模拟 worker 进程退出收尾：调度器取消执行器，且无任何取消/暂停标志。
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

        task = service.get_task(record.task_id)
        assert task is not None
        assert str(task.status or "").strip().lower() == "in_progress"
        assert not bool(task.cancel_requested)
        assert not bool(task.pause_requested)
        assert not bool(task.is_paused)
        assert not str(task.failure_reason or "").strip()

        root = service.store.get_node(record.root_node_id)
        assert root is not None
        assert str(root.status or "").strip().lower() == "in_progress"
        assert not str(root.failure_reason or "").strip()

        # E2：重排钩子立即触发，入队随后落地（进程存活时任务不悬空）。
        assert requeue_requests == [record.task_id]
        await asyncio.sleep(0.05)
        assert enqueued == [record.task_id]
    finally:
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


# ---------------------------------------------------------------------------
# E4：服务层重排守护——只重排「存活且无标志」的 in_progress 任务
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_interrupted_task_respects_flags(tmp_path: Path) -> None:
    service = _build_service(tmp_path, _GatedBackend())
    enqueued: list[str] = []

    async def _recording_enqueue(task_id: str) -> None:
        enqueued.append(str(task_id or "").strip())

    record = await service.create_task("requeue guard probe", session_id="web:ceo-demo")
    service.global_scheduler.enqueue_task = _recording_enqueue

    await service._requeue_interrupted_task(record.task_id)
    assert enqueued == [record.task_id]

    # 已请求取消的任务不得被重排（用户取消语义优先）。
    service.log_service.request_cancel(record.task_id)
    await service._requeue_interrupted_task(record.task_id)
    assert enqueued == [record.task_id]

    # 调度器已关闭（进程退出收尾）时不得重排。
    service.log_service.update_task_control(record.task_id, cancel_requested=False)
    await service.global_scheduler.close()
    await service._requeue_interrupted_task(record.task_id)
    assert enqueued == [record.task_id]


# ---------------------------------------------------------------------------
# E3：用户主动取消语义不变——仍终态为 failed/'canceled'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_cancel_still_terminalizes(tmp_path: Path) -> None:
    service, backend, record, runner = await _start_gated_task(tmp_path)
    try:
        service.log_service.request_cancel(record.task_id)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        # 根节点协程仍在 gate 上：关掉 dispatcher 让取消穿透到节点层。
        dispatcher = service.task_actor_service._dispatchers.get(record.task_id)
        if dispatcher is not None:
            await dispatcher.close()

        task = service.get_task(record.task_id)
        assert task is not None
        assert str(task.status or "").strip().lower() == "failed"
        assert str(task.failure_reason or "").strip() == "canceled"

        root = service.store.get_node(record.root_node_id)
        assert root is not None
        assert str(root.status or "").strip().lower() == "failed"
        assert str(root.failure_reason or "").strip() == "canceled"
    finally:
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
