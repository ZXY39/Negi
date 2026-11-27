from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from g3ku.deployment import data_root as data_root_module
from g3ku.heartbeat.owned_process_scanner import SessionOwnedProcessScanner
from g3ku.runtime import owned_processes


@pytest.fixture()
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    try:
        yield tmp_path
    finally:
        data_root_module.reset_cache()


class _RecordingHeartbeat:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def enqueue_tool_job(self, payload: dict | None) -> bool:
        self.payloads.append(dict(payload or {}))
        return True


def _spawn(log_path: Path, code: str, *, wait: bool) -> subprocess.Popen:
    with open(log_path, "ab", buffering=0) as handle:
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdout=handle, stderr=subprocess.STDOUT
        )
    if wait:
        process.wait(timeout=30)
    return process


def _register(process: subprocess.Popen, *, session_key: str = "web:ceo-1", task_id: str = "") -> dict:
    scope = owned_processes.scope_key(session_key=session_key, task_id=task_id)
    return owned_processes.register(
        scope=scope,
        pid=int(process.pid),
        cmd="python probe.py",
        log_path="",
        session_key=session_key,
        task_id=task_id,
        node_id="node:a" if task_id else "",
        actor_role="ceo",
    )


@pytest.mark.asyncio
async def test_scanner_reports_dead_session_entry(isolated_root: Path) -> None:
    process = _spawn(isolated_root / "probe.log", "print('done')", wait=True)
    _register(process)
    scope = owned_processes.scope_key(session_key="web:ceo-1")
    heartbeat = _RecordingHeartbeat()
    scanner = SessionOwnedProcessScanner(heartbeat=heartbeat)

    reported = await scanner.scan_once()

    assert reported == 1
    assert len(heartbeat.payloads) == 1
    payload = heartbeat.payloads[0]
    assert payload["kind"] == "process"
    assert payload["status"] == "interrupted"
    assert payload["pid"] == int(process.pid)
    assert payload["exit_code"] is None
    assert "no longer running" in payload["result_preview"]
    assert owned_processes.find(scope, int(process.pid)) is None


@pytest.mark.asyncio
async def test_scanner_leaves_alive_session_entry_alone(isolated_root: Path) -> None:
    process = _spawn(isolated_root / "probe.log", "import time; time.sleep(30)", wait=False)
    try:
        _register(process)
        scope = owned_processes.scope_key(session_key="web:ceo-1")
        heartbeat = _RecordingHeartbeat()
        scanner = SessionOwnedProcessScanner(heartbeat=heartbeat)

        reported = await scanner.scan_once()

        assert reported == 0
        assert heartbeat.payloads == []
        assert owned_processes.find(scope, int(process.pid)) is not None
    finally:
        process.kill()
        process.wait(timeout=20)


@pytest.mark.asyncio
async def test_scanner_ignores_node_entries(isolated_root: Path) -> None:
    process = _spawn(isolated_root / "probe.log", "print('done')", wait=True)
    _register(process, task_id="task:t1")
    scope = owned_processes.scope_key(session_key="web:ceo-1", task_id="task:t1")
    heartbeat = _RecordingHeartbeat()
    scanner = SessionOwnedProcessScanner(heartbeat=heartbeat)

    reported = await scanner.scan_once()

    assert reported == 0
    assert heartbeat.payloads == []
    assert owned_processes.find(scope, int(process.pid)) is not None


@pytest.mark.asyncio
async def test_scanner_skips_entry_collected_by_another_lane(isolated_root: Path) -> None:
    process = _spawn(isolated_root / "probe.log", "print('done')", wait=True)
    _register(process)
    scope = owned_processes.scope_key(session_key="web:ceo-1")
    owned_processes.drop(scope, int(process.pid))
    heartbeat = _RecordingHeartbeat()
    scanner = SessionOwnedProcessScanner(heartbeat=heartbeat)

    reported = await scanner.scan_once()

    assert reported == 0
    assert heartbeat.payloads == []
