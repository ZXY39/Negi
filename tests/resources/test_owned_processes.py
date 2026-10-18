from __future__ import annotations

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
        __g3ku_runtime={"session_key": session_key, "actor_role": "ceo"},
    )
    payload = json.loads(raw)

    assert payload["status"] == "success"
    assert payload["background"] is True
    pid = int(payload["pid"])
    log_path = Path(payload["log_path"])
    assert log_path.exists()
    assert log_path.parent.parent == isolated_root / "owned-processes"

    entry = owned_processes.find(owned_processes.scope_key(session_key=session_key), pid)
    assert entry is not None
    assert entry["pid"] == pid

    kill_payload = json.loads(
        await ProcessKillTool().execute(
            pid=pid,
            __g3ku_runtime={"session_key": session_key, "actor_role": "ceo"},
        )
    )
    assert kill_payload["ok"] is True
    assert _wait_gone(pid)
