"""节点后台作业生命周期联动测试：所有权、准入、生命周期停止、终态投递与恢复。

对应实现计划 docs/FIX_PLAN_node-background-jobs-and-lifecycle.md：
- 准入在域关闭后拒绝；恢复只开新准入，旧作业不复活；
- 生命周期停止有逐项结果；终态先落 outbox、经 sink 投节点信箱；
- LogService 的真实状态写点触发统一通知（node_pause/node_terminal/task_pause/...）；
- 存储按 owner_kind 分流，节点行按 task_id 删除。
"""

from __future__ import annotations

import asyncio

import pytest

from g3ku.runtime.tool_jobs import (
    TOOL_JOB_OWNER_KIND_NODE,
    TOOL_JOB_OWNER_KIND_SESSION,
    TOOL_JOB_STATUS_INTERRUPTED,
    TOOL_JOB_STATUS_RUNNING,
    TOOL_JOB_STATUS_STOPPING,
    TOOL_JOB_STATUS_SUCCESS,
    ToolJobOutcome,
)
from main.monitoring.log_service import TaskLogService
from main.protocol import now_iso
from main.runtime.node_background_jobs import (
    STOP_REASON_NODE_PAUSED,
    STOP_REASON_NODE_TERMINAL,
    STOP_REASON_TASK_PAUSED,
    NodeBackgroundAdmissionClosed,
    NodeBackgroundJobManager,
)
from main.storage.sqlite_store import SQLiteTaskStore


@pytest.fixture()
def store(tmp_path) -> SQLiteTaskStore:
    return SQLiteTaskStore(tmp_path / "runtime.sqlite3")


class _DeliveryRecorder:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.accept: bool = True

    async def __call__(self, payload: dict) -> bool:
        self.events.append(dict(payload))
        return self.accept


def _manager(store: SQLiteTaskStore, sink: _DeliveryRecorder) -> NodeBackgroundJobManager:
    return NodeBackgroundJobManager(
        store_getter=lambda: store,
        delivery_sink=sink,
        stop_grace_seconds=0.05,
    )


def _job_run(delay: float = 30.0, text: str = "done"):
    async def run() -> ToolJobOutcome:
        await asyncio.sleep(delay)
        return ToolJobOutcome(status=TOOL_JOB_STATUS_SUCCESS, result_text=text, result_preview=text)

    return run


async def _wait_no_active_jobs(manager: NodeBackgroundJobManager, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = [
            key
            for key, state in manager._scopes.items()
            if state.runner is not None and state.runner.list_jobs()
        ]
        if not remaining:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("tool jobs still active after scope stop")


@pytest.mark.asyncio
async def test_admission_refused_after_scope_close_and_reopened_on_resume(store):
    sink = _DeliveryRecorder()
    manager = _manager(store, sink)

    job, reused = manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:a",
        turn_id="round:node:a:1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="hash-1",
        hint="long poll",
        run=_job_run(),
        timeout_seconds=0.0,
    )
    assert reused is False
    assert manager.admission_reason("task:demo", "node:a") == ""
    # 交棒（前台窗口耗尽）：耐久登记 job，随后节点/任务暂停要停掉它。
    manager._scope("task:demo", "node:a").runner._promote(job)
    assert store.get_tool_job(job.job_id) is not None

    manager.handle_lifecycle_event(
        task_id="task:demo", node_id="node:a", action="node_pause", reason="manual pause"
    )
    assert manager.admission_reason("task:demo", "node:a") == STOP_REASON_NODE_PAUSED
    with pytest.raises(NodeBackgroundAdmissionClosed):
        manager.admit_tool_job(
            task_id="task:demo",
            node_id="node:a",
            turn_id="round:node:a:2",
            tool_call_id="call-2",
            tool_name="exec",
            args_hash="hash-2",
            hint="again",
            run=_job_run(),
            timeout_seconds=0.0,
        )

    # 暂停停止：作业收成 interrupted，payload 带停止来源（不冒充用户手动停止）。
    await _wait_no_active_jobs(manager)
    row = store.get_tool_job(job.job_id)
    assert row is not None
    assert row["owner_kind"] == TOOL_JOB_OWNER_KIND_NODE
    assert row["task_id"] == "task:demo"
    assert row["node_id"] == "node:a"
    assert row["status"] in {TOOL_JOB_STATUS_INTERRUPTED, TOOL_JOB_STATUS_STOPPING}

    manager.handle_lifecycle_event(task_id="task:demo", node_id="node:a", action="node_resume")
    assert manager.admission_reason("task:demo", "node:a") == ""
    # 恢复后新调用准入开放；旧作业保持已停止，不复活。
    assert store.get_tool_job(job.job_id)["status"] != TOOL_JOB_STATUS_RUNNING


