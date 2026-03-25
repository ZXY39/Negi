from __future__ import annotations

from types import SimpleNamespace

from main.governance.models import ToolActionRecord, ToolFamilyRecord
from main.service.runtime_service import MainRuntimeService


def test_main_runtime_service_keeps_enabled_but_unregistered_callable_tool_visible_for_context_lookup():
    family = ToolFamilyRecord(
        tool_id='agent_browser',
        display_name='Agent Browser',
        description='Browser automation via the upstream CLI.',
        primary_executor_name='agent_browser',
        enabled=True,
        available=True,
        callable=True,
        source_path='tools/agent_browser',
        actions=[
            ToolActionRecord(action_id='browse', label='Browse', allowed_roles=['ceo'], executor_names=['agent_browser']),
        ],
        metadata={},
    )

    class _Registry:
        def list_tool_families(self):
            return [family]

    class _PolicyEngine:
        def evaluate_tool_action(self, *, subject, tool_id: str, action_id: str):
            _ = subject, tool_id, action_id
            return SimpleNamespace(allowed=True, reason_code='ok')

    service = object.__new__(MainRuntimeService)
    service.resource_registry = _Registry()
    service.policy_engine = _PolicyEngine()
    service.list_effective_tool_names = lambda **kwargs: []
    service._subject = lambda **kwargs: SimpleNamespace(**kwargs)

    visible = service.list_visible_tool_families(actor_role='ceo', session_id='web:shared')

    assert len(visible) == 1
    assert visible[0].tool_id == 'agent_browser'
    assert [action.action_id for action in visible[0].actions] == ['browse']
    assert visible[0].metadata['context_lookup_only'] is True


def test_load_tool_context_search_includes_enabled_but_unregistered_callable_tool():
    family = ToolFamilyRecord(
        tool_id='agent_browser',
        display_name='Agent Browser',
        description='Browser automation via the upstream CLI.',
        primary_executor_name='agent_browser',
        enabled=True,
        available=True,
        callable=True,
        source_path='tools/agent_browser',
        actions=[
            ToolActionRecord(action_id='browse', label='Browse', allowed_roles=['ceo'], executor_names=['agent_browser']),
        ],
        metadata={},
    )

    class _Registry:
        def list_tool_families(self):
            return [family]

    class _PolicyEngine:
        def evaluate_tool_action(self, *, subject, tool_id: str, action_id: str):
            _ = subject, tool_id, action_id
            return SimpleNamespace(allowed=True, reason_code='ok')

    service = object.__new__(MainRuntimeService)
    service.resource_registry = _Registry()
    service.policy_engine = _PolicyEngine()
    service.list_effective_tool_names = lambda **kwargs: []
    service._subject = lambda **kwargs: SimpleNamespace(**kwargs)

    payload = service.load_tool_context_v2(
        actor_role='ceo',
        session_id='web:shared',
        search_query='browser automation',
    )

    assert payload['ok'] is True
    assert payload['mode'] == 'search'
    assert payload['candidates'][0]['tool_id'] == 'agent_browser'
    assert payload['candidates'][0]['callable'] is True
    assert payload['candidates'][0]['available'] is True
