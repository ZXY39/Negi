"""`enter_wait`: when it may park, and the tool's forwarding."""

from __future__ import annotations

import json

import pytest

from main.runtime.internal_tools import EnterWaitTool
from main.runtime.node_wake import WAKE_ON_ALL_CHILDREN, WAKE_ON_ANY, enter_wait_verdict
from main.runtime.tool_names import ENTER_WAIT_TOOL_NAME


class TestVerdict:
    def test_plain_wait_parks_when_mailbox_is_empty(self):
        assert enter_wait_verdict(wake_on=WAKE_ON_ANY, unread_count=0) == (True, '')

    def test_existing_mail_refuses_the_park(self):
        # R3：入睡前到达的消息不能变成一次错过。
        assert enter_wait_verdict(wake_on=WAKE_ON_ANY, unread_count=2) == (False, 'unread_present')

    def test_all_children_parks_while_a_child_is_unsettled(self):
        children = [{'node_id': 'a', 'status': 'in_progress'}]
        assert enter_wait_verdict(
            wake_on=WAKE_ON_ALL_CHILDREN,
            unread_count=0,
            children=children,
            unread_final_sources=[],
        ) == (True, '')

    def test_all_children_does_not_park_when_everything_is_in(self):
        children = [{'node_id': 'a', 'status': 'success'}]
        assert enter_wait_verdict(
            wake_on=WAKE_ON_ALL_CHILDREN,
            unread_count=1,
            children=children,
            unread_final_sources=['a'],
        ) == (False, 'no_unsettled_children')

    def test_all_children_with_no_children_never_parks(self):
        # 空停靠 = 没人能唤醒的节点。
        assert enter_wait_verdict(wake_on=WAKE_ON_ALL_CHILDREN, unread_count=0) == (
            False,
            'no_unsettled_children',
        )

    def test_all_children_ignores_unreported_settled_child(self):
        children = [{'node_id': 'a', 'status': 'success'}]
        assert enter_wait_verdict(
            wake_on=WAKE_ON_ALL_CHILDREN,
            unread_count=0,
            children=children,
            unread_final_sources=[],
        ) == (True, '')

    def test_acceptance_child_does_not_hold_the_park(self):
        children = [
            {'node_id': 'a', 'status': 'success', 'node_kind': 'execution'},
            {'node_id': 'v', 'status': 'in_progress', 'node_kind': 'acceptance'},
        ]
        assert enter_wait_verdict(
            wake_on=WAKE_ON_ALL_CHILDREN,
            unread_count=1,
            children=children,
            unread_final_sources=['a'],
        ) == (False, 'no_unsettled_children')

    def test_unknown_policy_is_refused(self):
        may_park, reason = enter_wait_verdict(wake_on='later', unread_count=0)
        assert may_park is False
        assert reason.startswith('unknown_wake_on')


class _Capture:
    def __init__(self):
        self.calls: list[dict] = []


def _tool(record: _Capture):
    async def park(**payload):
        record.calls.append(payload)
        return {'status': 'parked', 'wake_on': payload.get('wake_on'), 'note': payload.get('note')}

    return EnterWaitTool(park)


class TestTool:
    def test_name_and_surface(self):
        tool = _tool(_Capture())
        assert tool.name == ENTER_WAIT_TOOL_NAME
        schema = tool.parameters
        assert schema['required'] == []
        assert schema['properties']['wake_on']['enum'] == ['any', 'all_children']

    def test_timeout_parameter_hidden(self):
        assert _tool(_Capture()).hide_universal_timeout_parameter is True

    @pytest.mark.asyncio
    async def test_defaults_to_any_with_empty_note(self):
        record = _Capture()
        raw = await _tool(record).execute()
        assert record.calls == [{'wake_on': 'any', 'note': ''}]
        assert json.loads(raw)['status'] == 'parked'

    @pytest.mark.asyncio
    async def test_forwards_policy_and_note(self):
        record = _Capture()
        await _tool(record).execute(wake_on='all_children', note='等三个子节点')
        assert record.calls == [{'wake_on': 'all_children', 'note': '等三个子节点'}]
