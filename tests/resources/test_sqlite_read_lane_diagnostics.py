"""读车道 CANTOPEN 的现场诊断：一条车道坏了时，另一条同一时刻能不能跑同一条 SQL。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

import main.storage.sqlite_store as sqlite_store_module
from main.storage.sqlite_store import SQLiteTaskStore


class _CantOpen(sqlite3.OperationalError):
    """带着 SQLite 扩展身份的 CANTOPEN（手工构造的 OperationalError 没有这两个属性）。"""

    @property
    def sqlite_errorname(self) -> str:  # type: ignore[override]
        return 'SQLITE_CANTOPEN'

    @property
    def sqlite_errorcode(self) -> int:  # type: ignore[override]
        return 14


class _FailingConn:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def execute(self, sql, params=()):  # noqa: ANN001
        raise self._exc

    def close(self) -> None:
        return None


class _LogCapture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def opt(self, **kwargs):  # noqa: ANN003
        return self

    def error(self, message, *args):  # noqa: ANN001
        try:
            self.lines.append(str(message).format(*args))
        except Exception:
            self.lines.append(str(message))

    def warning(self, message, *args):  # noqa: ANN001
        self.error(message, *args)

    def info(self, message, *args):  # noqa: ANN001
        self.error(message, *args)


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteTaskStore:
    item = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    yield item
    item.close()


def _capture_logger(monkeypatch: pytest.MonkeyPatch) -> _LogCapture:
    capture = _LogCapture()
    monkeypatch.setattr(sqlite_store_module, 'logger', capture)
    return capture


def test_cantopen_on_light_lane_reports_the_other_lane_result(store: SQLiteTaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """light 车道坏、read 车道好 ⇒ 现场必须把"另一条车道 ok:N"打出来，并原样抛出异常。"""
    capture = _capture_logger(monkeypatch)
    store._light_read_conn = _FailingConn(_CantOpen('unable to open database file'))

    with pytest.raises(sqlite3.OperationalError):
        store._fetchall_light('select task_id, total_bytes from task_disk_usage', ())

    joined = '\n'.join(capture.lines)
    assert 'sqlite_errorname=SQLITE_CANTOPEN' in joined
    assert 'lane=light' in joined
    assert 'cross_check[read]=ok:' in joined
    assert 'cross_check[write]=' in joined
    assert 'fresh_open=' in joined
    assert 'fd_budget=' in joined
    assert 'open_probe=' in joined and 'shm=' in joined
    assert 'shm_map=' in joined
    assert 'db_handles=' in joined
    assert 'cwd_exists=' in joined and 'tmpdir=' in joined and 'rel_open=' in joined
    assert 'spill=ok:' in joined and 'point=ok' in joined
    assert 'env_tmp=' in joined and 'env_temp=' in joined
    assert 'sqlite_temp_directory=' not in joined and 'scratch_temp=' not in joined
    assert 'live_header=' in joined
    assert 'conn_age_s=' in joined
    assert 'lane_cantopen_occurrences=1' in joined
    assert f'pid={__import__("os").getpid()}' in joined


def test_fresh_open_reports_the_real_result_for_a_reachable_file(store: SQLiteTaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """库文件本身在本地是好的 ⇒ 新开连接必须能读（这一项决定方向往哪边查）。"""
    capture = _capture_logger(monkeypatch)
    store._light_read_conn = _FailingConn(_CantOpen('unable to open database file'))

    with pytest.raises(sqlite3.OperationalError):
        store._fetchall_light('select 1', ())

    assert 'fresh_open=ok:1' in '\n'.join(capture.lines)


def test_cantopen_counts_per_lane(store: SQLiteTaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _capture_logger(monkeypatch)
    store._light_read_conn = _FailingConn(_CantOpen('unable to open database file'))

    for expected in (1, 2):
        with pytest.raises(sqlite3.OperationalError):
            store._fetchall_light('select 1', ())
        assert f'lane_cantopen_occurrences={expected}' in '\n'.join(capture.lines[-1:])


def test_other_lane_failure_cross_checks_the_light_lane(store: SQLiteTaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _capture_logger(monkeypatch)
    store._read_conn = _FailingConn(_CantOpen('unable to open database file'))

    with pytest.raises(sqlite3.OperationalError):
        store._fetchall('select 1', ())

    joined = '\n'.join(capture.lines)
    assert 'lane=read' in joined
    assert 'cross_check[light]=ok:1' in joined


def test_non_cantopen_read_error_adds_no_lane_detail(store: SQLiteTaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """普通 SQL 错误（没有表）不该被当成车道故障，也不该去碰另一条车道。"""
    capture = _capture_logger(monkeypatch)

    with pytest.raises(sqlite3.OperationalError):
        store._fetchall_light('select * from table_that_does_not_exist', ())

    joined = '\n'.join(capture.lines)
    assert 'lane=' not in joined
    assert 'cross_check' not in joined


def test_cross_check_never_blocks_when_other_lane_is_busy(store: SQLiteTaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """另一条车道被占住时必须报 busy，而不是阻塞拿锁——`refresh_read_snapshot` 按 read→light
    顺序拿锁，反向阻塞拿会死锁。"""
    capture = _capture_logger(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        store._light_read_lock.acquire()
        started.set()
        release.wait(5.0)
        store._light_read_lock.release()

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    started.wait(5.0)
    store._read_conn = _FailingConn(_CantOpen('unable to open database file'))
    try:
        with pytest.raises(sqlite3.OperationalError):
            store._fetchall('select 1', ())
    finally:
        release.set()
        holder.join(5.0)

    joined = '\n'.join(capture.lines)
    assert 'cross_check[light]=busy' in joined


def test_spill_probe_isolates_the_temp_dir_from_the_database(
    store: SQLiteTaskStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """临时目录不可用而库是好的：探针必须报 `spill=failed:SQLITE_CANTOPEN` 且 `point=ok`。

    `SQLITE_CANTOPEN` 的消息里印的是库的绝对路径，所以"库打不开"与"排序临时文件建不出来"
    在异常文本上完全同形——这条用例钉住的就是把两者切开的那一格。
    """
    missing = tmp_path / 'temp-dir-that-does-not-exist'
    monkeypatch.setenv('TMP', str(missing))
    monkeypatch.setenv('TEMP', str(missing))

    detail = store._scratch_db_check()

    assert 'spill=failed:SQLITE_CANTOPEN' in detail
    assert 'point=ok' in detail
    env_line = store._environment_probe()
    assert repr(str(missing)) in env_line
