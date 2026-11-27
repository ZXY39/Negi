"""节点进程退出：process_exit 消息、always-waking、watcher 与操作员停止。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.deployment import data_root as data_root_module
from g3ku.runtime import owned_processes
from main.runtime.node_message_kind import (
    ALWAYS_WAKING_KINDS,
    KIND_PROCESS_EXIT,
    ORIGIN_NODE,
    SYSTEM_KINDS,
    classify,
)
from main.runtime.node_message_text import message_label
from main.service import runtime_service as runtime_service_module
from main.service.runtime_service import MainRuntimeService


class _Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def __call__(self, **kwargs):
        if self.fail:
            raise RuntimeError("mailbox unavailable")
        self.calls.append(dict(kwargs))
        return {"status": "delivered"}


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    try:
        yield tmp_path
    finally:
        data_root_module.reset_cache()


class _ExitedProcess:
    def __init__(self, code: int = 7) -> None:
        self.code = code

    def poll(self):
        return self.code


def _entry(tmp_path, **overrides) -> dict:
    entry = {
        "pid": 123,
        "create_time": 1794309779.64,
        "scope": "task:task:abc",
        "task_id": "task:abc",
        "node_id": "node:a",
        "session_key": "web:proc",
        "label": "dev server :3000",
        "cmd": "node server.js",
        "log_path": str(tmp_path / "process.log"),
        "started_at": "2026-11-10T19:22:59",
    }
    entry.update(overrides)
    return entry


def _service(*, node_status: str = "in_progress", paused: bool = False, fail: bool = False) -> MainRuntimeService:
    service = object.__new__(MainRuntimeService)
    service._node_owned_process_tasks = set()
    task = SimpleNamespace(
        task_id="task:abc",
        status="in_progress",
        is_paused=False,
        pause_requested=False,
    )
    node = SimpleNamespace(
        node_id="node:a",
        goal="渲染 ep03",
        status=node_status,
        is_paused=paused,
        pause_requested=False,
    )
    outbox: list[dict] = []
    service.store = SimpleNamespace(
        get_task=lambda task_id: task,
        get_node=lambda node_id: node,
        put_tool_job_outbox=lambda **kwargs: outbox.append(kwargs),
    )
    service.deliver_node_message = _Recorder(fail=fail)
    service._test_outbox = outbox
    return service


def test_process_exit_is_a_system_kind_and_always_waking() -> None:
    assert KIND_PROCESS_EXIT in SYSTEM_KINDS
    assert KIND_PROCESS_EXIT in ALWAYS_WAKING_KINDS
    assert classify(origin=ORIGIN_NODE, system_kind=KIND_PROCESS_EXIT) == (
        KIND_PROCESS_EXIT,
        "high",
    )
    assert message_label(KIND_PROCESS_EXIT) == "来自系统"


@pytest.mark.asyncio
async def test_watcher_delivers_process_exit_to_the_owning_node(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path)
    (tmp_path / "process.log").write_text("last line of output", encoding="utf-8")
    service = _service()
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: entry)
    dropped: list[tuple[str, int]] = []
    monkeypatch.setattr(
        owned_processes, "drop", lambda scope, pid: dropped.append((scope, pid))
    )

    service.adopt_node_owned_process(entry=entry, process=_ExitedProcess(7))
    await asyncio.gather(*list(service._node_owned_process_tasks))

    assert len(service.deliver_node_message.calls) == 1
    call = service.deliver_node_message.calls[0]
    assert call["task_id"] == "task:abc"
    assert call["target_node_id"] == "node:a"
    assert call["system_kind"] == KIND_PROCESS_EXIT
    assert call["origin"] == ORIGIN_NODE
    assert call["source_node_id"] == "node:a"
    assert call["wake"] is True
    assert "pid 123" in call["message"]
    assert "dev server :3000" in call["message"]
    assert "Exit code: 7." in call["message"]
    assert "node server.js" in call["message"]
    assert str(tmp_path / "process.log") in call["message"]
    assert "last line of output" in call["message"]
    assert dropped == [("task:task:abc", 123)]


@pytest.mark.asyncio
async def test_watcher_keeps_a_paused_node_from_waking(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path)
    service = _service(paused=True)
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: entry)
    monkeypatch.setattr(owned_processes, "drop", lambda scope, pid: None)

    service.adopt_node_owned_process(entry=entry, process=_ExitedProcess(0))
    await asyncio.gather(*list(service._node_owned_process_tasks))

    assert service.deliver_node_message.calls[0]["wake"] is False


@pytest.mark.asyncio
async def test_watcher_stays_quiet_when_the_entry_was_claimed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path)
    service = _service()
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: None)
    monkeypatch.setattr(owned_processes, "drop", lambda scope, pid: None)

    service.adopt_node_owned_process(entry=entry, process=_ExitedProcess(0))
    await asyncio.gather(*list(service._node_owned_process_tasks))

    assert service.deliver_node_message.calls == []


@pytest.mark.asyncio
async def test_watcher_drops_without_delivery_when_the_node_is_terminal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path)
    service = _service(node_status="success")
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: entry)
    dropped: list[tuple[str, int]] = []
    monkeypatch.setattr(
        owned_processes, "drop", lambda scope, pid: dropped.append((scope, pid))
    )

    service.adopt_node_owned_process(entry=entry, process=_ExitedProcess(0))
    await asyncio.gather(*list(service._node_owned_process_tasks))

    assert service.deliver_node_message.calls == []
    assert dropped == [("task:task:abc", 123)]


@pytest.mark.asyncio
async def test_delivery_failure_marks_a_notice_for_the_startup_consumer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path)
    service = _service(fail=True)
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: entry)
    monkeypatch.setattr(owned_processes, "drop", lambda scope, pid: None)
    monkeypatch.setattr(
        runtime_service_module,
        "_NODE_OWNED_PROCESS_EXIT_DELIVERY_BACKOFF_SECONDS",
        0,
    )
    notices: list[dict] = []
    monkeypatch.setattr(
        owned_processes,
        "mark_notice",
        lambda scope, pid, **kwargs: notices.append({"scope": scope, "pid": pid, **kwargs}),
    )

    service.adopt_node_owned_process(entry=entry, process=_ExitedProcess(3))
    await asyncio.gather(*list(service._node_owned_process_tasks))

    assert notices
    assert notices[0]["exit_reason"] == "process_exited"
    assert notices[0]["exit_code"] == 3


@pytest.mark.asyncio
async def test_handleless_watcher_reports_gone_without_exit_code(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows broker 车道 / 重启认领的进程没有句柄：存活轮询发现消失，报 process_gone。"""
    entry = _entry(tmp_path)
    service = _service()
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: entry)
    monkeypatch.setattr(owned_processes, "is_alive", lambda pid: False)
    monkeypatch.setattr(owned_processes, "drop", lambda scope, pid: None)

    service.adopt_node_owned_process(entry=entry, process=None)
    await asyncio.gather(*list(service._node_owned_process_tasks))

    assert len(service.deliver_node_message.calls) == 1
    call = service.deliver_node_message.calls[0]
    assert call["system_kind"] == KIND_PROCESS_EXIT
    assert call["wake"] is True
    assert "no longer running" in call["message"]
    assert "Exit code" not in call["message"]


