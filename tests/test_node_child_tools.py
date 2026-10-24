"""Read-only child inspection tools: surface and forwarding."""

from __future__ import annotations

import json

import pytest

from main.runtime.child_tools import (
    ListChildNodesTool,
    ReadChildProgressTool,
    ViewNodeTreeTool,
)
from main.runtime.tool_names import (
    LIST_CHILD_NODES_TOOL_NAME,
    READ_CHILD_PROGRESS_TOOL_NAME,
    VIEW_NODE_TREE_TOOL_NAME,
)


class TestListChildNodes:
    def test_surface(self):
        tool = ListChildNodesTool(_noop)
        assert tool.name == LIST_CHILD_NODES_TOOL_NAME
        assert tool.parameters['required'] == []
        assert tool.hide_universal_timeout_parameter is True

    @pytest.mark.asyncio
    async def test_forwards_filters(self):
        seen: list[dict] = []

        async def fake(**payload):
            seen.append(payload)
            return {'children': [], 'count': 0}

        raw = await ListChildNodesTool(fake).execute(state=['running'], settled=False)
        assert seen == [{'state': ['running'], 'settled': False}]
        assert json.loads(raw)['count'] == 0


class TestReadChildProgress:
    def test_surface(self):
        tool = ReadChildProgressTool(_noop)
        assert tool.name == READ_CHILD_PROGRESS_TOOL_NAME
        assert tool.parameters['required'] == ['node_id']

    @pytest.mark.asyncio
    async def test_passes_rejections_through(self):
        async def fake(**payload):
            return {
                'ok': False,
                'error': 'not_a_direct_child',
                'node_id': payload['node_id'],
                'legal_targets': ['node:a'],
            }

        raw = await ReadChildProgressTool(fake).execute(node_id='node:x')
        payload = json.loads(raw)
        assert payload['error'] == 'not_a_direct_child'
        assert payload['legal_targets'] == ['node:a']


class TestViewNodeTree:
    def test_surface(self):
        tool = ViewNodeTreeTool(_noop)
        assert tool.name == VIEW_NODE_TREE_TOOL_NAME
        assert tool.parameters['required'] == []

    @pytest.mark.asyncio
    async def test_truncation_is_reported(self):
        async def fake(**payload):
            return {'ok': True, 'nodes': [], 'truncated': True, 'pointer': 'use read_child_progress'}

        raw = await ViewNodeTreeTool(fake).execute(depth=2)
        payload = json.loads(raw)
        assert payload['truncated'] is True
        assert payload['pointer']


async def _noop(**payload):
    return {'ok': True, 'payload': payload}
