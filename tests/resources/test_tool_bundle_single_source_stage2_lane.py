"""阶段 2 · G4 契约：前门的记录面必须就是发送面那一份数组（从车道进口喂）。

计划里那条"测试一律从车道进口喂"的硬约束针对的正是本次的原始缺陷：预检无条件把 `tool_schemas`
覆盖成 `_selected_tool_schemas(...)` 的另渲结果，于是工件 `tool_schemas`、`actual_tool_schema_hash`、
token 估算一直在读一份比线上少名单尾部两颗合成控制工具的数组——而直接调渲染器的用例全都绿着，
因为这个偏差发生在渲染器**之后**。这里的用例跑真实装配（`_graph_prepare_turn` →
`_graph_call_model`），只把 provider 那一发和工件落盘换成捕获，比的是这两个捕获点是否同源。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor import pinned_tool_bundle
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.frontdoor.tool_contract import clear_pinned_contract_stores
from main.runtime.chat_backend import _tool_signature_hash

_TASK_TOOLS = ['filesystem', 'exec']


class _Tool:
    def __init__(self, name: str, description: str = 'desc') -> None:
        self.name = name
        self.description = description
        self.parameters: dict = {}
        self._description = description

    def rewrite(self, description: str) -> None:
        self._description = description

    def to_model_schema(self) -> dict:
        return {
            'type': 'function',
            'function': {
                'name': self.name,
                'description': self._description,
                'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
            },
        }


def _synthetic_control_tools() -> dict[str, _Tool]:
    return {
        'filesystem_stat': _Tool('filesystem_stat', 'start the next stage'),
        'silent': _Tool('silent', 'reply with nothing'),
    }


def _names(schemas: list[dict]) -> list[str]:
    return [str((item.get('function') or {}).get('name') or '') for item in schemas]


async def _run_hop(runner, runtime, captured: list[dict], state: dict) -> dict:
    """跑一跳真实装配，返回 {wire, recorded, tool_count, recorded_diagnostics, next_state}。"""
    hop: dict[str, object] = {}
    captured.append(hop)

    async def _call_model_with_tools(**kwargs):
        # provider 自己按 `len(tools)` 计 tool_count（`openai_chat_provider.py:260`），
        # 这里照同一判据算，契约测试比的才是"发送体的 tool_count"而不是测试自己数出来的数。
        wire_schemas = [dict(item) for item in list(kwargs.get('tool_schemas') or [])]
        hop['wire'] = wire_schemas
        hop['tool_count'] = len(wire_schemas)
        return {'content': 'ok', 'tool_calls': []}

    def _persist(**kwargs):
        hop['recorded'] = [dict(item) for item in list(kwargs.get('tool_schemas') or [])]
        hop['recorded_diagnostics'] = dict(kwargs.get('prompt_cache_diagnostics') or {})
        hop['state'] = dict(kwargs.get('state') or {})
        return {}

    runner._call_model_with_tools = _call_model_with_tools
    runner._persist_frontdoor_actual_request = _persist
    hop['next_state'] = await runner._graph_call_model(state, runtime=runtime)
    return hop


async def _make_lane(loop_tools: dict[str, _Tool], monkeypatch: pytest.MonkeyPatch) -> tuple:
    """建一条前门车道：runner + runtime + 首跳 state。

    只替掉三处外部面（解析/装配的输入、provider 那一发、工件落盘），渲染器、可渲染性闸门、
    整表载体、预检与诊断都跑真代码——G4 要证的正是这些环节接起来之后记录面还同不同源。
    """
    session_key = 'web:bundle-g4'
    control = _synthetic_control_tools()
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools=dict(loop_tools),
        max_iterations=8,
        workspace=None,
        temp_dir='',
    )
    runner = CreateAgentCeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, 'current_project_environment', lambda workspace_root=None: {})

    all_tools = {**loop_tools, **control}

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': list(loop_tools.keys())}

    async def _build_for_ceo(**kwargs):
        stable_messages = list(kwargs.get('request_body_seed_messages') or [])
        user_content = str(kwargs.get('user_content') or '').strip()
        return SimpleNamespace(
            tool_names=list(loop_tools.keys()),
            model_messages=[*stable_messages, {'role': 'user', 'content': user_content}],
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                'selected_skills': [],
                'tool_selection': {},
                'capability_snapshot': {
                    'visible_tool_ids': list(loop_tools.keys()),
                    'visible_skill_ids': [],
                },
            },
            cache_family_revision='frontdoor:v1',
            turn_overlay_text='',
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._builder, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])
    monkeypatch.setattr(runner, '_checkpoint_safe_model_response_payload', lambda _message: {'ok': True})
    # 执行束按名字取实例即可（这颗替身只替"哪些实例算本轮声明面"，不替渲染路径本身）。
    monkeypatch.setattr(
        runner,
        '_frontdoor_execution_bundle',
        lambda **kwargs: SimpleNamespace(declared_tools=dict(all_tools), callable_tool_names=list(all_tools.keys())),
    )
    monkeypatch.setattr(
        runner,
        '_resolve_frontdoor_send_model_context_window',
        lambda **_: {
            'model_key': 'openai_codex:gpt-test',
            'provider_model': 'openai_codex:gpt-test',
            'context_window_tokens': 128000,
        },
        raising=False,
    )

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel='web',
        _memory_chat_id='bundle',
        _channel='web',
        _chat_id='bundle',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[
            {'role': 'system', 'content': 'SYSTEM'},
            {'role': 'user', 'content': 'old question'},
            {'role': 'assistant', 'content': 'older answer'},
        ],
        _frontdoor_history_shrink_reason='',
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={'active_stage_id': '', 'transition_required': False, 'stages': []},
        _compression_state={'status': '', 'text': '', 'source': '', 'needs_recheck': False},
        _semantic_context_state={'summary_text': '', 'needs_refresh': False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
        _frontdoor_previous_actual_request_path='',
        _frontdoor_previous_actual_request_history=[],
        _frontdoor_actual_request_path='',
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    captured: list[dict] = []
    state = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={'content': 'next question', 'metadata': {}}),
        runtime=runtime,
    )
    return runner, runtime, captured, state


@pytest.fixture(autouse=True)
def _isolate_bundle_stores(tmp_path, monkeypatch):
    clear_pinned_contract_stores()
    pinned_tool_bundle._entries.clear()
    monkeypatch.setattr(pinned_tool_bundle, 'data_root', lambda *args, **kwargs: tmp_path)
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    yield tmp_path
    pinned_tool_bundle._entries.clear()
    clear_pinned_contract_stores()


@pytest.mark.asyncio
async def test_frontdoor_record_face_and_wire_are_the_same_array(monkeypatch) -> None:
    """G4：工件收到的 `tool_schemas` 与 provider 收到的那份必须逐条相等。

    偏差的形状是记录面比 wire 少 `filesystem_stat` / `silent` 两颗（预检另渲的那份不含它们），
    所以这条同时是 N5 的回归守卫：wire 里少了任何一颗阶段工具都是功能回归，不只是缓存事件。
    """
    runner, runtime, captured, state = await _make_lane({name: _Tool(name) for name in _TASK_TOOLS}, monkeypatch)
    hop = await _run_hop(runner, runtime, captured, state)

    wire = list(hop['wire'] or [])
    recorded = list(hop['recorded'] or [])
    assert wire, 'wire 不能是空表'
    assert recorded == wire, '记录面与发送面必须同源'
    assert hop['tool_count'] == len(recorded)
    assert 'silent' in _names(wire)


@pytest.mark.asyncio
async def test_frontdoor_artifact_hashes_are_recomputed_from_the_wire(monkeypatch) -> None:
    """诊断里那两枚工具哈希必须由 wire 那一份算出，而不是由代理数组算出。"""
    runner, runtime, captured, state = await _make_lane({name: _Tool(name) for name in _TASK_TOOLS}, monkeypatch)
    hop = await _run_hop(runner, runtime, captured, state)

    wire = list(hop['wire'] or [])
    diagnostics = dict(hop['recorded_diagnostics'] or {})
    assert diagnostics.get('actual_tool_schema_hash') == _tool_signature_hash(wire)
    hop_state = dict(hop['state'] or {})
    # `wire_tool_schema_hash` 是按位置的读数，与按名字排序的签名哈希互补：
    # 成员没变而顺序被改写时只有它看得见。
    assert hop_state.get('frontdoor_wire_tool_schema_hash')
    assert hop_state.get('frontdoor_wire_tool_name_sequence_hash')
    assert str(hop_state.get('frontdoor_tools_record_basis') or '') == 'wire'


@pytest.mark.asyncio
async def test_frontdoor_frozen_hop_on_the_lane_serves_bundle_bytes(monkeypatch) -> None:
    """同一会话的第二跳：运维改了参数定义，wire 上仍是整表里那份旧字节（编辑到边界才被吸收）。

    今天派发字典里的名字每跳由实例现渲，编辑会在冻结跳换字节；单源之后只有构建整表的那一刻
    才读实例，所以第二跳与第一跳必须逐字节相同。
    """
    tools = {name: _Tool(name, 'v1') for name in _TASK_TOOLS}
    runner, runtime, captured, state = await _make_lane(tools, monkeypatch)
    first = await _run_hop(runner, runtime, captured, state)

    tools['filesystem'].rewrite('v2 edited outside any reprint boundary')
    second = await _run_hop(runner, runtime, captured, dict(first['next_state'] or {}))

    descriptions = {
        str((item.get('function') or {}).get('name')): str((item.get('function') or {}).get('description'))
        for item in list(second['wire'] or [])
    }
    assert descriptions['filesystem'] == 'v1', '冻结跳不能把边界之外的编辑铺进 wire'
    assert list(second['wire'] or []) == list(first['wire'] or []), '两跳的 tools[] 必须逐字节相同'
