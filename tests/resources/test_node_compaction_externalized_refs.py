"""节点压缩稳定块：已外置结果索引由运行时机器重算，不依赖摘要器保真。

- 索引段写在元数据行之后、摘要正文之前；条数进 `externalized_ref_count`，
  未列出条数进 `externalized_ref_omitted`。
- 只列本节点产出的 `tool_result:*` artifact，内部单例与不可读条目不列。
- 取最近 N 条；无外置结果时不出空块；同一块内不叠加两份索引。
"""

from __future__ import annotations

import json

from main.runtime.react_loop import (
    _NODE_EXTERNALIZED_REFS_HEADING,
    _NODE_EXTERNALIZED_REFS_MAX_ROWS,
    ReActToolLoop,
    _render_externalized_refs_section,
)


def _request_messages() -> list[dict[str, object]]:
    return [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': '{"task_id":"task-r","goal":"demo"}'},
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


def _row(index: int, *, kind: str = 'tool_result:exec', readable: bool = True) -> dict[str, object]:
    return {
        'ref': f'artifact:ref{index:03d}',
        'label': f'exec full output {index}',
        'size_bytes': 1024 * (index + 1),
        'created_at': f'2026-11-20T00:00:{index:02d}+08:00',
        'kind': kind,
        'readable': readable,
    }


class _FakeArtifact:
    def __init__(self, row: dict[str, object]) -> None:
        self.artifact_id = str(row['ref'])
        self.kind = str(row['kind'])
        self.title = str(row['label'])
        self.size_bytes = int(row['size_bytes'])
        self.created_at = str(row['created_at'])
        self.readable = bool(row['readable'])
        self.path = f'/artifacts/{self.artifact_id}.txt'


class _FakeArtifactStore:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._artifacts = [_FakeArtifact(row) for row in rows]

    def list_artifacts_for_node(self, task_id, node_id):
        return list(self._artifacts)

    @staticmethod
    def _artifact_is_readable(artifact) -> bool:
        return bool(artifact.readable)


class _FakeService:
    def __init__(self, store) -> None:
        self.artifact_store = store


class _FakeLog:
    _store = None

    def read_runtime_frame(self, task_id, node_id):
        return {}


def _loop(rows: list[dict[str, object]]) -> ReActToolLoop:
    loop = ReActToolLoop(chat_backend=None, log_service=_FakeLog())
    loop.main_task_service = _FakeService(_FakeArtifactStore(rows))
    return loop


class TestRewriteCarriesExternalizedRefsSection:
    def test_section_lands_between_metadata_and_summary(self):
        rewritten, _payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=_request_messages(),
            compressed_text='summary text',
            recent_tail_count=4,
            externalized_refs=([_row(1), _row(2)], 0),
        )
        content = _compact_block(rewritten)
        lines = content.splitlines()
        metadata = json.loads(lines[1])
        assert metadata['externalized_ref_count'] == 2
        assert metadata['externalized_ref_omitted'] == 0
        assert content.count(_NODE_EXTERNALIZED_REFS_HEADING) == 1
        assert '- artifact:ref001 | exec full output 1 | 2048 bytes' in content
        assert content.index(_NODE_EXTERNALIZED_REFS_HEADING) < content.index('summary text')
        assert content.rstrip().endswith('summary text')

    def test_no_refs_produces_no_empty_section(self):
        rewritten, _payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=_request_messages(),
            compressed_text='summary',
            recent_tail_count=4,
            externalized_refs=([], 0),
        )
        content = _compact_block(rewritten)
        assert _NODE_EXTERNALIZED_REFS_HEADING not in content
        assert 'externalized_ref_count' not in json.loads(content.splitlines()[1])

    def test_omitted_earlier_results_are_named(self):
        section = _render_externalized_refs_section([_row(1)], omitted=5)
        assert '另有 5 条更早的外置结果未列出' in section
        assert 'content_search' in section

    def test_rows_without_ref_are_dropped(self):
        rewritten, _payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=_request_messages(),
            compressed_text='summary',
            recent_tail_count=4,
            externalized_refs=([{'label': 'no ref'}, _row(3)], 0),
        )
        content = _compact_block(rewritten)
        assert json.loads(content.splitlines()[1])['externalized_ref_count'] == 1
        assert 'no ref' not in content

    def test_previous_block_does_not_duplicate_section(self):
        first, _ = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=_request_messages(),
            compressed_text='older summary',
            recent_tail_count=4,
            externalized_refs=([_row(1)], 0),
        )
        older_block = _compact_block(first)
        messages = [*_request_messages(), {'role': 'assistant', 'content': older_block}]
        second, _ = ReActToolLoop._rewrite_request_messages_for_token_compaction(
            node_id='node:root',
            request_messages=messages,
            compressed_text='newer summary',
            recent_tail_count=4,
            externalized_refs=([_row(1), _row(2)], 0),
        )
        content = _compact_block(second)
        assert content.count(_NODE_EXTERNALIZED_REFS_HEADING) == 1
        assert json.loads(content.splitlines()[1])['externalized_ref_count'] == 2
        assert content.rstrip().endswith('newer summary')


class TestFetchFiltersAndBounds:
    def test_internal_kinds_and_unreadable_are_excluded(self):
        loop = _loop([
            _row(1),
            _row(2, kind='node_input'),
            _row(3, kind='task_execution_trace'),
            _row(4, readable=False),
        ])
        rows, omitted = loop._node_externalized_artifact_refs(task_id='task-r', node_id='node:root')
        assert [row['ref'] for row in rows] == ['artifact:ref001']
        assert omitted == 0

    def test_keeps_most_recent_n_and_reports_omitted(self):
        total = _NODE_EXTERNALIZED_REFS_MAX_ROWS + 5
        loop = _loop([_row(index) for index in range(total)])
        rows, omitted = loop._node_externalized_artifact_refs(task_id='task-r', node_id='node:root')
        assert len(rows) == _NODE_EXTERNALIZED_REFS_MAX_ROWS
        assert omitted == 5
        assert rows[-1]['ref'] == f'artifact:ref{total - 1:03d}'
        assert rows[0]['ref'] == f'artifact:ref{5:03d}'

    def test_missing_store_capability_returns_empty(self):
        loop = ReActToolLoop(chat_backend=None, log_service=_FakeLog())
        loop.main_task_service = None
        assert loop._node_externalized_artifact_refs(task_id='task-r', node_id='node:root') == ([], 0)

    def test_store_failure_does_not_block_compaction(self):
        class _Boom:
            def list_artifacts_for_node(self, task_id, node_id):
                raise RuntimeError('store unavailable')

        loop = ReActToolLoop(chat_backend=None, log_service=_FakeLog())
        loop.main_task_service = _FakeService(_Boom())
        assert loop._node_externalized_artifact_refs(task_id='task-r', node_id='node:root') == ([], 0)
