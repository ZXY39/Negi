"""Reachable-session roster + non-waking delta contract (session links, model face)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from g3ku.runtime import skill_roster_notice as srn
from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.external_sessions import reset_external_session_registry
from g3ku.runtime.frontdoor.tool_contract import (
    REACHABLE_SESSION_ROSTER_HEADING,
    frontdoor_pinned_contract_text,
    reachable_session_ids_from_text,
    render_reachable_session_roster,
)


@pytest.fixture(autouse=True)
def _isolated_stores():
    srn.clear_skill_roster_notice_stores()
    yield
    srn.clear_skill_roster_notice_stores()


def test_roster_render_empty_lines_and_overflow():
    assert render_reachable_session_roster([]) == ''
    assert render_reachable_session_roster(None) == ''
    text = render_reachable_session_roster([{'short_id': '3f9a1b2c', 'title': '研究会话'}])
    assert text == f'{REACHABLE_SESSION_ROSTER_HEADING}\n- 3f9a1b2c | 研究会话'
    # Pipe in titles keeps the column shape; long titles truncate.
    text = render_reachable_session_roster([{'short_id': '3f9a1b2c', 'title': 'a|b' + 'x' * 80}])
    assert 'a｜b' in text
    assert '…' in text
    many = [{'short_id': f'id{i:06d}', 'title': f't{i}'} for i in range(1, 12)]
    text = render_reachable_session_roster(many)
    assert '另有 3 条，用 list_sessions 查看' in text
    assert '- id000008 |' in text and '- id000009 |' not in text


def test_ids_from_text_parses_section_only():
    pinned = (
        '## Runtime Contract (pinned)\n'
        'candidate_skills (loadable with `load_skill_context`):\n'
        '- `x` | X | desc\n'
        f'{REACHABLE_SESSION_ROSTER_HEADING}\n'
        '- 3f9a1b2c | 研究会话\n'
        '- 77aa0011\n'
        '另有 2 条，用 list_sessions 查看\n'
        '### 在跑的后台进程\n'
        '- 123 | purpose\n'
    )
    assert reachable_session_ids_from_text(pinned) == ['3f9a1b2c', '77aa0011']
    assert reachable_session_ids_from_text('') == []
    assert reachable_session_ids_from_text('### 在挂的长期任务\n- task:x | g | 在挂') == []


def _links_provider(entries):
    return lambda **kwargs: {'reachable': entries}


def test_delta_first_hop_seeds_from_covered_then_announces_once():
    covered = ['3f9a1b2c']
    live = [
        {'short_id': '3f9a1b2c', 'title': '老会话'},
        {'short_id': '77aa0011', 'title': '新会话'},
    ]
    message, _ = srn.deliver_for_scope(
        'session:web:a',
        [],
        now='t0',
        session_links_provider=_links_provider(live),
        covered_session_ids=covered,
    )
    assert message is not None
    body = message['content']
    assert srn.RUNTIME_NOTICE_MARKER in body
    assert srn.SESSION_LINKS_ADDED_HEADING in body
    assert '- 77aa0011 | 新会话' in body
    # The covered id was already pinned in the head: not re-announced.
    assert '- 3f9a1b2c' not in body
    # Same live set next hop: nothing new, no message.
    message, _ = srn.deliver_for_scope(
        'session:web:a',
        [],
        now='t1',
        session_links_provider=_links_provider(live),
        covered_session_ids=covered,
    )
    assert message is None


def test_delta_announces_removal():
    live = [{'short_id': '3f9a1b2c', 'title': 't'}]
    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider(live), covered_session_ids=[],
    )
    message, _ = srn.deliver_for_scope(
        'session:web:a', [], now='t1',
        session_links_provider=_links_provider([]), covered_session_ids=[],
    )
    assert message is not None
    assert srn.SESSION_LINKS_REMOVED_HEADING in message['content']
    assert '- 3f9a1b2c' in message['content']


def test_delta_normalizes_legacy_cursor_instead_of_replaying_everything():
    """存量台账记的是 s{seq}：provider 归一后不播一场假的「全移除 + 全新增」。"""
    live = [{'short_id': '3f9a1b2c', 'title': 't'}]

    def provider(**kwargs):
        known = list(kwargs.get('known_short_ids') or [])
        mapped = ['3f9a1b2c' if item == 's2' else item for item in known]
        return {'reachable': live, 'known': mapped}

    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=provider, covered_session_ids=[],
    )
    # Rewrite the cursor to the legacy spelling an upgraded session still carries.
    context = srn.notice_context_for('session:web:a')
    context['session_link_cursor'] = {'known': ['s2'], 'initialized': True}
    message, _ = srn.deliver_for_scope(
        'session:web:a', [], now='t1',
        session_links_provider=provider, covered_session_ids=[],
    )
    assert message is None
    assert srn.notice_context_for('session:web:a')['session_link_cursor'] == {
        'known': ['3f9a1b2c'], 'initialized': True,
    }


def test_delta_cursor_survives_normalize_and_has_state():
    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider([{'short_id': '3f9a1b2c', 'title': 't'}]),
        covered_session_ids=[],
    )
    context = srn.notice_context_for('session:web:a')
    assert context['session_link_cursor'] == {'known': ['3f9a1b2c'], 'initialized': True}
    roundtrip = srn.normalize_skill_roster_notice_context(context)
    assert roundtrip['session_link_cursor'] == {'known': ['3f9a1b2c'], 'initialized': True}
    assert srn.context_has_state(roundtrip) is True
    # Malformed payload drops to defaults instead of crashing.
    assert srn.normalize_skill_roster_notice_context({'session_link_cursor': 'bad'})['session_link_cursor'] == {
        'known': [], 'initialized': False,
    }


def test_compaction_reset_reseeds_from_new_head():
    live = [{'short_id': '3f9a1b2c', 'title': 't'}]
    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider(live), covered_session_ids=[],
    )
    srn.reset_contract_sections_for_compaction('session:web:a')
    context = srn.notice_context_for('session:web:a')
    assert context['session_link_cursor'] == {'known': [], 'initialized': False}
    # After compaction the head covers the id: no re-announcement.
    message, _ = srn.deliver_for_scope(
        'session:web:a', [], now='t1',
        session_links_provider=_links_provider(live), covered_session_ids=['3f9a1b2c'],
    )
    assert message is None


def test_truncation_resets_link_cursor():
    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider([{'short_id': '3f9a1b2c', 'title': 't'}]),
        covered_session_ids=[],
    )
    dropped = srn.drop_records_after(srn.notice_context_for('session:web:a'), cutoff_iso='')
    assert dropped.get('session_link_cursor', {'known': [], 'initialized': False})['initialized'] is False


def test_echo_guard_strips_session_links_only_notice():
    message, _ = srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider([{'short_id': '3f9a1b2c', 'title': 't'}]),
        covered_session_ids=[],
    )
    echoed = '我的回答。' + message['content']
    assert srn.strip_skill_roster_notice_echo(echoed) == '我的回答。'


def test_pinned_head_carries_roster_only_with_provider():
    session = SimpleNamespace()
    entries = [{'short_id': '3f9a1b2c', 'title': '研究会话'}]
    with_provider = frontdoor_pinned_contract_text(
        session,
        skill_items=[],
        exec_runtime_policy=None,
        session_temp_dir=None,
        skill_roster_epoch=0,
        session_key='web:a',
        reachable_sessions_provider=lambda: entries,
    )
    assert REACHABLE_SESSION_ROSTER_HEADING in with_provider
    assert '- 3f9a1b2c | 研究会话' in with_provider
    # Node lane passes no provider: section absent.
    without = frontdoor_pinned_contract_text(
        SimpleNamespace(),
        skill_items=[],
        exec_runtime_policy=None,
        session_temp_dir=None,
        skill_roster_epoch=0,
        session_key='web:b',
    )
    assert REACHABLE_SESSION_ROSTER_HEADING not in without


def _list_sessions_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        'list_sessions_tool', 'tools/list_sessions/main/tool.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def directory_env(tmp_path, monkeypatch):
    """目录取数落在 tmp 工作区：不读本机真实会话清单。"""
    from main.service.runtime_service import MainRuntimeService
    from main.storage.sqlite_store import SQLiteTaskStore

    monkeypatch.setattr(wcs, 'workspace_path', lambda: tmp_path)
    reset_external_session_registry()
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    service = MainRuntimeService.__new__(MainRuntimeService)
    service.store = store

    async def _startup():
        return None

    service.startup = _startup
    from g3ku.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    for key in ('web:ceo-aaaa1111bbbb', 'web:ceo-cccc2222dddd'):
        session = manager.get_or_create(key)
        session.metadata['title'] = f'标题 {key[-4:]}'
        manager.save(session)
    yield SimpleNamespace(service=service, store=store, manager=manager, workspace=tmp_path)
    store.close()
    reset_external_session_registry()


def test_list_sessions_linked_only_without_directory_candidates(directory_env):
    module = _list_sessions_module()
    service = directory_env.service
    service.replace_session_links('web:ceo-aaaa1111bbbb', ['web:ceo-cccc2222dddd'])
    handler = module.build(SimpleNamespace(services=SimpleNamespace(main_task_service=service)))
    assert handler is not None
    payload = json.loads(
        asyncio.run(handler.execute(__g3ku_runtime={'session_key': 'web:ceo-aaaa1111bbbb'}))
    )
    assert payload['ok'] is True
    # Both sessions exist; only the linked one is a peer, the other is a candidate.
    rows = {row['short_id']: row for row in payload['sessions']}
    linked_id = service.session_short_id('web:ceo-cccc2222dddd')
    candidate_id = service.session_short_id('web:ceo-aaaa1111bbbb')
    assert rows[linked_id]['linked'] is True
    assert rows[linked_id]['trust'] == 'owner'
    assert rows[linked_id]['title'] == '标题 dddd'
    assert candidate_id not in rows  # self is never a candidate
    assert payload['count'] == len(payload['sessions'])


def test_list_sessions_owner_sees_unlinked_candidates(directory_env):
    module = _list_sessions_module()
    service = directory_env.service
    third = directory_env.manager.get_or_create('web:ceo-eeee3333ffff')
    third.metadata['title'] = '未关联会话'
    directory_env.manager.save(third)
    handler = module.build(SimpleNamespace(services=SimpleNamespace(main_task_service=service)))
    payload = json.loads(
        asyncio.run(handler.execute(__g3ku_runtime={'session_key': 'web:ceo-aaaa1111bbbb'}))
    )
    rows = {row['title']: row for row in payload['sessions']}
    assert rows['未关联会话']['linked'] is False
    assert rows['未关联会话']['short_id'] == service.session_short_id('web:ceo-eeee3333ffff')
    assert 'links them automatically' in payload['notice']


def test_list_sessions_third_party_caller_gets_linked_only(directory_env):
    module = _list_sessions_module()
    service = directory_env.service
    # An unregistered ext key is third party by fail-closed default.
    sender = 'ext:qq-official:stranger'
    service.replace_session_links(sender, ['web:ceo-cccc2222dddd'])
    handler = module.build(SimpleNamespace(services=SimpleNamespace(main_task_service=service)))
    payload = json.loads(asyncio.run(handler.execute(__g3ku_runtime={'session_key': sender})))
    assert payload['count'] == 1
    assert payload['sessions'][0]['linked'] is True
    assert payload['sessions'][0]['short_id'] == service.session_short_id('web:ceo-cccc2222dddd')
