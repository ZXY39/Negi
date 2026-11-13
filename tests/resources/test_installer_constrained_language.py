"""安装器的 ConstrainedLanguage 兼容车道（P0-2 B+C）。

公司受管设备把 PowerShell 锁进 ConstrainedLanguage（机器级 __PSLockdownPolicy=4），
非核心 .NET / COM 方法全拒。安装器因此为 uv 获取、用户 PATH、尺寸测量、快捷方式各备了
一条 cmdlet-only 车道。这里钉两件事：文本上车道存在且用对了构造；功能上把子进程的
__PSLockdownPolicy 设成 4 真跑一遍，确认锁定模式下尺寸车道给出正确数字。

只在有 powershell 的 Windows 上跑功能段；其余平台只跑文本断言。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "install.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
MAIN_FLOW_MARKER = 'Write-Step "target directory: $Dir"'


def test_uv_bootstrap_has_a_cmdlet_only_zip_lane() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "function Test-ConstrainedLanguage" in text
    assert "function Install-UvFromZip" in text
    assert "$UvVersion = '0.13.8'" in text
    assert "$UvZipMirrors = @(" in text
    assert "releases.astral.sh/github/uv/releases/download" in text
    # Ensure-Uv 锁定直走 zip，正常先 astral 再回退 zip。
    assert "if (Test-ConstrainedLanguage) {" in text
    assert "falling back to the official release zip" in text
    # zip 车道必须用 cmdlet 与 $env:TEMP / [Guid]，不能用被拒的 [System.IO.Path]。
    zip_lane = text[text.index("function Install-UvFromZip"):text.index("function Ensure-Uv")]
    assert "Invoke-WebRequest" in zip_lane
    assert "Expand-Archive" in zip_lane
    assert "$env:TEMP" in zip_lane
    assert "[System.IO.Path]::" not in zip_lane


def test_first_install_surface_has_constrained_branches() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    for fn in ("Add-UserPathEntry", "Get-TreeSizeBytes", "Install-DesktopIcons"):
        start = text.index(f"function {fn}")
        # 函数体到下一个顶层 function 为止。
        rest = text[start + 1:]
        nxt = rest.find("\nfunction ")
        body = rest[:nxt] if nxt != -1 else rest
        assert "Test-ConstrainedLanguage" in body, f"{fn} lost its constrained branch"
    # PATH 车道用注册表 provider + reg.exe 原始读，保留 %VAR%。
    assert "Set-ItemProperty -Path 'HKCU:\\Environment' -Name Path -Type ExpandString" in text
    assert "reg.exe query 'HKCU\\Environment' /v Path" in text
    # 尺寸车道用 cmdlet 管道。
    assert "Measure-Object -Property Length -Sum" in text


@pytest.mark.skipif(sys.platform != "win32" or not POWERSHELL.exists(), reason="needs Windows PowerShell")
def test_size_lane_runs_under_constrained_language(tmp_path: Path) -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    funcs = text[:text.index(MAIN_FLOW_MARKER)]
    funcs_file = tmp_path / "funcs.ps1"
    funcs_file.write_text(funcs, encoding="utf-8")

    probe = tmp_path / "tree"
    probe.mkdir()
    (probe / "a.bin").write_bytes(b"x" * 1000)
    (probe / "b.bin").write_bytes(b"y" * 2500)

    script = tmp_path / "probe.ps1"
    script.write_text(
        ". (Join-Path $PSScriptRoot 'funcs.ps1')\n"
        "$ErrorActionPreference = 'Stop'\n"
        "'constrained=' + (Test-ConstrainedLanguage)\n"
        "$size = Get-TreeSizeBytes -Path (Join-Path $PSScriptRoot 'tree')\n"
        "'size=' + $size\n",
        encoding="utf-8",
    )
    env = {**os.environ, "__PSLockdownPolicy": "4"}
    done = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "constrained=True" in done.stdout, done.stdout + done.stderr
    assert "size=3500" in done.stdout, done.stdout + done.stderr