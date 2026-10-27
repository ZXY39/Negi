"""jail 内文件助手：根白名单、symlink、结构化入口。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import main  # noqa: F401  (production import order)
from g3ku.sandbox import fsop


def _roots(tmp_path: Path) -> tuple[Path, ...]:
    root = tmp_path / "workspace"
    root.mkdir()
    return (root.resolve(),)


def test_read_write_replace_roundtrip(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    base = roots[0] / "note.txt"

    written = fsop.run({"op": "write", "path": str(base), "content": "a\nb\nc\n"}, roots=roots)
    assert written["ok"] and written["created"] is True

    read = fsop.run({"op": "read", "path": str(base), "start_line": 2, "end_line": 3}, roots=roots)
    assert read["ok"]
    assert read["text"] == "b\nc"
    assert read["line_count"] == 3

    replaced = fsop.run({"op": "replace", "path": str(base), "old": "b", "new": "B"}, roots=roots)
    assert replaced["ok"] and replaced["replacements"] == 1
    assert (roots[0] / "note.txt").read_text(encoding="utf-8").startswith("a\nB")


def test_outside_root_is_denied(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    outside = tmp_path / "host-secret.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(fsop.FsOpError) as denied:
        fsop.run({"op": "read", "path": str(outside)}, roots=roots)
    assert denied.value.kind == fsop.KIND_DENIED

    with pytest.raises(fsop.FsOpError):
        fsop.run({"op": "write", "path": str(outside), "content": "x"}, roots=roots)


def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = roots[0] / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not available on this platform")
    with pytest.raises(fsop.FsOpError) as denied:
        fsop.run({"op": "read", "path": str(link)}, roots=roots)
    assert denied.value.kind == fsop.KIND_DENIED


def test_list_stat_search(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    base = roots[0]
    (base / "sub").mkdir()
    (base / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (base / "sub" / "b.txt").write_text("gamma\n", encoding="utf-8")

    listed = fsop.run({"op": "list", "path": str(base)}, roots=roots)
    names = [entry["name"] for entry in listed["entries"]]
    assert names == ["a.txt", "sub"]

    stat = fsop.run(
        {"op": "stat", "paths": [str(base / "a.txt"), str(base / "missing.txt")]},
        roots=roots,
    )
    assert stat["items"][0]["exists"] is True
    assert stat["items"][1]["exists"] is False

    searched = fsop.run({"op": "search", "path": str(base / "a.txt"), "query": "beta"}, roots=roots)
    assert searched["matches"] == [{"line": 2, "text": "beta"}]

    regexed = fsop.run(
        {"op": "search", "path": str(base / "a.txt"), "query": "^al", "regex": True},
        roots=roots,
    )
    assert len(regexed["matches"]) == 1


def test_unknown_op_and_bad_json_are_invalid(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    with pytest.raises(fsop.FsOpError) as invalid:
        fsop.run({"op": "chmod", "path": str(roots[0])}, roots=roots)
    assert invalid.value.kind == fsop.KIND_INVALID


def test_cli_entrypoint_speaks_json(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "cli.txt"
    request = json.dumps({"op": "write", "path": str(target), "content": "hi"})
    completed = subprocess.run(
        [sys.executable, "-m", "g3ku.sandbox.fsop", "--root", str(root)],
        input=request.encode("utf-8"),
        capture_output=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[1],
    )
    # 助手走字节流：父进程也按 UTF-8 读，别用 locale 编码
    payload = json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])
    assert payload["ok"] is True
    assert target.read_text(encoding="utf-8") == "hi"

    denied = subprocess.run(
        [sys.executable, "-m", "g3ku.sandbox.fsop"],
        input=json.dumps({"op": "read", "path": str(target)}).encode("utf-8"),
        capture_output=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[1],
    )
    payload = json.loads(denied.stdout.decode("utf-8").strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["kind"] == fsop.KIND_INVALID
