"""Reachable-session roster + non-waking delta contract (session links, model face)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.runtime import skill_roster_notice as srn
from g3ku.runtime.frontdoor.tool_contract import (
    REACHABLE_SESSION_ROSTER_HEADING,
    frontdoor_pinned_contract_text,
    reachable_session_aliases_from_text,
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
    text = render_reachable_session_roster([{'alias': 's2', 'title': '研究会话'}])
    assert text == f'{REACHABLE_SESSION_ROSTER_HEADING}\n- s2 | 研究会话'
    # Pipe in titles keeps the column shape; long titles truncate.
    text = render_reachable_session_roster([{'alias': 's2', 'title': 'a|b' + 'x' * 80}])
    assert 'a｜b' in text
    assert '…' in text
    many = [{'alias': f's{i}', 'title': f't{i}'} for i in range(1, 12)]
    text = render_reachable_session_roster(many)
    assert '另有 3 条，用 list_sessions 查看' in text
    assert '- s8 |' in text and '- s9 |' not in text


def test_aliases_from_text_parses_section_only():
    pinned = (
        '## Runtime Contract (pinned)\n'
        'candidate_skills (loadable with `load_skill_context`):\n'
        '- `x` | X | desc\n'
        f'{REACHABLE_SESSION_ROSTER_HEADING}\n'
        '- s2 | 研究会话\n'
        '- s3\n'
        '另有 2 条，用 list_sessions 查看\n'
        '### 在跑的后台进程\n'
        '- 123 | purpose\n'
    )
    assert reachable_session_aliases_from_text(pinned) == ['s2', 's3']
    assert reachable_session_aliases_from_text('') == []
    assert reachable_session_aliases_from_text('### 在挂的长期任务\n- task:x | g | 在挂') == []


def _links_provider(entries):
    return lambda **kwargs: {'reachable': entries}


def test_delta_first_hop_seeds_from_covered_then_announces_once():
    covered = ['s2']
    live = [{'alias': 's2', 'title': '老会话'}, {'alias': 's3', 'title': '新会话'}]
    message, _ = srn.deliver_for_scope(
        'session:web:a',
        [],
        now='t0',
        session_links_provider=_links_provider(live),
        covered_session_aliases=covered,
    )
    assert message is not None
    body = message['content']
    assert srn.RUNTIME_NOTICE_MARKER in body
    assert srn.SESSION_LINKS_ADDED_HEADING in body
    assert '- s3 | 新会话' in body
    # s2 was covered by the pinned head: not re-announced.
    assert '- s2' not in body
    # Same live set next hop: nothing new, no message.
    message, _ = srn.deliver_for_scope(
        'session:web:a',
        [],
        now='t1',
        session_links_provider=_links_provider(live),
        covered_session_aliases=covered,
    )
    assert message is None


def test_delta_announces_removal():
    live = [{'alias': 's2', 'title': 't'}]
    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider(live), covered_session_aliases=[],
    )
    message, _ = srn.deliver_for_scope(
        'session:web:a', [], now='t1',
        session_links_provider=_links_provider([]), covered_session_aliases=[],
    )
    assert message is not None
    assert srn.SESSION_LINKS_REMOVED_HEADING in message['content']
    assert '- s2' in message['content']


def test_delta_cursor_survives_normalize_and_has_state():
    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider([{'alias': 's2', 'title': 't'}]),
        covered_session_aliases=[],
    )
    context = srn.notice_context_for('session:web:a')
    assert context['session_link_cursor'] == {'known': ['s2'], 'initialized': True}
    roundtrip = srn.normalize_skill_roster_notice_context(context)
    assert roundtrip['session_link_cursor'] == {'known': ['s2'], 'initialized': True}
    assert srn.context_has_state(roundtrip) is True
    # Malformed payload drops to defaults instead of crashing.
    assert srn.normalize_skill_roster_notice_context({'session_link_cursor': 'bad'})['session_link_cursor'] == {
        'known': [], 'initialized': False,
    }


def test_compaction_reset_reseeds_from_new_head():
    live = [{'alias': 's2', 'title': 't'}]
    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider(live), covered_session_aliases=[],
    )
    srn.reset_contract_sections_for_compaction('session:web:a')
    context = srn.notice_context_for('session:web:a')
    assert context['session_link_cursor'] == {'known': [], 'initialized': False}
    # After compaction the head covers s2: no re-announcement.
    message, _ = srn.deliver_for_scope(
        'session:web:a', [], now='t1',
        session_links_provider=_links_provider(live), covered_session_aliases=['s2'],
    )
    assert message is None


def test_truncation_resets_link_cursor():
    srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider([{'alias': 's2', 'title': 't'}]),
        covered_session_aliases=[],
    )
    dropped = srn.drop_records_after(srn.notice_context_for('session:web:a'), cutoff_iso='')
    assert dropped.get('session_link_cursor', {'known': [], 'initialized': False})['initialized'] is False


def test_echo_guard_strips_session_links_only_notice():
    message, _ = srn.deliver_for_scope(
        'session:web:a', [], now='t0',
        session_links_provider=_links_provider([{'alias': 's2', 'title': 't'}]),
        covered_session_aliases=[],
    )
    echoed = '我的回答。' + message['content']
    assert srn.strip_skill_roster_notice_echo(echoed) == '我的回答。'


def test_pinned_head_carries_roster_only_with_provider():
    session = SimpleNamespace()
    entries = [{'alias': 's2', 'title': '研究会话'}]
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
    assert '- s2 | 研究会话' in with_provider
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


def test_list_sessions_tool_handler(tmp_path):
    import importlib.util

    from main.storage.sqlite_store import SQLiteTaskStore

    spec = importlib.util.spec_from_file_location(
        'list_sessions_tool', 'tools/list_sessions/main/tool.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    service = SimpleNamespace(store=store)

    async def _startup():
        return None

    service.startup = _startup
    service.replace_session_links = lambda key, targets: None
    # Wire the real service methods used by the roster provider.
    from main.service.runtime_service import MainRuntimeService

    real = MainRuntimeService.__new__(MainRuntimeService)
    real.store = store
    service.reachable_session_roster = real.reachable_session_roster
    service.get_session_alias = real.get_session_alias
    store.add_session_link('web:ceo-a', 'web:ceo-b', created_at='t0')
    store.get_or_create_session_alias('web:ceo-b', created_at='t0')
    handler = module.build(SimpleNamespace(services=SimpleNamespace(main_task_service=service)))
    assert handler is not None
    result = asyncio.run(handler.execute(__g3ku_runtime={'session_key': 'web:ceo-a'}))
    import json

    payload = json.loads(result)
    assert payload['ok'] is True
    assert payload['count'] == 1
    assert payload['sessions'][0]['alias'] == 's1'
    assert payload['sessions'][0]['trust'] == 'owner'
    store.close()