@pytest.mark.asyncio
async def test_scope_stop_close_and_wait_reports_confirmed_processes(store):
    sink = _DeliveryRecorder()
    manager = _manager(store, sink)
    manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:a",
        turn_id="round:node:a:1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="hash-1",
        hint="long poll",
        run=_job_run(),
        timeout_seconds=0.0,
    )
    result = await manager.close_scope_and_wait(
        "task:demo",
        node_id="node:a",
        stop_reason=STOP_REASON_NODE_PAUSED,
        note="manual pause",
        timeout_seconds=3.0,
    )
    assert result["completed"] is True
    assert result["stopped_tool_jobs"] == 1
    assert result["failures"] == []
    await _wait_no_active_jobs(manager)


@pytest.mark.asyncio
async def test_terminal_event_written_to_outbox_first_then_delivered(store):
    sink = _DeliveryRecorder()
    sink.accept = False
    manager = _manager(store, sink)
    # 后台化作业（窗口耗尽交棒）验证 outbox 写序：先落 outbox、投递失败留 pending。
    job2, _reused2 = manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:b",
        turn_id="round:node:b:2",
        tool_call_id="call-2",
        tool_name="exec",
        args_hash="hash-2",
        hint="slow",
        run=_job_run(delay=5.0),
        timeout_seconds=0.0,
    )
    state = manager._scope("task:demo", "node:b")
    runner = state.runner
    runner._promote(job2)
    row = store.get_tool_job(job2.job_id)
    assert row is not None and row["status"] == TOOL_JOB_STATUS_RUNNING
    await manager.close_scope_and_wait(
        "task:demo",
        node_id="node:b",
        stop_reason=STOP_REASON_NODE_TERMINAL,
        timeout_seconds=3.0,
    )
    await _wait_no_active_jobs(manager)
    terminal_row = store.get_tool_job(job2.job_id)
    assert terminal_row["status"] == TOOL_JOB_STATUS_INTERRUPTED
    # 终态先落 outbox（payload 带 external_event_id 身份），sink 交付后标记 delivered。
    dedupe_key = f"tool-job:{job2.job_id}:{TOOL_JOB_STATUS_INTERRUPTED}"
    outbox_row = store.get_tool_job_outbox(dedupe_key)
    assert outbox_row is not None
    assert outbox_row["owner_kind"] == TOOL_JOB_OWNER_KIND_NODE
    assert outbox_row["payload"]["external_event_id"] == dedupe_key
    assert outbox_row["payload"]["task_id"] == "task:demo"
    assert outbox_row["payload"]["node_id"] == "node:b"
    assert outbox_row["payload"]["stopped_by"] == STOP_REASON_NODE_TERMINAL
    # sink 交付失败：outbox 保持 pending，等补投。
    assert outbox_row["delivery_state"] == "pending"


