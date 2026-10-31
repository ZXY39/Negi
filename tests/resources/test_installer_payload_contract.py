r"""Negi-Setup.exe carries the offline kit, so one download installs with no network.

The wizard used to ship the tag's checkout and uv only: Python came from
python-build-standalone and every dependency from an index (with a mirror retry).
The payload is now the win-x64 offline kit - the same archive the release publishes
as Negi-<version>-offline-win-x64.zip - and install.ps1 -Kit installs it with
`--offline --no-index`. These cases pin the four places that have to agree, because
a build that silently fell back to the old payload would still produce an exe.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ISS = REPO_ROOT / "packaging" / "negi.iss"
BUILD = REPO_ROOT / "packaging" / "build-setup.ps1"
KIT_BUILD = REPO_ROOT / "packaging" / "build-offline-kit.ps1"
SETUP_RUN = REPO_ROOT / "packaging" / "setup-run.ps1"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_SH = REPO_ROOT / "install.sh"
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"


def test_wizard_ships_the_kit_as_its_only_payload() -> None:
    iss = ISS.read_text(encoding="utf-8")
    assert "{#KitZip}" in iss
    assert '{tmp}\\negi-kit.zip' in iss
    assert '-Kit ""{tmp}\\negi-kit.zip""' in iss
    # The old payload must not come back: it made the exe need an index.
    for gone in ("negi-source.zip", "uv.exe", "-SourceArchive", "-UvPath", "PayloadDir"):
        assert gone not in iss, gone


def test_setup_runner_forwards_the_kit_and_nothing_else() -> None:
    text = SETUP_RUN.read_text(encoding="utf-8-sig")
    assert "[string]$Kit" in text
    assert "$parameters['Kit'] = $Kit" in text
    for gone in ("SourceArchive", "UvPath"):
        assert gone not in text, gone
    # No auto-discovery of a payload file next to the scripts: the wizard names it.
    assert "'negi-source.zip'" not in text


def test_build_embeds_only_a_kit_it_validated() -> None:
    build = BUILD.read_text(encoding="utf-8")
    assert "[string]$Kit" in build
    assert "build-offline-kit.ps1" in build
    assert "kit.json" in build
    assert "$manifest.platform -ne 'win-x64'" in build
    assert "$manifest.version -ne $Version" in build
    assert "/DKitZip=$kitPayload" in build
    # A kit for another version or platform must fail the build, not ship.
    assert "kit source declares" in build
    # The clone / uv download lanes belonged to the old payload.
    assert "negi-source.zip" not in build
    assert "uv-version.txt" not in build
    assert "clone" not in build or "Invoke-Captured" in build
    # The version pins still have to agree with install.ps1 and install.sh.
    assert "install.sh" in build


def test_installer_uses_the_kit_before_any_network_lane() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "[string]$Kit = ''" in text
    assert "function Resolve-OfflineKit" in text
    assert "-Kit $Kit" in text
    assert "'%s\\System32\\tar.exe'" % "$env:SystemRoot" in text or "System32\\tar.exe" in text
    # The kit's uv is persisted before Ensure-Uv, or a device without uv would be
    # sent to astral.sh - the one call an offline install must not make.
    main = text[text.index("$resolvedKit = $null"):]
    assert main.index("Install-BundledUv -Path $resolvedKit.Uv") < main.index("$uv = Ensure-Uv")
    stage = text[text.index("function Get-CheckoutStage"):]
    assert stage.index("using the offline kit's source tree") < stage.index("git', 'clone")
    # The bundled-source and bundled-uv parameters are gone with the old payload.
    for gone in ("SourceArchive", "UvPath", "Expand-SourceArchive"):
        assert gone not in text, gone


def test_kit_metadata_is_what_the_installer_verifies() -> None:
    kit = KIT_BUILD.read_text(encoding="utf-8")
    # kit.json records the platform, the version, every wheel with its digest, the
    # interpreter, uv and the requirement list - the six things install.ps1 checks.
    for field in ("platform", "version", "wheels", "python_executable", "uv_binary", "requirements"):
        assert field in kit, field
    install = INSTALL_PS1.read_text(encoding="utf-8")
    for check in ("$manifest.platform -ne $device", "$sourceVersion -ne $manifest.version",
                  "does not match its recorded sha256", "kit has no requirements list"):
        assert check in install, check


def test_the_installer_job_reuses_the_windows_kit() -> None:
    release = RELEASE.read_text(encoding="utf-8")
    windows = release[release.index("offline-kit-windows:"):]
    assert "upload-artifact" in windows
    assert "offline-kit-win-x64" in windows
    installer = release[release.index("windows-installer:"):release.index("offline-kit-windows:")]
    assert "download-artifact" in installer
    assert "-Kit $kit.FullName" in installer
    # The matrix would build the same kit a second time for Windows.
    matrix = release[release.index("offline-kits:"):]
    assert "win-x64" not in matrix.split("jobs:")[0]


def test_both_installers_retry_failed_downloads_through_mirrors() -> None:
    ps = INSTALL_PS1.read_text(encoding="utf-8")
    sh = INSTALL_SH.read_text(encoding="utf-8")
    for text in (ps, sh):
        assert "UV_DEFAULT_INDEX" in text
        assert "UV_PYTHON_INSTALL_MIRROR" in text
        assert "pypi.tuna.tsinghua.edu.cn" in text
        assert "mirror.sjtu.edu.cn" in text
    assert "-Mirror" in ps
    assert "-NoMirror" in ps
    assert "--mirror" in sh
    assert "--no-mirror" in sh


def test_version_pins_are_still_compared_before_building() -> None:
    build = BUILD.read_text(encoding="utf-8")
    assert re.search(r"cannot read the \$Ref pin", build)
    assert re.search(r"cannot read the REF pin", build)
    assert "throw \"install.ps1 pins" in build
    assert "throw \"install.sh pins" in build
