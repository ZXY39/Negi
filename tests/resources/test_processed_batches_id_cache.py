"""已处理批次 request_id 台账的缓存契约。

覆盖：文件没动就不重读台账、台账一变（size/mtime 变）就重新读、返回的是副本所以
调用方改集合不污染缓存、台账文件读不到时按"没有已处理"处理而不是抛。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from g3ku.agent.memory_agent_runtime import MemoryManager


class _Ledger:
    _processed_batches_file_key = MemoryManager._processed_batches_file_key
    _processed_request_ids = MemoryManager._processed_request_ids

    def __init__(self, ops_file: Path) -> None:
        self.ops_file = ops_file
        self._io_lock = threading.RLock()
        self._processed_request_ids_cache = None
        self.reads = 0

    def _read_processed_batches(self):
        self.reads += 1
        if not self.ops_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.ops_file.read_text(encoding='utf-8').splitlines()
            if line.strip()
        ]


def _append(ops_file: Path, request_id: str) -> None:
    with ops_file.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({
            'batch_id': f'batch:{request_id}',
            'request_ids': [request_id],
            'processed_at': '2026-10-02T12:00:00+08:00',
            # 实盘每行连批次正文一起存，这里塞一段同量级的载荷
            'changes': [{'note': 'x' * 4096}],
        }) + '\n')


def test_unchanged_ledger_is_parsed_once_no_matter_how_often_it_is_asked(tmp_path: Path) -> None:
    ops_file = tmp_path / 'ops.jsonl'
    ops_file.write_text('', encoding='utf-8')
    for index in range(3):
        _append(ops_file, f'req-{index}')
    ledger = _Ledger(ops_file)

    first = ledger._processed_request_ids()
    second = ledger._processed_request_ids()

    assert first == {'req-0', 'req-1', 'req-2'}
    assert second == first
    assert ledger.reads == 1


def test_appending_a_batch_invalidates_the_cache_by_file_identity(tmp_path: Path) -> None:
    ops_file = tmp_path / 'ops.jsonl'
    ops_file.write_text('', encoding='utf-8')
    _append(ops_file, 'req-0')
    ledger = _Ledger(ops_file)
    assert ledger._processed_request_ids() == {'req-0'}

    time.sleep(0.01)
    _append(ops_file, 'req-1')

    assert ledger._processed_request_ids() == {'req-0', 'req-1'}
    assert ledger.reads == 2


def test_callers_get_a_copy_so_the_cache_cannot_be_edited(tmp_path: Path) -> None:
    ops_file = tmp_path / 'ops.jsonl'
    ops_file.write_text('', encoding='utf-8')
    _append(ops_file, 'req-0')
    ledger = _Ledger(ops_file)

    ids = ledger._processed_request_ids()
    ids.add('req-injected')

    assert ledger._processed_request_ids() == {'req-0'}
    assert ledger.reads == 1


def test_unreadable_ledger_is_treated_as_no_history(tmp_path: Path) -> None:
    ledger = _Ledger(tmp_path / 'missing' / 'ops.jsonl')

    assert ledger._processed_request_ids() == set()
    assert ledger.reads == 1
