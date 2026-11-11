"""节点压缩稳定块：未终态直接子节点列表写进压缩块，压缩后不遗忘。

- 压缩块重写把未终态直接子节点快照写在元数据行之后、摘要正文之前；
  元数据行仍是可独立解析的第二行 JSON，行数进 `unsettled_child_count`。
- 快照只列自己派生的直接子节点（孙节点及以下不列），终态（success/failed）不列。
"""

from __future__ import annotations

import json

from main.runtime.react_loop import (
    _NODE_UNSETTLED_CHILDREN_HEADING,
    ReActToolLoop,
    _render_unsettled_children_section,
)


def _request_messages() -> list[dict[str, object]]:
    return [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': '{"task_id":"task-u","goal":"demo"}'},
        *[
            {'role': 'user' if index % 2 == 0 else 'assistant', 'content': f'history {index}'}
            for index in range(40)
        ],
    ]


def _compact_block(rewritten: list[dict[str, object]]) -> str:
    return next(
        str(item.get('content') or '')
        for item in rewritten
        if str(item.get('content') or '').startswith('[TOKEN_COMPACT_V2]')
    )


class _FakeNode:
    def __init__(
        self,
        node_id,
        *,
        parent_node_id='',
        status='in_progress',
        is_paused=False,
        pause_requested=False,
        metadata=None,
        goal='',
        created_at='2026-11-12T00:00:00+08:00',
    ):
        self.node_id = node_id
        self.parent_node_id = parent_node_id
        self.status = status
        self.is_paused = is_paused
        self.pause_requested = pause_requested
        self.metadata = dict(metadata or {})
        self.goal = goal
        self.created_at = created_at
        self.updated_at = created_at


class _FakeStore:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def list_nodes(self, task_id):
        return list(self._nodes)


class _FakeLog:
    def __init__(self, store, frames=None):
        self._store = store
        self._frames = dict(frames or {})

    def read_runtime_frame(self, task_id, node_id):
        return dict(self._frames.get(node_id) or {})


def _loop(nodes, frames=None):
    store = _FakeStore(nodes)
    return ReActToolLoop(chat_backend=None, log_service=_FakeLog(store, frames))


class TestRewriteCarriesUnsettledChildrenSection:
    def test_section_lands_between_metadata_and_summary(self):
        rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=_request_messages(),
            compressed_text='summary text',
            recent_tail_count=4,
            unsettled_children=[
                {
                    'node_id': 'node:a',
                    'created_at': '2026-11-12T00:00:01+08:00',
                    'goal': '分支A',
                    'state': 'running',
                },
                {
                    'node_id': 'node:b',
                    'created_at': '2026-11-12T00:00:02+08:00',
                    'goal': '分支B',
                    'state': 'paused',
                },
            ],
        )
        content = _compact_block(rewritten)
        lines = content.splitlines()
        # 第二行仍是可独立解析的 JSON 元数据（落盘点按它读收口水位线）。
        metadata = json.loads(lines[1])
        assert metadata['unsettled_child_count'] == 2
        assert content.count(_NODE_UNSETTLED_CHILDREN_HEADING) == 1
        assert '- node:a | created_at=2026-11-12T00:00:01+08:00 | goal=分支A | state=running' in content
        assert '| state=paused' in content
        # 段落在摘要正文之前，摘要原样保留在块尾。
        assert content.index(_NODE_UNSETTLED_CHILDREN_HEADING) < content.index('summary text')
        assert content.rstrip().endswith('summary text')

    def test_long_goal_is_clamped(self):
        section = _render_unsettled_children_section(
            [
                {
                    'node_id': 'node:a',
                    'created_at': '2026-11-12T00:00:01+08:00',
                    'goal': '很长的目标' * 60,
                    'state': 'running',
                }
            ]
        )
        row = next(line for line in section.splitlines() if line.startswith('- node:a'))
        assert row.endswith('… | state=running')
        assert len(row) < 200

    def test_rows_without_node_id_are_dropped(self):
        rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=_request_messages(),
            compressed_text='summary',
            recent_tail_count=4,
            unsettled_children=[{'goal': 'no id'}, {'node_id': 'node:a', 'created_at': '', 'goal': '', 'state': ''}],
        )
        content = _compact_block(rewritten)
        assert 'unsettled_child_count' in payload
        assert payload['unsettled_child_count'] == 1
        assert '- node:a | created_at=未知 | goal=（无） | state=running' in content

    def test_no_children_leaves_block_shape_unchanged(self):
        rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=_request_messages(),
            compressed_text='summary',
            recent_tail_count=4,
        )
        content = _compact_block(rewritten)
        assert _NODE_UNSETTLED_CHILDREN_HEADING not in content
        assert 'unsettled_child_count' not in payload

    def test_send_mode_block_without_summary_still_carries_section(self):
        rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=_request_messages(),
            compressed_text='',
            recent_tail_count=4,
            unsettled_children=[
                {'node_id': 'node:a', 'created_at': '2026-11-12T00:00:01+08:00', 'goal': '分支A', 'state': 'running'}
            ],
        )
        content = _compact_block(rewritten)
        assert payload['kind'] == 'node_send_token_compaction'
        assert _NODE_UNSETTLED_CHILDREN_HEADING in content


