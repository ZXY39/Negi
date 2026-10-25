"""The installed copy carries the runtime tree, not the development tree.

`packaging/distribution-excludes.txt` is the single list both one-line installers
read out of the payload they just fetched. These cases pin the three things that
make that safe: the list stays a set of top-levels nothing on a device reads, both
lanes actually consume it, and the exclusion is expressed as sparse-checkout rules
rather than deleted files (a delete would show up as tracked changes and
`-Upgrade`'s first guard is "no tracked changes").
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIST_FILE = REPO_ROOT / "packaging" / "distribution-excludes.txt"
INSTALLERS = (REPO_ROOT / "install.ps1", REPO_ROOT / "install.sh")

# Read the list the same way the installers do: blank lines and #-comments gone,
# every remaining line a single top-level name (the shipped validators refuse
# anything else, so this file refuses it too).
def _read_list(path: Path) -> list[str]:
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Za-z0-9._-]+", line) and ".." not in line, (
            f"{line!r} is not a single top-level name"
        )
        names.append(line)
    return names


# Directories the runtime reads at install time are the ones the list may not name:
# docs/ is what skills/negi-architecture-maintenance works on, scripts/ is named by
# AGENTS.md, and install.sh builds the desktop icon out of assets/icon.png.
MUST_STAY_SHIPPED = {"docs", "scripts", "assets"}

# Everything below is pointed at only by development tooling.
CODE_SUFFIXES = {".py", ".js", ".ps1", ".sh", ".iss", ".yml", ".yaml"}
CODE_TREES = ("g3ku", "main", "bridges", "packaging", "docker")


def test_list_is_a_pinned_set_of_zero_reader_top_levels() -> None:
    assert LIST_FILE.is_file(), "the installers read their exclusion list from this file"
    names = set(_read_list(LIST_FILE))
    assert names == {"tests", "work", ".github"}, (
        "adding a name needs the same proof the existing three have: nothing on a "
        "device reads it and no shipped text points at it"
    )
    assert not (names & MUST_STAY_SHIPPED), f"{names & MUST_STAY_SHIPPED} are read on a device"


def test_every_listed_name_is_a_tracked_top_level() -> None:
    tracked = {
        line.split("/", 1)[0]
        for line in subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.splitlines()
        if line
    }
    for name in _read_list(LIST_FILE):
        assert name in tracked, f"{name} is excluded but not tracked, so the list is stale"


def _code_lines(path: Path):
    """Yield (line number, text) for lines that are not whole-line comments.

    Naming a test file in prose is not the same as reading that path at runtime, and
    four shipped modules do exactly that (their equivalence notes point at
    `tests/resources/...`). Only lines that can execute count as readers.
    """
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        stripped = line.strip()
        if stripped.startswith(("#", "//", "/*", "* ")):
            continue
        yield lineno, line


def test_no_runtime_code_reads_an_excluded_top_level() -> None:
    names = _read_list(LIST_FILE)
    offenders: list[str] = []
    for tree in CODE_TREES:
        for path in (REPO_ROOT / tree).rglob("*"):
            if path.suffix not in CODE_SUFFIXES or not path.is_file():
                continue
            for lineno, line in _code_lines(path):
                for name in names:
                    if re.search(rf"(?<![\w.-]){re.escape(name)}[/\\]", line):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{lineno} -> {name}/: {line.strip()[:80]}"
                        )
    for installer in INSTALLERS:
        for lineno, line in _code_lines(installer):
            for name in names:
                if re.search(rf"(?<![\w.-]){re.escape(name)}[/\\]", line):
                    offenders.append(f"{installer.name}:{lineno} -> {name}/")
    assert not offenders, "an excluded path has a reader: " + ", ".join(offenders)


def test_both_installers_read_the_list_from_the_payload() -> None:
    for installer in INSTALLERS:
        text = installer.read_text(encoding="utf-8", errors="replace")
        assert "packaging/distribution-excludes.txt" in text, (
            f"{installer.name} no longer consumes the list, so it silently ships the "
            "development tree again"
        )
        assert "sparse" in text, f"{installer.name} lost its git-lane exclusion step"


def test_exclusion_leaves_the_device_repository_clean(tmp_path: Path) -> None:
    """The whole point of sparse rules over deleting: git still believes the tree.

    `install.ps1` and `install.sh` both refuse to upgrade a checkout with tracked
    changes, so an exclusion that reads as deleted files would lock every device
    installed by this change out of all later upgrades.
    """
    git = shutil.which("git")
    if not git:
        pytest.skip("git is required to install Negi in the first place")

    origin = tmp_path / "origin"
    origin.mkdir()
    for rel in ("g3ku/app.py", "tests/test_a.py", "docs/read.md"):
        target = origin / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("payload\n", encoding="utf-8")
    def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [git, "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

    for args in (
        ["init", "-q", "--initial-branch=main", "."],
        ["config", "user.name", "lane test"],
        ["config", "user.email", "lane@test.local"],
        ["add", "-A"],
        ["commit", "-q", "-m", "seed"],
        ["tag", "-a", "v9.9.9", "-m", "seed tag"],
    ):
        run(*args, cwd=origin)

    stage = tmp_path / "stage"
    subprocess.run(
        [git, "clone", "--depth", "1", "--branch", "v9.9.9", "--no-checkout", str(origin), str(stage)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    patterns = ["/*"] + [f"!/{name}/" for name in _read_list(LIST_FILE)]
    run("sparse-checkout", "set", "--no-cone", *patterns, cwd=stage)
    run("checkout", cwd=stage)

    assert (stage / "g3ku" / "app.py").is_file()
    assert (stage / "docs" / "read.md").is_file()
    for name in _read_list(LIST_FILE):
        assert not (stage / name).exists(), f"{name} should not be materialized"

    # Exactly the guards -Upgrade runs before it touches anything.
    assert run("diff", "--quiet", cwd=stage).returncode == 0
    assert run("diff", "--cached", "--quiet", cwd=stage).returncode == 0
    assert run("status", "--porcelain", cwd=stage).stdout.strip() == ""

    # A device keeps what it already has: an excluded path that exists on disk is not
    # deleted by the exclusion. Only newly fetched content stays out of the tree.
    (stage / "work").mkdir()
    (stage / "work" / "operator-script.py").write_text("keep me\n", encoding="utf-8")

    # A release after this one changes both an excluded path and a shipped one; the
    # upgrade checkout must take the code and still not materialize tests/.
    (origin / "g3ku" / "app.py").write_text("payload v2\n", encoding="utf-8")
    (origin / "tests" / "test_a.py").write_text("more tests\n", encoding="utf-8")
    run("add", "-A", cwd=origin)
    run("commit", "-q", "-m", "second", cwd=origin)
    run("tag", "-a", "v9.9.10", "-m", "second tag", cwd=origin)
    run("fetch", "--depth", "1", "origin", "refs/tags/v9.9.10:refs/tags/v9.9.10", cwd=stage)
    run("checkout", "--detach", "refs/tags/v9.9.10", cwd=stage)

    assert (stage / "g3ku" / "app.py").read_text(encoding="utf-8") == "payload v2\n"
    assert not (stage / "tests").exists(), "the upgrade re-materialized an excluded path"
    assert (stage / "work" / "operator-script.py").is_file(), "the device lost a local file"
    assert run("status", "--porcelain", "--untracked-files=no", cwd=stage).stdout.strip() == ""


def _bash_function(name: str) -> str:
    """Return the shipped body of a shell function, brace-depth independent.

    Feeding the lane through the real function text is the point: a rewritten copy of
    the skip logic in this file would prove nothing about what the installer does.
    install.sh closes every function with `}` in column 0, which is where the slice ends.
    """
    lines = (REPO_ROOT / "install.sh").read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(f"{name}()")), None)
    assert start is not None, f"{name}() is gone from install.sh"
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if line == "}":
            body.append(line)
            return "\n".join(body) + "\n"
        body.append(line)
    raise AssertionError(f"unterminated {name}() in install.sh")


def test_archive_lane_skips_the_listed_top_levels(tmp_path: Path) -> None:
    """The source-archive lane has no index to keep honest, so it skips at merge time."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is only present where the shell installer can run")

    source = tmp_path / "checkout"
    target = tmp_path / "install"
    target.mkdir()
    for rel in (
        "g3ku/app.py",
        "docs/read.md",
        "tests/test_a.py",
        "work/scratch.py",
        ".github/workflows/ci.yml",
    ):
        leaf = source / rel
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("payload\n", encoding="utf-8")
    # The lane reads the shipped list itself, so the fixture carries the real one.
    list_copy = source / "packaging" / "distribution-excludes.txt"
    list_copy.parent.mkdir(parents=True, exist_ok=True)
    list_copy.write_text(LIST_FILE.read_text(encoding="utf-8"), encoding="utf-8")

    script = (
        "set -euo pipefail\n"
        # The two helpers the real installer calls around this function.
        + "fail() { printf 'FAIL: %s\\n' \"$1\" >&2; exit 1; }\n"
        + "log() { :; }\n"
        + _bash_function("exclude_names_from_stream")
        + _bash_function("is_excluded_top_level")
        + _bash_function("copy_checkout_into_root")
        + f"CHECKOUT_DIR='{source.as_posix()}'\n"
        + f"DIR='{target.as_posix()}'\n"
        + "PROTECTED_ENTRIES='.venv .g3ku .git'\n"
        + "copy_checkout_into_root 0\n"
        + f"find '{target.as_posix()}' -mindepth 1 -maxdepth 1 -printf '%f\\n' | sort\n"
    )
    listing = subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert listing.returncode == 0, listing.stderr
    landed = {line for line in listing.stdout.splitlines() if line}
    assert landed == {"g3ku", "packaging", "docs"}, landed
    for name in _read_list(LIST_FILE):
        assert not (target / name).exists(), f"{name} reached the device through the archive lane"


