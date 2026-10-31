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
    [string]$Kit
)
$payload = [ordered]@{
    dir = $Dir
    upgrade = $Upgrade.IsPresent
    no_uninstall_entry = $NoUninstallEntry.IsPresent
    no_start = $NoStart.IsPresent
    kit = $Kit
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


def test_kit_is_forwarded_and_nothing_is_auto_discovered(tmp_path: Path) -> None:
    # The wizard unpacks one kit and names it; this script only hands the path on.
    stage, local, script = _stage(tmp_path)
    kit = stage / "negi-kit.zip"
    kit.write_bytes(b"PK\x03\x04")
    out = tmp_path / "bound.json"

    _run(script, local, out, ["-Dir", str(tmp_path / "fresh"), "-Quiet", "-Kit", str(kit)])

    bound = _bound(out)
    assert bound["kit"] == str(kit)

    # An older payload file lying next to the script must not be picked up: a second
    # implicit payload is exactly how an exe stops being offline without anyone noticing.
    (stage / "negi-source.zip").write_bytes(b"PK\x03\x04")
    (stage / "uv.exe").write_bytes(b"MZ")
    out2 = tmp_path / "bound2.json"
    _run(script, local, out2, ["-Dir", str(tmp_path / "fresh"), "-Quiet"])
    assert _bound(out2)["kit"] == ""


def test_setup_never_starts_the_server_inside_the_install_step(tmp_path: Path) -> None:
    # install.ps1 ends by running the web server in the foreground, and a [Run] entry
    # waits for that process to exit: without -NoStart the wizard never reports success
    # and its own postinstall launch entry never runs (measured on the 2.9.0 build -
    # the install sat on "negi_bootstrap.py web" until it was killed).
    stage, local, script = _stage(tmp_path)
    out = tmp_path / "bound.json"

    _run(script, local, out, ["-Dir", str(tmp_path / "fresh"), "-Quiet"])

    assert _bound(out)["no_start"] is True


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
