"""正文查重的哈希比较下推到 SQL，缺哈希的存量行由回填补上。

实盘形状：一个任务 8,563 行 artifact / 8.10 MB，其中 984 行没有 `content_hash`。
旧写法每次写正文都把整任务行取回，遇到没哈希的行就把那份文件整个读回来算哈希
（200 行样本 439 ms / 96.4 MB ⇒ 全量外推 ≈ 2.2 s），榜上是
`fetchall:artifacts[from=_find_existing_text_artifact]` 2,246.9 / 2,326.4 ms，
与 09:36:42 那拍 2,519 ms 的事件循环滞后同量级。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:dedupe'
BODY = '# 报告正文\n' + ('内容段落。' * 40)


def _build(tmp_path: Path) -> tuple[SQLiteTaskStore, TaskArtifactStore]:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    return store, artifacts


def _create(artifacts: TaskArtifactStore, content: str, *, node_id: str | None = 'node:a', kind: str = 'node_result_payload'):
    return artifacts.create_text_artifact(
        task_id=TASK_ID,
        node_id=node_id,
        kind=kind,
        title='t',
        content=content,
        extension='.md',
    )


def test_identical_content_reuses_one_artifact(tmp_path: Path):
    store, artifacts = _build(tmp_path)
    try:
        first = _create(artifacts, BODY)
        second = _create(artifacts, BODY)
        third = _create(artifacts, BODY + '不同的一份')
        assert first.artifact_id == second.artifact_id
        assert third.artifact_id != first.artifact_id
        assert int(store._fetchone('SELECT COUNT(*) AS c FROM artifacts')['c']) == 2  # noqa: SLF001
    finally:
        store.close()


def test_candidate_set_is_filtered_in_sql(tmp_path: Path):
    store, artifacts = _build(tmp_path)
    try:
        _create(artifacts, 'AAA')
        target = _create(artifacts, 'BBB')
        _create(artifacts, 'CCC')
        digest = hashlib.sha256('BBB'.encode('utf-8')).hexdigest()
        candidates = store.find_artifact_dedupe_candidates(TASK_ID, digest)
        assert [str(row['artifact_id']) for row in candidates] == [target.artifact_id]
        assert str(candidates[0]['content_hash']) == digest
        assert int(candidates[0]['size_bytes']) == len('BBB'.encode('utf-8'))
    finally:
        store.close()


def test_legacy_row_without_hash_still_dedupes(tmp_path: Path):
    store, artifacts = _build(tmp_path)
    try:
        created = _create(artifacts, BODY)
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "UPDATE artifacts SET payload_json = json_remove(payload_json, '$.content_hash') WHERE artifact_id = ?",
                (created.artifact_id,),
            )
        assert store.count_artifacts_missing_content_hash() == 1
        # 新进程视角（清掉进程内缓存）：没有哈希的行仍要被考虑，不能把同一份正文存成两件
        fresh = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        fresh._content_index.clear()  # noqa: SLF001
        fresh._artifact_hash_by_id.clear()  # noqa: SLF001
        again = _create(fresh, BODY)
        assert again.artifact_id == created.artifact_id
    finally:
        store.close()


def test_backfill_removes_the_file_sweep_from_the_hot_lane(tmp_path: Path):
    store, artifacts = _build(tmp_path)
    try:
        created = _create(artifacts, BODY)
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "UPDATE artifacts SET payload_json = json_remove(payload_json, '$.content_hash') WHERE artifact_id = ?",
                (created.artifact_id,),
            )
        assert artifacts.backfill_missing_content_hashes(limit=10, batches=3) == 1
        assert store.count_artifacts_missing_content_hash() == 0
        # 幂等：第二趟没有缺口可补
        assert artifacts.backfill_missing_content_hashes(limit=10, batches=3) == 0

        # 回填之后查重不该再读任何文件：把兜底的现算函数换成抛错，命中仍要成立
        fresh = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        fresh._content_index.clear()  # noqa: SLF001
        fresh._artifact_hash_by_id.clear()  # noqa: SLF001

        def _boom(*args, **kwargs):  # noqa: ARG001
            raise AssertionError('dedupe must not fall back to reading artifact files after the backfill')

        fresh._legacy_content_hash = _boom  # type: ignore[method-assign]
        again = _create(fresh, BODY)
        assert again.artifact_id == created.artifact_id
    finally:
        store.close()


def test_missing_hash_counter_and_writer_are_consistent(tmp_path: Path):
    store, artifacts = _build(tmp_path)
    try:
        created = _create(artifacts, BODY)
        assert store.count_artifacts_missing_content_hash() == 0
        row = store.get_artifact(created.artifact_id)
        assert row is not None and str(row.content_hash) == hashlib.sha256(BODY.encode('utf-8')).hexdigest()
        # 手工清一条后 set 能补回，且只补空、不覆盖已有值
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "UPDATE artifacts SET payload_json = json_remove(payload_json, '$.content_hash') WHERE artifact_id = ?",
                (created.artifact_id,),
            )
        assert store.count_artifacts_missing_content_hash() == 1
        assert store.set_artifact_content_hash(created.artifact_id, 'deadbeef') is True
        assert store.set_artifact_content_hash(created.artifact_id, 'another') is False
        refreshed = store.get_artifact(created.artifact_id)
        assert refreshed is not None and str(refreshed.content_hash) == 'deadbeef'
    finally:
        store.close()
