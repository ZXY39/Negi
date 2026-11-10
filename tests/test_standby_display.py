"""连续（待命）节点的三处显示层回归：

1. 任务级恢复必须重发布任务摘要：`update_task_control` 旗标翻面时刷新道可能因
   "无字段变化"早退而不发布，待命任务恢复后没有任何后续活动，卡片会一直停在
   旧状态（实机验收 F-1）；
2. REST `/api/tasks` 列表 item 必须携带 `standby`，整页刷新后卡片才不会回退成
   「运行」（实机验收 F-2）；
3. 任务树前端归一化必须保留 `continuous` / `wait_external`，树内「待命」徽标
   才能渲染（实机验收 F-3）。
4. 待命卡片在「结束」那一格印「最近」，读数取可见输出锚点，REST 列表与 WS
   摘要同源；`updated_at` 被 `mark_task_read` 等账本动作刷新时不许跟着动。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from main.models import NodeRecord, TaskRecord
from main.service.runtime_service import MainRuntimeService

REPO_ROOT = Path(__file__).resolve().parents[1]
STAMP = '2026-11-07T00:00:00+08:00'
RUN_STAMP = '2026-11-07T03:04:05+08:00'


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f'chat backend must not run in these tests: {kwargs!r}')


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


def _service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


def _seed_continuous_root(service: MainRuntimeService) -> tuple[TaskRecord, NodeRecord]:
    task = TaskRecord(
        task_id='task:standby',
        title='t',
        user_request='u',
        root_node_id='node:root',
        created_at=STAMP,
        updated_at=STAMP,
        metadata={'execution_policy': {'mode': 'focus'}},
    )
    parent = NodeRecord(
        node_id='node:root',
        task_id=task.task_id,
        parent_node_id=None,
        root_node_id='node:root',
        depth=0,
        node_kind='execution',
        goal='root',
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        metadata={
            'execution_policy': {'mode': 'focus'},
            'continuous': True,
            'node_wait_external': True,
            'node_wait_targets': [],
        },
    )
    service.store.upsert_task(task)
    service.store.upsert_node(parent)
    service.log_service.upsert_frame(
        task.task_id,
        {'node_id': parent.node_id, 'depth': 0, 'node_kind': 'execution', 'phase': 'waiting_explicit'},
    )
    return task, parent


def _summary_fingerprint(service: MainRuntimeService, task_id: str) -> dict[str, object]:
    meta = service.store.get_task_runtime_meta(task_id) or {}
    raw = str(meta.get('summary_fingerprint') or '')
    if not raw:
        return {}
    return json.loads(raw)


def test_resume_republishes_summary_with_standby(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_continuous_root(service)

    service.log_service.update_task_control(
        'task:standby', pause_requested=True, is_paused=True
    )
    paused_fp = _summary_fingerprint(service, 'task:standby')
    assert paused_fp.get('is_paused') is True
    assert paused_fp.get('standby') is False

    service.log_service.update_task_control(
        'task:standby', pause_requested=False, is_paused=False
    )
    resumed_fp = _summary_fingerprint(service, 'task:standby')
    assert resumed_fp.get('is_paused') is False
    # 恢复后没有节点活动可依赖：恢复本身必须把待命态重新发布出去（F-1）。
    assert resumed_fp.get('standby') is True


def test_resume_without_flag_change_does_not_republish(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_continuous_root(service)
    # 先建立已发布基线（暂停→恢复各发布一次），再验证无旗标变化不重发布。
    service.log_service.update_task_control(
        'task:standby', pause_requested=True, is_paused=True
    )
    service.log_service.update_task_control(
        'task:standby', pause_requested=False, is_paused=False
    )
    before = (service.store.get_task_runtime_meta('task:standby') or {}).get('summary_last_published_at')
    assert before

    service.log_service.update_task_control('task:standby')
    after = (service.store.get_task_runtime_meta('task:standby') or {}).get('summary_last_published_at')

    assert before == after


def test_task_list_items_carry_standby(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_continuous_root(service)

    items = {item.task_id: item for item in service.query_service.get_tasks(None, 'all')}
    item = items['task:standby']
    assert item.status == 'in_progress'
    assert item.standby is True

    service.log_service.update_task_control(
        'task:standby', pause_requested=True, is_paused=True
    )
    items = {item.task_id: item for item in service.query_service.get_tasks(None, 'all')}
    assert items['task:standby'].standby is False
    assert items['task:standby'].is_paused is True


def test_task_list_items_mark_plain_task_not_standby(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = TaskRecord(
        task_id='task:plain',
        title='t',
        user_request='u',
        root_node_id='node:plain',
        created_at=STAMP,
        updated_at=STAMP,
        metadata={'execution_policy': {'mode': 'focus'}},
    )
    node = NodeRecord(
        node_id='node:plain',
        task_id=task.task_id,
        parent_node_id=None,
        root_node_id='node:plain',
        depth=0,
        node_kind='execution',
        goal='root',
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        metadata={'execution_policy': {'mode': 'focus'}},
    )
    service.store.upsert_task(task)
    service.store.upsert_node(node)

    items = {item.task_id: item for item in service.query_service.get_tasks(None, 'all')}
    assert items['task:plain'].standby is False


def test_standby_item_carries_last_run_at_only_in_standby(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_continuous_root(service)
    service.log_service.update_task_runtime_meta(
        'task:standby', last_visible_output_at=RUN_STAMP
    )

    items = {item.task_id: item for item in service.query_service.get_tasks(None, 'all')}
    assert items['task:standby'].standby is True
    assert items['task:standby'].last_run_at == RUN_STAMP

    # 离开待命就不带这一格：卡片只在待命档渲染它，暂停档不该留着旧读数。
    service.log_service.update_task_control(
        'task:standby', pause_requested=True, is_paused=True
    )
    items = {item.task_id: item for item in service.query_service.get_tasks(None, 'all')}
    assert items['task:standby'].standby is False
    assert items['task:standby'].last_run_at == ''


def test_last_run_at_survives_mark_task_read_on_both_lanes(tmp_path: Path) -> None:
    """账本动作刷新 updated_at 时，最近运行读数不许跟着动（REST 与 WS 同源）。"""
    service = _service(tmp_path)
    _seed_continuous_root(service)
    service.log_service.update_task_runtime_meta(
        'task:standby', last_visible_output_at=RUN_STAMP
    )

    service.log_service.mark_task_read('task:standby')
    after = service.store.get_task('task:standby')
    assert after is not None
    assert after.is_unread is False
    # 先证污染前提成立：mark_task_read 确实把 updated_at 推到了锚点之外。
    assert after.updated_at != RUN_STAMP

    items = {item.task_id: item for item in service.query_service.get_tasks(None, 'all')}
    assert items['task:standby'].last_run_at == RUN_STAMP
    # mark_task_read 走的就是摘要发布车道：指纹里的读数必须与 REST 列表逐字相同。
    fingerprint = _summary_fingerprint(service, 'task:standby')
    assert fingerprint.get('standby') is True
    assert fingerprint.get('last_run_at') == RUN_STAMP


def test_last_run_at_falls_back_to_created_at_without_anchor(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_continuous_root(service)
    service.log_service.update_task_runtime_meta('task:standby', last_visible_output_at='')

    items = {item.task_id: item for item in service.query_service.get_tasks(None, 'all')}
    assert items['task:standby'].standby is True
    assert items['task:standby'].last_run_at == STAMP


def test_summary_payload_omits_empty_last_run_at(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, _parent = _seed_continuous_root(service)

    # 前端是合并语义：非待命档省略字段，不下发空串去清旧值。
    assert 'last_run_at' not in service.log_service._task_summary_payload(task)
    payload = service.log_service._task_summary_payload(
        task, standby=True, last_run_at=RUN_STAMP
    )
    assert payload['last_run_at'] == RUN_STAMP


def test_summary_fingerprint_covers_last_run_at(tmp_path: Path) -> None:
    """载荷里印出去的读数必须进指纹，否则"无变化"早退会吞掉新的最近运行时间。"""
    service = _service(tmp_path)
    task, _parent = _seed_continuous_root(service)

    first = service.log_service._task_summary_fingerprint(
        task, standby=True, last_run_at=RUN_STAMP
    )
    second = service.log_service._task_summary_fingerprint(
        task, standby=True, last_run_at='2026-11-07T05:06:07+08:00'
    )
    assert first != second


def test_tree_frontends_render_standby_badge_from_snapshot_fields() -> None:
    if not shutil.which('node'):
        raise AssertionError('node is required to run the tree-store frontend harness')
    script = """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          currentTaskId: "task:standby",
          currentTask: { task_id: "task:standby", status: "in_progress" },
          treeRootNodeId: "",
          treeNodesById: {},
          treeSnapshotVersion: "",
          treeView: null,
          treeLargeMode: false,
          treeDirtyParentsById: {},
          treeBranchSyncInFlightById: {},
          treeBranchSyncQueuedById: {},
          treeBranchSyncTokenById: {},
          treeSelectedRoundByNodeId: {},
          treeSnapshotSelfHealToken: null,
          treeSnapshotSelfHealAttempts: 0,
          taskNodeDetails: {},
          liveFrameMap: {
            root: { node_id: "root", depth: 0, node_kind: "execution", phase: "waiting_explicit" },
          },
          taskRuntimeSummary: null,
        };
        global.U = { tree: {} };
        global.ApiClient = { getTaskTreeSnapshot: async () => ({}) };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        const code = [
          "g3ku/web/frontend/tasks/tree-store.js",
          "g3ku/web/frontend/tasks/tree-view.js",
          "g3ku/web/frontend/tasks/node-detail-store.js",
          "g3ku/web/frontend/tasks/node-detail.js",
          "g3ku/web/frontend/org_graph_task_view.js",
        ].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        applyTaskTreeSnapshotPayload({
          root_node_id: "root",
          snapshot_version: "1",
          nodes_by_id: {
            root: {
              node_id: "root",
              title: "standby root",
              status: "in_progress",
              node_kind: "execution",
              continuous: true,
              wait_external: true,
              rounds: [],
              auxiliary_child_ids: [],
            },
          },
        });
        // task.node.patch 只带状态字段：合并不得抖掉 continuous / wait_external。
        applyTaskTreeSnapshotChunkPayload({
          root_node_id: "root",
          nodes_by_id: {
            root: { node_id: "root", status: "in_progress" },
          },
        });
        const tree = buildExecutionTreeFromSnapshot();
        console.log(JSON.stringify({
          displayState: tree && tree.display_state,
          visualState: tree && tree.visual_state,
        }));
    """
    completed = subprocess.run(
        ['node', '-'],
        input=textwrap.dedent(script),
        text=True,
        encoding='utf-8',
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    result = json.loads(completed.stdout.strip())
    assert result['visualState'] == 'standby'
    assert result['displayState'] == '待命'
