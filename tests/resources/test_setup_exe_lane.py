r"""packaging\setup-run.ps1 decides what Negi-Setup.exe asks install.ps1 to do.

The exe is the adoption channel for devices that already run Negi, so the two
decisions it owns - which folder this device's Negi lives in, and whether this is
an upgrade - are pinned here by running the real script against a stub installer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_RUN = REPO_ROOT / "packaging" / "setup-run.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

STUB_INSTALLER = """
param(
    [string]$Dir,
    [switch]$Upgrade,
    [switch]$NoUninstallEntry,
    [switch]$NoStart,
    [string]$SourceArchive,
    [string]$UvPath
)
$payload = [ordered]@{
    dir = $Dir
    upgrade = $Upgrade.IsPresent
    no_uninstall_entry = $NoUninstallEntry.IsPresent
    no_start = $NoStart.IsPresent
    source_archive = $SourceArchive
    uv_path = $UvPath
}
$payload | ConvertTo-Json | Set-Content -LiteralPath $env:NEGI_STUB_OUT -Encoding ascii
exit 3
"""


def _stage(tmp_path: Path) -> tuple[Path, Path, Path]:
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copyfile(SETUP_RUN, stage / "setup-run.ps1")
    (stage / "install.ps1").write_text(STUB_INSTALLER, encoding="ascii")
    local = tmp_path / "localappdata"
    (local / "Negi").mkdir(parents=True)
    return stage, local, stage / "setup-run.ps1"


def _run(script: Path, local: Path, out: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "LOCALAPPDATA": str(local), "NEGI_STUB_OUT": str(out)}
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def _bound(out: Path) -> dict[str, object]:
    assert out.exists(), "install.ps1 was never called"
    return json.loads(out.read_text(encoding="utf-8"))


def test_existing_checkout_is_upgraded_not_reinstalled(tmp_path: Path) -> None:
    stage, local, script = _stage(tmp_path)
    root = tmp_path / "chosen"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    out = tmp_path / "bound.json"

    result = _run(script, local, out, ["-Dir", str(root), "-Quiet"])

    assert result.returncode == 3, result.stdout + result.stderr
    bound = _bound(out)
    assert bound["dir"] == str(root)
    # Without -Upgrade, install.ps1 leaves the code alone but still rewrites the
    # shortcuts, and the new `start --window` argument would then be handed to a
    # bootstrap old enough not to know it.
    assert bound["upgrade"] is True


def test_recorded_root_wins_over_the_wizard_default(tmp_path: Path) -> None:
    stage, local, script = _stage(tmp_path)
    elsewhere = tmp_path / "installed elsewhere"
    elsewhere.mkdir()
    (elsewhere / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (local / "Negi" / "install-root.json").write_text(
        json.dumps({"version": "v2.5.0", "root": str(elsewhere)}), encoding="utf-8"
    )
    untouched = tmp_path / "wizard default"
    untouched.mkdir()
    out = tmp_path / "bound.json"

    _run(script, local, out, ["-Dir", str(untouched), "-Quiet", "-NoUninstallEntry"])

    bound = _bound(out)
    assert bound["dir"] == str(elsewhere)
    assert bound["upgrade"] is True
    assert bound["no_uninstall_entry"] is True


def test_pointer_without_code_under_it_stays_a_fresh_install(tmp_path: Path) -> None:
    # A kept-data uninstall leaves the pointer behind and the code gone; the next
    # install is meant to be a fresh one, not an upgrade of a missing tree.
    stage, local, script = _stage(tmp_path)
    gone = tmp_path / "removed code"
    (local / "Negi" / "install-root.json").write_text(
        json.dumps({"root": str(gone)}), encoding="utf-8"
    )
    target = tmp_path / "fresh"
    target.mkdir()
    out = tmp_path / "bound.json"

    _run(script, local, out, ["-Dir", str(target), "-Quiet"])

    bound = _bound(out)
    assert bound["dir"] == str(target)
    assert bound["upgrade"] is False


def test_payload_next_to_the_scripts_is_handed_to_install_ps1(tmp_path: Path) -> None:
    stage, local, script = _stage(tmp_path)
    source = stage / "negi-source.zip"
    source.write_bytes(b"PK\x03\x04")
    uv = stage / "uv.exe"
    uv.write_bytes(b"MZ")
    out = tmp_path / "bound.json"

    _run(script, local, out, ["-Dir", str(tmp_path / "fresh"), "-Quiet"])

    bound = _bound(out)
    assert bound["source_archive"] == str(source)
    assert bound["uv_path"] == str(uv)


def test_setup_keeps_a_log_a_user_can_send_back(tmp_path: Path) -> None:
    stage, local, script = _stage(tmp_path)
    out = tmp_path / "bound.json"

    _run(script, local, out, ["-Dir", str(tmp_path / "fresh"), "-Quiet"])

    log = local / "Negi" / "logs" / "setup.log"
    assert log.exists()
    assert "setup start" in log.read_text(encoding="utf-8-sig")


def test_relative_answer_resolves_against_the_caller_cwd(tmp_path: Path) -> None:
    stage, local, script = _stage(tmp_path)
    (tmp_path / "rel-target").mkdir()
    out = tmp_path / "bound.json"
    env = {**os.environ, "LOCALAPPDATA": str(local), "NEGI_STUB_OUT": str(out)}
    subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Dir",
            "rel-target",
            "-Quiet",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )

    assert _bound(out)["dir"] == str(tmp_path / "rel-target")