@pytest.mark.asyncio
async def test_handleless_watcher_polls_until_the_process_disappears(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path)
    service = _service()
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: entry)
    monkeypatch.setattr(owned_processes, "identity_matches", lambda item: True)
    monkeypatch.setattr(owned_processes, "drop", lambda scope, pid: None)
    monkeypatch.setattr(
        runtime_service_module, "_NODE_OWNED_PROCESS_EXIT_POLL_SECONDS", 0
    )
    polls = {"n": 0}

    def _alive(pid):
        polls["n"] += 1
        return polls["n"] < 3

    monkeypatch.setattr(owned_processes, "is_alive", _alive)

    service.adopt_node_owned_process(entry=entry, process=None)
    await asyncio.gather(*list(service._node_owned_process_tasks))

    assert polls["n"] >= 3
    assert len(service.deliver_node_message.calls) == 1


@pytest.mark.asyncio
async def test_handleless_watcher_treats_pid_reuse_as_gone(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path)
    service = _service()
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: entry)
    monkeypatch.setattr(owned_processes, "is_alive", lambda pid: True)
    monkeypatch.setattr(owned_processes, "identity_matches", lambda item: False)
    monkeypatch.setattr(owned_processes, "drop", lambda scope, pid: None)

    service.adopt_node_owned_process(entry=entry, process=None)
    await asyncio.gather(*list(service._node_owned_process_tasks))

    assert len(service.deliver_node_message.calls) == 1
    assert "no longer running" in service.deliver_node_message.calls[0]["message"]


