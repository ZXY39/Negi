"""artifact 原子替换撞读重试（2026-10-16 实盘：WinError 5 把已成功返回的模型回合作废成 error-pause）。

用例从车道进口喂：一律走 `create_or_replace_singleton_text_artifact`（`node_input`
每回合覆盖写同一文件的真实入口），只把 `Path.replace` 换成"前几次抛争用错误"的桩。
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from main.storage import artifact_store as artifact_store_module
from main.storage.artifact_store import TaskArtifactStore, read_artifact_text
from main.storage.disk_guard import DiskPolicies, configure_disk_policies
from main.storage.sqlite_store import SQLiteTaskStore

CONTENTION_MESSAGE = 'another process is using the file'


def _contention(winerror: int) -> PermissionError:
    exc = PermissionError(errno.EACCES, CONTENTION_MESSAGE)
    exc.winerror = winerror
    return exc


@pytest.fixture()
def policies_guard():
    previous = artifact_store_module.disk_policies()
    yield
    configure_disk_policies(previous)


@pytest.fixture()
def sleeps(monkeypatch):
    recorded: list[float] = []
    sleeper = getattr(artifact_store_module, 'time', None)
    if sleeper is not None:
        monkeypatch.setattr(sleeper, 'sleep', lambda seconds: recorded.append(seconds))
    return recorded


def _flaky_replace(monkeypatch, failures: list[Exception]):
    """让改名前 len(failures) 次抛给定异常，之后按真实语义改名。"""
    calls: list[Path] = []
    original = Path.replace

    def _replace(self: Path, target: Path) -> Path:
        calls.append(target)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return original(self, target)

    monkeypatch.setattr(Path, 'replace', _replace)
    return calls


def _singleton_store(tmp_path):
    configure_disk_policies(DiskPolicies(artifact_gzip_threshold_bytes=1024 * 1024))
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    return store, artifacts


def _write_singleton(artifacts, content: str):
    return artifacts.create_or_replace_singleton_text_artifact(
        task_id='task:t1',
        node_id='node:n1',
        kind='node_input',
        title='node-input:node:n1',
        content=content,
        extension='.txt',
    )


def test_reader_contention_retries_then_lands(tmp_path, policies_guard, sleeps, monkeypatch):
    store, artifacts = _singleton_store(tmp_path)
    try:
        first = _write_singleton(artifacts, 'first')
        calls = _flaky_replace(
            monkeypatch,
            [_contention(5), _contention(32)],
        )

        second = _write_singleton(artifacts, 'second')

        assert calls == [Path(first.path), Path(first.path), Path(first.path)]
        assert sleeps == [0.025, 0.025]
        assert second.artifact_id == first.artifact_id
        assert read_artifact_text(second) == 'second'
        assert not Path(f'{first.path}.tmp').exists()
    finally:
        store.close()


def test_contention_gives_up_after_four_attempts_and_clears_tmp(tmp_path, policies_guard, sleeps, monkeypatch):
    store, artifacts = _singleton_store(tmp_path)
    try:
        first = _write_singleton(artifacts, 'first')
        calls = _flaky_replace(monkeypatch, [_contention(5)] * 4)

        with pytest.raises(PermissionError):
            _write_singleton(artifacts, 'second')

        # 档位是定稿数字：改名最多试 4 次、间隔 25 ms
        assert len(calls) == 4
        assert sleeps == [0.025, 0.025, 0.025]
        # 放弃后旧正文必须原样还在，且不残留 .tmp（否则下一回合仍撞同一份脏文件）
        assert read_artifact_text(artifacts.get_artifact(first.artifact_id)) == 'first'
        assert not Path(f'{first.path}.tmp').exists()
    finally:
        store.close()


def test_non_contention_error_is_not_retried(tmp_path, policies_guard, sleeps, monkeypatch):
    store, artifacts = _singleton_store(tmp_path)
    try:
        first = _write_singleton(artifacts, 'first')
        refused = PermissionError(errno.EACCES, 'access denied by acl')
        refused.winerror = 1312  # ERROR_INVALID_SERVER_STATE：不是争用，重试也不会好
        calls = _flaky_replace(monkeypatch, [refused])

        with pytest.raises(PermissionError):
            _write_singleton(artifacts, 'second')

        assert len(calls) == 1
        assert sleeps == []
        assert read_artifact_text(first) == 'first'  # 非争用失败同样不能碰坏旧正文
    finally:
        store.close()
