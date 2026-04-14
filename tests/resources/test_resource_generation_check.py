from __future__ import annotations

from types import SimpleNamespace

import pytest

from main.service.runtime_service import MainRuntimeService


@pytest.mark.asyncio
async def test_maybe_refresh_external_resource_changes_triggers_targeted_catalog_sync() -> None:
    before_state = {
        "skills": {"demo_skill": "old-skill-fingerprint"},
        "tools": {"demo_tool": "stable-tool-fingerprint"},
    }
    after_state = {
        "skills": {"demo_skill": "new-skill-fingerprint"},
        "tools": {"demo_tool": "stable-tool-fingerprint"},
    }
    refreshed: list[tuple[dict[str, dict[str, str]], str, str]] = []
    synced: list[tuple[set[str], set[str]]] = []

    class _Manager:
        def capture_resource_tree_state(self):
            return after_state

    class _MemoryManager:
        async def sync_catalog(self, service, *, skill_ids=None, tool_ids=None):
            _ = service
            synced.append((set(skill_ids or set()), set(tool_ids or set())))
            return {"created": 0, "updated": 1, "removed": 0}

    service = object.__new__(MainRuntimeService)
    service._resource_manager = _Manager()
    service.memory_manager = _MemoryManager()
    service._resource_tree_state_cache = before_state
    service._resource_tree_state_checked_at = 0.0
    service._resource_tree_state_poll_interval_ms = 0
    service.refresh_changed_resources = (
        lambda state, *, trigger, session_id: refreshed.append((state, trigger, session_id))
        or {"ok": True, "session_id": session_id, "skills": 1, "tools": 1}
    )

    result = await service.maybe_refresh_external_resource_changes(session_id="web:shared")

    assert refreshed == [(before_state, "external-resource-generation-check", "web:shared")]
    assert synced == [({"demo_skill"}, set())]
    assert result["refreshed"] is True
    assert result["catalog_synced"] is True
    assert result["skill_ids"] == ["demo_skill"]
    assert result["tool_ids"] == []
