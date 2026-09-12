"""tree-snapshot 分块加载回归测试。

锁定三条契约（对应大任务树打开超时治理）：
1. max_nodes 分块按稳定排序切片，各块无重叠，游标续传最终并集与不分块全量快照一致；
2. 响应携带 truncated / total_node_count / next_after_node_id 供前端进度与续传使用；
3. 不分块调用（max_nodes=None）与子树调用（scope_root_id）保持原有语义。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _mark_worker_online(service: MainRuntimeService) -> None:
    updated_at = now_iso()
    item = {
        "worker_id": "worker:test",
        "role": "task_worker",
        "status": "running",
        "updated_at": updated_at,
        "payload": {"execution_mode": "worker", "active_task_count": 0},
    }
    service.store.upsert_worker_status(
        worker_id=str(item["worker_id"]),
        role=str(item["role"]),
        status=str(item["status"]),
        updated_at=str(item["updated_at"]),
        payload=dict(item["payload"]),
    )
    service.publish_worker_status_event(item=item)


def _build_service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )


def _create_tree_with_children(service: MainRuntimeService, child_count: int = 5):
    _mark_worker_online(service)
    record = asyncio.run(service.create_task("tree pagination test", session_id="web:shared"))
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None
    for index in range(child_count):
        service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=root,
            goal=f"child {index}:检查结果是否满足要求。",
            acceptance_prompt="检查结果是否满足要求。",
            parent_node_id=root.node_id,
            metadata={},
        )
    return task, root


def _collect_chunks(service: MainRuntimeService, task_id: str, max_nodes: int):
    cursor = ""
    chunks = []
    guard = 0
    while guard < 100:
        guard += 1
        snapshot = service.query_service.get_tree_snapshot(
            task_id,
            max_nodes=max_nodes,
            after_node_id=cursor,
        )
        assert snapshot is not None
        chunks.append(snapshot)
        if not snapshot.truncated:
            return chunks
        cursor = snapshot.next_after_node_id
        assert cursor, "truncated 快照必须携带续传游标"
    raise AssertionError("分块续传未收敛（防御上限触发）")


def test_tree_snapshot_pagination_matches_full_snapshot(tmp_path: Path):
    service = _build_service(tmp_path)
    task, _root = _create_tree_with_children(service, child_count=5)
    expected_total = 1 + 5

    chunks = _collect_chunks(service, task.task_id, max_nodes=2)
    assert len(chunks) == ((expected_total + 1) // 2)

    merged_ids: list[str] = []
    total_seen: set[int] = set()
    for chunk in chunks:
        if not chunk.truncated:
            assert chunk.next_after_node_id == ""
        else:
            assert chunk.next_after_node_id
        assert chunk.total_node_count == expected_total
        total_seen.add(int(chunk.total_node_count or 0))
        merged_ids.extend(sorted(chunk.nodes_by_id.keys()))
    # 每个块的总数声明一致，且恰好是整树节点数。
    assert total_seen == {expected_total}
    # 无重叠且有稳定顺序：分块就是全量 id 的顺序切片。
    assert len(merged_ids) == len(set(merged_ids)) == expected_total

    full = service.query_service.get_tree_snapshot(task.task_id)
    assert full is not None
    assert not full.truncated
    assert full.total_node_count is None
    assert set(merged_ids) == set(full.nodes_by_id.keys())
    # 同一节点的内容与全量快照逐字段一致。
    for node_id, node in full.nodes_by_id.items():
        for chunk in chunks:
            if node_id in chunk.nodes_by_id:
                assert chunk.nodes_by_id[node_id].model_dump(mode="json") == node.model_dump(mode="json")
                break
        else:
            raise AssertionError(f"node {node_id} missing from all chunks")


def test_tree_snapshot_pagination_cursor_skips_nothing(tmp_path: Path):
    service = _build_service(tmp_path)
    task, _root = _create_tree_with_children(service, child_count=4)
    expected_total = 5

    chunks = _collect_chunks(service, task.task_id, max_nodes=3)
    # 5 个节点 / 每块 3 个 → 两块，第二块起续传游标等于第一块最后一个 id。
    assert len(chunks) == 2
    first, second = chunks
    assert first.truncated and not second.truncated
    assert second.next_after_node_id == ""
    assert not (set(first.nodes_by_id.keys()) & set(second.nodes_by_id.keys()))
    union = {*first.nodes_by_id.keys(), *second.nodes_by_id.keys()}
    full = service.query_service.get_tree_snapshot(task.task_id)
    assert full is not None
    assert len(union) == expected_total
    assert union == set(full.nodes_by_id.keys())


def test_tree_snapshot_subtree_scope_keeps_full_subtree_semantics(tmp_path: Path):
    service = _build_service(tmp_path)
    task, root = _create_tree_with_children(service, child_count=3)
    subtree = service.query_service.get_tree_subtree(task.task_id, root.node_id)
    assert subtree is not None
    assert not subtree.truncated
    assert subtree.root_node_id == root.node_id
    assert root.node_id in subtree.nodes_by_id
