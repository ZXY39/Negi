"""The offline kit's plan must be exactly satisfiable, per platform.

`packaging/offline_kit_plan.py` decides what a kit carries by reading uv.lock, so a
mistake here is a kit that installs fine in CI and fails on a device, or a kit that
carries every ABI variant on Earth. Measured history: filename-substring matching
turned a 35 MB win kit into 263 MB across 271 files, and `cp37` (one-digit minor) was
parsed as nothing at all, which pushed cryptography and psutil into the "build from
sdist" pile. These cases pin both shapes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "negi_offline_kit_plan", REPO_ROOT / "packaging" / "offline_kit_plan.py"
)
kit = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(kit)

PLATFORMS = ("win-x64", "linux-x64", "macos-arm64")


def _plan(platform: str) -> dict:
    return kit.plan(platform)


def test_wheel_tag_matching_covers_the_shapes_the_lock_actually_uses() -> None:
    # forward-compatible abi3, single-digit minor: this was silently rejected before
    assert kit.wheel_matches("cryptography-44.0.3-cp37-abi3-win_amd64.whl", "win-x64", (3, 12))
    assert kit.wheel_matches("cryptography-44.0.3-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                             "linux-x64", (3, 12))
    # exact interpreter match
    assert kit.wheel_matches("pywin32-311-cp312-cp312-win_amd64.whl", "win-x64", (3, 12))
    # same interpreter, wrong machine or wrong bitness
    assert not kit.wheel_matches("pywin32-311-cp312-cp312-win32.whl", "win-x64", (3, 12))
    assert not kit.wheel_matches("pywin32-311-cp312-cp312-win_arm64.whl", "win-x64", (3, 12))
    assert not kit.wheel_matches("pywin32-311-cp312-cp312-win_amd64.whl", "linux-x64", (3, 12))
    # a newer interpreter is not this kit's interpreter
    assert not kit.wheel_matches("psutil-7.2.2-cp313-cp313-win_amd64.whl", "win-x64", (3, 12))
    # free-threaded builds are excluded even when the minor matches
    assert not kit.wheel_matches("psutil-7.2.2-cp312-cp312t-macosx_11_0_arm64.whl", "macos-arm64", (3, 12))
    # pure python and multi-tag wheels
    assert kit.wheel_matches("zhconv-1.4.3-py3-none-any.whl", "linux-x64", (3, 12))
    assert kit.wheel_matches("pythonnet-3.2.1-cp311.cp312.cp313.cp314.cp315-none-any.whl", "win-x64", (3, 12))


def test_every_platform_plan_is_satisfiable_and_carries_one_file_per_package() -> None:
    for platform in PLATFORMS:
        result = _plan(platform)
        assert not result["missing"], f"{platform}: no artifact for {result['missing']}"
        names = [a["name"] for a in result["artifacts"]]
        assert len(names) == len(set(names)), f"{platform}: a package got several wheels (ABI inflation)"
        expected = result["packages"] - len(result["prebuild"])
        assert result["wheel_files"] == expected, f"{platform}: {result['wheel_files']} != {expected}"
        for entry in result["artifacts"] + result["prebuild"]:
            assert len(entry["sha256"]) == 64, f"{platform}: {entry['filename']} has no digest"
            assert entry["size"] > 0, f"{platform}: {entry['filename']} has no recorded size"
            assert entry["url"].startswith("https://"), f"{platform}: {entry['url']}"


def test_plans_stay_the_size_that_makes_a_kit_worth_shipping() -> None:
    """A ceiling, not a fingerprint: the bug this guards was a 7x inflation."""
    for platform in PLATFORMS:
        result = _plan(platform)
        megabytes = result["bytes_total"] / 1048576
        assert 15 < megabytes < 60, f"{platform}: {megabytes:.1f} MB is outside the plausible kit"


def test_windows_only_members_follow_the_markers() -> None:
    win = set(_plan("win-x64")["package_names"])
    linux = set(_plan("linux-x64")["package_names"])
    mac = set(_plan("macos-arm64")["package_names"])
    assert {"pywebview", "pywin32", "pythonnet", "clr-loader"} <= win
    for name in ("pywebview", "pywin32", "pythonnet", "clr-loader"):
        assert name not in linux and name not in mac, f"{name} leaked into a non-Windows kit"
    # the shared runtime is on every platform
    for name in ("fastapi", "uvicorn", "openai", "mcp", "pillow", "psutil", "cryptography"):
        assert name in win and name in linux and name in mac, f"{name} vanished from a plan"


def test_the_retired_browser_stack_is_not_planned_anywhere() -> None:
    gone = {"playwright", "greenlet", "pyee"}
    for platform in PLATFORMS:
        names = set(_plan(platform)["package_names"])
        assert not (names & gone), f"{platform} still plans {sorted(names & gone)}"
    assert "playwright" not in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_source_only_packages_are_named_for_prebuilding() -> None:
    win = {p["name"] for p in _plan("win-x64")["prebuild"]}
    linux = {p["name"] for p in _plan("linux-x64")["prebuild"]}
    assert "zhconv" in win and "zhconv" in linux
    assert "proxy-tools" in win, "proxy-tools is Windows-only and ships no wheel; it must be built"
    assert "proxy-tools" not in linux
    # a package that ships a wheel must never land in the prebuild pile
    for platform in PLATFORMS:
        result = _plan(platform)
        wheel_names = {a["name"] for a in result["artifacts"]}
        prebuild_names = {p["name"] for p in result["prebuild"]}
        assert not (wheel_names & prebuild_names), f"{platform}: {wheel_names & prebuild_names}"


def test_lock_digest_is_recorded_from_the_lock_that_was_planned(tmp_path: Path) -> None:
    """A kit is bound to a lock; the digest is how the installer proves it."""
    result = _plan("win-x64")
    assert result["lock_sha256"] == hashlib.sha256((REPO_ROOT / "uv.lock").read_bytes()).hexdigest()
    copied = tmp_path / "uv.lock"
    copied.write_bytes((REPO_ROOT / "uv.lock").read_bytes())
    assert kit.plan("win-x64", lock_path=copied)["lock_sha256"] == result["lock_sha256"]
    copied.write_text(copied.read_text(encoding="utf-8") + '\n[[package]]\nname = "stray"\nversion = "0"\n', encoding="utf-8")
    assert kit.plan("win-x64", lock_path=copied)["lock_sha256"] != result["lock_sha256"]


def test_cli_reports_a_satisfiable_plan_and_fails_on_an_empty_closure(tmp_path: Path) -> None:
    out = tmp_path / "plan.json"
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "packaging" / "offline_kit_plan.py"),
         "--platform", "win-x64", "--json", str(out), "--quiet"],
        check=True,
        cwd=str(REPO_ROOT),
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["packages"] > 50 and not payload["missing"]
    empty = tmp_path / "empty.lock"
    empty.write_text('version = 1\n', encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "packaging" / "offline_kit_plan.py"),
         "--platform", "win-x64", "--lock", str(empty), "--quiet"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert failed.returncode != 0, "an empty plan must not look like a successful one"


def test_builder_plans_and_snapshots_from_the_same_ref() -> None:
    """The kit's source and its wheel list must come from one ref, or they drift."""
    script = (REPO_ROOT / "packaging" / "build-offline-kit.ps1").read_text(encoding="utf-8")
    assert "--lock', $refLock" in script, "the planner must read the ref's uv.lock"
    assert "Get-RefFileText -Ref $Ref -Path 'uv.lock'" in script
    assert "Write-Utf8NoBom" in script, "Set-Content -Encoding UTF8 writes a BOM tomllib rejects"
    assert "System32\\tar.exe" in script, "bsdtar must be pinned; Git tar reads C: as a remote host"
    assert "Get-RefFileText -Ref $Ref -Path '.python-version'" in script, "the interpreter pin is the ref's too"
    assert "':(exclude)'" in script, "the snapshot must apply the exclusion list"
    assert "'python', 'install', $pin, '--install-dir'" in script, (
        "the interpreter must be installed into the kit; `uv python find` inside a "
        "project resolves that project's .venv, which is a scripts folder"
    )
    # guard the call form, not the prose: the comment explaining why find() is wrong
    # would otherwise trip a ban on the words themselves
    assert "python', 'find'" not in script and "$uv python find" not in script, (
        "find() resolves a project .venv; a kit runtime must come from --install-dir"
    )
    assert "--no-bin" in script, "a kit build must not drop a python shim into ~/.local/bin"
    assert "[Environment]::OSVersion.Version.Platform" not in script, (
        "Windows PowerShell 5.1 has no such property and Set-StrictMode throws; use $env:OS"
    )
    assert "$pythonRel" in script and "$uvName" in script, (
        "manifest paths must be computed, not guessed from whether a name already exists"
    )
    assert "function Remove-Tree" in script and "robocopy" in script, (
        "the copied CPython tree has paths past MAX_PATH; Remove-Item cannot delete them"
    )
    assert "Remove-Item -LiteralPath $work -Recurse" not in script, "use Remove-Tree for the work dir"
    assert "-Include 'python.exe'" not in script, (
        "-Include stops filtering with -LiteralPath; the interpreter name test belongs in code"
    )
    assert "$_.Name -in @('python.exe'" in script, "interpreter discovery must filter by name itself"
    assert "--version" in script, (
        "the candidate interpreter must be run: CPython ships a venv seed stub at "
        "Lib/venv/scripts/nt/python.exe that sorts before the real one"
    )
    assert "'sync', '--frozen', '--offline', '--no-index'" in script, "offline is the whole point"
    assert "import main.protocol" in script, "the self-check has to import something real"
    assert "Push-Location $proj" in script, (
        "main/ is a top-level directory, not part of the editable install: the "
        "import check has to run from the installed project, like a device does"
    )
    assert "$tarExe, '-a', '-cf'" in script, "the zip step must call the pinned bsdtar"
    assert "	" not in script, "a literal tab means an escape was eaten while writing this file"


