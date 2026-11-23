"""exec 全量输出进 artifact（单落点）与导航读端守卫的判据用例。

覆盖计划 work/exec-fulloutput-and-ref-index-plan.md 的 P0：
tee 落盘 → 流式登记 → 信封 ref 指向全量 → content_search/content_open 取回中段。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import g3ku.agent.tools.shell as shell_module
from g3ku.agent.tools.shell import ExecTool
from g3ku.content import ContentNavigationService, parse_content_envelope
from g3ku.content.navigation import CONTENT_READ_SIZE_LIMIT_BYTES
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore


class _ChunkedStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int = -1) -> bytes:
        if not self._chunks:
            return b''
        return self._chunks.pop(0)


class _StubProcess:
    def __init__(self, *, stdout_chunks: list[bytes], returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = _ChunkedStream(list(stdout_chunks))
        self.stderr = _ChunkedStream([])

    async def communicate(self):
        raise MemoryError('communicate must not be used for large exec output')

    async def wait(self):
        return self.returncode

    def kill(self):
        return None


def _tee_leftovers() -> list[Path]:
    """按生产同一个路径 helper 取中转目录；数据根隔离由 tests/conftest.py 负责。"""
    root = shell_module.data_g3ku_path('exec-tmp')
    return list(root.rglob('*.log')) if root.exists() else []


def _stack(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    return store, artifact_store, content_store


def _patch_process(monkeypatch, stdout_chunks: list[bytes], returncode: int = 0):
    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _StubProcess(stdout_chunks=stdout_chunks, returncode=returncode)

    monkeypatch.setattr(shell_module.os, 'name', 'nt', raising=False)
    monkeypatch.setattr(shell_module.asyncio, 'create_subprocess_exec', _fake_create_subprocess_exec)


def _externalize(content_store, rendered, *, status='success'):
    message_content = content_store.externalize_for_message(
        rendered,
        runtime={'task_id': 'task:full', 'node_id': 'node:full'},
        display_name='tool:exec',
        source_kind='tool_result:exec',
        compact=True,
        delivery_metadata={'tool_status': status, 'invocation_text': 'exec(big)'},
    )
    return parse_content_envelope(message_content)


def _big_output() -> tuple[list[bytes], str, str, str]:
    """头/中/尾各埋一个哨兵；中段必须落在 64 KB 捕获环之外。"""
    head = 'HEAD-SENTINEL\n'
    middle = 'MIDDLE-SENTINEL-needle\n'
    tail = 'TAIL-SENTINEL\n'
    filler = 'x' * 200 + '\n'
    chunks = [head.encode('utf-8')]
    chunks += [filler.encode('utf-8') for _ in range(400)]  # 80 KB，把头环挤满
    chunks.append(middle.encode('utf-8'))
    chunks += [filler.encode('utf-8') for _ in range(400)]
    chunks.append(tail.encode('utf-8'))
    return chunks, head.strip(), middle.strip(), tail.strip()


@pytest.mark.asyncio
async def test_middle_is_recoverable_through_single_ref(monkeypatch, tmp_path) -> None:
    """核心判据：环丢中段后，ref 背后是全量，中段能搜到并取回上下文。"""
    chunks, head, middle, tail = _big_output()
    _patch_process(monkeypatch, chunks)
    store, artifact_store, content_store = _stack(tmp_path)
    tool = ExecTool(workspace_root=str(tmp_path), content_store=content_store)
    try:
        rendered = await tool.execute(
            command='big',
            __g3ku_runtime={'session_key': 'web:shared', 'task_id': 'task:full', 'node_id': 'node:full'},
        )
        payload = json.loads(rendered)
        assert payload['stdout_truncated'] is True, '这个体量必须真的挤爆捕获环'
        ref = str(payload.get('full_output_ref') or '')
        assert ref.startswith('artifact:'), '丢中段就必须登记全量并给 ref'
        assert '中段未保留' not in payload['head_preview'], '有全量时不许谎报未保留'
        assert payload['full_output_line_count'] > 800
        assert payload['full_output_char_count'] > 160000

        envelope = _externalize(content_store, rendered)
        assert envelope is not None
        assert envelope.ref == ref, '信封必须指向全量 artifact，而不是另存一份残缺载荷'
        assert f'content_open(ref="{ref}")' in envelope.summary
        # 信封规模读数必须是全量的，不是头尾摘录的。
        assert f"({payload['full_output_line_count']} lines" in envelope.summary

        hits = content_store.search(ref=ref, query='MIDDLE-SENTINEL')
        assert hits['ok'] is True and hits['count'] >= 1, '中段必须能搜到'
        line = int(hits['hits'][0]['line'])
        opened = content_store.open(ref=ref, around_line=line, window=6)
        assert middle in str(opened.get('excerpt') or '')

        # 全量文件自身可读且含三个哨兵；临时中转文件必须已回收。
        records = artifact_store.list_artifacts_for_node('task:full', 'node:full')
        assert len(records) == 1, '只该登记一份全量 artifact，且归属到本节点'
        record = records[0]
        text = Path(record.path).read_text(encoding='utf-8')
        assert head in text and middle in text and tail in text
        assert record.content_encoding == 'plain'
        leftovers = _tee_leftovers()
        assert leftovers == [], f'临时 tee 文件未回收: {leftovers}'
    finally:
        store.close()


@pytest.mark.asyncio
async def test_small_output_registers_nothing_and_leaves_no_file(monkeypatch, tmp_path) -> None:
    """未超捕获上限时环里已是全量：不登记、不留临时文件、不暴露 ref。"""
    text = '\n'.join(f'line-{i:05d}' for i in range(2000)) + '\n'
    assert len(text) > shell_module._EXEC_INLINE_CHAR_LIMIT
    assert len(text.encode('utf-8')) < shell_module._STREAM_CAPTURE_BYTE_LIMIT
    _patch_process(monkeypatch, [text.encode('utf-8')])
    store, _artifact_store, content_store = _stack(tmp_path)
    tool = ExecTool(workspace_root=str(tmp_path), content_store=content_store)
    try:
        rendered = await tool.execute(
            command='small',
            __g3ku_runtime={'session_key': 'web:shared', 'task_id': 'task:full', 'node_id': 'node:full'},
        )
        payload = json.loads(rendered)
        assert payload['stdout_truncated'] is False
        assert 'full_output_ref' not in payload
        envelope = _externalize(content_store, rendered)
        assert envelope is not None, '超内联闸门仍照常外置'
        assert 'line-01000' in str(content_store.open(ref=envelope.ref, start_line=1000, end_line=1001).get('excerpt'))
        assert _tee_leftovers() == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_registration_failure_falls_back_honestly(monkeypatch, tmp_path) -> None:
    """登记能力缺席时退回现状文案，不谎报有全量。"""
    chunks, _head, _middle, _tail = _big_output()
    _patch_process(monkeypatch, chunks)
    store, _artifact_store, content_store = _stack(tmp_path)
    monkeypatch.setattr(content_store, 'register_full_output_file', None, raising=False)
    tool = ExecTool(workspace_root=str(tmp_path), content_store=content_store)
    try:
        rendered = await tool.execute(
            command='big',
            __g3ku_runtime={'session_key': 'web:shared', 'task_id': 'task:full', 'node_id': 'node:full'},
        )
        payload = json.loads(rendered)
        assert payload['stdout_truncated'] is True
        assert 'full_output_ref' not in payload
        assert '中段未保留' in payload['head_preview']
        assert _tee_leftovers() == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_full_output_file_cap_is_reported(monkeypatch, tmp_path) -> None:
    """全量文件自身超上限时如实标注，不假装完整。"""
    chunks, _head, _middle, _tail = _big_output()
    _patch_process(monkeypatch, chunks)
    monkeypatch.setattr(shell_module, '_EXEC_FULL_OUTPUT_BYTE_LIMIT', 20 * 1024)
    store, _artifact_store, content_store = _stack(tmp_path)
    tool = ExecTool(workspace_root=str(tmp_path), content_store=content_store)
    try:
        rendered = await tool.execute(
            command='big',
            __g3ku_runtime={'session_key': 'web:shared', 'task_id': 'task:full', 'node_id': 'node:full'},
        )
        payload = json.loads(rendered)
        assert payload['full_output_truncated'] is True
        assert payload['full_output_size_bytes'] <= 20 * 1024
        assert '其后未保留' in payload['head_preview']
        envelope = _externalize(content_store, rendered)
        assert envelope is not None
        assert 'itself capped' in envelope.summary
    finally:
        store.close()


def test_store_file_entry_streams_counts_and_dedupes(tmp_path) -> None:
    """流式登记：哈希/行数/字符数/preview 正确，强制 plain，按哈希去重，不搬源文件。"""
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    try:
        lines = [f'row-{i:06d}' for i in range(120000)]  # > 1 MiB，str 入口会 gzip
        text = '\n'.join(lines) + '\n'
        source = tmp_path / 'source.log'
        source.write_bytes(text.encode('utf-8'))

        record, line_count, char_count = artifact_store.create_text_artifact_from_file(
            task_id='task:full', node_id='node:full', kind='tool_result:exec',
            title='exec full output', source_path=source,
        )
        assert record.content_encoding == 'plain', '这类 artifact 必须明文，保住 exec 里可 grep'
        assert not record.path.endswith('.gz')
        assert line_count == len(lines)
        assert char_count == len(text)
        assert record.size_bytes == len(text.encode('utf-8'))
        assert record.content_hash == hashlib.sha256(text.encode('utf-8')).hexdigest()
        assert record.preview_text == text[:400]
        assert source.exists(), '源文件只被读，不被搬走'

        again, again_lines, again_chars = artifact_store.create_text_artifact_from_file(
            task_id='task:full', node_id='node:full', kind='tool_result:exec',
            title='exec full output', source_path=source,
        )
        assert again.artifact_id == record.artifact_id, '同哈希同体积必须去重'
        assert (again_lines, again_chars) == (line_count, char_count)
    finally:
        store.close()


def test_store_file_entry_transcodes_source_encoding(tmp_path) -> None:
    """非 UTF-8 源按嗅出的编码增量解码后以 UTF-8 落盘。"""
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    try:
        text = '站点经验\n第二行\n'
        source = tmp_path / 'gbk.log'
        source.write_bytes(text.encode('gbk'))
        record, line_count, char_count = artifact_store.create_text_artifact_from_file(
            task_id='task:enc', node_id=None, kind='tool_result:exec',
            title='exec full output', source_path=source, source_encoding='gbk',
        )
        assert Path(record.path).read_text(encoding='utf-8') == text
        assert line_count == 2
        assert char_count == len(text)
    finally:
        store.close()


def test_navigation_read_guard_blocks_oversized_target(tmp_path, monkeypatch) -> None:
    """超整读上限的目标给可行动出路，且任何区间都绕不过守卫。"""
    content_store = ContentNavigationService(workspace=tmp_path)
    big = tmp_path / 'big.log'
    big.write_text('y' * 4096, encoding='utf-8')
    monkeypatch.setattr('g3ku.content.navigation.CONTENT_READ_SIZE_LIMIT_BYTES', 1024)
    with pytest.raises(ValueError) as excinfo:
        content_store.open(path=str(big), start_line=1, end_line=2)
    message = str(excinfo.value)
    assert 'navigation read limit' in message
    assert 'exec' in message and 'rg -n' in message
    with pytest.raises(ValueError):
        content_store.search(path=str(big), query='y')


def test_navigation_read_guard_keeps_largest_known_good_case(tmp_path) -> None:
    """口径校验：已知最大正常用例（7,024,971 字符）必须仍在守卫之下。"""
    assert CONTENT_READ_SIZE_LIMIT_BYTES > 7_024_971
    content_store = ContentNavigationService(workspace=tmp_path)
    target = tmp_path / 'large-but-legal.log'
    target.write_text('z' * 7_024_971, encoding='utf-8')
    opened = content_store.open(path=str(target), start_line=1, end_line=1)
    assert opened['ok'] is True
    assert opened['char_count'] == 7_024_971
