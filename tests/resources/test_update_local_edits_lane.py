"""The swap-away-from-a-dirty-tree lane: `g3ku/update_local_edits.py`.

Both installer lanes call this module instead of describing the loss in log text only. These
cases pin what the settings row and the "hand it to Negi" button are built on:

- the copy lane (Setup.exe / offline kit, no `.git`) claims *only* files this device edited,
  decided by the installed-tree fingerprint -- never the files the release itself changed;
- with no fingerprint yet (first upgrade after a fresh install) it claims nothing, because a
  false "your edits were overwritten" list is worse than silence;
- the git lane reverts *only* the genuinely unmerged paths, so the edits that did not conflict
  stay in the tree (the old whole-tree `reset --hard` wiped those too), and it leaves no
  unmerged index entry and no conflict markers for a live process to pick up;
- modify/delete is resolved toward the release while the device's version is still readable as
  a file, not only as patch text;
- the batch becomes ledger fields on the apply record, and only for *this* upgrade.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from g3ku import update_apply
from g3ku import update_local_edits as u


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=probe", "-c", "user.email=probe@localhost", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def device(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "device"
    root.mkdir()
    return {"root": root, "data": tmp_path / "data"}


# ---- 拷贝车道（没有 .git 的设备）--------------------------------------------


def test_copy_lane_claims_only_files_the_device_edited(device: dict[str, Path]) -> None:
    root, data = device["root"], device["data"]
    _write(root / "tools" / "foo" / "tool.py", "INSTALLED")
    _write(root / "tools" / "foo" / "other.py", "INSTALLED-OTHER")
    _write(root / "tools" / "foo" / "untouched.py", "SAME")
    assert u.record_tree(root, "v1", data_root=data) == 3

    # The device owner edits one file; the next release edits two (one of them the same file).
    _write(root / "tools" / "foo" / "tool.py", "DEVICE FIX")
    release = root.parent / "release"
    _write(release / "tools" / "foo" / "tool.py", "RELEASE NEW")
    _write(release / "tools" / "foo" / "other.py", "RELEASE REFACTORED")
    _write(release / "tools" / "foo" / "untouched.py", "SAME")

    record = u.preserve_copy(root, release, "v2", data_root=data)

    assert record is not None
    assert record["lane"] == "copy-overwrite"
    # `other.py` differs too, but the device never touched it -- listing it would tell the
    # operator his edit was overwritten when it was the release that changed the file.
    assert record["paths"] == ["tools/foo/tool.py"]
    saved = Path(record["batch_dir"]) / "overwritten" / "tools" / "foo" / "tool.py"
    assert saved.read_text(encoding="utf-8") == "DEVICE FIX"


def test_copy_lane_without_fingerprint_claims_nothing(device: dict[str, Path]) -> None:
    root, data = device["root"], device["data"]
    _write(root / "tools" / "foo" / "tool.py", "DEVICE FIX")
    release = root.parent / "release"
    _write(release / "tools" / "foo" / "tool.py", "RELEASE NEW")

    assert u.preserve_copy(root, release, "v2", data_root=data) is None
    assert u.list_batches(data) == []


def test_record_skips_the_environment_and_operator_data(device: dict[str, Path]) -> None:
    root, data = device["root"], device["data"]
    _write(root / "g3ku" / "app.py", "code")
    _write(root / ".venv" / "Lib" / "site-packages" / "dep.py", "third party")
    _write(root / ".g3ku" / "sessions" / "one.json", "operator data")
    _write(root / "g3ku" / "__pycache__" / "app.cpython-312.pyc", "byte")

    assert u.record_tree(root, "v1", data_root=data) == 1
    ledger = u.read_tree_ledger(data)
    assert list(ledger) == ["g3ku/app.py"]


# ---- git 车道 --------------------------------------------------------------


def _git_device(device: dict[str, Path]) -> tuple[Path, Path]:
    """One repo holding two releases; the device itself is parked on v1.

    v2 edits `notes.md` and deletes `app.py`, and never touches `hist.txt` -- that is what
    makes "one conflicting file in a batch of three" reproducible instead of theoretical.
    """
    root = device["root"]
    data = device["data"]
    _git(root, "init", "-q", ".")
    _write(root / "app.py", "line1\nline2\nline3\n")
    _write(root / "notes.md", "notes A\nnotes B\n")
    _write(root / "hist.txt", "seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "v1")
    _git(root, "tag", "v1")
    _git(root, "rm", "-q", "app.py")
    _write(root / "notes.md", "notes A\nnotes B-RELEASE\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "v2")
    _git(root, "tag", "v2")
    assert _git(root, "checkout", "-q", "--detach", "v1").returncode == 0
    return root, data


def _park_conflict(root: Path) -> None:
    """Leave the tree exactly where a failed `stash pop` leaves it after the swap."""
    _write(root / "app.py", "line1\nline2-DEVICE\nline3\n")
    _write(root / "notes.md", "notes A\nnotes B-DEVICE\n")
    _write(root / "hist.txt", "seed\nDEVICE LOCAL\n")
    assert _git(root, "stash", "push", "-q", "-m", "negi-upgrade-test").returncode == 0
    assert _git(root, "checkout", "-q", "--detach", "v2").returncode == 0
    assert _git(root, "stash", "pop", "-q").returncode != 0


def test_git_lane_reverts_only_unmerged_paths_and_keeps_the_rest(device: dict[str, Path]) -> None:
    root, data = _git_device(device)
    _park_conflict(root)
    assert (_git(root, "ls-files", "-u").stdout or "").strip() != ""

    record = u.preserve_git(root, "v2", "negi-upgrade-test", data_root=data)

    assert record is not None
    assert record["lane"] == "merge-conflict"
    assert sorted(record["paths"]) == ["app.py", "notes.md"]
    # Nothing unmerged may survive: this tree is about to be handed to a live process.
    assert (_git(root, "ls-files", "-u").stdout or "").strip() == ""
    assert _git(root, "grep", "-l", "-e", "<<<<<<<").returncode != 0
    # The edit that did not conflict stays in the tree (the old reset --hard dropped it).
    assert (root / "hist.txt").read_text(encoding="utf-8").endswith("DEVICE LOCAL\n")
    # The file the release deleted follows the release, and the device's version stays readable.
    assert not (root / "app.py").exists()
    saved = Path(record["batch_dir"]) / "overwritten" / "app.py"
    assert saved.read_text(encoding="utf-8") == "line1\nline2-DEVICE\nline3\n"
    # The stash is never dropped, and the patch copy exists next to the manifest.
    assert len((_git(root, "stash", "list").stdout or "").splitlines()) == 1
    assert (Path(record["batch_dir"]) / "local-edits.patch").is_file()


def test_clean_pop_leaves_no_batch(device: dict[str, Path]) -> None:
    root, data = _git_device(device)
    _write(root / "hist.txt", "seed\nDEVICE LOCAL\n")
    _git(root, "stash", "push", "-q", "-m", "negi-upgrade-test")
    _git(root, "checkout", "-q", "--detach", "v2")
    assert _git(root, "stash", "pop", "-q").returncode == 0
    # Restore-TrackedChanges only calls the module when pop failed; a clean pop writes nothing.
    assert u.list_batches(data) == []


# ---- 批次对象与保留 --------------------------------------------------------


def test_batches_are_bounded_to_the_newest_three(device: dict[str, Path]) -> None:
    data = device["data"]
    for index in range(6):
        stamp = f"2025{index + 1:02d}01T000000Z"
        batch = data / u.CONFLICTS_DIRNAME / stamp
        u.write_manifest(batch, lane="copy-overwrite", ref="v", paths=[f"f{index}.py"], at=stamp)

    assert u.prune(keep=3, data_root=data) == 3
    assert [entry.name for entry in u.list_batches(data)] == [
        "20250401T000000Z",
        "20250501T000000Z",
        "20250601T000000Z",
    ]


def test_batch_without_paths_is_not_rendered(device: dict[str, Path]) -> None:
    data = device["data"]
    batch = data / u.CONFLICTS_DIRNAME / "20250101T000000Z"
    u.write_manifest(batch, lane="merge-conflict", ref="v", paths=[], at="2025-01-01T00:00:00Z")

    assert u.read_latest(data_root=data) is None
    assert u.read_batch(batch) is None


def test_read_latest_skips_batches_from_an_earlier_upgrade(device: dict[str, Path]) -> None:
    data = device["data"]
    batch = data / u.CONFLICTS_DIRNAME / "20250101T000000Z"
    u.write_manifest(batch, lane="merge-conflict", ref="v9", paths=["a.py"], at="2025-01-01T00:00:00Z")

    assert u.read_latest(data_root=data) is not None
    # A batch older than this run belongs to a previous upgrade: it stays on disk, but it must
    # not be announced again as if this click had just lost it.
    assert u.read_latest(since="2026-01-01T00:00:00Z", data_root=data) is None


# ---- 台账落点 --------------------------------------------------------------


def test_apply_ledger_carries_the_conflict_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".g3ku").mkdir()
    batch = tmp_path / ".g3ku" / u.CONFLICTS_DIRNAME / "20260101T000000Z"
    u.write_manifest(
        batch,
        lane="merge-conflict",
        ref="v2",
        paths=["tools/foo/tool.py", "notes.md"],
        stash_label="negi-upgrade-x",
        at="2026-01-01T00:00:00Z",
    )
    monkeypatch.chdir(tmp_path)

    extra = update_apply._conflict_extra("2026-01-01T00:00:00Z")

    assert extra is not None
    assert extra["conflict_count"] == 2
    assert extra["conflict_paths"] == ["tools/foo/tool.py", "notes.md"]
    assert extra["conflict_lane"] == "merge-conflict"
    assert extra["conflict_stash"] == "negi-upgrade-x"
    assert extra["conflict_at"] == "2026-01-01T00:00:00Z"

    update_apply._record("ok", "v2", "relaunched", stage="ok", extra=extra)
    from g3ku import update_check

    written = update_check.read_apply_result()
    assert written is not None
    assert written["outcome"] == "ok"
    assert written["conflict_count"] == 2
    assert written["conflict_paths"] == ["tools/foo/tool.py", "notes.md"]


def test_no_batch_means_no_conflict_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".g3ku").mkdir()
    monkeypatch.chdir(tmp_path)

    assert update_apply._conflict_extra("2026-01-01T00:00:00Z") is None
