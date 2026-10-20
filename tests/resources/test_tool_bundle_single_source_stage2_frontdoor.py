"""阶段 2 前门道：参数表只从钉住整表取字节（开关 `G3KU_TOOLS_BUNDLE_SINGLE_SOURCE`）。

前门线上那份 `tools[]` 由 `_frontdoor_tool_schemas_for_state` 渲染，所以这些用例直接喂它——
它就是发送体那一份的来源。开关关闭时的形状由 `test_pinned_tool_bundle_stage1.py` 与
`test_tool_bundle_wire_readings_stage0.py` 钉住，这里只写开关打开后的合同。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.resources import retired_schemas
from g3ku.runtime.frontdoor import pinned_tool_bundle
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from main.runtime import chat_backend


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


def _session(key: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(session_key=key))


def _runner(tools: dict[str, _Tool], *, declared: dict[str, _Tool] | None = None) -> CreateAgentCeoFrontDoorRunner:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(tools=dict(tools)))
    # 生产里 `declared_tools` 一定含两颗合成控制工具（`_frontdoor_execution_bundle` 现场加），
    # 替身不给就会让它们掉进占位档——那正是 N5 警告过的形状，别用它当常态。
    control = {
        'submit_next_stage': _Tool('submit_next_stage', 'start the next stage'),
        'silent': _Tool('silent', 'reply with nothing'),
    }
    runner._frontdoor_execution_bundle = lambda **kwargs: SimpleNamespace(
        declared_tools={**tools, **(declared or control)}
    )
    return runner


def _render(runner, session, key: str, names: list[str], *, mode: str = '', state_out: dict | None = None) -> list[dict]:
    state = {
        'provider_tool_names': list(names),
        'session_key': key,
        'provider_tool_bundle_mode': mode,
        'hydrated_tool_names': [],
        'rbac_visible_tool_names': [],
        'messages': [],
        'frontdoor_stage_state': {},
    }
    if state_out is None:
        return runner._frontdoor_tool_schemas_for_state(
            state=state,
            runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
        )
    rendered = runner._frontdoor_tool_schemas_for_state(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )
    # 计数与占位是渲染器写回 state 的，必须在调用之后读。
    state_out.update(state)
    return rendered


def _names(schemas: list[dict]) -> list[str]:
    return [str((item.get('function') or {}).get('name') or '') for item in schemas]


def _schema_payload(name: str, description: str) -> dict:
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': description,
            'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
        },
    }


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    monkeypatch.setattr(pinned_tool_bundle, 'data_root', lambda *args, **kwargs: tmp_path)
    pinned_tool_bundle._entries.clear()
    retired_schemas.clear_tool_schemas()
    yield tmp_path
    pinned_tool_bundle._entries.clear()
    retired_schemas.clear_tool_schemas()


def test_single_source_freezes_a_schema_edit_outside_the_boundary() -> None:
    """D1 / Q2-A：同名工具被编辑，冻结跳的字节必须是整表里那份，不是活实例的新字节。"""
    key = 'web:edit-frozen'
    tools = {'alpha': _Tool('alpha', 'v1'), 'beta': _Tool('beta')}
    runner = _runner(tools)
    session = _session(key)
    before = _render(runner, session, key, ['alpha', 'beta'])
    assert _names(before)[:2] == ['alpha', 'beta']

    tools['alpha'].rewrite('v2 edited outside any reprint boundary')
    after = _render(runner, _session(key), key, ['alpha', 'beta'], mode='pinned_frozen')
    descriptions = {str((item.get('function') or {}).get('name')): str((item.get('function') or {}).get('description')) for item in after}
    assert descriptions['alpha'] == 'v1'
    # 声明面与注册表的实例都不再是来源：整表供得出全部名字时一次重写都不该发生。
    writes_after_seed = pinned_tool_bundle._entries.get(key) or {}
    assert writes_after_seed.get('revision')


def test_deleted_tool_survives_a_restart_with_identical_bytes() -> None:
    """阶段 1 那条红测在单源下必须转绿：整表持久 + 恢复 ⇒ 首跳逐字节等于重启前最后一跳。"""
    key = 'web:restart-green'
    tools = {'alpha': _Tool('alpha'), 'beta': _Tool('beta')}
    runner = _runner(tools)
    session = _session(key)
    before = _render(runner, session, key, ['alpha', 'beta'])
    path = str(session._frontdoor_pinned_tool_bundle_path)
    revision = str(session._frontdoor_pinned_tool_bundle_revision)

    runner._loop.tools.pop('beta', None)
    assert retired_schemas.lookup_tool_schema('beta') is None
    pinned_tool_bundle._entries.clear()

    restored = _session(key)
    assert pinned_tool_bundle.restore_pinned_tool_bundle_state(
        restored, session_key=key, path=path, revision=revision
    ) is True
    after = _render(runner, restored, key, ['alpha', 'beta'], mode='pinned_frozen')
    assert after == before, '恢复后不该重建、也不该按缺名落占位'


def test_missing_name_rebuilds_once_and_then_stays_byte_stable() -> None:
    """整表缺名 ⇒ 就地重建一次；重建后的那一跳再读就不该又重写。"""
    key = 'web:rebuild'
    tools = {'alpha': _Tool('alpha'), 'beta': _Tool('beta')}
    runner = _runner(tools)
    session = _session(key)
    _render(runner, session, key, ['alpha'])
    first_revision = str(session._frontdoor_pinned_tool_bundle_revision)

    rebuilt = _render(runner, _session(key), key, ['alpha', 'beta'], mode='pinned_frozen')
    assert _names(rebuilt)[:2] == ['alpha', 'beta'], '缺名的那跳不能把 beta 摘掉'
    second_revision = str(pinned_tool_bundle._entries[key]['revision'])
    assert second_revision != first_revision, '重建必须重写整表并换 revision'

    settled_session = _session(key)
    settled = _render(runner, settled_session, key, ['alpha', 'beta'], mode='pinned_frozen')
    assert settled == rebuilt, '重建之后的冻结跳必须逐字节稳定'
    assert str(pinned_tool_bundle._entries[key]['revision']) == second_revision, '冻结跳不该再重写整表'


def test_unrenderable_name_is_pinned_as_placeholder_and_counted() -> None:
    """三份来源都没有 ⇒ 占位留在名单里、可数，并且下一跳逐字节复用同一份占位。"""
    key = 'web:placeholder'
    tools = {'alpha': _Tool('alpha')}
    runner = _runner(tools)
    state_out: dict = {}
    first = _render(runner, _session(key), key, ['alpha', 'ghost'], state_out=state_out)
    assert 'ghost' in _names(first)
    assert state_out['bundle_placeholder_names'] == ['ghost']
    assert state_out['frontdoor_tool_schema_hits']['placeholder'] == 1

    second = _render(runner, _session(key), key, ['alpha', 'ghost'], mode='pinned_frozen')
    assert second == first


def test_denied_tools_names_a_pinned_placeholder_name() -> None:
    """单源后尾块 `denied_tools` 按名单判：占位名也点名（与节点侧同形）。

    可渲染性那道筛在这里不再起作用——名字留在钉住清单里就一定带着一份参数表（供不出的是
    占位），所以"这一跳在 `tools[]` 里"对名单里的每个名字恒真。
    """
    runner = _runner({'alpha': _Tool('alpha')})
    runner._frontdoor_live_granted_tool_names = lambda session_key: ['alpha', 'submit_next_stage', 'silent']
    state = {
        'provider_tool_names': ['alpha', 'ghost'],
        'session_key': 'web:denied-line',
        'hydrated_tool_names': [],
        'rbac_visible_tool_names': [],
    }

    assert runner._frontdoor_declared_denied_tool_names_for_state(state) == ['ghost']


def test_delta_estimate_stays_incomparable_when_both_sides_share_one_basis() -> None:
    """同一口径下工具哈希不等 ⇒ 照旧判不可比（放宽只认口径变化这一件事）。"""
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    previous_messages = [
        {'role': 'system', 'content': 'SYSTEM'},
        {'role': 'user', 'content': 'hello'},
    ]
    current_messages = [*previous_messages, {'role': 'user', 'content': 'next ' * 40}]

    _delta, comparable, _shrink = runner._frontdoor_append_only_delta_estimate_tokens(
        previous_request_messages=previous_messages,
        current_request_messages=current_messages,
        previous_tool_schemas=[_schema_payload('alpha', 'v1')],
        current_tool_schemas=[_schema_payload('alpha', 'v2')],
        previous_tools_record_basis='wire',
        current_tools_record_basis='wire',
    )

    assert comparable is False


def test_delta_estimate_survives_the_record_basis_switch() -> None:
    """C-1：口径从 proxy 换到 wire 那一跳，工具哈希必然不等，但比对不能因此作废。

    `comparable=False` 会把这一跳的 usage-first 估算整条换成全量 preview，并连带
    `required_context_window_tokens`（路由成员筛）与压缩触发判定——一条纯口径变化不该
    让这些下游跟着动。
    """
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    previous_messages = [
        {'role': 'system', 'content': 'SYSTEM'},
        {'role': 'user', 'content': 'hello'},
    ]
    current_messages = [*previous_messages, {'role': 'user', 'content': 'next ' * 40}]

    delta, comparable, _shrink = runner._frontdoor_append_only_delta_estimate_tokens(
        previous_request_messages=previous_messages,
        current_request_messages=current_messages,
        # proxy 那份比 wire 少尾部两颗合成控制工具，哈希因此不同
        previous_tool_schemas=[_schema_payload('alpha', 'v1')],
        current_tool_schemas=[_schema_payload('alpha', 'v1'), _schema_payload('silent', 'reply nothing')],
        previous_tools_record_basis='proxy',
        current_tools_record_basis='wire',
    )

    assert comparable is True
    assert delta > 0


def test_delta_estimate_defaults_to_proxy_for_legacy_records() -> None:
    """不传口径参数（存量记录的读法）时，两侧都按 proxy 比对：哈希不等仍判不可比。

    默认值就是 legacy 语义，这条断言替"旧工件没有这个键"那一档兜住读侧。
    """
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    previous_messages = [
        {'role': 'system', 'content': 'SYSTEM'},
        {'role': 'user', 'content': 'hello'},
    ]
    current_messages = [*previous_messages, {'role': 'user', 'content': 'next ' * 40}]

    _delta, comparable, _shrink = runner._frontdoor_append_only_delta_estimate_tokens(
        previous_request_messages=previous_messages,
        current_request_messages=current_messages,
        previous_tool_schemas=[_schema_payload('alpha', 'v1')],
        current_tool_schemas=[_schema_payload('alpha', 'v2')],
    )

    assert comparable is False


def test_prompt_cache_key_is_insensitive_to_tool_schemas() -> None:
    """方案 C 的前提：cache key 对 `tool_schemas` 不敏感，由这条测试钉住而不是靠人记。

    哪天有人让 key 变成 tool-aware，这条会红——那时再决定是否给 contract builder 加
    "记录用数组"形参，不在今天为假想需求预付复杂度。
    """
    messages = [
        {'role': 'system', 'content': 'stable head'},
        {'role': 'user', 'content': 'bootstrap user'},
    ]
    one = [{'type': 'function', 'function': {'name': 'a', 'description': 'A', 'parameters': {}}}]
    two = [*one, {'type': 'function', 'function': {'name': 'silent', 'description': 'S', 'parameters': {}}}]

    assert (
        chat_backend.build_stable_prompt_cache_key(messages, one, 'provider/model')
        == chat_backend.build_stable_prompt_cache_key(messages, two, 'provider/model')
    )
    assert (
        chat_backend.build_session_prompt_cache_key(
            session_key='web:x', provider_model='provider/model', stable_messages=messages, tool_schemas=one
        )
        == chat_backend.build_session_prompt_cache_key(
            session_key='web:x', provider_model='provider/model', stable_messages=messages, tool_schemas=two
        )
    )
