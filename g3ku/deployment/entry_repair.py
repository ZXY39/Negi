"""一次性把旧入口登记面迁移到带回落链的形状。

web 启动时 best-effort 运行：只改写与 legacy_launcher_command() 精确匹配的自启登记，
以及 TargetPath 落在本安装根 .venv\\Scripts 下、参数含 negi_bootstrap.py 的快捷方式。
操作员自定义形状、未登记、读不动的登记一律不动——与 ensure_enabled_on_fresh_install
同一条边界：不替操作员做他没要求的决定。任何失败都不许挡启动。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from g3ku.deployment import autostart


def _autostart_needs_migration() -> bool:
    registered = autostart.registered_argv()
    if not registered:
        return False
    legacy = autostart.legacy_launcher_command()
    if os.name == "nt":
        return [item.lower() for item in registered] == [item.lower() for item in legacy]
    return registered == legacy


def _repair_shortcut_script(root: Path) -> str:
    escaped_root = str(root).replace("'", "''")
    wscript = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe")
    arguments = ('"' + str(root / "negi-start.vbs") + '" start').replace("'", "''")
    return (
        "$shell = New-Object -ComObject WScript.Shell\n"
        "$root = '" + escaped_root + "'\n"
        "foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {\n"
        "  if (-not $dir) { continue }\n"
        "  $lnk = Join-Path $dir 'Negi.lnk'\n"
        "  if (-not (Test-Path -LiteralPath $lnk)) { continue }\n"
        "  $link = $shell.CreateShortcut($lnk)\n"
        "  $venvPrefix = (Join-Path $root '.venv\\Scripts') + '\\'\n"
        "  if ($link.TargetPath.StartsWith($venvPrefix, [StringComparison]::OrdinalIgnoreCase) -and\n"
        "      $link.Arguments -like '*negi_bootstrap.py*') {\n"
        "    $link.TargetPath = '" + wscript.replace("'", "''") + "'\n"
        "    $link.Arguments = '" + arguments + "'\n"
        "    $link.WorkingDirectory = $root\n"
        "    $link.Save()\n"
        "    Write-Output ('repaired: ' + $lnk)\n"
        "  }\n"
        "}\n"
    )


def repair_entries(root: Path | None = None) -> dict[str, object]:
    root = root or autostart.RUNTIME_ROOT
    report: dict[str, object] = {"autostart": "untouched", "shortcuts": []}
    try:
        if _autostart_needs_migration():
            state = autostart.apply_enabled(True)
            report["autostart"] = "migrated" if state.get("ok") else f"failed: {state.get('reason')}"
    except Exception as exc:  # noqa: BLE001 - 迁移失败绝不挡启动
        report["autostart"] = f"failed: {exc}"
    if os.name == "nt":
        wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
        if wscript.is_file() and (root / "negi-start.vbs").is_file():
            try:
                completed = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", _repair_shortcut_script(root)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                report["shortcuts"] = [
                    line for line in completed.stdout.splitlines() if line.startswith("repaired:")
                ]
            except (OSError, subprocess.SubprocessError) as exc:
                report["shortcuts"] = [f"failed: {exc}"]
    return report
