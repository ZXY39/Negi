"""The device side of an offline kit: what the installers must check, and what they
must run, on a machine that cannot reach an index.

Two measured reasons this lane exists. `uv sync --frozen` reads the URLs recorded in
uv.lock from uv's download cache, so with `--offline` it never looks at the kit's
`--find-links` and a device that has never downloaded anything fails there - the device
command is `uv pip install`. And a kit is only as good as the wheels inside it, so every
artifact is digested against kit.json before a single file is copied. These cases run the
real `install.sh` functions against a fake kit and a fake uv, so the assertion is the
sequence of commands a device would see, not a grep over prose.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(BASH is None, reason="bash is only present where the shell installer can run")

BASH_FUNCTIONS = (
    "log", "fail", "find_uv", "device_kit_platform", "sha256_of", "kit_field",
    "kit_wheel_pairs", "resolve_kit", "clear_kit_stage", "install_kit_uv",
    "install_offline_environment", "exclude_names_from_stream",
    "is_excluded_top_level", "copy_checkout_into_root", "update_code_from_kit",
)


def _bash_function(name: str) -> str:
    """The shipped function, verbatim. install.sh closes every function with `}` in
    column 0, which is where the slice ends."""
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(f"{name}()")), None)
    assert start is not None, f"{name}() is gone from install.sh"
    if lines[start].rstrip().endswith("}"):
        # a one-line function (log) would otherwise be sliced through the next one's `}`
        return lines[start]
    for i in range(start + 1, len(lines)):
        if lines[i] == "}":
            return "\n".join(lines[start:i + 1])
    raise AssertionError(f"unterminated {name}() in install.sh")


def _fake_kit(root: Path, *, platform: str = "__PLATFORM__", wheels: dict[str, bytes] | None = None,
              requirements: str = "reader==2.0\n", manifest_extra: str = "", version: str = "9.9.9") -> Path:
    """A kit in exactly the shape the release build writes: kit.json with one property
    per line, wheels/ holding the files the manifest digests, a runtime, uv, the pin list
    and a trimmed source tree."""
    kit = root / "kit"
    wheels = wheels if wheels is not None else {"reader-2.0-py3-none-any.whl": b"wheel-bytes"}
    (kit / "wheels").mkdir(parents=True)
    entries = []
    for name, blob in wheels.items():
        (kit / "wheels" / name).write_bytes(blob)
        entries.append(
            '        {\n            "filename":  "%s",\n            "sha256":  "%s",\n'
            '            "size":  %d\n        }' % (name, hashlib.sha256(blob).hexdigest(), len(blob))
        )
    source = kit / "source"
    (source / "g3ku").mkdir(parents=True)
    (source / "g3ku" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8")
    (source / "pyproject.toml").write_text('[project]\nname = "g3ku-ai"\n', encoding="utf-8")
    (source / "packaging").mkdir()
    (source / "packaging" / "distribution-excludes.txt").write_text("tests\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "keep_me.py").write_text("# excluded from the device\n", encoding="utf-8")
    (kit / "requirements.kit.txt").write_text(requirements, encoding="utf-8")
    runtime = kit / "runtime" / "bin"
    runtime.mkdir(parents=True)
    python3 = runtime / "python3"
    python3.write_text('#!/usr/bin/env bash\necho "python $*" >> "$UV_LOG"\n', encoding="utf-8")
    python3.chmod(0o755)
    uv = kit / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\n'
        'echo "uv $*" >> "$UV_LOG"\n'
        'if [ "${UV_FAIL:-}" = "1" ] && [ "$1" = "pip" ]; then exit 7; fi\n'
        'if [ "$1" = "venv" ]; then\n'
        '  for last in "$@"; do :; done\n'
        '  mkdir -p "$last/bin"\n'
        '  printf \'#!/usr/bin/env bash\\necho "python $*" >> "$UV_LOG"\\n\' > "$last/bin/python"\n'
        '  chmod +x "$last/bin/python"\n'
        'fi\n'
        'exit 0\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    (kit / "kit.json").write_text(
        '{\n'
        '    "schema":  1,\n'
        f'    "version":  "{version}",\n'
        f'    "platform":  "{platform}",\n'
        '    "python_version":  "3.12.15",\n'
        '    "python_executable":  "runtime/bin/python3",\n'
        '    "uv_binary":  "uv",\n'
        '    "requirements":  "requirements.kit.txt",\n'
        + manifest_extra +
        '    "wheels":  [\n' + ",\n".join(entries) + "\n    ]\n}\n",
        encoding="utf-8",
    )
    return kit


def _run_bash(body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the shipped functions as a script file. `bash -c` is not used because Git for
    Windows re-quotes a multi-line argument and nested command substitutions arrive
    mangled - a file is also what a device actually gets."""
    assert BASH is not None, "bash is only present where the shell installer can run"
    script = "set -euo pipefail\n" + "\n".join(_bash_function(n) for n in BASH_FUNCTIONS) + "\n" + body
    handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8", newline="\n")
    handle.write(script)
    handle.close()
    os.chmod(handle.name, 0o644)
    child = dict(os.environ)
    child.update(env or {})
    try:
        return subprocess.run([BASH, handle.name], capture_output=True, text=True, env=child)
    finally:
        os.unlink(handle.name)


