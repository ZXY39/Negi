from __future__ import annotations

import asyncio

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