@pytest.mark.asyncio
async def test_recover_after_restart_marks_interrupted_and_replays_outbox(store):
    sink = _DeliveryRecorder()
    manager = _manager(store, sink)
    job, _reused = manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:c",
        turn_id="round:node:c:1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="hash-1",
        hint="slow",
        run=_job_run(delay=30.0),
        timeout_seconds=0.0,
    )
    state = manager._scope("task:demo", "node:c")
    state.runner._promote(job)
    # 模拟进程重启：清空内存态，然后走启动恢复。
    state.runner._jobs.clear()
    state.runner._inflight.clear()
    manager._scopes.clear()
    manager._task_scopes.clear()

    recovery = await manager.recover_after_restart()

    assert recovery["recovered"] == 1
    row = store.get_tool_job(job.job_id)
    assert row["status"] == TOOL_JOB_STATUS_INTERRUPTED
    assert sink.events and sink.events[0]["job_id"] == job.job_id
    assert sink.events[0]["owner_kind"] == TOOL_JOB_OWNER_KIND_NODE
    dedupe_key = f"tool-job:{job.job_id}:{TOOL_JOB_STATUS_INTERRUPTED}"
    outbox_row = store.get_tool_job_outbox(dedupe_key)
    assert outbox_row is not None and outbox_row["delivery_state"] == "delivered"


@pytest.mark.asyncio
async def test_replay_pending_outbox_redelivers_unconsumed_events(store):
    sink = _DeliveryRecorder()
    sink.accept = False
    manager = _manager(store, sink)
    job, _reused = manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:d",
        turn_id="round:node:d:1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="hash-1",
        hint="slow",
        run=_job_run(delay=30.0),
        timeout_seconds=0.0,
    )
    state = manager._scope("task:demo", "node:d")
    state.runner._promote(job)
    await manager.close_scope_and_wait(
        "task:demo", node_id="node:d", stop_reason=STOP_REASON_NODE_PAUSED, timeout_seconds=3.0
    )
    await _wait_no_active_jobs(manager)
    dedupe_key = f"tool-job:{job.job_id}:{TOOL_JOB_STATUS_INTERRUPTED}"
    assert store.get_tool_job_outbox(dedupe_key)["delivery_state"] == "pending"

    sink.accept = True
    delivered = await manager.replay_pending_outbox()

    assert delivered == 1
    assert store.get_tool_job_outbox(dedupe_key)["delivery_state"] == "delivered"


def test_snapshot_and_count_cover_both_background_kinds(store):
    import subprocess
    import sys
    import time as _time

    from g3ku.runtime import owned_processes

    sink = _DeliveryRecorder()
    manager = _manager(store, sink)
    scope = owned_processes.scope_key(session_key="", task_id="task:panel")
    # 条目按身份自愈清死行：这里起一个真进程，注册才能活到快照读取。
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _time.sleep(0.05)
    entry = owned_processes.register(
        scope=scope,
        pid=process.pid,
        cmd="python worker.py",
        log_path="",
        session_key="",
        task_id="task:panel",
        node_id="node:a",
        label="worker",
    )
    try:
        snapshot = manager.snapshot_task_background("task:panel")
        kinds = [item["kind"] for item in snapshot["processes"]]
        assert kinds == ["process"]
        assert snapshot["processes"][0]["node_id"] == "node:a"
        assert snapshot["total"] >= 1
        assert manager.count_active_for_task("task:panel") >= 1
    finally:
        process.kill()
        process.wait(timeout=10)
        owned_processes.drop(scope, entry["pid"])


