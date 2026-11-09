from __future__ import annotations

import pytest

from main.service.runtime_service import MainRuntimeService


@pytest.mark.asyncio
async def test_maybe_refresh_reconciles_when_no_baseline_exists() -> None:
    after_state = {"skills": {}, "tools": {"demo_tool": "fingerprint"}}
    refreshed: list[tuple[dict[str, dict[str, str]], str, str]] = []

    class _Manager:
        def capture_resource_tree_state(self):
            return after_state

    service = object.__new__(MainRuntimeService)
    service._resource_manager = _Manager()
    service.memory_manager = None
    service._resource_tree_state_cache = None
    service._resource_tree_state_checked_at = 0.0
    service._resource_tree_state_poll_interval_ms = 0
    service.refresh_changed_resources = (
        lambda state, *, trigger, session_id: refreshed.append((state, trigger, session_id))
        or {"ok": True, "session_id": session_id, "skills": 0, "tools": 1}
    )

    result = await service.maybe_refresh_external_resource_changes(session_id="web:shared")

    assert refreshed == [({}, "external-resource-generation-check", "web:shared")]
    assert result["refreshed"] is True
    assert result["resources"]["tools"] == 1


@pytest.mark.asyncio
async def test_reload_resources_async_enqueues_worker_reload_in_web_mode() -> None:
    service = object.__new__(MainRuntimeService)
    service.execution_mode = 'web'
    service.reload_resources = lambda *, session_id='web:shared': {
        'ok': True,
        'session_id': session_id,
        'skills': 1,
        'tools': 2,
    }

    async def _sync_catalog_targets(**kwargs):
        return {'catalog_synced': False}

    service._sync_catalog_targets = _sync_catalog_targets
    service.list_skill_resources = lambda: []
    service.list_tool_resources = lambda: []
    enqueued: list[dict] = []

    def _enqueue(**kwargs):
        enqueued.append(kwargs)
        return 'cmd-1'

    service._enqueue_task_command = _enqueue

    result = await service.reload_resources_async(session_id='web:shared')

    assert enqueued[0]['command_type'] == 'reload_resources'
    assert enqueued[0]['task_id'] is None
    assert result['worker_reload'] == {
        'requested': True,
        'command_id': 'cmd-1',
        'status': 'pending',
    }


@pytest.mark.asyncio
async def test_reload_resources_async_skips_worker_reload_outside_web_mode() -> None:
    service = object.__new__(MainRuntimeService)
    service.execution_mode = 'worker'
    service.reload_resources = lambda *, session_id='web:shared': {
        'ok': True,
        'session_id': session_id,
        'skills': 1,
        'tools': 2,
    }

    async def _sync_catalog_targets(**kwargs):
        return {'catalog_synced': False}

    service._sync_catalog_targets = _sync_catalog_targets
    service.list_skill_resources = lambda: []
    service.list_tool_resources = lambda: []
    service._enqueue_task_command = lambda **kwargs: 'should-not-be-called'

    result = await service.reload_resources_async(session_id='web:shared')

    assert result['worker_reload'] == {'requested': False, 'status': 'local'}


@pytest.mark.asyncio
async def test_worker_command_reload_resources_calls_local_reload() -> None:
    service = object.__new__(MainRuntimeService)
    service.worker_id = 'w1'
    service.normalize_task_id = lambda value: str(value or '').strip()
    calls: list[str] = []
    service.reload_resources = lambda *, session_id='web:shared': calls.append(session_id) or {
        'ok': True,
        'skills': 1,
        'tools': 2,
    }
    finished: list[tuple[str, dict]] = []

    class _Store:
        def finish_task_command(self, command_id, **kwargs):
            finished.append((command_id, kwargs))

    service.store = _Store()

    await service._process_worker_command(
        {
            'command_id': 'c1',
            'command_type': 'reload_resources',
            'task_id': '',
            'payload': {'session_id': 'web:shared'},
        }
    )

    assert calls == ['web:shared']
    assert finished[0][0] == 'c1'
    assert finished[0][1]['success'] is True
    assert finished[0][1]['result']['tools'] == 2


