r"""Negi-Setup.exe carries source and uv, and the installers can fall back.

Three parts only work together: the wizard ships negi-source.zip and uv.exe,
install.ps1 consumes them before touching the network, and both installers
retry a failed Python/package download through a mirror. These cases pin the
names the build, the wizard and the scripts have to agree on.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ISS = REPO_ROOT / "packaging" / "negi.iss"
BUILD = REPO_ROOT / "packaging" / "build-setup.ps1"
SETUP_RUN = REPO_ROOT / "packaging" / "setup-run.ps1"
UV_VERSION = REPO_ROOT / "packaging" / "uv-version.txt"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_SH = REPO_ROOT / "install.sh"


def test_wizard_ships_and_hands_over_the_payload() -> None:
    iss = ISS.read_text(encoding="utf-8")
    assert "{#PayloadDir}\\negi-source.zip" in iss
    assert "{#PayloadDir}\\uv.exe" in iss
    assert "-SourceArchive" in iss
    assert "-UvPath" in iss


def test_setup_runner_finds_the_payload_next_to_itself() -> None:
    text = SETUP_RUN.read_text(encoding="utf-8-sig")
    assert "'negi-source.zip'" in text
    assert "'uv.exe'" in text
    assert "SourceArchive" in text
    assert "UvPath" in text


def test_build_reuses_the_single_exclusion_list_and_verifies_uv() -> None:
    build = BUILD.read_text(encoding="utf-8")
    assert "distribution-excludes.txt" in build
    assert "uv-version.txt" in build
    assert "Get-FileHash" in build
    # The payload must force LF: a CRLF checkout built on the CI runner would look
    # like a dirty worktree on devices whose git uses autocrlf=false.
    assert "core.autocrlf" in build
    # install.sh carries the same pinned tag as install.ps1; the build compares both.
    assert "install.sh" in build
    version = UV_VERSION.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


def test_installer_prefers_the_bundled_payload_over_the_network() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    stage = text[text.index("function Get-CheckoutStage"):]
    assert stage.index("extracting bundled source") < stage.index("git', 'clone")
    assert "Install-BundledUv" in text
    assert "Get-CheckoutVersion" in text
    assert "dropping .git from the bundled source" in text


def test_installer_rejects_a_payload_for_another_version() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "instead of $Ref" in text


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
