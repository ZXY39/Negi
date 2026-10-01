from __future__ import annotations

import asyncio

import pytest

from main.service.runtime_service import MainRuntimeService


def test_suspend_site_reports_a_code_site_for_a_running_task() -> None:
    async def _parked() -> None:
        await asyncio.sleep(30)

    async def _case() -> str:
        task = asyncio.create_task(_parked(), name='probe-parked')
        await asyncio.sleep(0.05)
        try:
            return MainRuntimeService._task_suspend_site(task)
        finally:
            task.cancel()

    site = asyncio.run(_case())
    assert '.py:' in site, site
    assert site.endswith(' in _parked'), site


def test_suspend_site_labels_finished_and_missing_tasks() -> None:
    async def _noop() -> None:
        return None

    async def _case() -> tuple[str, str]:
        done = asyncio.create_task(_noop(), name='probe-done')
        await done
        return MainRuntimeService._task_suspend_site(done), 'missing'

    first, second = asyncio.run(_case())
    assert first == 'done'
    assert second == 'missing'


def test_census_line_reports_top_sites_when_marker_is_present(tmp_path, monkeypatch) -> None:
    """标记在的时候那一行必须带挂起点榜——任务名前缀只说"谁建的"，回环连接归因要的是"卡在哪个调用"。"""
    import main.service.runtime_service as module

    real_sleep = asyncio.sleep
    logged: list[str] = []

    class _Recorder:
        def warning(self, message: str, *args) -> None:
            logged.append(str(message).format(*args) if args else str(message))

    class _Stub:
        worker_id = 'worker:probe'

        def _loop_census_marker_path(self):
            return tmp_path / 'loop-census.on'

        @staticmethod
        def _task_suspend_site(task):
            return module.MainRuntimeService._task_suspend_site(task)

    (tmp_path / 'loop-census.on').write_text('', encoding='utf-8')
    monkeypatch.setattr(module, 'logger', _Recorder())

    ticks = {'n': 0}

    async def _fake_sleep(_seconds: float) -> None:
        ticks['n'] += 1
        await real_sleep(0.02)
        if ticks['n'] > 1:
            raise asyncio.CancelledError

    async def _parked() -> None:
        await real_sleep(30)

    async def _case() -> None:
        monkeypatch.setattr(module.asyncio, 'sleep', _fake_sleep)
        task = asyncio.create_task(_parked(), name='probe-parked')
        await real_sleep(0.02)
        try:
            await module.MainRuntimeService._loop_census_loop(_Stub())
        finally:
            task.cancel()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_case())

    assert len(logged) == 1, logged
    line = logged[0]
    assert 'top_sites=' in line
    assert 'in _parked' in line
    assert 'poller=missing' in line
