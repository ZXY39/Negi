"""Runtime temp anchoring: the entry points pin TMP/TEMP to the data root's `temp/`.

Why this exists: SQLite looks up its temp directory from `TMP` / `TEMP` on Windows and spills
its sorter there. When the inherited directory is not writable, every read that needs a temp
B-tree raises `SQLITE_CANTOPEN` whose message prints the *database's* absolute path, so a
blocked temp directory is indistinguishable from an unopenable database in the error text.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from g3ku.deployment import data_root as data_root_module
from g3ku.deployment import runtime_startup


class _LogCapture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def warning(self, message: str) -> None:
        self.lines.append(f"WARN {message}")

    def info(self, message: str) -> None:
        self.lines.append(f"INFO {message}")


@pytest.fixture()
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the default data root (cwd) at a throwaway directory for one test."""
    monkeypatch.chdir(tmp_path)
    data_root_module.reset_cache()
    yield tmp_path
    data_root_module.reset_cache()


@pytest.fixture(autouse=True)
def restore_temp_env(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN201
    """锚定动作写的是真进程环境：每条用例跑完把 TMP/TEMP/TMPDIR 与 tempfile 还原回去。"""
    saved = {name: os.environ.get(name) for name in ("TMP", "TEMP", "TMPDIR")}
    saved_tempdir = tempfile.tempdir
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    monkeypatch.setattr(tempfile, "tempdir", saved_tempdir)


def _build_sort_db(db_path: Path, rows: int = 12000) -> None:
    """~4.9 MB 的一张表：排序它就必须落盘（实测 1.4 MB 还在内存里，3.4 MB 起落盘）。"""
    con = sqlite3.connect(db_path)
    try:
        con.execute("create table t(a integer primary key, v text)")
        tail = "x" * 400
        con.executemany(
            "insert into t(a, v) values(?, ?)",
            ((index, f"{index:06d}{tail}") for index in range(rows)),
        )
        con.commit()
    finally:
        con.close()


def _spill_read(db_path: Path) -> str:
    """Run a sort that must spill and return `ok:N` or the SQLite error name."""
    con = sqlite3.connect(db_path)
    try:
        try:
            count = con.execute("select count(*) from (select v from t order by v)").fetchone()[0]
            return f"ok:{count}"
        except sqlite3.Error as exc:
            return f"failed:{getattr(exc, 'sqlite_errorname', type(exc).__name__)}"
    finally:
        con.close()


def test_anchor_pins_env_and_tempfile_to_data_root_temp(
    isolated_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _LogCapture()
    monkeypatch.setattr(runtime_startup, "logger", capture)

    anchored = runtime_startup.anchor_process_temp_dir()

    expected = str(isolated_cwd / "temp")
    assert anchored == expected
    for name in ("TMP", "TEMP", "TMPDIR"):
        assert os.environ[name] == expected
    assert tempfile.tempdir == expected
    assert Path(expected).is_dir()
    assert "INFO Runtime temp anchored:" in capture.lines[0]


def test_spilling_read_recovers_after_anchor_isolates_it_from_the_caller_env(
    isolated_cwd: Path,
    tmp_path: Path,
) -> None:
    """继承到不可用的临时目录时排序读失败；锚定到数据根之后同一条读成功。"""
    db_path = isolated_cwd / "sortme.sqlite3"
    _build_sort_db(db_path)
    blocked = tmp_path / "caller-temp-that-does-not-exist"
    os.environ["TMP"] = str(blocked)
    os.environ["TEMP"] = str(blocked)

    before = _spill_read(db_path)
    anchored = runtime_startup.anchor_process_temp_dir()
    after = _spill_read(db_path)

    assert before == "failed:SQLITE_CANTOPEN"
    assert anchored == str(isolated_cwd / "temp")
    assert after == "ok:12000"


def test_unwritable_data_root_temp_leaves_the_inherited_env_alone(
    isolated_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据根只读/被占名时不锚：把一个可能还能用的环境换成一定能坏的，是更坏的处置。"""
    (isolated_cwd / "temp").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("TMP", str(isolated_cwd / "keep-me"))
    monkeypatch.setenv("TEMP", str(isolated_cwd / "keep-me"))
    capture = _LogCapture()
    monkeypatch.setattr(runtime_startup, "logger", capture)

    anchored = runtime_startup.anchor_process_temp_dir()

    assert anchored == ""
    assert os.environ["TMP"] == str(isolated_cwd / "keep-me")
    assert "Runtime temp anchor skipped" in "\n".join(capture.lines)
    assert "not writable" in "\n".join(capture.lines)
