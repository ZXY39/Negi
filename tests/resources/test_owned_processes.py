from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from g3ku.agent.tools.process_control import ProcessKillTool
from g3ku.agent.tools.shell import ExecTool
from g3ku.deployment import data_root as data_root_module
from g3ku.runtime import owned_processes
from g3ku.runtime.tool_jobs import ToolJobRunner, build_owned_process_event_payload

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    try:
        yield tmp_path
    finally:
        data_root_module.reset_cache()


def _spawn_sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _wait_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not owned_processes.is_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_scope_key_separates_session_and_task() -> None:
    assert owned_processes.scope_key(session_key="web:ceo-1") == "session:web:ceo-1"
    assert owned_processes.scope_key(session_key="web:ceo-1", task_id="t-1") == "task:t-1"
    assert owned_processes.scope_key(session_key="") == "session:web:shared"


def test_process_kill_manifest_is_a_candidate_granted_to_ceo_and_execution() -> None:
    manifest = yaml.safe_load(
        (REPO_ROOT / "tools" / "process_kill" / "resource.yaml").read_text(encoding="utf-8")
    )

    assert manifest["name"] == "process_kill"
    # 走水合：作为候选工具暴露，不进常驻 tools[]。
    assert manifest["exposure"] == {"agent": True, "main_runtime": True}
    assert manifest["governance"]["allowed_roles"] == ["ceo", "execution"]
    assert manifest["parameters"]["required"] == ["pid"]
    assert (REPO_ROOT / "tools" / "process_kill" / "toolskills" / "SKILL.md").exists()


def test_exec_manifest_exposes_background() -> None:
    manifest = yaml.safe_load(
        (REPO_ROOT / "tools" / "exec" / "resource.yaml").read_text(encoding="utf-8")
    )

    assert "background" in manifest["parameters"]["properties"]
    assert "purpose" in manifest["parameters"]["properties"]
    assert manifest["parameters"]["required"] == ["command"]


def test_register_find_drop_roundtrip(isolated_root: Path) -> None:
    scope = owned_processes.scope_key(session_key="web:ceo-1")
    process = _spawn_sleeper()
    try:
        entry = owned_processes.register(
            scope=scope,
            pid=process.pid,
            cmd="sleep",
            log_path=str(isolated_root / "x.log"),
            session_key="web:ceo-1",
        )
        assert entry["pid"] == process.pid
        assert entry["create_time"] > 0
        assert entry["node_id"] == ""
        assert entry["owner_create_time"] > 0

        found = owned_processes.find(scope, process.pid)
        assert found is not None
        assert found["cmd"] == "sleep"

        listed = owned_processes.list_owned(scope)
        assert [item["pid"] for item in listed] == [process.pid]

        owned_processes.drop(scope, process.pid)
        assert owned_processes.find(scope, process.pid) is None
        assert owned_processes.list_owned(scope) == []
    finally:
        process.kill()
        process.wait(timeout=10)


def test_scope_isolation_rejects_foreign_pid(isolated_root: Path) -> None:
    owner_scope = owned_processes.scope_key(session_key="web:ceo-1")
    other_scope = owned_processes.scope_key(session_key="web:ceo-2")
    process = _spawn_sleeper()
    try:
        owned_processes.register(
            scope=owner_scope,
            pid=process.pid,
            cmd="sleep",
            log_path="",
            session_key="web:ceo-1",
        )
        result = owned_processes.kill_owned(other_scope, process.pid)
        assert result["ok"] is False
        assert result["reason"] == "not_owned"
        assert owned_processes.is_alive(process.pid)
    finally:
        owned_processes.drop(owner_scope, process.pid)
        process.kill()
        process.wait(timeout=10)


def test_kill_owned_terminates_tree_and_drops_entry(isolated_root: Path) -> None:
    scope = owned_processes.scope_key(session_key="web:ceo-1")
    process = _spawn_sleeper()
    owned_processes.register(
        scope=scope,
        pid=process.pid,
        cmd="sleep",
        log_path="",
        session_key="web:ceo-1",
    )

    result = owned_processes.kill_owned(scope, process.pid)

    assert result["ok"] is True
    assert result["reason"] == "terminated"
    assert _wait_gone(process.pid)
    assert owned_processes.find(scope, process.pid) is None