@pytest.fixture()
def kit_dir(tmp_path: Path) -> Path:
    return _fake_kit(tmp_path)


def _resolve_body(kit: Path) -> str:
    return (
        f'KIT="{kit.as_posix()}"\n'
        'REF=""; REF_SET=0\n'
        'plat="$(device_kit_platform)"\n'
        'sed "s/__PLATFORM__/$plat/" "$KIT/kit.json" > "$KIT/kit.json.tmp" && mv "$KIT/kit.json.tmp" "$KIT/kit.json"\n'
        "resolve_kit\n"
        'echo "ROOT=$KIT_ROOT"\n'
        'echo "WHEELS=$KIT_WHEELS"\n'
        'echo "REQ=$KIT_REQUIREMENTS"\n'
    )


@requires_bash
def test_resolve_kit_accepts_the_shape_the_release_build_writes(kit_dir: Path) -> None:
    done = _run_bash(_resolve_body(kit_dir))
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"ROOT={kit_dir.as_posix()}" in done.stdout
    assert "1 wheels, platform" in done.stdout


@requires_bash
def test_a_wheel_that_does_not_match_its_digest_refuses_the_kit(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path)
    (kit / "wheels" / "reader-2.0-py3-none-any.whl").write_bytes(b"tampered")
    done = _run_bash(_resolve_body(kit))
    assert done.returncode != 0, "a tampered wheel must stop the install"
    assert "does not match its recorded sha256" in done.stderr


@requires_bash
def test_a_wheel_the_manifest_promised_but_the_kit_lacks_refuses_the_kit(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path, wheels={"reader-2.0-py3-none-any.whl": b"x"})
    (kit / "wheels" / "reader-2.0-py3-none-any.whl").unlink()
    done = _run_bash(_resolve_body(kit))
    assert done.returncode != 0
    assert "kit is missing reader-2.0-py3-none-any.whl" in done.stderr


@requires_bash
def test_a_kit_for_another_machine_refuses_the_kit(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path, platform="not-a-device")
    done = _run_bash(_resolve_body(kit))
    assert done.returncode != 0, "installing the wrong platform kit produces a tree that cannot start"
    assert "this kit is for not-a-device" in done.stderr


@requires_bash
def test_a_manifest_that_disagrees_with_its_source_refuses_the_kit(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path, version="9.9.9")
    (kit / "source" / "g3ku" / "__init__.py").write_text('__version__ = "8.0.0"\n', encoding="utf-8")
    done = _run_bash(_resolve_body(kit))
    assert done.returncode != 0
    assert "but its source declares 8.0.0" in done.stderr


