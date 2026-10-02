from __future__ import annotations

from main.service.runtime_service import MainRuntimeService


class _Manager:
    def __init__(self, after_state: dict[str, dict[str, str]]) -> None:
        self._after_state = after_state
        self.walks = 0
        self.refresh_calls: list[tuple[dict[str, dict[str, str]], str, dict[str, dict[str, str]] | None]] = []

    def capture_resource_tree_state(self) -> dict[str, dict[str, str]]:
        self.walks += 1
        return dict(self._after_state)

    def refresh_changed_tree_state(
        self,
        before_state,
        *,
        trigger: str = 'path-change',
        after_state=None,
    ) -> None:
        self.refresh_calls.append((dict(before_state or {}), trigger, after_state))


class _Registry:
    def refresh_from_current_resources(self):
        return ['demo_skill'], ['exec']


def _service(manager: _Manager, cache) -> MainRuntimeService:
    service = object.__new__(MainRuntimeService)
    service._resource_manager = manager
    service._resource_tree_state_cache = cache
    service._resource_tree_state_checked_at = 0.0
    service.resource_registry = _Registry()
    service.policy_engine = None
    return service


def test_exec_refresh_walks_the_tree_once_and_diffs_against_the_service_baseline() -> None:
    before_state = {'skills': {'demo_skill': 'old'}, 'tools': {'exec': 'stable'}}
    after_state = {'skills': {'demo_skill': 'new'}, 'tools': {'exec': 'stable'}}
    manager = _Manager(after_state)
    service = _service(manager, before_state)

    result = service.refresh_changed_resources(None, trigger='tool:exec', session_id='web:shared')

    assert manager.walks == 1
    assert manager.refresh_calls == [(before_state, 'tool:exec', after_state)]
    assert result == {'ok': True, 'session_id': 'web:shared', 'skills': 1, 'tools': 1}
    assert service._resource_tree_state_cache == after_state


def test_explicit_before_state_still_wins_over_the_cached_baseline() -> None:
    cached = {'skills': {'demo_skill': 'stale'}, 'tools': {}}
    caller_state = {'skills': {'demo_skill': 'older'}, 'tools': {}}
    after_state = {'skills': {'demo_skill': 'new'}, 'tools': {}}
    manager = _Manager(after_state)
    service = _service(manager, cached)

    service.refresh_changed_resources(caller_state, trigger='admin', session_id='web:shared')

    assert manager.refresh_calls == [(caller_state, 'admin', after_state)]


def test_missing_baseline_records_the_walk_without_claiming_every_root_changed() -> None:
    after_state = {'skills': {'demo_skill': 'new'}, 'tools': {'exec': 'stable'}}
    manager = _Manager(after_state)
    service = _service(manager, None)

    service.refresh_changed_resources(None, trigger='tool:exec', session_id='web:shared')

    assert manager.walks == 1
    assert manager.refresh_calls == []
    assert service._resource_tree_state_cache == after_state