def _build_deps_module():
    spec = importlib.util.spec_from_file_location(
        "negi_offline_build_deps", REPO_ROOT / "packaging" / "offline_build_deps.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_backend_pins_are_resolved_and_cannot_silently_miss() -> None:
    """uv.lock omits [build-system] requires, so the kit needs its own resolution."""
    deps = _build_deps_module()
    pins = deps.parse_pins(
        (REPO_ROOT / "packaging" / "build-requires.txt").read_text(encoding="utf-8")
    )
    names = {name for name, _ in pins}
    assert {"hatchling", "editables"} <= names, "the project's own backend must be in the set"
    for name, version in pins:
        assert version, f"{name} is unpinned: an offline kit cannot resolve at install time"

    release = {"urls": [
        {"packagetype": "bdist_wheel", "filename": "thing-1.0-py3-none-any.whl",
         "url": "https://files.pythonhosted.org/thing-1.0-py3-none-any.whl",
         "digests": {"sha256": "a" * 64}, "size": 10},
        {"packagetype": "sdist", "filename": "thing-1.0.tar.gz",
         "url": "https://files.pythonhosted.org/thing-1.0.tar.gz",
         "digests": {"sha256": "b" * 64}, "size": 10},
    ]}
    assert deps.choose_file(release, "thing", "1.0", "win-x64")["filename"].endswith("py3-none-any.whl")
    only_source = {"urls": [release["urls"][1]]}
    for platform in ("win-x64", "linux-x64", "macos-arm64"):
        try:
            deps.choose_file(only_source, "thing", "1.0", platform)
        except LookupError:
            continue
        raise AssertionError(f"{platform}: an sdist-only pin must be refused, not shipped")


def test_builder_fetches_the_build_backend_from_the_same_ref() -> None:
    script = (REPO_ROOT / "packaging" / "build-offline-kit.ps1").read_text(encoding="utf-8")
    assert "offline_build_deps.py" in script, "the kit must carry the build backend"
    assert "Get-RefFileText -Ref $Ref -Path 'packaging/build-requires.txt'" in script
    assert "build_backend" in script, "kit.json has to record the backend digests"


def test_kit_artifacts_stay_untracked() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(line.strip() in ("dist/", "/dist/") for line in ignored), (
        "kits are build output; committing one would put 35 MB of wheels in the repo"
    )