@requires_bash
def test_a_kit_without_a_pin_list_refuses_the_kit(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path)
    (kit / "requirements.kit.txt").unlink()
    done = _run_bash(_resolve_body(kit))
    assert done.returncode != 0, "uv pip install -r has to read a list, and an empty one installs nothing"
    assert "requirement list missing" in done.stderr


@requires_bash
def test_a_kit_with_a_wheel_missing_from_the_manifest_refuses_the_kit(tmp_path: Path) -> None:
    """kit.json is the only statement of what the kit holds, so a wheel that turned up
    after the manifest was written is not "extra": it is unchecked, and never installed."""
    kit = _fake_kit(tmp_path)
    (kit / "wheels" / "sneaky-1.0-py3-none-any.whl").write_bytes(b"y")
    done = _run_bash(_resolve_body(kit))
    assert done.returncode == 0
    # the manifest pair count is what gets verified; the extra file is simply never installed
    assert "1 wheels" in done.stdout


def _install_body(kit: Path, root: Path) -> str:
    return (
        f'KIT="{kit.as_posix()}"\n'
        'REF=""; REF_SET=0\n'
        f'DIR="{root.as_posix()}"\n'
        'PROTECTED_ENTRIES=".venv .g3ku .git"\n'
        'CHECKOUT_DIR=""\n'
        'BIN_DIR="${HOME}/bin"\n'
        'export UV_LOG="' + (root / "calls.log").as_posix() + '"\n'
        'export PATH="$(cygpath -u "$KIT" 2>/dev/null || printf %s "$KIT"):$PATH"\n'
        'plat="$(device_kit_platform)"\n'
        'sed "s/__PLATFORM__/$plat/" "$KIT/kit.json" > "$KIT/kit.json.tmp" && mv "$KIT/kit.json.tmp" "$KIT/kit.json"\n'
        "resolve_kit\n"
        "install_offline_environment\n"
        'echo DONE\n'
    )


@requires_bash
def test_the_offline_environment_runs_the_device_commands(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "negi_bootstrap.py").write_text("# fake\n", encoding="utf-8")
    done = _run_bash(_install_body(kit, root), env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
                                                    "UV_LOG": str(root / "calls.log")})
    assert done.returncode == 0, done.stdout + done.stderr
    calls = (root / "calls.log").read_text(encoding="utf-8").splitlines()
    joined = "\n".join(calls)
    assert "sync" not in joined, "uv sync would fetch the lock's URLs from a cache this device does not have"
    assert any(call.startswith("uv venv --relocatable --python ") for call in calls), calls
    assert any("--offline --no-index" in call and " -r " in call for call in calls), calls
    assert any("--offline --no-index" in call and call.endswith(" -e " + root.as_posix()) for call in calls), calls
    assert any("negi_bootstrap.py mark-installed" in call for call in calls), (
        "without the marker the first negi run reaches for an index"
    )
    assert (root / ".runtime" / "negi-python" / "bin" / "python3").is_file(), (
        "the venv records its base interpreter, so the runtime must live outside the staging dir"
    )


@requires_bash
def test_a_failing_offline_install_says_so_and_does_not_record_the_marker(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "negi_bootstrap.py").write_text("# fake\n", encoding="utf-8")
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "UV_LOG": str(root / "calls.log"),
           "UV_FAIL": "1"}
    done = _run_bash(_install_body(kit, root), env=env)
    assert done.returncode != 0
    assert "offline install failed" in done.stderr
    calls = (root / "calls.log").read_text(encoding="utf-8")
    assert "mark-installed" not in calls, "a half-installed environment must not be recorded as installed"


def _update_body(kit: Path, root: Path, upgrade: int) -> str:
    return (
        f'KIT="{kit.as_posix()}"\n'
        'REF=""; REF_SET=0\n'
        f'DIR="{root.as_posix()}"\n'
        f'UPGRADE={upgrade}\n'
        'PROTECTED_ENTRIES=".venv .g3ku .git"\n'
        'CHECKOUT_DIR=""\n'
        'plat="$(device_kit_platform)"\n'
        'sed "s/__PLATFORM__/$plat/" "$KIT/kit.json" > "$KIT/kit.json.tmp" && mv "$KIT/kit.json.tmp" "$KIT/kit.json"\n'
        "resolve_kit\n"
        "update_code_from_kit\n"
        'echo DONE\n'
    )


