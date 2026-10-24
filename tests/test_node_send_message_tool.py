"""`send_message` tool surface: schema, forwarding, and honest failure."""

from __future__ import annotations

import json

import pytest

from main.runtime.internal_tools import SendMessageTool
from main.runtime.tool_names import SEND_MESSAGE_TOOL_NAME


def _tool(record: list[dict]):
    async def deliver(**payload):
        record.append(payload)
        return {'ok': True, 'results': [{'target': 'parent', 'status': 'delivered'}], 'legal_targets': ['parent']}

    return SendMessageTool(deliver)


class TestSurface:
    def test_name(self):
        assert _tool([]).name == SEND_MESSAGE_TOOL_NAME

    def test_required_params(self):
        schema = _tool([]).parameters
        assert schema['required'] == ['to', 'content']

    def test_to_accepts_single_or_list(self):
        to_schema = _tool([]).parameters['properties']['to']
        assert to_schema['type'] == ['string', 'array']
        assert to_schema['items'] == {'type': 'string'}

    def test_only_declared_fields_are_exposed(self):
        schema = _tool([]).parameters
        assert set(schema['properties']) == {'to', 'content', 'ref'}

    def test_timeout_parameter_hidden(self):
        # 内部协议工具不向模型暴露 timeout_seconds。
        assert _tool([]).hide_universal_timeout_parameter is True

    def test_validation_rejects_empty_content(self):
        errors = _tool([]).validate_params({'to': 'parent', 'content': '   '})
        assert 'content must not be empty' in errors


class TestExecution:
    @pytest.mark.asyncio
    async def test_forwards_every_field(self):
        record: list[dict] = []
        raw = await _tool(record).execute(
            to=['a', 'b'],
            content='把结果发我',
            ref='artifact:1',
        )
        assert record == [
            {
                'to': ['a', 'b'],
                'content': '把结果发我',
                'ref': 'artifact:1',
            }
        ]
        payload = json.loads(raw)
        assert payload['ok'] is True
        assert payload['legal_targets'] == ['parent']

    @pytest.mark.asyncio
    async def test_returns_rejections_verbatim(self):
        async def deliver(**payload):
            return {
                'ok': False,
                'results': [{'target': 'x', 'status': 'rejected', 'reason': 'not_messageable'}],
                'legal_targets': ['parent', 'c1'],
            }

        raw = await SendMessageTool(deliver).execute(to='x', content='hi')
        payload = json.loads(raw)
        assert payload['ok'] is False
        assert payload['results'][0]['reason'] == 'not_messageable'
        assert payload['legal_targets'] == ['parent', 'c1']