def test_sparse_patterns_reach_git_as_one_quoted_array() -> None:
    """`/*` unquoted is a glob, and bash expands it against the current directory.

    On a Linux device that turns the rule set into `/bin /etc /usr ...`, so the
    checkout materializes nothing but `.git`. The array form is the only shape that
    survives, so the guard is textual.
    """
    text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    lane = text[text.index("stage_code() {") : text.index("copy_checkout_into_root() {")]
    assert 'local -a patterns' in lane, "patterns must be an array"
    assert '"${patterns[@]}"' in lane, "the array must expand quoted, not bare"
    assert "sparse-checkout set --no-cone $patterns" not in lane


def test_the_shipped_validator_refuses_a_nested_name(tmp_path: Path) -> None:
    """A name with a separator would become a sparse pattern for a subpath.

    The installers are asked to stop rather than guess which subset was meant.
    """
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is only present where the shell installer can run")
    script = (
        "set -euo pipefail\n"
        + _bash_function("exclude_names_from_stream")
        + "printf '# comment\\ntests\\nsources/api\\n' | exclude_names_from_stream\n"
    )
    result = subprocess.run([bash, "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode != 0, result.stdout
    assert "not a single top-level name" in result.stderr, result.stderr
    assert "tests" in result.stdout  # the good name is accepted before the bad one stops it


def test_git_lane_stages_the_trimmed_tree(tmp_path: Path) -> None:
    """Execute install.sh's own `stage_code` against a local origin.

    This is the shell installer's git lane: `--no-checkout`, the shipped list read out
    of the fetched commit, sparse rules, then `checkout`. Running the real function
    text (rather than a restatement) is the point - the awk validator bug was only
    visible this way.
    """
    bash = shutil.which("bash")
    git = shutil.which("git")
    if not bash or not git:
        pytest.skip("this lane needs both bash and git on PATH")
    if sys.platform == "win32":
        pytest.skip(
            "install.sh targets Linux and macOS, and Git-for-Windows bash rewrites both "
            "the leading-slash sparse patterns and the staging paths, so running it here "
            "measures the emulator rather than the script. No job executes it today: "
            "ci.yml runs pytest on windows-latest only, and its ubuntu job is lint."
        )

    origin = tmp_path / "origin"
    origin.mkdir()
    for rel in ("g3ku/app.py", "README.md", "tests/test_a.py", "work/scratch.py", ".github/ci.yml"):
        leaf = origin / rel
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("payload\n", encoding="utf-8")
    shipped_list = origin / "packaging" / "distribution-excludes.txt"
    shipped_list.parent.mkdir(parents=True, exist_ok=True)
    shipped_list.write_text(LIST_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    for args in (
        ["init", "-q", "--initial-branch=main", "."],
        ["config", "user.name", "lane test"],
        ["config", "user.email", "lane@test.local"],
        ["add", "-A"],
        ["commit", "-q", "-m", "seed"],
        ["tag", "-a", "v1.0.0", "-m", "seed"],
    ):
        subprocess.run([git, "-C", str(origin), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)

    script = (
        "set -euo pipefail\n"
        + "fail() { printf 'FAIL: %s\\n' \"$1\" >&2; exit 1; }\n"
        + "log() { :; }\n"
        + _bash_function("exclude_names_from_stream")
        + _bash_function("stage_code")
        + f"REPO_GIT='{origin.as_posix()}'\n"
        + "REF='v1.0.0'\n"
        + "stage_code\n"
        + 'printf "CHECKOUT=%s\\n" "$CHECKOUT_DIR"\n'
        + 'find "$CHECKOUT_DIR" -mindepth 1 -maxdepth 1 -not -name .git -printf "%f\\n" | sort\n'
    )
    # Git-for-Windows bash rewrites any argv word that starts with `/` into a Windows
    # path, so `!/tests/` would reach git as `!C:/Program Files/Git/tests/`. install.sh
    # runs on Linux and macOS, where no such conversion happens; disabling it here is
    # what makes the harness speak the shipped script's real argv instead of this box's.
    env = {**os.environ, "MSYS2_ARG_CONV_EXCL": "*", "MSYS_NO_PATHCONV": "1"}
    result = subprocess.run([bash, "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert result.returncode == 0, result.stderr
    staged = {line for line in result.stdout.splitlines() if line and not line.startswith("CHECKOUT=")}
    assert staged == {"g3ku", "README.md", "packaging"}, staged
    """Back-compat: `-Ref v2.6.0` on a new installer must not fail or half-install.

    The list travels inside the payload, so a release predating it simply has no
    list to read, and the git lane falls through to a plain checkout.
    """
    git = shutil.which("git")
    if not git:
        pytest.skip("git is required to install Negi in the first place")
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "tests").mkdir()
    (origin / "tests" / "test_a.py").write_text("payload\n", encoding="utf-8")
    for args in (
        ["init", "-q", "--initial-branch=main", "."],
        ["config", "user.name", "lane test"],
        ["config", "user.email", "lane@test.local"],
        ["add", "-A"],
        ["commit", "-q", "-m", "before the list"],
        ["tag", "-a", "v0.0.1", "-m", "before the list"],
    ):
        subprocess.run([git, "-C", str(origin), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)

    stage = tmp_path / "stage"
    subprocess.run(
        [git, "clone", "--depth", "1", "--branch", "v0.0.1", "--no-checkout", str(origin), str(stage)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    # This is what Get-CheckoutStage / stage_code do when the list file is absent:
    # no sparse rules at all, then a plain checkout.
    absent = subprocess.run(
        [git, "-C", str(stage), "cat-file", "-e", "HEAD:packaging/distribution-excludes.txt"],
        capture_output=True,
        text=True,
    )
    assert absent.returncode != 0
    subprocess.run([git, "-C", str(stage), "checkout"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    assert (stage / "tests" / "test_a.py").is_file()
    assert (
        subprocess.run(
            [git, "-C", str(stage), "status", "--porcelain"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
        ).stdout.strip()
        == ""
    )
