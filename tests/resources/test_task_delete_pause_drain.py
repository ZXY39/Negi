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


# ---------------------------------------------------------------------------
# orphaned claims from previous worker generations
# ---------------------------------------------------------------------------

def _seed_drain_task(service: MainRuntimeService, task_id: str = "task:gen") -> None:
    from main.models import TaskRecord
    from main.protocol import now_iso

    stamp = now_iso()
    service.store.upsert_task(
        TaskRecord(
            task_id=task_id,
            session_id="web:demo",
            title="t",
            user_request="r",
            root_node_id="node:gen-root",
            status="in_progress",
            created_at=stamp,
            updated_at=stamp,
            is_paused=True,
            pause_requested=True,
        )
    )


def _enqueue_and_claim_pause(service: MainRuntimeService, task_id: str, worker_id: str, command_id: str) -> None:
    from main.protocol import now_iso

    stamp = now_iso()
    service.store.enqueue_task_command(
        command_id=command_id,
        task_id=task_id,
        session_id="web:demo",
        command_type="pause_task",
        created_at=stamp,
        payload={"task_id": task_id},
    )
    claimed = service.store.claim_pending_task_commands(worker_id=worker_id, claimed_at=stamp)
    assert [item["command_id"] for item in claimed] == [command_id]


def _mark_live_worker(service: MainRuntimeService, worker_id: str = "worker:live") -> None:
    from main.protocol import now_iso

    service.store.upsert_worker_status(
        worker_id=worker_id,
        role="task_worker",
        status="running",
        updated_at=now_iso(),
        payload={"execution_mode": "worker"},
    )


@pytest.mark.asyncio
async def test_drain_ignores_claim_from_previous_worker_generation(tmp_path, monkeypatch) -> None:
    """死代际的滞留领取是死账：不阻断删除，卡片也不再报「暂停中」。"""
    service = _make_web_service(tmp_path)
    _seed_drain_task(service, "task:gen")
    _enqueue_and_claim_pause(service, "task:gen", "worker:dead", "cmd:gen-old")
    _mark_live_worker(service, "worker:live")

    assert service._task_pause_drain_active("task:gen") is False
    payload = service.get_task_pause_state_payload("task:gen")
    assert payload is not None and payload["draining"] is False

    monkeypatch.setattr(runtime_service_module, "_DELETE_PAUSE_DRAIN_TIMEOUT_SECONDS", 0.3)
    wiped: list[str] = []

    async def _noop_wipe(task, *, reason):
        wiped.append(reason)
        return {}

    async def _background_stop(*args, **kwargs):
        return {"completed": True}

    service._wipe_task_data = _noop_wipe  # type: ignore[method-assign]
    service._node_background_jobs.close_scope_and_wait = _background_stop  # type: ignore[method-assign]

    deleted = await service.delete_task("task:gen")

    assert deleted is not None
    assert wiped == ["user_delete"]


@pytest.mark.asyncio
async def test_drain_blocks_when_claim_owned_by_live_worker(tmp_path, monkeypatch) -> None:
    """正控：领取者就是当前存活代际时，删除照旧被挡。"""
    service = _make_web_service(tmp_path)
    _seed_drain_task(service, "task:gen")
    _enqueue_and_claim_pause(service, "task:gen", "worker:live", "cmd:gen-live")
    _mark_live_worker(service, "worker:live")

    assert service._task_pause_drain_active("task:gen") is True
    payload = service.get_task_pause_state_payload("task:gen")
    assert payload is not None and payload["draining"] is True

    monkeypatch.setattr(runtime_service_module, "_DELETE_PAUSE_DRAIN_TIMEOUT_SECONDS", 0.3)
    with pytest.raises(ValueError) as excinfo:
        await service.delete_task("task:gen")
    assert str(excinfo.value) == "task_still_stopping"


def test_drain_counts_legacy_claims_without_owner(tmp_path) -> None:
    """无归属的历史行无从证明是死账，保守计入。"""
    service = _make_web_service(tmp_path)
    _seed_drain_task(service, "task:gen")
    _enqueue_and_claim_pause(service, "task:gen", "worker:dead", "cmd:gen-legacy")
    _mark_live_worker(service, "worker:live")
    service.store._execute_write(
        "UPDATE task_commands SET worker_id = NULL WHERE command_id = ?",
        ("cmd:gen-legacy",),
    )

    assert service._task_pause_drain_active("task:gen") is True