class TestNodeUnsettledDirectChildren:
    def _nodes(self):
        running = _FakeNode(
            'node:a',
            parent_node_id='node:root',
            goal='分支A',
            created_at='2026-11-12T00:00:02+08:00',
        )
        paused = _FakeNode(
            'node:b',
            parent_node_id='node:root',
            goal='分支B',
            is_paused=True,
            created_at='2026-11-12T00:00:01+08:00',
        )
        waiting = _FakeNode(
            'node:c',
            parent_node_id='node:root',
            goal='分支C',
            created_at='2026-11-12T00:00:03+08:00',
        )
        standby = _FakeNode(
            'node:d',
            parent_node_id='node:root',
            goal='待命',
            metadata={'continuous': True, 'node_wait_external': True},
            created_at='2026-11-12T00:00:04+08:00',
        )
        success = _FakeNode(
            'node:e',
            parent_node_id='node:root',
            status='success',
            created_at='2026-11-12T00:00:05+08:00',
        )
        failed = _FakeNode(
            'node:f',
            parent_node_id='node:root',
            status='failed',
            created_at='2026-11-12T00:00:06+08:00',
        )
        grandchild = _FakeNode(
            'node:g',
            parent_node_id='node:a',
            created_at='2026-11-12T00:00:07+08:00',
        )
        other_parent = _FakeNode(
            'node:h',
            parent_node_id='node:elsewhere',
            created_at='2026-11-12T00:00:08+08:00',
        )
        return [running, paused, waiting, standby, success, failed, grandchild, other_parent]

    def test_lists_only_non_terminal_direct_children_with_state_labels(self):
        loop = _loop(self._nodes(), frames={'node:c': {'phase': 'waiting_explicit'}})
        rows = loop._node_unsettled_direct_children(task_id='task:u', node_id='node:root')
        assert [(row['node_id'], row['state']) for row in rows] == [
            ('node:b', 'paused'),
            ('node:a', 'running'),
            ('node:c', 'waiting'),
            ('node:d', 'standby'),
        ]
        assert all(row['created_at'] for row in rows)
        assert rows[0]['goal'] == '分支B'

    def test_pause_requested_counts_as_paused(self):
        nodes = [
            _FakeNode('node:a', parent_node_id='node:root', pause_requested=True),
            _FakeNode('node:b', parent_node_id='node:root'),
        ]
        loop = _loop(nodes)
        rows = loop._node_unsettled_direct_children(task_id='task:u', node_id='node:root')
        assert [(row['node_id'], row['state']) for row in rows] == [
            ('node:a', 'paused'),
            ('node:b', 'running'),
        ]

    def test_store_failure_returns_empty_rows(self):
        class _BrokenStore:
            def list_nodes(self, task_id):
                raise RuntimeError('disk gone')

        class _BrokenLog:
            _store = _BrokenStore()

            def read_runtime_frame(self, task_id, node_id):
                return {}

        loop = ReActToolLoop(chat_backend=None, log_service=_BrokenLog())
        assert loop._node_unsettled_direct_children(task_id='task:u', node_id='node:root') == []

    def test_missing_store_returns_empty_rows(self):
        class _BareLog:
            _store = None

            def read_runtime_frame(self, task_id, node_id):
                return {}

        loop = ReActToolLoop(chat_backend=None, log_service=_BareLog())
        assert loop._node_unsettled_direct_children(task_id='task:u', node_id='node:root') == []
