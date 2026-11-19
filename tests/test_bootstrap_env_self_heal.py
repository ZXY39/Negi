import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import negi_bootstrap as nb  # noqa: E402


def test_read_python_pin(tmp_path, monkeypatch):
    pin_file = tmp_path / ".python-version"
    monkeypatch.setattr(nb, "PYTHON_PIN_FILE", pin_file)
    assert nb._read_python_pin() is None
    pin_file.write_text("3.12\n", encoding="utf-8")
    assert nb._read_python_pin() == "3.12"
    pin_file.write_text("  \n", encoding="utf-8")
    assert nb._read_python_pin() is None


def _venv_setup(tmp_path, monkeypatch):
    venv = tmp_path / ".venv"
    monkeypatch.setattr(nb, "VENV_DIR", venv)
    monkeypatch.setattr(nb, "VENV_PYTHON", venv / "Scripts" / "python.exe")
    return venv


def test_ensure_venv_uses_uv_with_pin(tmp_path, monkeypatch):
    venv = _venv_setup(tmp_path, monkeypatch)
    pin_file = tmp_path / ".python-version"
    pin_file.write_text("3.12\n", encoding="utf-8")
    monkeypatch.setattr(nb, "PYTHON_PIN_FILE", pin_file)
    probes = iter([False, True])
    monkeypatch.setattr(nb, "_is_runnable_python", lambda path: next(probes))
    monkeypatch.setattr(nb, "_uv_executable", lambda: "uv")
    calls = []
    monkeypatch.setattr(nb, "_run", lambda argv, *, cwd: calls.append(argv))
    nb._ensure_venv()
    assert calls == [["uv", "venv", "--relocatable", "--python", "3.12", str(venv)]]


def test_ensure_venv_falls_back_to_stdlib_on_uv_failure(tmp_path, monkeypatch):
    venv = _venv_setup(tmp_path, monkeypatch)
    pin_file = tmp_path / ".python-version"
    pin_file.write_text("3.12\n", encoding="utf-8")
    monkeypatch.setattr(nb, "PYTHON_PIN_FILE", pin_file)
    monkeypatch.setattr(nb, "_is_runnable_python", lambda path: False)
    monkeypatch.setattr(nb, "_uv_executable", lambda: "uv")
    calls = []

    def fake_run(argv, *, cwd):
        calls.append(argv)
        if argv[0] == "uv":
            raise subprocess.CalledProcessError(2, argv)

    monkeypatch.setattr(nb, "_run", fake_run)
    nb._ensure_venv()
    assert calls[-1] == [sys.executable, "-m", "venv", str(venv)]


def test_ensure_venv_keeps_runnable_venv(tmp_path, monkeypatch):
    _venv_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(nb, "_is_runnable_python", lambda path: True)
    calls = []
    monkeypatch.setattr(nb, "_run", lambda argv, *, cwd: calls.append(argv))
    nb._ensure_venv()
    assert calls == []


def test_running_inside_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(nb.sys, "executable", str(nb.VENV_DIR / "Scripts" / "python.exe"))
    assert nb._running_inside_venv() is True
    # 注意：pytest 自己就跑在 .venv 里，"venv 外"分支必须用显式外部路径，
    # 不能拿真实 sys.executable 当外部样本。
    monkeypatch.setattr(nb.sys, "executable", str(tmp_path / "python.exe"))
    assert nb._running_inside_venv() is False


def test_external_candidates_exclude_venv_path_python(tmp_path, monkeypatch):
    venv = _venv_setup(tmp_path, monkeypatch)
    venv_python = venv / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(nb, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(nb.os, "name", "nt")
    monkeypatch.setattr(nb.shutil, "which", lambda name: str(venv_python) if name in ("py", "python") else None)
    monkeypatch.setattr(nb, "_argv_is_runnable_python", lambda argv: True)
    candidates = nb._external_python_candidates()
    assert [str(venv_python)] not in candidates
    assert ["py", "-3"] in candidates


def test_main_delegates_repair_to_external_interpreter(monkeypatch):
    monkeypatch.setattr(nb, "_running_inside_venv", lambda: True)
    monkeypatch.setattr(nb, "_marker_matches_current", lambda: False)
    monkeypatch.setattr(nb, "_external_python_candidates", lambda: [["fake-python"]])
    recorded = []

    class Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        recorded.append(argv)
        return Done()

    monkeypatch.setattr(nb.subprocess, "run", fake_run)
    code = nb.main(["--version"])
    assert code == 0
    assert len(recorded) == 1
    argv = recorded[0]
    assert argv[0] == "fake-python"
    assert argv[1].endswith("negi_bootstrap.py")
    assert argv[2:] == ["--version"]


def test_main_no_delegation_when_healthy(monkeypatch):
    monkeypatch.setattr(nb, "_running_inside_venv", lambda: True)
    monkeypatch.setattr(nb, "_marker_matches_current", lambda: True)
    monkeypatch.setattr(nb, "_venv_has_runtime_deps", lambda: True)
    monkeypatch.setattr(nb, "_ensure_venv", lambda: None)
    monkeypatch.setattr(nb, "_ensure_project_installed", lambda: None)
    monkeypatch.setattr(nb, "_rotate_runtime_console_log", lambda: None)
    recorded = []

    class Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        recorded.append(argv)
        return Done()

    monkeypatch.setattr(nb.subprocess, "run", fake_run)
    nb.main(["--version"])
    assert recorded[0][0] == str(nb.VENV_PYTHON)


def test_delegate_falls_through_without_candidates(monkeypatch):
    monkeypatch.setattr(nb, "_running_inside_venv", lambda: True)
    monkeypatch.setattr(nb, "_marker_matches_current", lambda: False)
    monkeypatch.setattr(nb, "_external_python_candidates", lambda: [])
    assert nb._delegate_env_repair(["--version"]) is None