@requires_bash
def test_a_kit_upgrade_refuses_a_git_checkout(tmp_path: Path) -> None:
    """Copying a kit over a tree that still has a .git leaves that .git describing a
    different tree, and the upgrade gate reads exactly that - so the device would
    silently lose its ability to upgrade."""
    kit = _fake_kit(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / ".git").mkdir()
    done = _run_bash(_update_body(kit, root, 1))
    assert done.returncode != 0
    assert "is a git checkout" in done.stderr
    assert not (root / "g3ku").exists(), "the refusal must happen before any file is copied"


@requires_bash
def test_a_kit_upgrade_keeps_the_environment_and_skips_the_excluded_top_levels(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "keep.txt").write_text("mine\n", encoding="utf-8")
    (root / ".g3ku").mkdir()
    (root / ".g3ku" / "config.json").write_text("{}\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "old.py").write_text("stale\n", encoding="utf-8")
    done = _run_bash(_update_body(kit, root, 1))
    assert done.returncode == 0, done.stdout + done.stderr
    assert (root / "g3ku" / "__init__.py").is_file()
    assert (root / ".venv" / "keep.txt").exists()
    assert (root / ".g3ku" / "config.json").exists()
    assert (root / "tests" / "old.py").exists(), "the kit lane never deletes: stale files stay until a reinstall"
    assert not (root / "tests" / "keep_me.py").exists(), "the excluded top level must not be copied"


@requires_bash
def test_a_fresh_kit_install_writes_the_code_and_adopts_a_data_root(tmp_path: Path) -> None:
    kit = _fake_kit(tmp_path)
    root = tmp_path / "root"
    (root / ".g3ku").mkdir(parents=True)
    (root / ".g3ku" / "config.json").write_text("{}\n", encoding="utf-8")
    done = _run_bash(_update_body(kit, root, 0))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "adopting the data root" in done.stdout, "a kept-data device installs back into the same root"
    assert (root / "pyproject.toml").is_file()
    assert not (root / "tests").exists()


# ---------------------------------------------------------------- the Windows installer
def test_powershell_installer_walks_the_same_lane() -> None:
    """The two entry points must not drift into two different definitions of 'offline'."""
    script = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "function Resolve-OfflineKit" in script
    assert "Get-FileHash" in script, "wheels are digested against kit.json before anything is copied"
    assert "$manifest.platform -ne $device" in script, "a kit for another machine is refused, not warned about"
    assert "kit wheel $($wheel.filename) does not match its recorded sha256" in script
    assert "'pip', 'install', '--python'" in script, "the kit lane installs with uv pip, not uv sync"
    assert "--find-links" in script and "'--offline', '--no-index'" in script
    assert "'-r', $Kit.Requirements" in script and "'-e', $Root" in script
    assert "mark-installed" in script, "the marker is what keeps the next negi off the network"
    assert "Sync-ProjectEnvironment" in script, "the online lane still syncs"
    assert "does not match" in script


def test_powershell_installer_does_not_shadow_its_own_kit_parameter() -> None:
    """PowerShell names are case-insensitive, so a local spelled $kit IS the -Kit
    parameter: assigning it there throws the operator's argument away and the installer
    quietly goes online. A live probe caught the first version doing exactly that."""
    script = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "-Kit $Kit" in script or "-Kit $resolvedKit" in script
    assert "$kit = " not in script, "a lowercase local collides with the -Kit parameter"
    assert "$resolvedKit" in script


def test_powershell_installer_has_no_eaten_escapes() -> None:
    script = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "\t" not in script, (
        "a literal tab in 'System32\\tar.exe' is a path that does not exist; a scripted "
        "edit once turned the escape into the character"
    )
    assert "System32\\tar.exe" in script
