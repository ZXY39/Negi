from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
BOOTSTRAP_MARKER = VENV_DIR / ".g3ku_bootstrap_complete"
MIN_PYTHON = (3, 11)


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=str(cwd), check=True)


def _is_runnable_python(python_path: Path) -> bool:
    if not python_path.exists():
        return False
    try:
        completed = subprocess.run(
            [str(python_path), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _reset_venv() -> None:
    if VENV_DIR.exists():
        shutil.rmtree(VENV_DIR)


def _ensure_host_python_supported() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        current = ".".join(str(part) for part in sys.version_info[:3])
        raise SystemExit(
            f"[g3ku] Python {required}+ is required to bootstrap this project. Current interpreter: {current} ({sys.executable})"
        )


def _ensure_venv() -> None:
    _ensure_host_python_supported()
    if _is_runnable_python(VENV_PYTHON):
        return
    if VENV_DIR.exists():
        print(f"[g3ku] Recreating stale virtualenv at {VENV_DIR}")
        _reset_venv()
    _run([sys.executable, "-m", "venv", str(VENV_DIR)], cwd=PROJECT_ROOT)


def _ensure_project_installed() -> None:
    if BOOTSTRAP_MARKER.exists() and _is_runnable_python(VENV_PYTHON):
        return
    _run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], cwd=PROJECT_ROOT)
    _run([str(VENV_PYTHON), "-m", "pip", "install", "-e", "."], cwd=PROJECT_ROOT)
    BOOTSTRAP_MARKER.write_text("ok\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    os.chdir(PROJECT_ROOT)
    _ensure_venv()
    _ensure_project_installed()
    args = list(argv if argv is not None else sys.argv[1:])
    completed = subprocess.run([str(VENV_PYTHON), "-m", "g3ku.g3ku_cli", *args], cwd=str(PROJECT_ROOT))
    return int(completed.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
