from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.main_runtime import LoadSkillContextTool
from main.governance.models import ToolRecord
from main.service.runtime_service import MainRuntimeService


def _agent_browser_record(**overrides) -> ToolRecord:
    payload = {
        'tool_id': 'agent_browser',
        'display_name': 'Agent Browser',
        'description': 'Browser automation via the upstream CLI.',
        'allowed_roles': ['ceo'],
        'enabled': True,
        'available': True,
        'callable': True,
        'source_path': 'tools/agent_browser',
        'metadata': {},
    }
    payload.update(overrides)
    return ToolRecord(**payload)


def test_main_runtime_service_keeps_enabled_but_unregistered_callable_tool_visible_for_context_lookup():
    tool = _agent_browser_record()

    class _Registry:
        def list_tools(self):
            return [tool]

    class _PolicyEngine:
        def evaluate_tool(self, *, subject, tool_id: str):
            _ = subject, tool_id
            return SimpleNamespace(allowed=True, reason_code='ok')

    service = object.__new__(MainRuntimeService)
    service.resource_registry = _Registry()
    service.policy_engine = _PolicyEngine()
    service.list_effective_tool_names = lambda **kwargs: []
    service._subject = lambda **kwargs: SimpleNamespace(**kwargs)

    visible = service.list_visible_tools(actor_role='ceo', session_id='web:shared')

    assert len(visible) == 1
    assert visible[0].tool_id == 'agent_browser'
    # 一层形状：工具记录自己就是被授权的对象，没有动作名单可对照，可见项就是这条记录本身。
    assert not hasattr(visible[0], 'actions')
    assert visible[0].metadata['context_lookup_only'] is True


def test_load_tool_context_search_includes_enabled_but_unregistered_callable_tool():
    tool = _agent_browser_record()

    class _Registry:
        def list_tools(self):
            return [tool]

    class _PolicyEngine:
        def evaluate_tool(self, *, subject, tool_id: str):
            _ = subject, tool_id
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


def test_decorated_tool_exposes_repair_required_metadata_and_toolskill_flag():
    tool = _agent_browser_record(available=False, metadata={'warnings': ['missing required paths']})

    class _Registry:
        def list_tools(self):
            return [tool]

        def get_tool(self, tool_id: str):
            return tool if tool_id == 'agent_browser' else None

    service = object.__new__(MainRuntimeService)
    service.resource_registry = _Registry()
    service._core_tool_resolution = lambda: SimpleNamespace(tool_ids=set(), unresolved_entries=[])
    service._raw_tool = lambda tool_id: tool if tool_id == 'agent_browser' else None
    service._resource_manager = None

    decorated = service.get_tool('agent_browser')
    payload = service.get_tool_toolskill('agent_browser')

    assert decorated is not None
    assert decorated.metadata['repair_required'] is True
    assert payload is not None
    assert payload['tool_id'] == 'agent_browser'
    assert payload['repair_required'] is True
    # 族粒度取消后 payload 不再携带任何族/主执行器字段。
    assert 'family_tool_id' not in payload
    assert 'primary_executor_name' not in payload
    assert 'toolskill_source_name' not in payload


def test_load_tool_context_returns_a_registered_toolskill_for_a_non_visible_tool():
    """说明可读性与调用授权分开：不可见但注册过 toolskill 的工具照常返回正文。

    exec 在节点道是常驻内置、又带 tools/exec/toolskills；inspection 角色不可调用它，
    但验收节点需要读契约来判断子节点的命令是否合规。
    """
    service = object.__new__(MainRuntimeService)
    service._resource_manager = SimpleNamespace()
    service._visible_tool_map = lambda **kwargs: {}
    service.get_tool_toolskill = lambda tool_id: (
        {
            'tool_id': tool_id,
            'content': '# exec contract',
            'description': 'exec',
            'path': 'tools/exec/toolskills/SKILL.md',
            'callable': True,
            'available': True,
            'parameters_schema': {'type': 'object', 'properties': {}, 'required': []},
        }
        if tool_id == 'exec'
        else None
    )
    service._tool_context_contract_payload = lambda **kwargs: {
        'callable_now': False,
        'will_be_hydrated_next_hop': False,
        'hydration_targets': [],
        'exec_runtime_policy': None,
    }

    payload = service.load_tool_context_v2(
        actor_role='inspection',
        session_id='web:shared',
        tool_id='exec',
    )

    assert payload['ok'] is True
    assert payload['content'] == '# exec contract'
    assert payload['callable'] is False and payload['available'] is False
    assert payload['callable_now'] is False
    assert payload['documentation_only'] is True
    assert any('仅返回说明正文' in item for item in payload['warnings'])


def test_load_tool_context_still_rejects_a_non_visible_tool_without_a_toolskill():
    service = object.__new__(MainRuntimeService)
    service._resource_manager = SimpleNamespace()
    service._visible_tool_map = lambda **kwargs: {}
    service.get_tool_toolskill = lambda tool_id: None

    payload = service.load_tool_context_v2(
        actor_role='inspection',
        session_id='web:shared',
        tool_id='nope',
    )

    assert payload == {'ok': False, 'error': 'Tool not visible: nope'}


@pytest.mark.asyncio
async def test_load_skill_context_tool_requires_known_visible_skill_id():
    class _Service:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def startup(self) -> None:
            return None

        def load_skill_context_v2(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {'ok': True, 'skill_id': kwargs['skill_id'], 'content': 'demo body'}

    service = _Service()
    tool = LoadSkillContextTool(lambda: service)

    assert set(tool.parameters['properties']) == {'skill_id'}
    assert tool.parameters['required'] == ['skill_id']

    payload = json.loads(
        await tool.execute(
            skill_id='demo_visible_skill',
        )
    )

    assert payload['ok'] is True
    assert payload['skill_id'] == 'demo_visible_skill'
    assert len(service.calls) == 1
    assert service.calls[0]['session_id'] == 'web:shared'
    assert service.calls[0]['skill_id'] == 'demo_visible_skill'


@pytest.mark.asyncio
async def test_load_skill_context_tool_rejects_search_query_discovery_mode():
    class _Service:
        async def startup(self) -> None:
            return None

    tool = LoadSkillContextTool(lambda: _Service())
    payload = json.loads(await tool.execute(skill_id='', search_query='rollback planning'))

    assert payload['ok'] is False
    assert payload['error'] == 'skill_search_not_allowed'