@pytest.mark.asyncio
async def test_tool_job_purpose_flows_to_panel_rows(store):
    import os

    sink = _DeliveryRecorder()
    manager = _manager(store, sink)
    job, _reused = manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:e",
        turn_id="round:node:e:1",
        tool_call_id="call-e",
        tool_name="exec",
        args_hash="hash-purpose",
        hint="exec (command=..., purpose=rebuild gate, timeout_seconds=900)",
        purpose="rebuild and re-run check gate",
        run=_job_run(delay=5.0),
        timeout_seconds=0.0,
    )
    manager._scope("task:demo", "node:e").runner._promote(job)
    snapshot = manager.snapshot_task_background("task:demo")
    rows = [item for item in snapshot["tool_jobs"] if item["job_id"] == job.job_id]
    assert rows and rows[0]["purpose"] == "rebuild and re-run check gate"
    row = store.get_tool_job(job.job_id)
    assert row is not None and row["purpose"] == "rebuild and re-run check gate"

    # 孤儿行（进程重启后内存为空、只剩账本行）同样把 purpose 带给面板。
    store.put_tool_job(
        job_id="tooljob:orphan",
        session_id="task:demo",
        turn_id="round:orphan",
        tool_call_id="call-orphan",
        tool_name="exec",
        args_hash="hash-orphan",
        hint="exec (command=..., purpose=orphan gate)",
        status=TOOL_JOB_STATUS_RUNNING,
        started_at=now_iso(),
        updated_at=now_iso(),
        timeout_seconds=0,
        owner_pid=os.getpid(),
        recovery_policy="model_decide",
        owner_kind=TOOL_JOB_OWNER_KIND_NODE,
        task_id="task:demo",
        node_id="node:f",
        purpose="orphan gate",
    )
    orphan_snapshot = manager.snapshot_task_background("task:demo")
    orphan_rows = [item for item in orphan_snapshot["tool_jobs"] if item["job_id"] == "tooljob:orphan"]
    assert orphan_rows and orphan_rows[0]["purpose"] == "orphan gate"
    assert orphan_rows[0]["orphaned"] is True

    await manager.close_scope_and_wait(
        "task:demo",
        node_id="node:e",
        stop_reason=STOP_REASON_NODE_TERMINAL,
        timeout_seconds=3.0,
    )
    await _wait_no_active_jobs(manager)


def test_log_service_lifecycle_write_points_notify_listener(tmp_path):
    """真实状态写点触发统一通知：节点暂停/终态、任务暂停/恢复、残留清扫。"""
    from main.models import NodeRecord, TaskRecord, TokenUsageSummary
    from main.monitoring.file_store import TaskFileStore

    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    log_service = TaskLogService(
        store=store,
        file_store=TaskFileStore(tmp_path / "files"),
        registry=None,
        event_history_enabled=False,
    )
    events: list[dict] = []
    log_service.add_node_lifecycle_listener(events.append)

    task = TaskRecord(
        task_id="task:lc",
        root_node_id="node:lc",
        title="demo",
        user_request="p",
        session_id="web:shared",
        status='in_progress',
        created_at=now_iso(),
        updated_at=now_iso(),
        metadata={},
    )
    node = NodeRecord(
        node_id="node:lc",
        task_id=task.task_id,
        parent_node_id=None,
        root_node_id="node:lc",
        depth=0,
        node_kind='execution',
        status='in_progress',
        goal='g',
        prompt='p',
        input='p',
        output=[],
        check_result='',
        final_output='',
        can_spawn_children=False,
        created_at=now_iso(),
        updated_at=now_iso(),
        token_usage=TokenUsageSummary(tracked=True),
    )
    store.upsert_task(task)
    store.upsert_node(node)
    log_service.set_node_pause_state(task.task_id, node.node_id, pause_requested=True, is_paused=True)
    assert any(e["action"] == "node_pause" and e["node_id"] == node.node_id for e in events)
    log_service.set_node_pause_state(task.task_id, node.node_id, pause_requested=False, is_paused=False)
    assert any(e["action"] == "node_resume" and e["node_id"] == node.node_id for e in events)

    log_service.set_pause_state(task.task_id, pause_requested=True, is_paused=True)
    assert any(e["action"] == "task_pause" and e["task_id"] == task.task_id for e in events)
    log_service.set_pause_state(task.task_id, pause_requested=False, is_paused=False)
    assert any(e["action"] == "task_resume" and e["task_id"] == task.task_id for e in events)

    log_service.update_node_status(task.task_id, node.node_id, status="success", final_output="ok")
    assert any(e["action"] == "node_terminal" and e["node_id"] == node.node_id for e in events)