@pytest.mark.asyncio
async def test_maybe_refresh_external_resource_changes_reports_targeted_ids_without_catalog_sync() -> None:
    before_state = {
        "skills": {"demo_skill": "old-skill-fingerprint"},
        "tools": {"demo_tool": "stable-tool-fingerprint"},
    }
    after_state = {
        "skills": {"demo_skill": "new-skill-fingerprint"},
        "tools": {"demo_tool": "stable-tool-fingerprint"},
    }
    refreshed: list[tuple[dict[str, dict[str, str]], str, str]] = []

    class _Manager:
        def capture_resource_tree_state(self):
            return after_state

    service = object.__new__(MainRuntimeService)
    service._resource_manager = _Manager()
    service.memory_manager = None
    service._resource_tree_state_cache = before_state
    service._resource_tree_state_checked_at = 0.0
    service._resource_tree_state_poll_interval_ms = 0
    service.refresh_changed_resources = (
        lambda state, *, trigger, session_id: refreshed.append((state, trigger, session_id))
        or {"ok": True, "session_id": session_id, "skills": 1, "tools": 1}
    )

    result = await service.maybe_refresh_external_resource_changes(session_id="web:shared")

    assert refreshed == [(before_state, "external-resource-generation-check", "web:shared")]
    assert result["refreshed"] is True
    assert result["catalog_synced"] is False
    assert result["skill_ids"] == ["demo_skill"]
    assert result["tool_ids"] == []


def test_zero_poll_interval_survives_the_reader() -> None:
    """`poll_interval_ms=0` 是「每个调用点都允许比对」，不能被默认值顶掉。"""
    from types import SimpleNamespace

    def _config(interval):
        reload_cfg = {"poll_interval_ms": interval} if interval is not None else {}
        return SimpleNamespace(resources=SimpleNamespace(reload=SimpleNamespace(**reload_cfg)))

    assert MainRuntimeService._resource_reload_poll_interval_ms(_config(0)) == 0
    assert MainRuntimeService._resource_reload_poll_interval_ms(_config(250)) == 250
    assert MainRuntimeService._resource_reload_poll_interval_ms(_config(None)) == 1000


@pytest.mark.asyncio
async def test_zero_interval_compares_on_every_call_site() -> None:
    class _Manager:
        def __init__(self, states):
            self._states = list(states)
            self.calls = 0

        def capture_resource_tree_state(self):
            self.calls += 1
            return self._states.pop(0)

    def _service(*, interval_ms: int, states):
        service = object.__new__(MainRuntimeService)
        service.memory_manager = None
        service._resource_tree_state_cache = {"skills": {"demo_skill": "a"}, "tools": {}}
        service._resource_tree_state_checked_at = 0.0
        service._resource_tree_state_poll_interval_ms = interval_ms
        service._resource_manager = _Manager(states)
        service.refresh_changed_resources = lambda state, *, trigger, session_id: {
            "ok": True,
            "session_id": session_id,
            "skills": 1,
            "tools": 0,
        }
        return service

    zero = _service(
        interval_ms=0,
        states=[
            {"skills": {"demo_skill": "b"}, "tools": {}},
            {"skills": {"demo_skill": "c"}, "tools": {}},
        ],
    )
    await zero.maybe_refresh_external_resource_changes(session_id="web:shared")
    await zero.maybe_refresh_external_resource_changes(session_id="web:shared")
    assert zero._resource_manager.calls == 2

    throttled = _service(
        interval_ms=10_000,
        states=[{"skills": {"demo_skill": "b"}, "tools": {}}],
    )
    await throttled.maybe_refresh_external_resource_changes(session_id="web:shared")
    await throttled.maybe_refresh_external_resource_changes(session_id="web:shared")
    assert throttled._resource_manager.calls == 1
