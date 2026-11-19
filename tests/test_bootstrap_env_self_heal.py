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
