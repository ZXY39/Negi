"""Windows 后台 broker 中转：spec 形状、pid 回执、失败道与登记接线。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from g3ku.agent.tools import shell as shell_module
from g3ku.agent.tools.shell import ExecTool
from g3ku.runtime import owned_processes


def _tool(tmp_path: Path) -> ExecTool:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ExecTool(
        working_dir=None,
        workspace_root=str(workspace),
        temp_root=str(workspace / "temp"),
        externaltools_root=str(workspace / "externaltools"),
        execution_mode_default="full_access",
    )


class _FakeBroker:
    def __init__(self, *, out: bytes = b"", err: bytes = b"", returncode: int = 0, timeout: bool = False) -> None:
        self.out = out
        self.err = err
        self.returncode = returncode
        self.timeout = timeout
        self.killed = False
        self.spec_input: bytes | None = None

    def communicate(self, input=None, timeout=None):
        self.spec_input = input
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd="broker", timeout=timeout or 0)
        return self.out, self.err

    def kill(self):
        self.killed = True
        self.timeout = False


def test_broker_spawns_target_with_breakaway_and_reports_pid(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool(tmp_path)
    captured: dict = {}
    broker = _FakeBroker(out=b'{"pid": 4242}\n')

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return broker

    monkeypatch.setattr(shell_module.subprocess, "Popen", _fake_popen)

    pid, error = tool._start_background_windows_broker(
        command="node server.js",
        cwd=str(tmp_path),
        env={"PATH": "x"},
        log_path=str(tmp_path / "p.log"),
    )

    assert pid == 4242
    assert error == ""
    argv = captured["argv"]
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-u", "-c"]
    assert argv[3] == shell_module._BACKGROUND_BROKER_SCRIPT
    spec = json.loads(broker.spec_input.decode("utf-8"))
    assert spec["creationflags"] & 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
    assert spec["creationflags"] & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    assert spec["argv"][-1] == "node server.js"
    assert spec["log_path"] == str(tmp_path / "p.log")


def test_broker_failure_returns_error_without_pid(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool(tmp_path)
    broker = _FakeBroker(out=b"", err=b"boom", returncode=1)
    monkeypatch.setattr(shell_module.subprocess, "Popen", lambda *a, **k: broker)

    pid, error = tool._start_background_windows_broker(
        command="x", cwd=str(tmp_path), env={}, log_path=str(tmp_path / "p.log")
    )

    assert pid is None
    assert "boom" in error


def test_broker_timeout_kills_broker_and_errors(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool(tmp_path)
    broker = _FakeBroker(timeout=True)
    monkeypatch.setattr(shell_module.subprocess, "Popen", lambda *a, **k: broker)

    pid, error = tool._start_background_windows_broker(
        command="x", cwd=str(tmp_path), env={}, log_path=str(tmp_path / "p.log")
    )

    assert pid is None
    assert "timed out" in error
    assert broker.killed is True


def test_start_background_registers_target_pid_and_adopts_without_handle(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _tool(tmp_path)
    monkeypatch.setattr(shell_module.os, "name", "nt")
    monkeypatch.setattr(
        tool, "_start_background_windows_broker", lambda **kwargs: (4242, "")
    )
    log_path = tmp_path / "p.log"
    monkeypatch.setattr(owned_processes, "new_log_path", lambda scope: log_path)
    registered: dict = {}

    def _fake_register(**kwargs):
        registered.update(kwargs)
        return {
            "pid": kwargs["pid"],
            "cmd": kwargs["cmd"],
            "log_path": kwargs["log_path"],
            "label": kwargs.get("label", ""),
        }

    monkeypatch.setattr(owned_processes, "register", _fake_register)
    adopted: dict = {}

    def _adopt(entry, process):
        adopted["entry"] = entry
        adopted["process"] = process

    payload = json.loads(
        tool._start_background(
            command="node server.js",
            cwd=str(tmp_path),
            env={},
            runtime={
                "session_key": "web:ceo",
                "task_id": "task:abc",
                "node_id": "node:a",
                "_adopt_background_process": _adopt,
            },
            purpose="dev server",
        )
    )

    assert payload["status"] == "success"
    assert payload["pid"] == 4242
    assert registered["pid"] == 4242
    assert adopted["process"] is None
    assert adopted["entry"]["pid"] == 4242


def test_start_background_broker_error_does_not_register(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _tool(tmp_path)
    monkeypatch.setattr(shell_module.os, "name", "nt")
    monkeypatch.setattr(
        tool, "_start_background_windows_broker", lambda **kwargs: (None, "broker exploded")
    )

    def _no_register(**kwargs):
        raise AssertionError("a failed broker start must not register an entry")

    monkeypatch.setattr(owned_processes, "register", _no_register)

    payload = json.loads(
        tool._start_background(
            command="x",
            cwd=str(tmp_path),
            env={},
            runtime={"session_key": "web:ceo"},
            purpose="probe",
        )
    )

    assert payload["status"] == "error"
    assert "broker exploded" in payload["error"]
