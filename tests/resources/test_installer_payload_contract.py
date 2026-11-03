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


def test_the_release_publishes_one_installer_per_platform() -> None:
    """Assets are the two scripts and the offline exe - no kit archives.

    The Windows kit is an input to the exe, so publishing it next to the exe would
    ship the same hundred megabytes twice. Linux and macOS install from the scripts;
    a kit for them is built where it is needed.
    """
    release = RELEASE.read_text(encoding="utf-8")
    assert "offline-kits:" not in release, "the kit matrix is back; it would publish duplicates"
    uploads = [ln.strip() for ln in release.splitlines() if "gh release upload" in ln]
    assert uploads, "nothing is uploaded any more"
    assert all("INSTALLER" in up or "SETUP_ASSET" in up or "install.ps1 install.sh" in up for up in uploads), uploads
    kit_job = release[release.index("offline-kit-windows:"):release.index("windows-installer:")]
    assert "gh release upload" not in kit_job


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


def test_icon_and_postinstall_launch_the_browser() -> None:
    """The desktop/start-menu icon and the installer's finish action start Negi
    with `start` alone. `--window` stays an explicit opt-in for the WebView2
    shell, so the default entry never depends on it.
    """
    install = INSTALL_PS1.read_text(encoding="utf-8")
    assert '$link.Arguments = """$Root\\negi_bootstrap.py"" start"' in install
    assert "start --window" not in install
    iss = ISS.read_text(encoding="utf-8")
    assert 'Parameters: "start"; Description: "启动 Negi（浏览器）"' in iss
    assert "start --window" not in iss


def test_fresh_install_registers_logon_autostart_and_upgrade_does_not() -> None:
    """默认开启只发生在全新安装：升级不替操作员动系统登记，卸载必须收干净。

    这条登记写的是操作员账户的登录钩子，装错一处就是每次登录失败一次，所以两个安装器
    与两个卸载器都按文本钉住，实现侧的判据在 tests/resources/test_autostart_registration.py。
    """
    install = INSTALL_PS1.read_text(encoding="utf-8")
    assert "if (-not $Upgrade) { Register-NegiAutostart -Root $Dir }" in install
    assert "& $python -m g3ku autostart on 2>&1" in install

    install_sh = INSTALL_SH.read_text(encoding="utf-8")
    assert 'if [ "$UPGRADE" -eq 1 ]; then' in install_sh
    assert '"$DIR/.venv/bin/python" -m g3ku autostart on 2>&1' in install_sh

    uninstall = (REPO_ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    assert "CurrentVersion\\Run" in uninstall
    assert "$AppDisplayName" in uninstall

    uninstall_sh = (REPO_ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert "systemctl --user disable negi.service" in uninstall_sh
    assert "com.negi.web.plist" in uninstall_sh


def test_version_pins_are_still_compared_before_building() -> None:
    build = BUILD.read_text(encoding="utf-8")
    assert re.search(r"cannot read the \$Ref pin", build)
    assert re.search(r"cannot read the REF pin", build)
    assert "throw \"install.ps1 pins" in build
    assert "throw \"install.sh pins" in build
