import os
import time

import pytest

import negi_bootstrap
from main.service.runtime_service import MainRuntimeService


def _write_lines(path, count, *, line_bytes=200):
    payload = ''.join(f'line-{index:06d} {"x" * (line_bytes - 12)}\n' for index in range(count))
    path.write_bytes(payload.encode('utf-8'))
    return len(payload.encode('utf-8'))


def test_cap_console_log_file_noop_under_cap(tmp_path) -> None:
    log = tmp_path / 'console.log'
    size = _write_lines(log, 50)

    dropped = MainRuntimeService._cap_console_log_file(log, cap_bytes=size + 1, keep_bytes=1024)

    assert dropped == 0
    assert log.stat().st_size == size


def test_cap_console_log_file_keeps_tail_and_marks_dropped_head(tmp_path) -> None:
    log = tmp_path / 'console.log'
    size = _write_lines(log, 1000)
    keep = 2000

    dropped = MainRuntimeService._cap_console_log_file(log, cap_bytes=100, keep_bytes=keep)

    assert dropped > 0
    assert size - keep <= dropped <= size, '丢掉的量应落在"整个头部"这一档，不多不少'
    text = log.read_text(encoding='utf-8')
    assert '[log-cap]' in text
    assert 'line-000999' in text, '尾部最后一行必须还在'
    assert 'line-000000' not in text, '头部应被丢掉'
    # 截尾后仍以整行开始，不留半行
    body = text.split('[log-cap]', 1)[1].split('\n', 1)[1]
    assert body.startswith('line-')
    assert all(chunk.endswith('\n') for chunk in body.splitlines(keepends=True))
    assert log.stat().st_size <= keep + 200


def test_cap_console_log_file_survives_live_append_handle(tmp_path) -> None:
    """封顶必须打得开：正文写在一个继承来的 append 句柄上，截尾后它还得继续往后写。"""
    log = tmp_path / 'console.log'
    _write_lines(log, 1000)
    handle = log.open('a', encoding='utf-8')
    try:
        dropped = MainRuntimeService._cap_console_log_file(log, cap_bytes=100, keep_bytes=2000)
        assert dropped > 0
        handle.write('line-after-cap\n')
        handle.flush()
        raw = log.read_bytes()
        assert b'\x00' not in raw, '截尾不能留下空洞'
        # 文本模式句柄会按平台翻译换行，这里只断言"落在新 EOF 之后且是最后一行"
        lines = raw.decode('utf-8').splitlines()
        assert lines[-1] == 'line-after-cap'
        assert raw.decode('utf-8').lstrip('\n').startswith('[log-cap]')
    finally:
        handle.close()


def test_cap_console_log_file_tolerates_missing_path(tmp_path) -> None:
    assert MainRuntimeService._cap_console_log_file(
        tmp_path / 'absent.log', cap_bytes=1, keep_bytes=1
    ) == 0


@pytest.fixture()
def bootstrap_log_dir(tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    monkeypatch.setattr(negi_bootstrap, 'RUNTIME_LOG_DIR', log_dir)
    monkeypatch.setattr(negi_bootstrap, 'RUNTIME_CONSOLE_LOG_FILE', log_dir / 'console.log')
    return log_dir


def test_bootstrap_rotates_oversized_console_log(bootstrap_log_dir, monkeypatch) -> None:
    log = bootstrap_log_dir / 'console.log'
    _write_lines(log, 1000)
    monkeypatch.setattr(negi_bootstrap, 'RUNTIME_CONSOLE_LOG_MAX_BYTES', 100)

    negi_bootstrap._rotate_runtime_console_log()

    assert not log.exists(), '超上限的当前代必须被换走，让新句柄从空文件开始'
    generations = sorted(bootstrap_log_dir.glob('console.log.*'))
    assert len(generations) == 1
    assert generations[0].stat().st_size > 100

    stream = negi_bootstrap._open_runtime_console_log_stream()
    assert stream is not None
    stream.close()
    assert log.exists() and log.stat().st_size == 0


def test_bootstrap_keeps_console_log_under_cap(bootstrap_log_dir, monkeypatch) -> None:
    log = bootstrap_log_dir / 'console.log'
    _write_lines(log, 10)
    monkeypatch.setattr(negi_bootstrap, 'RUNTIME_CONSOLE_LOG_MAX_BYTES', 1024 * 1024)

    negi_bootstrap._rotate_runtime_console_log()

    assert log.exists()
    assert list(bootstrap_log_dir.glob('console.log.*')) == []


def test_bootstrap_prunes_expired_generations_only(bootstrap_log_dir) -> None:
    expired = bootstrap_log_dir / 'console.log.20200101-000000'
    fresh = bootstrap_log_dir / 'console.log.20990101-000000'
    expired.write_text('old\n', encoding='utf-8')
    fresh.write_text('new\n', encoding='utf-8')
    stale = time.time() - (negi_bootstrap.RUNTIME_CONSOLE_LOG_RETENTION_SECONDS + 3600)
    os.utime(expired, (stale, stale))

    negi_bootstrap._rotate_runtime_console_log()

    assert not expired.exists()
    assert fresh.exists()
