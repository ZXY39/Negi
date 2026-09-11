from __future__ import annotations

import os
import sys
import types

from g3ku.web import launcher


def test_web_runtime_cmdline_matches() -> None:
    assert launcher._web_runtime_cmdline_matches(["python.exe", "-m", "g3ku", "web"])
    assert launcher._web_runtime_cmdline_matches(["python", "-m", "g3ku", "web", "--port", "18790"])
    assert launcher._web_runtime_cmdline_matches(
        ["python", "-u", "-c", "from g3ku.web.launcher import run_web_server_entrypoint; run_web_server_entrypoint()"]
    )
    assert not launcher._web_runtime_cmdline_matches(["python", "-m", "g3ku", "worker"])
    assert not launcher._web_runtime_cmdline_matches(["python", "g3ku_bootstrap.py", "web"])
    assert not launcher._web_runtime_cmdline_matches(["python", "-m", "pytest", "tests"])
    assert not launcher._web_runtime_cmdline_matches(["python", "-m", "g3ku"])
    assert not launcher._web_runtime_cmdline_matches([])


def test_terminate_stale_web_runtime_processes_kills_only_stale_same_workspace(monkeypatch) -> None:
    root = launcher.PROJECT_ROOT
    venv_python = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    exe = str(venv_python)
    kill_calls: list[int] = []

    class FakeProc:
        def __init__(self, pid: int, exe_: str, cmdline: list[str], cwd: str) -> None:
            self.info = {"pid": pid, "exe": exe_, "cmdline": cmdline}
            self.pid = pid
            self._cwd = cwd

        def cwd(self) -> str:
            return self._cwd

        def kill(self) -> None:
            kill_calls.append(int(self.pid))

    web_cmdline = [exe, "-m", "g3ku", "web"]
    dev_cmdline = [
        "/uv/python.exe",
        "-c",
        "from g3ku.web.launcher import run_web_server_entrypoint; run_web_server_entrypoint()",
    ]
    procs = [
        FakeProc(4242, exe, web_cmdline, str(root)),
        FakeProc(4343, "/uv/python.exe", dev_cmdline, str(root)),
        FakeProc(os.getpid(), exe, web_cmdline, str(root)),
        FakeProc(7777, "/other/.venv/bin/python", web_cmdline, "/other/workspace"),
        FakeProc(7878, "/uv/python.exe", dev_cmdline, "/other/workspace"),
        FakeProc(8888, exe, [exe, "-m", "g3ku", "worker"], str(root)),
    ]

    fake = types.ModuleType("psutil")
    fake.process_iter = lambda attrs=None: iter(procs)
    fake.wait_procs = lambda victims, timeout=None: (list(victims), [])
    monkeypatch.setitem(sys.modules, "psutil", fake)

    assert launcher._terminate_stale_web_runtime_processes() == [4242, 4343]
    assert kill_calls == [4242, 4343]


def test_terminate_stale_skips_ancestor_processes(monkeypatch) -> None:
    root = launcher.PROJECT_ROOT
    venv_python = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    exe = str(venv_python)
    kill_calls: list[int] = []
    own_pid = os.getpid()
    parent_pid = 3131

    class FakeProc:
        def __init__(self, pid: int, exe_: str, cmdline: list[str], cwd: str) -> None:
            self.info = {"pid": pid, "exe": exe_, "cmdline": cmdline}
            self.pid = pid
            self._cwd = cwd

        def cwd(self) -> str:
            return self._cwd

        def kill(self) -> None:
            kill_calls.append(int(self.pid))

    class FakeChain:
        def __init__(self, pid: int, parent: "FakeChain | None") -> None:
            self.pid = pid
            self._parent = parent

        def parent(self) -> "FakeChain | None":
            return self._parent

    web_cmdline = [exe, "-m", "g3ku", "web"]
    procs = [
        # the uv-venv redirector parent of the current `-m g3ku web` process:
        # same exe/cmdline/cwd as a stale server, but must never be killed
        FakeProc(parent_pid, exe, web_cmdline, str(root)),
        FakeProc(4242, exe, web_cmdline, str(root)),
    ]

    fake = types.ModuleType("psutil")
    fake.process_iter = lambda attrs=None: iter(procs)
    fake.wait_procs = lambda victims, timeout=None: (list(victims), [])
    fake.Process = lambda pid: FakeChain(own_pid, FakeChain(parent_pid, None)) if pid == own_pid else FakeChain(pid, None)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    assert launcher._terminate_stale_web_runtime_processes() == [4242]
    assert kill_calls == [4242]