def test_settle_orphaned_task_commands_only_previous_generations(tmp_path) -> None:
    from main.protocol import now_iso
    from main.storage.sqlite_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    stamp = now_iso()

    def _enqueue(command_id: str) -> None:
        store.enqueue_task_command(
            command_id=command_id,
            task_id="task:settle",
            session_id="web:demo",
            command_type="pause_task",
            created_at=stamp,
            payload={},
        )

    try:
        _enqueue("cmd:old")
        store.claim_pending_task_commands(worker_id="worker:old", claimed_at=stamp)
        _enqueue("cmd:self")
        store.claim_pending_task_commands(worker_id="worker:self", claimed_at=stamp)
        _enqueue("cmd:pending")
        _enqueue("cmd:done")
        store.finish_task_command("cmd:done", finished_at=stamp, success=True)

        assert store.settle_orphaned_task_commands(worker_id="worker:self", finished_at=stamp) == 1

        old = store.get_task_command("cmd:old")
        assert old is not None
        assert old["status"] == "failed"
        assert old["error_text"] == "orphaned_claim_previous_worker"
        assert store.get_task_command("cmd:self")["status"] == "claimed"
        assert store.get_task_command("cmd:pending")["status"] == "pending"
        assert store.get_task_command("cmd:done")["status"] == "completed"
        # 幂等：同一代际再启动一次不结算任何行。
        assert store.settle_orphaned_task_commands(worker_id="worker:self", finished_at=stamp) == 0
        with pytest.raises(ValueError, match="worker_id_required"):
            store.settle_orphaned_task_commands(worker_id="  ", finished_at=stamp)
    finally:
        store.close()


def _make_worker_service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )


def _seed_paused_task(service: MainRuntimeService, *, is_paused: bool) -> None:
    from main.models import NodeRecord, TaskRecord
    from main.protocol import now_iso

    stamp = now_iso()
    service.store.upsert_task(
        TaskRecord(
            task_id='task:cancel1',
            session_id='web:demo',
            title='t',
            user_request='r',
            root_node_id='node:root',
            status='in_progress',
            created_at=stamp,
            updated_at=stamp,
            is_paused=is_paused,
            pause_requested=is_paused,
        )
    )
    for node_id, parent in (('node:root', None), ('node:child', 'node:root')):
        service.store.upsert_node(
            NodeRecord(
                node_id=node_id,
                task_id='task:cancel1',
                root_node_id='node:root',
                parent_node_id=parent,
                goal='g',
                prompt='p',
                created_at=stamp,
                updated_at=stamp,
            )
        )


@pytest.mark.asyncio
async def test_cancel_terminalizes_a_paused_task_and_sweeps_residual_nodes(tmp_path) -> None:
    """取消暂停中的任务必须落终态并立即收尸残余节点。

    以前带 `not is_paused` 守卫：暂停 → 取消只留下 cancel_requested，没有任何协程会去
    观察它，任务永久挂在 in_progress + paused；节点也停在 in_progress 等下一次 worker
    启动才扫。页面没有取消按钮，模型侧 control tool 与 REST 是仅有的两条入口。
    """
    service = _make_worker_service(tmp_path)
    _seed_paused_task(service, is_paused=True)

    record = await service.cancel_task('task:cancel1')

    assert record is not None
    assert record.status == 'failed'
    assert bool(record.is_paused) is False
    assert bool(record.pause_requested) is False
    nodes = {node.node_id: node for node in service.store.list_nodes('task:cancel1')}
    assert nodes['node:root'].status == 'failed'
    assert nodes['node:child'].status == 'failed'
    assert 'task_terminal_cleanup' in str(nodes['node:child'].failure_reason)


@pytest.mark.asyncio
async def test_cancel_of_running_task_still_terminalizes(tmp_path) -> None:
    """非暂停路径的既有语义不变。"""
    service = _make_worker_service(tmp_path)
    _seed_paused_task(service, is_paused=False)

    record = await service.cancel_task('task:cancel1')

    assert record is not None
    assert record.status == 'failed'
