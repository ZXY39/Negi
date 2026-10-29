"""`enter_wait`: when it may park, and the tool's forwarding."""

from __future__ import annotations

import json

import pytest

from main.runtime.internal_tools import EnterWaitTool
from main.runtime.node_wake import enter_wait_verdict
from main.runtime.tool_names import ENTER_WAIT_TOOL_NAME


class TestVerdict:
    def test_parks_when_mailbox_is_empty(self):
        assert enter_wait_verdict(unread_count=0) == (True, '')

    def test_existing_mail_refuses_the_park(self):
        # R3：入睡前到达的消息不能变成一次错过。
        assert enter_wait_verdict(unread_count=2) == (False, 'unread_present')

    def test_no_child_set_is_refused(self):
        # 停靠不再看子节点；空信箱 + 无子节点仍可合法等待一条消息。
        assert enter_wait_verdict(unread_count=0) == (True, '')


class _Capture:
    def __init__(self):
        self.calls: list[dict] = []


def _tool(record: _Capture):
    async def park(**payload):
        record.calls.append(payload)
        return {'status': 'parked', 'note': payload.get('note')}

    return EnterWaitTool(park)


class TestTool:
    def test_name_and_surface(self):
        tool = _tool(_Capture())
        assert tool.name == ENTER_WAIT_TOOL_NAME
        schema = tool.parameters
        assert schema['required'] == []
        assert set(schema['properties']) == {'note', 'waiting_for'}

    def test_timeout_parameter_hidden(self):
        assert _tool(_Capture()).hide_universal_timeout_parameter is True

    @pytest.mark.asyncio
    async def test_defaults_to_empty_note(self):
        record = _Capture()
        raw = await _tool(record).execute()
        assert record.calls == [{'note': '', 'waiting_for': None}]
        assert json.loads(raw)['status'] == 'parked'

    @pytest.mark.asyncio
    async def test_forwards_note(self):
        record = _Capture()
        await _tool(record).execute(note='等三个子节点')
        assert record.calls == [{'note': '等三个子节点', 'waiting_for': None}]

    @pytest.mark.asyncio
    async def test_forwards_waiting_for(self):
        record = _Capture()
        await _tool(record).execute(note='等兄弟', waiting_for='node:2')
        assert record.calls == [{'note': '等兄弟', 'waiting_for': 'node:2'}]


def test_take_wait_note_is_one_shot():
    from types import SimpleNamespace

    from main.runtime.node_runner import NodeRunner

    node = SimpleNamespace(metadata={NodeRunner.NODE_WAIT_NOTE_KEY: '等三个子节点'})
    updates = []

    class _Store:
        def get_node(self, node_id):
            _ = node_id
            return node

    class _Log:
        def update_node_metadata(self, node_id, mutate):
            updates.append(mutate(dict(node.metadata)))

    runner = object.__new__(NodeRunner)
    runner._store = _Store()
    runner._log_service = _Log()

    first = runner._take_wait_note_message(node_id='node:1')
    assert first == {'role': 'user', 'content': '[WAIT_NOTE] 你上次停靠时在等：等三个子节点'}
    assert NodeRunner.NODE_WAIT_NOTE_KEY not in updates[0]
    node.metadata.pop(NodeRunner.NODE_WAIT_NOTE_KEY)
    assert runner._take_wait_note_message(node_id='node:1') is None
