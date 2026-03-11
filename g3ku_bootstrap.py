from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
BOOTSTRAP_MARKER = VENV_DIR / ".g3ku_bootstrap_complete"


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=str(cwd), check=True)


def _ensure_venv() -> None:
    if VENV_PYTHON.exists():
        return
    _run([sys.executable, "-m", "venv", str(VENV_DIR)], cwd=PROJECT_ROOT)


def _ensure_project_installed() -> None:
    if BOOTSTRAP_MARKER.exists():
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