def test_kill_owned_refuses_entry_without_identity(
    isolated_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = owned_processes.scope_key(session_key="web:ceo-1")
    process = _spawn_sleeper()
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(owned_processes, "process_create_time", lambda pid: None)
            owned_processes.register(
                scope=scope,
                pid=process.pid,
                cmd="sleep",
                log_path="",
                session_key="web:ceo-1",
            )

        result = owned_processes.kill_owned(scope, process.pid)

        assert result["ok"] is False
        assert result["reason"] == "identity_mismatch"
        assert owned_processes.is_alive(process.pid)
    finally:
        process.kill()
        process.wait(timeout=10)


def test_reap_scope_kills_every_owned_process(isolated_root: Path) -> None:
    scope = owned_processes.scope_key(session_key="web:ceo-1")
    first = _spawn_sleeper()
    second = _spawn_sleeper()
    for process in (first, second):
        owned_processes.register(
            scope=scope,
            pid=process.pid,
            cmd="sleep",
            log_path="",
            session_key="web:ceo-1",
        )

    killed = owned_processes.reap_scope(scope)

    assert sorted(killed) == sorted([first.pid, second.pid])
    assert _wait_gone(first.pid)
    assert _wait_gone(second.pid)
    assert owned_processes.list_owned(scope) == []


def test_exec_background_requires_purpose() -> None:
    tool = ExecTool()

    missing = tool.validate_params({"command": "sleep 1", "background": True})
    assert any("purpose is required" in error for error in missing)

    too_long = tool.validate_params(
        {"command": "sleep 1", "background": True, "purpose": "x" * 121}
    )
    assert any("<=120" in error for error in too_long)

    assert tool.validate_params(
        {"command": "sleep 1", "background": True, "purpose": "dev server"}
    ) == []
    assert tool.validate_params({"command": "sleep 1"}) == []


@pytest.mark.asyncio
async def test_process_kill_is_scoped_to_the_calling_node(isolated_root: Path) -> None:
    session_key = "web:ceo-1"
    scope = owned_processes.scope_key(session_key=session_key, task_id="task:abc")
    process = _spawn_sleeper()
    try:
        owned_processes.register(
            scope=scope,
            pid=process.pid,
            cmd="sleep",
            log_path="",
            session_key=session_key,
            task_id="task:abc",
            node_id="node:a",
            label="node a service",
        )

        foreign = json.loads(
            await ProcessKillTool().execute(
                pid=process.pid,
                __g3ku_runtime={
                    "session_key": session_key,
                    "task_id": "task:abc",
                    "node_id": "node:b",
                    "actor_role": "execution",
                },
            )
        )
        assert foreign["ok"] is False
        assert foreign["status"] == "not_owned"
        assert foreign["owned_processes"] == []
        assert owned_processes.is_alive(process.pid)

        own = json.loads(
            await ProcessKillTool().execute(
                pid=process.pid,
                __g3ku_runtime={
                    "session_key": session_key,
                    "task_id": "task:abc",
                    "node_id": "node:a",
                    "actor_role": "execution",
                },
            )
        )
        assert own["ok"] is True
        assert own["purpose"] == "node a service"
        assert _wait_gone(process.pid)
    finally:
        owned_processes.drop(scope, process.pid)
        process.kill()
        process.wait(timeout=10)


def test_reap_orphans_only_touches_dead_owners(isolated_root: Path) -> None:
    orphan_scope = owned_processes.scope_key(session_key="web:old")
    live_scope = owned_processes.scope_key(session_key="web:live")
    orphan = _spawn_sleeper()
    live = _spawn_sleeper()
    try:
        owned_processes.register(
            scope=orphan_scope,
            pid=orphan.pid,
            cmd="sleep",
            log_path="",
            session_key="web:old",
            owner_pid=999_999_999,
        )
        owned_processes.register(
            scope=live_scope,
            pid=live.pid,
            cmd="sleep",
            log_path="",
            session_key="web:live",
        )

        killed = owned_processes.reap_orphans()

        assert orphan.pid in killed
        assert _wait_gone(orphan.pid)
        assert owned_processes.is_alive(live.pid)
    finally:
        owned_processes.drop(live_scope, live.pid)
        live.kill()
        live.wait(timeout=10)


@pytest.mark.asyncio
async def test_process_kill_tool_stops_owned_pid(isolated_root: Path) -> None:
    session_key = "web:ceo-1"
    scope = owned_processes.scope_key(session_key=session_key)
    process = _spawn_sleeper()
    owned_processes.register(
        scope=scope,
        pid=process.pid,
        cmd="sleep",
        log_path="",
        session_key=session_key,
    )

    payload = json.loads(
        await ProcessKillTool().execute(
            pid=process.pid,
            __g3ku_runtime={"session_key": session_key, "actor_role": "ceo"},
        )
    )

    assert payload["ok"] is True
    assert payload["status"] == "terminated"
    assert _wait_gone(process.pid)


@pytest.mark.asyncio
async def test_process_kill_tool_rejects_foreign_pid_and_lists_owned(isolated_root: Path) -> None:
    session_key = "web:ceo-1"
    scope = owned_processes.scope_key(session_key=session_key)
    process = _spawn_sleeper()
    try:
        owned_processes.register(
            scope=scope,
            pid=process.pid,
            cmd="sleep",
            log_path="",
            session_key=session_key,
        )

        payload = json.loads(
            await ProcessKillTool().execute(
                pid=process.pid,
                __g3ku_runtime={"session_key": "web:ceo-2", "actor_role": "ceo"},
            )
        )

        assert payload["ok"] is False
        assert payload["status"] == "not_owned"
        assert payload["owned_processes"] == []
        assert owned_processes.is_alive(process.pid)
    finally:
        owned_processes.drop(scope, process.pid)
        process.kill()
        process.wait(timeout=10)


@pytest.mark.asyncio
async def test_exec_background_registers_pid_and_log(isolated_root: Path) -> None:
    session_key = "web:ceo-1"
    workspace = isolated_root / "workspace"
    workspace.mkdir()
    tool = ExecTool(
        working_dir=None,
        workspace_root=str(workspace),
        temp_root=str(workspace / "temp"),
        externaltools_root=str(workspace / "externaltools"),
        execution_mode_default="full_access",
    )

    raw = await tool.execute(
        command=f'"{sys.executable}" -c "import time; time.sleep(60)"',
        working_dir=str(workspace),
        background=True,
        purpose="probe sleeper for the owned-process test",
        __g3ku_runtime={"session_key": session_key, "actor_role": "ceo", "node_id": "node:a"},
    )
    payload = json.loads(raw)

    assert payload["status"] == "success"
    assert payload["background"] is True
    assert payload["purpose"] == "probe sleeper for the owned-process test"
    pid = int(payload["pid"])
    log_path = Path(payload["log_path"])
    assert log_path.exists()
    assert log_path.parent.parent == isolated_root / "owned-processes"

    entry = owned_processes.find(owned_processes.scope_key(session_key=session_key), pid)
    assert entry is not None
    assert entry["pid"] == pid
    assert entry["node_id"] == "node:a"
    assert entry["label"] == "probe sleeper for the owned-process test"

    kill_payload = json.loads(
        await ProcessKillTool().execute(
            pid=pid,
            __g3ku_runtime={"session_key": session_key, "actor_role": "ceo"},
        )
    )
    assert kill_payload["ok"] is True
    assert _wait_gone(pid)


class _RecordingHeartbeat:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def enqueue_tool_job(self, payload: dict | None) -> bool:
        self.payloads.append(dict(payload or {}))
        return True


def _runner(heartbeat: _RecordingHeartbeat, session_key: str = "web:ceo-1") -> ToolJobRunner:
    return ToolJobRunner(
        session_key=session_key,
        store_getter=lambda: None,
        heartbeat_getter=lambda: heartbeat,
    )


def _spawn_finished(log_path: Path, code: str) -> subprocess.Popen:
    with open(log_path, "ab", buffering=0) as handle:
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdout=handle, stderr=subprocess.STDOUT
        )
    process.wait(timeout=30)
    return process