@pytest.mark.asyncio
async def test_store_owner_kind_filters_and_task_delete(store):
    """owner_kind 分流：节点行不混进会话查询；删除按 task_id 清行与 outbox。"""
    store.put_tool_job(
        job_id="tooljob:node-1",
        session_id="task:demo",
        turn_id="round:1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="h1",
        hint="hint",
        status=TOOL_JOB_STATUS_RUNNING,
        started_at=now_iso(),
        updated_at=now_iso(),
        timeout_seconds=0,
        owner_pid=1,
        recovery_policy="model_decide",
        owner_kind=TOOL_JOB_OWNER_KIND_NODE,
        task_id="task:demo",
        node_id="node:a",
    )
    store.put_tool_job(
        job_id="tooljob:session-1",
        session_id="web:demo",
        turn_id="turn-1",
        tool_call_id="call-2",
        tool_name="exec",
        args_hash="h2",
        hint="hint",
        status=TOOL_JOB_STATUS_RUNNING,
        started_at=now_iso(),
        updated_at=now_iso(),
        timeout_seconds=0,
        owner_pid=1,
        recovery_policy="model_decide",
        owner_kind=TOOL_JOB_OWNER_KIND_SESSION,
    )
    node_rows = store.list_tool_jobs(owner_kind=TOOL_JOB_OWNER_KIND_NODE, task_id="task:demo")
    assert [row["job_id"] for row in node_rows] == ["tooljob:node-1"]
    session_rows = store.list_tool_jobs(
        owner_kind=TOOL_JOB_OWNER_KIND_SESSION, session_id="web:demo"
    )
    assert [row["job_id"] for row in session_rows] == ["tooljob:session-1"]
    by_call = store.list_tool_jobs(
        owner_kind=TOOL_JOB_OWNER_KIND_NODE,
        task_id="task:demo",
        node_id="node:a",
        tool_call_id="call-1",
    )
    assert [row["job_id"] for row in by_call] == ["tooljob:node-1"]

    store.put_tool_job_outbox(
        dedupe_key="tool-job:tooljob:node-1:interrupted",
        job_id="tooljob:node-1",
        session_id="task:demo",
        created_at=now_iso(),
        payload={"job_id": "tooljob:node-1"},
        owner_kind=TOOL_JOB_OWNER_KIND_NODE,
    )
    deleted = store.delete_tool_jobs_for_task("task:demo")
    assert deleted == 2
    assert store.get_tool_job("tooljob:node-1") is None
    assert store.get_tool_job("tooljob:session-1") is not None
    assert store.get_tool_job_outbox("tool-job:tooljob:node-1:interrupted") is None


@pytest.mark.asyncio
async def test_inflight_reuse_is_scoped_to_the_node(store):
    """同节点相同参数在飞复用；不同节点不复用（§3.1 禁止跨兄弟复用）。"""
    sink = _DeliveryRecorder()
    manager = _manager(store, sink)
    job_a, reused_a = manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:a",
        turn_id="round:node:a:1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="same-hash",
        hint="same",
        run=_job_run(),
        timeout_seconds=0.0,
    )
    _job_b, reused_b = manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:a",
        turn_id="round:node:a:1",
        tool_call_id="call-2",
        tool_name="exec",
        args_hash="same-hash",
        hint="same",
        run=_job_run(),
        timeout_seconds=0.0,
    )
    job_other, reused_other = manager.admit_tool_job(
        task_id="task:demo",
        node_id="node:b",
        turn_id="round:node:b:1",
        tool_call_id="call-3",
        tool_name="exec",
        args_hash="same-hash",
        hint="same",
        run=_job_run(),
        timeout_seconds=0.0,
    )
    assert reused_a is False
    assert reused_b is True
    assert reused_other is False
    assert job_other.job_id != job_a.job_id
    await manager.close_all(stop_reason=STOP_REASON_TASK_PAUSED, timeout_seconds=3.0)