@pytest.mark.asyncio
async def test_startup_consumer_delivers_survived_notice_and_keeps_entry(
    isolated_root, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = "task:task:abc"
    # survived 凭证由认领道落：条目保留，进程还活着。
    owned_processes.register(
        scope=scope,
        pid=123,
        cmd="node server.js",
        log_path=str(tmp_path / "process.log"),
        session_key="web:proc",
        task_id="task:abc",
        node_id="node:a",
        owner_pid=888_888_888,
        label="dev server :3000",
    )
    assert owned_processes.mark_notice(
        scope, 123, exit_reason="survived_restart", keep_entry=True
    )
    service = _service()

    await service._consume_owned_process_notices()

    assert len(service.deliver_node_message.calls) == 1
    call = service.deliver_node_message.calls[0]
    assert call["system_kind"] == KIND_PROCESS_EXIT
    assert call["wake"] is True
    assert "survived the project restart" in call["message"]
    assert "still running" in call["message"]
    assert owned_processes.find(scope, 123) is not None
    assert owned_processes.list_notices() == []


@pytest.mark.asyncio
async def test_startup_consumer_enqueues_session_survived_event(
    isolated_root, tmp_path
) -> None:
    scope = "session:web:proc"
    owned_processes.register(
        scope=scope,
        pid=456,
        cmd="node server.js",
        log_path=str(tmp_path / "process.log"),
        session_key="web:proc",
        owner_pid=999_999_999,
        label="dev server :3000",
    )
    assert owned_processes.mark_notice(
        scope, 456, exit_reason="survived_restart", keep_entry=True
    )

    class _OutboxRecorder:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def put_tool_job_outbox(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {"dedupe_key": kwargs.get("dedupe_key")}

    service = object.__new__(MainRuntimeService)
    service.store = _OutboxRecorder()
    service._node_owned_process_tasks = set()

    await service._consume_owned_process_notices()

    assert len(service.store.calls) == 1
    payload = service.store.calls[0]["payload"]
    assert payload["kind"] == "process"
    assert payload["status"] == "survived_restart"
    assert ":g" in payload["dedupe_key"]
    assert owned_processes.find(scope, 456) is not None
    assert owned_processes.list_notices() == []


@pytest.mark.asyncio
async def test_readopt_pass_adopts_live_process_and_marks_survived(
    isolated_root, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    scope = owned_processes.scope_key(session_key="", task_id="task:abc")
    process = _SleeperProcess()
    try:
        owned_processes.register(
            scope=scope,
            pid=process.pid,
            cmd="sleeper",
            log_path="",
            session_key="",
            task_id="task:abc",
            node_id="node:a",
            owner_pid=999_999_999,
        )
        service = _service()

        await service._readopt_owned_background_processes()

        entry = owned_processes.find(scope, process.pid)
        assert entry is not None
        assert entry["owner_pid"] == os.getpid()
        assert len(service._node_owned_process_tasks) == 1
        notices = owned_processes.list_notices()
        assert [(n["pid"], n["exit_reason"]) for n in notices] == [
            (process.pid, "survived_restart")
        ]
        for notice in notices:
            owned_processes.drop_notice(notice["_notice_path"])
    finally:
        for task in list(service._node_owned_process_tasks):
            task.cancel()
        owned_processes.drop(scope, process.pid)
        process.kill()
        process.wait(timeout=10)


@pytest.mark.asyncio
async def test_readopt_pass_reaps_processes_of_terminal_tasks(
    isolated_root, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = owned_processes.scope_key(session_key="", task_id="task:abc")
    process = _SleeperProcess()
    try:
        owned_processes.register(
            scope=scope,
            pid=process.pid,
            cmd="sleeper",
            log_path="",
            session_key="",
            task_id="task:abc",
            node_id="node:a",
            owner_pid=999_999_999,
        )
        service = _service(node_status="success")

        await service._readopt_owned_background_processes()

        deadline = asyncio.get_running_loop().time() + 15
        while owned_processes.is_alive(process.pid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        assert not owned_processes.is_alive(process.pid)
        assert owned_processes.find(scope, process.pid) is None
        assert owned_processes.list_notices() == []
    finally:
        if owned_processes.is_alive(process.pid):
            process.kill()
        process.wait(timeout=10)


class _SleeperProcess:
    """真实 sleeper 子进程：认领/终态守卫测试要验真杀真活。"""

    def __init__(self) -> None:
        import subprocess
        import sys

        self._proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid = self._proc.pid

    def kill(self) -> None:
        self._proc.kill()

    def wait(self, timeout: float | None = None):
        return self._proc.wait(timeout=timeout)


def test_list_task_owned_processes_maps_purpose_and_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(MainRuntimeService)
    service.store = SimpleNamespace(
        get_node=lambda node_id: SimpleNamespace(goal="渲染 ep03", status="in_progress")
    )
    entries = [
        {
            "pid": 2,
            "label": "后启动",
            "cmd": "node b.js",
            "started_at": "2026-11-10T19:23:00",
            "log_path": "C:/log/b.log",
            "node_id": "node:b",
        },
        {
            "pid": 1,
            "label": "早启动",
            "cmd": "node a.js",
            "started_at": "2026-11-10T19:22:59",
            "log_path": "C:/log/a.log",
            "node_id": "node:a",
        },
    ]
    monkeypatch.setattr(owned_processes, "list_owned", lambda scope: list(entries))

    items = service.list_task_owned_processes("task:abc")

    assert [item["pid"] for item in items] == [2, 1]
    assert items[0]["purpose"] == "后启动"
    assert items[0]["node_goal"] == "渲染 ep03"
    assert items[0]["node_status"] == "in_progress"


@pytest.mark.asyncio
async def test_operator_stop_notifies_the_node_as_a_process_exit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path)
    service = _service()
    monkeypatch.setattr(owned_processes, "find", lambda scope, pid: entry)
    monkeypatch.setattr(
        owned_processes,
        "kill_owned",
        lambda scope, pid: {"ok": True, "pid": pid, "reason": "terminated", "entry": entry},
    )
    monkeypatch.setattr(owned_processes, "drop", lambda scope, pid: None)

    result = await service.stop_task_owned_process("task:abc", 123)

    assert result["ok"] is True
    assert result["status"] == "terminated"
    call = service.deliver_node_message.calls[0]
    assert call["system_kind"] == KIND_PROCESS_EXIT
    assert call["wake"] is True
    assert "stopped by the operator" in call["message"]
    assert "dev server :3000" in call["message"]


def _register_notice(tmp_path, *, scope: str, pid: int, session_key: str, **overrides) -> None:
    owned_processes.register(
        scope=scope,
        pid=pid,
        cmd=str(overrides.pop("cmd", "node server.js")),
        log_path=str(overrides.pop("log_path", tmp_path / "process.log")),
        session_key=session_key,
        task_id=str(overrides.pop("task_id", "")),
        node_id=str(overrides.pop("node_id", "")),
        owner_pid=int(overrides.pop("owner_pid", 999_999_999)),
        label=str(overrides.pop("label", "dev server :3000")),
    )
    assert owned_processes.mark_notice(scope, pid, exit_reason="process_gone")


@pytest.mark.asyncio
async def test_startup_consumer_delivers_node_notice_and_drops_it(
    isolated_root, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = "task:task:abc"
    _register_notice(tmp_path, scope=scope, pid=123, session_key="web:proc", task_id="task:abc", node_id="node:a")
    service = _service()

    await service._consume_owned_process_notices()

    assert len(service.deliver_node_message.calls) == 1
    call = service.deliver_node_message.calls[0]
    assert call["system_kind"] == KIND_PROCESS_EXIT
    assert "pid 123" in call["message"]
    assert "no longer running" in call["message"]
    assert owned_processes.list_notices() == []


@pytest.mark.asyncio
async def test_startup_consumer_enqueues_a_session_heartbeat_event(
    isolated_root, tmp_path
) -> None:
    scope = "session:web:proc"
    _register_notice(tmp_path, scope=scope, pid=456, session_key="web:proc")

    class _OutboxRecorder:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def put_tool_job_outbox(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {"dedupe_key": kwargs.get("dedupe_key")}

    service = object.__new__(MainRuntimeService)
    service.store = _OutboxRecorder()
    service._node_owned_process_tasks = set()

    await service._consume_owned_process_notices()

    assert len(service.store.calls) == 1
    payload = service.store.calls[0]["payload"]
    assert payload["kind"] == "process"
    assert payload["status"] == "interrupted"
    assert payload["purpose"] == "dev server :3000"
    assert service.store.calls[0]["session_id"] == "web:proc"
    assert owned_processes.list_notices() == []


@pytest.mark.asyncio
async def test_startup_consumer_keeps_a_notice_when_delivery_fails(
    isolated_root, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = "task:task:abc"
    _register_notice(tmp_path, scope=scope, pid=123, session_key="web:proc", task_id="task:abc", node_id="node:a")
    service = _service(fail=True)
    monkeypatch.setattr(
        runtime_service_module,
        "_NODE_OWNED_PROCESS_EXIT_DELIVERY_BACKOFF_SECONDS",
        0,
    )
    monkeypatch.setattr(owned_processes, "mark_notice", lambda *args, **kwargs: False)

    await service._consume_owned_process_notices()

    assert len(owned_processes.list_notices()) == 1


@pytest.mark.asyncio
async def test_startup_consumer_drops_unattributed_legacy_task_notices(
    isolated_root, tmp_path
) -> None:
    scope = "task:task:abc"
    _register_notice(tmp_path, scope=scope, pid=123, session_key="web:proc", task_id="task:abc")

    def _no_outbox(**kwargs):
        raise AssertionError("legacy task notices must not become session heartbeat events")

    service = object.__new__(MainRuntimeService)
    service.store = SimpleNamespace(put_tool_job_outbox=_no_outbox)
    service._node_owned_process_tasks = set()
    service.deliver_node_message = _Recorder()

    await service._consume_owned_process_notices()

    assert service.deliver_node_message.calls == []
    assert owned_processes.list_notices() == []


def _swept_entry(**overrides) -> dict:
    entry = {
        "swept": True,
        "sweep_id": "abc123",
        "pid": 0,
        "create_time": 0.0,
        "scope": "session:web:ceo-1",
        "session_key": "web:ceo-1",
        "task_id": "",
        "node_id": "",
        "cmd": "python watcher.py",
        "started_at": "2026-11-27T10:00:00",
        "exit_reason": "swept_by_exec_cancel",
        "swept_pids": [
            {"pid": 111, "name": "python.exe"},
            {"pid": 222, "name": "msedge.exe"},
        ],
    }
    entry.update(overrides)
    return entry


@pytest.mark.asyncio
async def test_swept_notice_routes_to_session_outbox() -> None:
    service = _service()

    consumed = await service._consume_owned_process_notice(_swept_entry())

    assert consumed is True
    outbox = service._test_outbox
    assert len(outbox) == 1
    assert outbox[0]["session_id"] == "web:ceo-1"
    payload = outbox[0]["payload"]
    assert payload["kind"] == "process"
    assert payload["status"] == "interrupted"
    assert payload["job_id"] == "proc:swept:abc123"
    assert payload["dedupe_key"] == "tool-job:proc:swept:abc123:interrupted"
    assert "pid=111" in payload["result_preview"]
    assert payload["cmd"] == "python watcher.py"


@pytest.mark.asyncio
async def test_swept_notice_routes_to_node_mailbox() -> None:
    service = _service()
    entry = _swept_entry(task_id="task:abc", node_id="node:a", scope="task:task:abc")

    consumed = await service._consume_owned_process_notice(entry)

    assert consumed is True
    assert len(service.deliver_node_message.calls) == 1
    call = service.deliver_node_message.calls[0]
    assert call["system_kind"] == KIND_PROCESS_EXIT
    assert call["wake"] is True
    assert "Swept: pid=111 (python.exe), pid=222 (msedge.exe)." in call["message"]
    assert "python watcher.py" in call["message"]
