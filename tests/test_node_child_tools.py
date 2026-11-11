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

    def test_documents_restriction_switch_semantics(self):
        tool = ListChildNodesTool(_noop)
        assert 'never narrows' in tool.description
        assert 'entire subtree' in tool.description
        assert 'not a filter' in tool.parameters['properties']['state']['description']
        assert 'not a filter' in tool.parameters['properties']['settled']['description']

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


class _FakeNode:
    """NodeRecord 形状的替身：`_list_children` 只读这些属性。"""

    def __init__(
        self,
        node_id,
        *,
        parent_node_id='',
        goal='',
        node_kind='execution',
        status='in_progress',
        is_paused=False,
        pause_requested=False,
        metadata=None,
        created_at='2026-11-12T00:00:00+08:00',
    ):
        self.node_id = node_id
        self.parent_node_id = parent_node_id
        self.goal = goal
        self.node_kind = node_kind
        self.status = status
        self.is_paused = is_paused
        self.pause_requested = pause_requested
        self.metadata = dict(metadata or {})
        self.created_at = created_at
        self.updated_at = created_at
        self.pause_reason = ''


class _FakeStore:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def list_nodes(self, task_id):
        return list(self._nodes)

    def list_task_node_notifications(self, task_id, node_id):
        return []


class _FakeLog:
    def __init__(self, frames=None):
        self._frames = dict(frames or {})

    def read_runtime_frame(self, task_id, node_id):
        return dict(self._frames.get(node_id) or {})


def _runner(nodes, frames=None):
    from main.runtime.node_runner import NodeRunner

    return NodeRunner(
        store=_FakeStore(nodes),
        log_service=_FakeLog(frames),
        react_loop=None,
        tool_provider=None,
        execution_model_refs=['m1'],
        acceptance_model_refs=['m1'],
    )


def _tree_nodes():
    root = _FakeNode('node:root', goal='汇总报告')
    child_a = _FakeNode(
        'node:a',
        parent_node_id='node:root',
        goal='分支A',
        created_at='2026-11-12T00:00:01+08:00',
    )
    child_b = _FakeNode(
        'node:b',
        parent_node_id='node:root',
        goal='分支B',
        status='success',
        created_at='2026-11-12T00:00:02+08:00',
    )
    grand = _FakeNode(
        'node:g',
        parent_node_id='node:a',
        goal='孙节点',
        created_at='2026-11-12T00:00:03+08:00',
    )
    acceptance = _FakeNode(
        'node:acc',
        parent_node_id='node:a',
        goal='accept:分支A',
        node_kind='acceptance',
        metadata={'accepted_node_id': 'node:a'},
        created_at='2026-11-12T00:00:04+08:00',
    )
    return [root, child_a, child_b, grand, acceptance]


class TestListChildrenRestrictionReturnsWholeSubtree:
    @pytest.mark.asyncio
    async def test_no_params_keeps_direct_children_list(self):
        runner = _runner(_tree_nodes())
        root = runner._store.list_nodes('task:t')[0]
        payload = await runner._list_children(task_id='task:t', node=root)
        assert [row['node_id'] for row in payload['children']] == ['node:a', 'node:b']
        assert payload['count'] == 2

    @pytest.mark.asyncio
    async def test_state_restriction_returns_whole_subtree(self):
        runner = _runner(_tree_nodes())
        root = runner._store.list_nodes('task:t')[0]
        payload = await runner._list_children(task_id='task:t', node=root, state=['running'])
        assert payload['mode'] == 'full_subtree'
        assert payload['filter_applied'] is False
        assert payload['requested_filter'] == {'state': ['running'], 'settled': None}
        ids = [row['node_id'] for row in payload['nodes']]
        # 整颗子树：自己为根、直接子节点、孙节点、验收节点、已终态节点都在场——
        # 过滤不把任何子节点藏出视野（settled 的 node:b 也在）。
        assert ids[0] == 'node:root'
        assert ids == ['node:root', 'node:a', 'node:g', 'node:acc', 'node:b']
        assert payload['count'] == 5
        assert payload['truncated'] is False
        assert payload['pointer'] == ''
        # 呈现对齐前门 task_progress：|- 缩进 + (id,状态,目标)；验收节点走括号形态。
        assert payload['tree_text'].splitlines()[0] == '(node:root,running,汇总报告)'
        assert '|-(node:a,running,分支A)' in payload['tree_text']
        assert '  |-(node:g,running,孙节点)' in payload['tree_text']
        assert '([验收:node:a] node:acc,running,accept:分支A)' in payload['tree_text']
        assert '|-(node:b,success,分支B)' in payload['tree_text']

    @pytest.mark.asyncio
    async def test_settled_restriction_returns_whole_subtree(self):
        runner = _runner(_tree_nodes())
        root = runner._store.list_nodes('task:t')[0]
        payload = await runner._list_children(task_id='task:t', node=root, settled=True)
        assert payload['mode'] == 'full_subtree'
        assert payload['requested_filter'] == {'state': [], 'settled': True}
        assert 'node:a' in [row['node_id'] for row in payload['nodes']]

    @pytest.mark.asyncio
    async def test_structured_rows_carry_brief_fields(self):
        runner = _runner(_tree_nodes())
        root = runner._store.list_nodes('task:t')[0]
        payload = await runner._list_children(task_id='task:t', node=root, state=['running'])
        row = next(item for item in payload['nodes'] if item['node_id'] == 'node:b')
        assert row['settled'] is True
        assert row['state'] == 'success'
        assert row['level'] == 1
        assert row['relation'] == 'child'
        assert row['messageable'] is True
        assert row['controllable'] is True

    @pytest.mark.asyncio
    async def test_subtree_view_reports_truncation(self, monkeypatch):
        from main.runtime.node_runner import NodeRunner

        monkeypatch.setattr(NodeRunner, 'MAX_TREE_NODES', 2)
        runner = _runner(_tree_nodes())
        root = runner._store.list_nodes('task:t')[0]
        payload = await runner._list_children(task_id='task:t', node=root, state=['running'])
        assert payload['truncated'] is True
        assert payload['count'] == 2
        assert 'read_child_progress' in payload['pointer']
