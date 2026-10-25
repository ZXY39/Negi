"""The installed copy carries the runtime tree, not the development tree.

`packaging/distribution-excludes.txt` is the single list both one-line installers
read out of the payload they just fetched. These cases pin the three things that
make that safe: the list stays a set of top-levels nothing on a device reads, both
lanes actually consume it, and the exclusion is expressed as sparse-checkout rules
rather than deleted files (a delete would show up as tracked changes and
`-Upgrade`'s first guard is "no tracked changes").
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIST_FILE = REPO_ROOT / "packaging" / "distribution-excludes.txt"
INSTALLERS = (REPO_ROOT / "install.ps1", REPO_ROOT / "install.sh")

# Read the list the same way the installers do: blank lines and #-comments gone,
# every remaining line a single top-level name.
def _read_list(path: Path) -> list[str]:
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert not re.search(r"[/\\]|\.\.", line), f"{line!r} is not a top-level name"
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


def test_a_tag_older_than_the_list_installs_the_full_tree(tmp_path: Path) -> None:
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
        subprocess.run([git, "-C", str(origin), *args], capture_output=True, text=True, check=True)

    stage = tmp_path / "stage"
    subprocess.run(
        [git, "clone", "--depth", "1", "--branch", "v0.0.1", "--no-checkout", str(origin), str(stage)],
        capture_output=True,
        text=True,
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
    subprocess.run([git, "-C", str(stage), "checkout"], capture_output=True, text=True, check=True)
    assert (stage / "tests" / "test_a.py").is_file()
    assert (
        subprocess.run(
            [git, "-C", str(stage), "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
        == ""
    )
