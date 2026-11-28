import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import g3ku.update_apply as ua


def test_interpreter_healthy_probes_child_spawn(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "prefix", "")

    monkeypatch.setattr(ua.subprocess, "run", fake_run)
    assert ua._interpreter_healthy() is True
    assert seen["argv"][0] == sys.executable
    assert seen["argv"][1] == "-c"


def test_interpreter_unhealthy_on_spawn_failure(monkeypatch):
    def fake_run(argv, **kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr(ua.subprocess, "run", fake_run)
    assert ua._interpreter_healthy() is False


def test_run_aborts_before_exit_when_interpreter_unhealthy(monkeypatch):
    records = []
    monkeypatch.setattr(ua, "_resolve_port", lambda port: 18790)
    monkeypatch.setattr(ua, "_probe_self", lambda port: True)
    monkeypatch.setattr(ua, "_interpreter_healthy", lambda: False)
    monkeypatch.setattr(ua, "_record", lambda *a, **k: records.append((a, k)))
    monkeypatch.setattr(ua, "_log", lambda msg: None)
    monkeypatch.setattr(ua, "_stage", lambda *a, **k: None)

    def refuse_exit(*a, **k):
        raise AssertionError("must not request exit with a broken interpreter")

    monkeypatch.setattr(ua, "_request_exit", refuse_exit)
    assert ua.run("v9.9.9", port=18790) == 6
    assert records and records[-1][0][0] == "interpreter_unhealthy"


def test_relaunch_reports_failure_when_endpoint_never_answers(monkeypatch):
    monkeypatch.setattr(ua, "_spawn_detached", lambda *a, **k: None)
    monkeypatch.setattr(ua, "_probe_self", lambda port: False)
    monkeypatch.setattr(ua, "RELAUNCH_VERIFY_SECONDS", 0.0)
    monkeypatch.setattr(ua.time, "sleep", lambda _seconds: None)
    logs = []
    monkeypatch.setattr(ua, "_log", logs.append)

    assert ua._relaunch_web(18790) is False
    assert any("relaunch unverified" in line and "18790" in line for line in logs)


def test_relaunch_reports_spawn_failure(monkeypatch):
    def fail_spawn(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(ua, "_spawn_detached", fail_spawn)
    logs = []
    monkeypatch.setattr(ua, "_log", logs.append)

    assert ua._relaunch_web(18790) is False
    assert any("relaunch spawn failed" in line for line in logs)


def test_relaunch_goes_through_bootstrap_supervise(monkeypatch):
    spawned = {}
    monkeypatch.setattr(ua, "_spawn_detached", lambda *a, **k: spawned.update(args=a, kwargs=k))
    monkeypatch.setattr(ua, "_probe_self", lambda port: True)
    monkeypatch.setattr(ua, "_log", lambda msg: None)
    assert ua._relaunch_web(18790) is True
    args = spawned["args"]
    assert args[0].endswith("negi_bootstrap.py")
    assert "web" in args and "--supervise" in args
    assert "--port" in args and "18790" in args