async def _wait_for_watchers(runner: ToolJobRunner, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while runner._owned_process_tasks and time.time() < deadline:
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_owned_process_that_exits_on_its_own_reports_its_output(
    isolated_root: Path,
) -> None:
    session_key = "web:ceo-1"
    scope = owned_processes.scope_key(session_key=session_key)
    log_path = owned_processes.new_log_path(scope)
    process = _spawn_finished(log_path, "print('captured probe output')")
    entry = owned_processes.register(
        scope=scope,
        pid=int(process.pid),
        cmd="python probe.py",
        log_path=str(log_path),
        session_key=session_key,
        actor_role="ceo",
    )
    heartbeat = _RecordingHeartbeat()
    runner = _runner(heartbeat)

    runner.adopt_owned_process(entry=entry, process=process)
    await _wait_for_watchers(runner)

    assert [payload["status"] for payload in heartbeat.payloads] == ["success"]
    payload = heartbeat.payloads[0]
    assert payload["kind"] == "process"
    assert payload["job_id"] == f"proc:{process.pid}"
    assert payload["exit_code"] == 0
    assert "captured probe output" in payload["result_preview"]
    assert payload["log_path"] == str(log_path)
    assert payload["session_id"] == session_key
    # 报过之后条目摘掉：后台气泡里的行与事件只能各出现一次。
    assert owned_processes.find(scope, int(process.pid)) is None


@pytest.mark.asyncio
async def test_owned_process_nonzero_exit_reports_an_error(isolated_root: Path) -> None:
    session_key = "web:ceo-1"
    scope = owned_processes.scope_key(session_key=session_key)
    log_path = owned_processes.new_log_path(scope)
    process = _spawn_finished(log_path, "import sys; sys.exit(3)")
    entry = owned_processes.register(
        scope=scope,
        pid=int(process.pid),
        cmd="python probe.py",
        log_path=str(log_path),
        session_key=session_key,
        actor_role="ceo",
    )
    heartbeat = _RecordingHeartbeat()
    runner = _runner(heartbeat)

    runner.adopt_owned_process(entry=entry, process=process)
    await _wait_for_watchers(runner)

    assert heartbeat.payloads[0]["status"] == "error"
    assert heartbeat.payloads[0]["exit_code"] == 3


@pytest.mark.asyncio
async def test_owned_process_exit_stays_silent_once_another_lane_collected_it(
    isolated_root: Path,
) -> None:
    session_key = "web:ceo-1"
    scope = owned_processes.scope_key(session_key=session_key)
    log_path = owned_processes.new_log_path(scope)
    process = _spawn_finished(log_path, "print('already handed over')")
    entry = owned_processes.register(
        scope=scope,
        pid=int(process.pid),
        cmd="python probe.py",
        log_path=str(log_path),
        session_key=session_key,
        actor_role="ceo",
    )
    # 后台气泡的停止、process_kill、scope 回收都会先摘走条目并各自交代结果。
    owned_processes.drop(scope, int(process.pid))
    heartbeat = _RecordingHeartbeat()
    runner = _runner(heartbeat)

    runner.adopt_owned_process(entry=entry, process=process)
    await _wait_for_watchers(runner)

    assert heartbeat.payloads == []


def test_owned_process_event_identity_survives_pid_reuse() -> None:
    base = {
        "pid": 4242,
        "cmd": "python probe.py",
        "log_path": "probe.log",
        "session_key": "web:ceo-1",
        "started_at": "2026-11-05T20:00:13",
    }
    first = build_owned_process_event_payload(
        {**base, "create_time": 1000.0},
        session_id="web:ceo-1",
        finished_at="2026-11-05T20:02:00",
        status="success",
        exit_code=0,
    )
    second = build_owned_process_event_payload(
        {**base, "create_time": 2000.0},
        session_id="web:ceo-1",
        finished_at="2026-11-05T20:05:00",
        status="success",
        exit_code=0,
    )

    assert first["job_id"] == "proc:4242"
    assert first["dedupe_key"] != second["dedupe_key"]
    assert first["stopped_by"] == ""
    assert first["exit_code"] == 0


@pytest.mark.asyncio
async def test_exec_background_hands_the_process_to_the_adopting_lane(
    isolated_root: Path,
) -> None:
    session_key = "web:ceo-2"
    workspace = isolated_root / "workspace"
    workspace.mkdir()
    captured: dict = {}

    def adopt(*, entry: dict, process: object) -> None:
        captured["entry"] = entry
        captured["process"] = process

    tool = ExecTool(
        working_dir=None,
        workspace_root=str(workspace),
        temp_root=str(workspace / "temp"),
        externaltools_root=str(workspace / "externaltools"),
        execution_mode_default="full_access",
    )

    raw = await tool.execute(
        command=f'"{sys.executable}" -c "import time; time.sleep(60)"',
        working_dir=str(workspace),
        background=True,
        purpose="adoption probe",
        __g3ku_runtime={
            "session_key": session_key,
            "actor_role": "ceo",
            "_adopt_background_process": adopt,
        },
    )
    payload = json.loads(raw)
    pid = int(payload["pid"])
    process = captured.get("process")

    assert callable(getattr(process, "poll", None))
    assert captured["entry"]["pid"] == pid
    assert captured["entry"]["log_path"] == payload["log_path"]
    assert captured["entry"]["scope"] == owned_processes.scope_key(session_key=session_key)

    process.kill()
    process.wait(timeout=20)
