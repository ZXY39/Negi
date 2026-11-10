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
    service.store = SimpleNamespace(get_task=lambda task_id: task, get_node=lambda node_id: node)
    service.deliver_node_message = _Recorder(fail=fail)
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
