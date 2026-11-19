import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "install.ps1"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="install.ps1 lane is Windows")


def _function_body(name: str) -> str:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^function {re.escape(name)} \{{.*?^\}}", text)
    assert match, f"function {name} not found in install.ps1"
    return match.group(0)


def _probe(root: Path, pin: str) -> bool:
    script = "\n".join(
        [
            "Set-StrictMode -Version Latest",
            "$ErrorActionPreference = 'Stop'",
            _function_body("Get-VenvPython"),
            _function_body("Test-ProjectPythonHealthy"),
            '$ok = Test-ProjectPythonHealthy -Root $args[0] -Pin $args[1]',
            'Write-Output ("healthy=" + $ok)',
        ]
    )
    script_path = root / "probe.ps1"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), str(root), pin],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "healthy=True" in completed.stdout or "healthy=False" in completed.stdout, completed.stdout
    return "healthy=True" in completed.stdout


def test_missing_venv_is_unhealthy(tmp_path):
    assert _probe(tmp_path, "3.12") is False


def test_healthy_venv_with_matching_pin_is_reused(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(tmp_path / ".venv")],
        check=True,
        capture_output=True,
        timeout=120,
    )
    pin = f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert _probe(tmp_path, pin) is True
    assert _probe(tmp_path, "9.9") is False


def test_corrupted_venv_interpreter_is_unhealthy(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(tmp_path / ".venv")],
        check=True,
        capture_output=True,
        timeout=120,
    )
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_bytes(b"not an executable")
    pin = f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert _probe(tmp_path, pin) is False
