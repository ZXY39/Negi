from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_script(script: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "-"],
        input=textwrap.dedent(script),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout.strip())
    return json.loads(completed.stdout.strip())


def test_rendered_tree_builds_from_normalized_snapshot() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          currentTaskId: "task:test",
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
          taskNodeDetails: {},
          liveFrameMap: {},
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        applyTaskTreeSnapshotPayload({
          task_id: "task:test",
          root_node_id: "root",
          snapshot_version: "1",
          nodes_by_id: {
            root: {
              node_id: "root",
              title: "root",
              status: "in_progress",
              node_kind: "execution",
              default_round_id: "r1",
              rounds: [{ round_id: "r1", label: "Round 1", is_latest: true, child_ids: ["a", "b"] }],
              auxiliary_child_ids: [],
            },
            a: {
              node_id: "a",
              parent_node_id: "root",
              title: "a",
              status: "in_progress",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: ["a1"],
            },
            a1: {
              node_id: "a1",
              parent_node_id: "a",
              title: "a1",
              status: "in_progress",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
            b: {
              node_id: "b",
              parent_node_id: "root",
              title: "b",
              status: "in_progress",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
          },
        });

        const root = buildExecutionTreeFromSnapshot();
        const a = findTreeNode(root, "a");
        console.log(JSON.stringify({
          rootChildren: root.children.map((node) => node.node_id),
          aChildren: a.children.map((node) => node.node_id),
        }));
        """
    )

    assert result["rootChildren"] == ["a", "b"]
    assert result["aChildren"] == ["a1"]


def test_tree_node_pause_always_confirms_and_only_sends_confirmed_cascade() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = { currentTaskId: "task:test", currentTask: { is_paused: false, pause_requested: false } };
        global.U = {};
        const pauseCalls = [];
        const toasts = [];
        let confirmation = null;
        global.ApiClient = {
          pauseTaskNode: async (taskId, nodeId, payload) => { pauseCalls.push({ taskId, nodeId, payload }); },
          resumeTaskNode: async () => { throw new Error("resume should not be called"); },
        };
        global.showToast = (payload) => { toasts.push(payload); };
        global.loadTaskTreeSnapshot = async () => {};
        global.renderTree = () => {};
        global.openConfirm = (options) => { confirmation = options; };
        global.window.confirm = () => { throw new Error("native confirmation must not be used"); };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const activeParent = {
          node_id: "node:parent",
          is_paused: false,
          children: [{ node_id: "node:child", state: "in_progress", is_paused: false }],
        };
        const event = { preventDefault() {}, stopPropagation() {}, currentTarget: { id: "pause-button" } };
        Promise.resolve(handleTreeNodePauseAction(activeParent, event))
          .then(async () => {
            const noRequestBeforeConfirmation = pauseCalls.length === 0;
            const modal = confirmation && {
              title: confirmation.title,
              text: confirmation.text,
              confirmLabel: confirmation.confirmLabel,
              checkbox: confirmation.checkbox,
              hasOnConfirm: typeof confirmation.onConfirm === "function",
            };
            await confirmation.onConfirm({ checked: true });
            const afterConfirmedCascade = pauseCalls.slice();
            confirmation = null;
            await handleTreeNodePauseAction({
              node_id: "node:solo",
              is_paused: false,
              children: [],
            }, event);
            const noRequestBeforeSoloConfirmation = pauseCalls.length === afterConfirmedCascade.length;
            const soloModal = confirmation && {
              title: confirmation.title,
              text: confirmation.text,
              confirmLabel: confirmation.confirmLabel,
              checkbox: confirmation.checkbox,
              hasOnConfirm: typeof confirmation.onConfirm === "function",
            };
            await confirmation.onConfirm({ checked: false });
            console.log(JSON.stringify({
              nativeConfirmPresent: code.includes("window.confirm"),
              noRequestBeforeConfirmation,
              modal,
              afterConfirmedCascade,
              noRequestBeforeSoloConfirmation,
              soloModal,
              afterDirectPause: pauseCalls.slice(),
              toastCount: toasts.length,
            }));
          })
          .catch((error) => { console.error(error); process.exitCode = 1; });
        """
    )

    assert result["nativeConfirmPresent"] is False
    assert result["noRequestBeforeConfirmation"] is True
    assert result["modal"] == {
        "title": "暂停节点",
        "text": "暂停父节点本身不会自动停止子节点。",
        "confirmLabel": "暂停节点",
        "checkbox": {"label": "同时暂停所有子节点（包括检验节点）", "checked": False},
        "hasOnConfirm": True,
    }
    assert result["afterConfirmedCascade"] == [
        {"taskId": "task:test", "nodeId": "node:parent", "payload": {"cascade": True}}
    ]
    assert result["noRequestBeforeSoloConfirmation"] is True
    assert result["soloModal"] == {
        "title": "暂停节点",
        "text": "暂停后该节点将停止执行，需要手动恢复。",
        "confirmLabel": "暂停节点",
        "checkbox": None,
        "hasOnConfirm": True,
    }
    assert result["afterDirectPause"][-1] == {
        "taskId": "task:test",
        "nodeId": "node:solo",
        "payload": {"cascade": False},
    }



def test_task_pause_projects_over_non_terminal_tree_nodes_without_mutating_node_pause_state() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          currentTaskId: "task:test",
          currentTask: { task_id: "task:test", status: "in_progress", is_paused: true, pause_requested: true },
          treeRootNodeId: "root",
          treeNodesById: {},
          treeSnapshotVersion: "",
          // 该用例要用终态节点做对照，显式打开"显示已结束节点"。
          treeShowTerminal: true,
          treeView: null,
          treeLargeMode: false,
          treeDirtyParentsById: {},
          treeBranchSyncInFlightById: {},
          treeBranchSyncQueuedById: {},
          treeBranchSyncTokenById: {},
          treeSelectedRoundByNodeId: {},
          taskNodeDetails: {},
          liveFrameMap: {},
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        applyTaskTreeSnapshotPayload({
          task_id: "task:test",
          root_node_id: "root",
          snapshot_version: "1",
          nodes_by_id: {
            root: {
              node_id: "root",
              title: "root",
              status: "in_progress",
              node_kind: "execution",
              rounds: [{ round_id: "r1", label: "Round 1", is_latest: true, child_ids: ["running", "done", "locally-paused"] }],
              auxiliary_child_ids: [],
              all_child_ids: ["running", "done", "locally-paused"],
            },
            running: {
              node_id: "running",
              parent_node_id: "root",
              title: "running",
              status: "in_progress",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
            done: {
              node_id: "done",
              parent_node_id: "root",
              title: "done",
              status: "success",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
            "locally-paused": {
              node_id: "locally-paused",
              parent_node_id: "root",
              title: "locally paused",
              status: "in_progress",
              node_kind: "execution",
              is_paused: true,
              pause_reason: "manual",
              rounds: [],
              auxiliary_child_ids: [],
            },
          },
        });

        const pausedTree = buildExecutionTreeFromSnapshot();
        const pausedById = Object.fromEntries(
          [pausedTree, ...pausedTree.children].map((node) => [node.node_id, node])
        );
        S.currentTask = { task_id: "task:test", status: "in_progress", is_paused: false, pause_requested: false };
        S.liveFrameMap = { running: { node_id: "running", phase: "waiting_tool_results", tool_calls: [{ tool_call_id: "t1", tool_name: "exec", status: "running" }], child_pipelines: [] } };
        const resumedTree = buildExecutionTreeFromSnapshot();
        const resumedById = Object.fromEntries(
          [resumedTree, ...resumedTree.children].map((node) => [node.node_id, node])
        );
        console.log(JSON.stringify({
          paused: {
            root: [pausedById.root.display_state, pausedById.root.effective_is_paused, pausedById.root.is_paused, pausedById.root.task_paused],
            running: [pausedById.running.display_state, pausedById.running.effective_is_paused, pausedById.running.is_paused],
            done: [pausedById.done.display_state, pausedById.done.effective_is_paused],
            local: [pausedById["locally-paused"].display_state, pausedById["locally-paused"].effective_is_paused, pausedById["locally-paused"].is_paused],
          },
          resumed: {
            root: [resumedById.root.display_state, resumedById.root.effective_is_paused],
            running: [resumedById.running.display_state, resumedById.running.effective_is_paused],
            done: [resumedById.done.display_state, resumedById.done.effective_is_paused],
            local: [resumedById["locally-paused"].display_state, resumedById["locally-paused"].effective_is_paused, resumedById["locally-paused"].is_paused],
          },
        }));
        """
    )

    assert result["paused"]["root"] == ["\u4efb\u52a1\u6682\u505c", True, False, True]
    assert result["paused"]["running"] == ["\u4efb\u52a1\u6682\u505c", True, False]
    assert result["paused"]["done"] == ["SUCCESS", False]
    assert result["paused"]["local"] == ["\u4efb\u52a1\u6682\u505c", True, True]
    assert result["resumed"]["root"] == ["等待中", False]
    # 恢复后该节点处在 waiting_tool_results，派生显示态是"运行中（工具在途）"。
    assert result["resumed"]["running"] == ["运行中（工具在途）", False]
    assert result["resumed"]["done"] == ["SUCCESS", False]
    assert result["resumed"]["local"] == ["\u6682\u505c\u00b7\u666e\u901a", True, True]


def test_ensure_task_tree_subtree_uses_new_snapshot_endpoint() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        let requestCount = 0;
        global.S = {
          currentTaskId: "task:test",
          treeRootNodeId: "root",
          treeNodesById: {
            root: {
              node_id: "root",
              title: "root",
              status: "in_progress",
              node_kind: "execution",
              default_round_id: "",
              rounds: [],
              auxiliary_child_ids: ["old-child"],
            },
            "old-child": {
              node_id: "old-child",
              parent_node_id: "root",
              title: "old child",
              status: "in_progress",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
          },
          treeSnapshotVersion: "1",
          treeView: null,
          treeLargeMode: false,
          treeDirtyParentsById: { root: true },
          treeBranchSyncInFlightById: {},
          treeBranchSyncQueuedById: {},
          treeBranchSyncTokenById: {},
          treeSelectedRoundByNodeId: {},
          taskNodeDetails: {},
          liveFrameMap: {},
        };
        global.U = {};
        global.ApiClient = {
          getTaskTreeSnapshot: async () => ({}),
          getTaskNodeTreeSubtree: async () => {
            requestCount += 1;
            return {
              task_id: "task:test",
              root_node_id: "root",
              snapshot_version: "2",
              nodes_by_id: {
                root: {
                  node_id: "root",
                  title: "root",
                  status: "in_progress",
                  node_kind: "execution",
                  default_round_id: "",
                  rounds: [],
                  auxiliary_child_ids: ["fresh-child"],
                },
                "fresh-child": {
                  node_id: "fresh-child",
                  parent_node_id: "root",
                  title: "fresh child",
                  status: "in_progress",
                  node_kind: "execution",
                  rounds: [],
                  auxiliary_child_ids: [],
                },
              },
            };
          },
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        ensureTaskTreeSubtree("root", { force: true }).then((payload) => {
          console.log(JSON.stringify({
            requestCount,
            dirtyCleared: taskTreeParentIsDirty("root") === false,
            childIds: S.treeNodesById.root.all_child_ids,
            returnedRoot: payload.root_node_id,
          }));
        });
        """
    )

    assert result["requestCount"] == 1
    assert result["dirtyCleared"] is True
    assert result["childIds"] == ["fresh-child"]
    assert result["returnedRoot"] == "root"


def test_sync_task_tree_header_counts_non_terminal_non_waiting_nodes() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          currentTaskId: "task:test",
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
          taskNodeDetails: {},
          liveFrameMap: {},
          taskSummary: { active_node_count: 0 },
        };
        global.U = {
          tdActiveCount: { textContent: "" },
          taskTreeResetRounds: { hidden: true, disabled: true, classList: { toggle: () => {} }, title: "" },
        };
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderTree = () => {};
        S.liveFrameMap = indexTaskLiveFrames([
          { node_id: "root", phase: "after_model", child_pipelines: [] },
          { node_id: "parent", phase: "waiting_children", child_pipelines: [{ status: "running" }] },
          { node_id: "leaf-running", phase: "before_model", child_pipelines: [] },
          { node_id: "leaf-success", phase: "after_model", child_pipelines: [] },
          { node_id: "leaf-failed", phase: "after_model", child_pipelines: [] },
          { node_id: "leaf-waiting", phase: "after_model", child_pipelines: [{ status: "queued" }] },
        ]);

        applyTaskTreeSnapshotPayload({
          task_id: "task:test",
          root_node_id: "root",
          snapshot_version: "1",
          nodes_by_id: {
            root: {
              node_id: "root",
              title: "root",
              status: "in_progress",
              node_kind: "execution",
              default_round_id: "",
              rounds: [],
              auxiliary_child_ids: ["parent", "leaf-success", "leaf-failed", "leaf-waiting"],
            },
            parent: {
              node_id: "parent",
              parent_node_id: "root",
              title: "parent",
              status: "in_progress",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: ["leaf-running"],
            },
            "leaf-running": {
              node_id: "leaf-running",
              parent_node_id: "parent",
              title: "leaf-running",
              status: "running",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
            "leaf-success": {
              node_id: "leaf-success",
              parent_node_id: "root",
              title: "leaf-success",
              status: "success",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
            "leaf-failed": {
              node_id: "leaf-failed",
              parent_node_id: "root",
              title: "leaf-failed",
              status: "failed",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
            "leaf-waiting": {
              node_id: "leaf-waiting",
              parent_node_id: "root",
              title: "leaf-waiting",
              status: "waiting",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
          },
        });

        const root = buildExecutionTreeFromSnapshot();
        syncTaskTreeHeaderState(root);
        console.log(JSON.stringify({
          activeCountText: U.tdActiveCount.textContent,
          activeCountSummary: S.taskSummary.active_node_count,
          rootActiveNodeCount: root.activeNodeCount,
        }));
        """
    )

    assert result["activeCountText"] == "2"
    assert result["activeCountSummary"] == 2
    assert result["rootActiveNodeCount"] == 2


def test_task_status_helpers_treat_unpassed_as_non_failed_without_continue_action() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const pStatusStart = appCode.indexOf("const pStatus");
        const helperStart = appCode.indexOf("const canPause");
        const helperEnd = appCode.indexOf("function normalizeTokenUsage");
        vm.runInThisContext(appCode.slice(pStatusStart, helperStart));
        vm.runInThisContext(appCode.slice(helperStart, helperEnd));

        global.taskWorkerControlsAvailable = () => true;

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const labelStart = tasksCode.indexOf("function taskStatusLabel");
        const labelEnd = tasksCode.indexOf("function getSelectedTasks");
        const actionStart = tasksCode.indexOf("function taskActionTone");
        const actionEnd = tasksCode.indexOf("function taskActionSuccessTitle");
        vm.runInThisContext(tasksCode.slice(labelStart, labelEnd));
        vm.runInThisContext(tasksCode.slice(actionStart, actionEnd));

        const engineFailed = {
          task_id: "task:engine",
          status: "failed",
          failure_class: "engine_failure",
        };
        const unpassed = {
          task_id: "task:unpassed",
          status: "success",
          failure_class: "business_unpassed",
          final_acceptance: { status: "failed" },
        };

        console.log(JSON.stringify({
          engineRetry: canRetry(engineFailed),
          unpassedRetry: canRetry(unpassed),
          unpassedStatus: taskStatusKey(unpassed),
          unpassedLabel: taskStatusLabel(unpassed),
          unpassedInFailedBucket: statusBucketMatches(unpassed, "failed"),
          primaryAction: primaryTaskAction(unpassed),
          actions: taskCardActions(unpassed).map((item) => item.action),
        }));
        """
    )

    assert result["engineRetry"] is False
    assert result["unpassedRetry"] is False
    assert result["unpassedStatus"] == "unpassed"
    assert result["unpassedLabel"] == "未通过"
    assert result["unpassedInFailedBucket"] is False
    assert result["primaryAction"] is None
    # 终态任务卡片动作：清临时文件 + 删除（两者门槛不同，delete 还含 paused）。
    assert result["actions"] == ["clear_temp", "delete"]


def test_task_selection_menu_exposes_completed_and_unpassed_buckets() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert 'data-select-bucket="completed"' in html
    assert 'data-select-bucket="unpassed"' in html


def test_task_status_helpers_match_completed_and_unpassed_selection_buckets() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const pStatusStart = appCode.indexOf("const pStatus");
        const helperStart = appCode.indexOf("const canPause");
        const helperEnd = appCode.indexOf("function normalizeTokenUsage");
        vm.runInThisContext(appCode.slice(pStatusStart, helperStart));
        vm.runInThisContext(appCode.slice(helperStart, helperEnd));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const labelStart = tasksCode.indexOf("function taskStatusLabel");
        const labelEnd = tasksCode.indexOf("function getSelectedTasks");
        vm.runInThisContext(tasksCode.slice(labelStart, labelEnd));

        const completed = {
          task_id: "task:done",
          status: "success",
        };
        const unpassed = {
          task_id: "task:unpassed",
          status: "success",
          failure_class: "business_unpassed",
          final_acceptance: { status: "failed" },
        };

        console.log(JSON.stringify({
          completedBucket: statusBucketMatches(completed, "completed"),
          completedFailedBucket: statusBucketMatches(completed, "failed"),
          unpassedBucket: statusBucketMatches(unpassed, "unpassed"),
          unpassedCompletedBucket: statusBucketMatches(unpassed, "completed"),
        }));
        """
    )

    assert result["completedBucket"] is True
    assert result["completedFailedBucket"] is False
    assert result["unpassedBucket"] is True
    assert result["unpassedCompletedBucket"] is False


def test_task_status_helpers_ignore_legacy_continuation_metadata() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const pStatusStart = appCode.indexOf("const pStatus");
        const helperStart = appCode.indexOf("const canPause");
        const helperEnd = appCode.indexOf("function normalizeTokenUsage");
        vm.runInThisContext(appCode.slice(pStatusStart, helperStart));
        vm.runInThisContext(appCode.slice(helperStart, helperEnd));

        global.taskWorkerControlsAvailable = () => true;

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const labelStart = tasksCode.indexOf("function taskStatusLabel");
        const labelEnd = tasksCode.indexOf("function getSelectedTasks");
        const actionStart = tasksCode.indexOf("function taskActionTone");
        const actionEnd = tasksCode.indexOf("function taskActionSuccessTitle");
        vm.runInThisContext(tasksCode.slice(labelStart, labelEnd));
        vm.runInThisContext(tasksCode.slice(actionStart, actionEnd));

        const taskViewCode = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const detailStatusStart = taskViewCode.indexOf("function taskDetailStatusLabel");
        const detailStatusEnd = taskViewCode.indexOf("function taskInitialPromptText");
        vm.runInThisContext(taskViewCode.slice(detailStatusStart, detailStatusEnd));

        const recreated = {
          task_id: "task:recreated",
          status: "failed",
          failure_class: "engine_failure",
          continuation_state: "recreated",
          continued_by_task_id: "task:cont-1",
        };
        const retried = {
          task_id: "task:retried",
          status: "in_progress",
          continuation_state: "retried_in_place",
          retry_count: 2,
          recovery_notice: "legacy recovery notice",
        };

        console.log(JSON.stringify({
          recreatedRetry: canRetry(recreated),
          recreatedStatus: taskStatusKey(recreated),
          recreatedLabel: taskStatusLabel(recreated),
          recreatedSummary: taskContinuationSummary(recreated),
          recreatedActions: taskCardActions(recreated).map((item) => item.action),
          recreatedPrimary: primaryTaskAction(recreated),
          recreatedDetailLabel: taskDetailStatusLabel(recreated),
          retriedStatus: taskStatusKey(retried),
          retriedLabel: taskStatusLabel(retried),
          retriedSummary: taskContinuationSummary(retried),
          retriedDetailLabel: taskDetailStatusLabel(retried),
          retriedPrimary: primaryTaskAction(retried),
        }));
        """
    )

    assert result["recreatedRetry"] is False
    assert result["recreatedStatus"] == "failed"
    assert result["recreatedLabel"] == "失败"
    assert result["recreatedSummary"] == ""
    # zip 归档/pin 机制已移除：终态任务卡片动作是清临时文件 + 删除。
    assert result["recreatedActions"] == ["clear_temp", "delete"]
    assert result["recreatedPrimary"] is None
    assert result["recreatedDetailLabel"] == "失败"
    assert result["retriedStatus"] == "in_progress"
    assert result["retriedLabel"] == "运行"
    assert result["retriedSummary"] == ""
    assert result["retriedDetailLabel"] == "运行中"
    assert result["retriedPrimary"]["action"] == "pause"


def test_render_task_token_stats_paginates_model_calls_and_uses_chinese_labels() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        global.S = {
          currentTask: {
            token_usage: {
              tracked: true,
              input_tokens: 13500,
              output_tokens: 2700,
              cache_hit_tokens: 5400,
              call_count: 135,
              calls_with_usage: 135,
              calls_without_usage: 0,
              is_partial: false,
            },
          },
          taskSummary: {
            token_usage_by_model: [],
          },
          recentModelCalls: Array.from({ length: 135 }, (_, idx) => ({
            call_index: idx + 1,
            node_id: `node:demo:${idx + 1}`,
            created_at: new Date(Date.UTC(2026, 8, 14, 0, idx, 5)).toISOString(),
            prepared_message_count: idx + 2,
            prepared_message_chars: (idx + 1) * 100,
            response_tool_call_count: idx % 4,
            // 每 5 条缺耗时、每 7 条缺思考 token：第 2 页（序号 35..1）同时覆盖
            // 有值 / "--" 两种渲染；idx=1 → 920ms、idx=34 → 1.6s 覆盖两种耗时格式。
            duration_ms: idx % 5 === 0 ? null : 900 + idx * 20,
            first_token_ms: idx % 5 === 0 ? null : 200 + idx,
            thinking_tokens: idx % 7 === 0 ? null : idx * 3,
            delta_usage: {
              tracked: true,
              input_tokens: idx + 10,
              output_tokens: idx + 5,
              cache_hit_tokens: idx + 3,
              call_count: 1,
              calls_with_usage: 1,
              calls_without_usage: 0,
              is_partial: false,
            },
            delta_usage_by_model: [{ model_key: `model-${idx + 1}` }],
          })),
          taskModelCallsPage: 2,
          taskModelCallsPageSize: 100,
          taskModelCallsQuery: "",
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStart = appCode.indexOf("const EMPTY_TOKEN_USAGE");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStatsStart = tasksCode.indexOf("function renderTasks");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        renderTaskTokenStats();
        const html = U.taskTokenContent.innerHTML;
        const tableBody = html.match(/<tbody>([\\s\\S]*?)<\\/tbody>/)?.[1] || "";
        const callIndexValues = Array.from(tableBody.matchAll(/data-task-call-index>([\\d,]+)<\\/td>/g))
          .map((match) => Number(String(match[1] || "").replaceAll(",", "")));

        console.log(JSON.stringify({
          headingLocalized: html.includes("模型调用明细"),
          paginationLocalized: html.includes("第 2/2 页") && html.includes("显示 101-135 / 共 135 条"),
          columnsLocalized: [
            "序号",
            "时间",
            "节点ID",
            "类型",
            "预处理字符数",
            "新增输入 Token",
            "缓存命中",
            "命中率",
            "思考 Token",
            "工具调用数",
            "首 Token 耗时",
            "总耗时",
            "模型",
          ].every((label) => html.includes(label)),
          durationColumnsRendered: /<td class="task-token-call-duration"[^>]*>\\d+ms<\\/td>/.test(tableBody)
            && /<td class="task-token-call-duration"[^>]*>\\d+\\.\\ds<\\/td>/.test(tableBody),
          durationFallbackRendered: (tableBody.match(/task-token-call-duration[^>]*>--</g) || []).length >= 2,
          thinkingColumnRendered: (tableBody.match(/<td class="task-token-call-thinking"/g) || []).length
            === callIndexValues.length,
          thinkingFallbackRendered: /<td class="task-token-call-thinking"[^>]*>--<\\/td>/.test(tableBody),
          timeColumnRendered: /<td>\\d{1,2}:\\d{2}:\\d{2}<\\/td>/.test(tableBody)
            || /<td>\\d{2}-\\d{2} \\d{1,2}:\\d{2}:\\d{2}<\\/td>/.test(tableBody),
          nodeIdColumnRendered: tableBody.includes("node:demo:"),
          searchBoxRendered: html.includes("data-task-model-call-search")
            && html.includes("搜索序号 / 节点 ID / 类型 / 模型名称"),
          refreshButtonRendered: html.includes("data-task-model-call-refresh"),
          rowCount: callIndexValues.length,
          firstCallIndex: callIndexValues[0],
          lastCallIndex: callIndexValues[callIndexValues.length - 1],
        }));
        """
    )

    assert result["headingLocalized"] is True
    assert result["paginationLocalized"] is True
    assert result["columnsLocalized"] is True
    assert result["durationColumnsRendered"] is True
    assert result["durationFallbackRendered"] is True
    assert result["thinkingColumnRendered"] is True
    assert result["thinkingFallbackRendered"] is True
    assert result["timeColumnRendered"] is True
    assert result["nodeIdColumnRendered"] is True
    assert result["searchBoxRendered"] is True
    assert result["refreshButtonRendered"] is True
    assert result["rowCount"] == 35
    assert result["firstCallIndex"] == 35
    assert result["lastCallIndex"] == 1


def test_render_task_token_stats_sorts_model_calls_by_time_desc() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        global.S = {
          currentTask: {
            token_usage: {
              tracked: true,
              input_tokens: 30,
              output_tokens: 3,
              cache_hit_tokens: 0,
              call_count: 3,
              calls_with_usage: 3,
              calls_without_usage: 0,
              is_partial: false,
            },
          },
          taskSummary: { token_usage_by_model: [] },
          // created_at 顺序与 call_index 顺序不一致：时间才是默认排序键。
          recentModelCalls: [
            {
              call_index: 1,
              node_id: "node:a",
              created_at: "2026-09-13T10:00:05+08:00",
              prepared_message_count: 1,
              prepared_message_chars: 10,
              response_tool_call_count: 0,
              delta_usage: { tracked: true, input_tokens: 10, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
              delta_usage_by_model: [{ model_key: "m" }],
            },
            {
              call_index: 2,
              node_id: "node:b",
              created_at: "2026-09-13T10:00:01+08:00",
              prepared_message_count: 1,
              prepared_message_chars: 10,
              response_tool_call_count: 0,
              delta_usage: { tracked: true, input_tokens: 10, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
              delta_usage_by_model: [{ model_key: "m" }],
            },
            {
              call_index: 3,
              node_id: "node:c",
              created_at: "2026-09-13T10:00:03+08:00",
              prepared_message_count: 1,
              prepared_message_chars: 10,
              response_tool_call_count: 0,
              delta_usage: { tracked: true, input_tokens: 10, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
              delta_usage_by_model: [{ model_key: "m" }],
            },
          ],
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
          taskModelCallsQuery: "",
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStart = appCode.indexOf("const EMPTY_TOKEN_USAGE");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStatsStart = tasksCode.indexOf("function renderTasks");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        renderTaskTokenStats();
        const html = U.taskTokenContent.innerHTML;
        const tableBody = html.match(/<tbody>([\\s\\S]*?)<\\/tbody>/)?.[1] || "";
        const callIndexValues = Array.from(tableBody.matchAll(/data-task-call-index>([\\d,]+)<\\/td>/g))
          .map((match) => Number(String(match[1] || "").replaceAll(",", "")));

        console.log(JSON.stringify({ callIndexValues }));
        """
    )

    # 时间倒序：10:00:05(call 1) > 10:00:03(call 3) > 10:00:01(call 2)
    assert result["callIndexValues"] == [1, 3, 2]


def test_render_task_token_stats_search_filters_all_records_not_current_page() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        const makeCall = (idx) => ({
          call_index: idx,
          node_id: `node:alpha:${idx}`,
          created_at: new Date(Date.UTC(2026, 8, 14, 0, 0, idx)).toISOString(),
          prepared_message_count: 1,
          prepared_message_chars: 10,
          response_tool_call_count: 0,
          delta_usage: { tracked: true, input_tokens: 10, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
          delta_usage_by_model: [{ model_key: idx === 5 ? "zebra-model" : `model-${idx}` }],
        });
        global.S = {
          currentTask: {
            token_usage: {
              tracked: true,
              input_tokens: 1350,
              output_tokens: 135,
              cache_hit_tokens: 0,
              call_count: 135,
              calls_with_usage: 135,
              calls_without_usage: 0,
              is_partial: false,
            },
          },
          taskSummary: { token_usage_by_model: [] },
          recentModelCalls: Array.from({ length: 135 }, (_, idx) => makeCall(idx + 1)),
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStart = appCode.indexOf("const EMPTY_TOKEN_USAGE");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStatsStart = tasksCode.indexOf("function renderTasks");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        renderTaskTokenStats();
        const html = U.taskTokenContent.innerHTML;
        const extractCallIndexes = (markup) => Array
          .from(markup.matchAll(/data-task-call-index>([\\d,]+)<\\/td>/g))
          .map((match) => Number(String(match[1] || "").replaceAll(",", "")));

        // 未搜索：第 1 页 100 条，call 5 在第 2 页。
        const withoutQuery = {
          rowCount: extractCallIndexes(html).length,
          summary: html.includes("共 135 条"),
        };

        // 按模型名搜索：命中记录（call 5）在未过滤列表的第 2 页，
        // 搜索必须作用于全部记录而不是当前页。
        S.taskModelCallsQuery = "zebra";
        S.taskModelCallsPage = 1;
        renderTaskTokenStats({ force: true });
        const zebraHtml = U.taskTokenContent.innerHTML;
        const byModel = {
          indexes: extractCallIndexes(zebraHtml),
          summary: zebraHtml.includes("共 1 条"),
          searchValuePreserved: zebraHtml.includes('value="zebra"'),
        };

        // 按节点 ID 搜索：匹配 7、70-79 共 11 条。
        S.taskModelCallsQuery = "node:alpha:7";
        S.taskModelCallsPage = 1;
        renderTaskTokenStats({ force: true });
        const nodeHtml = U.taskTokenContent.innerHTML;
        const byNode = {
          indexes: extractCallIndexes(nodeHtml),
          summary: nodeHtml.includes("共 11 条"),
        };

        // 无匹配时给出空态提示。
        S.taskModelCallsQuery = "no-such-thing";
        S.taskModelCallsPage = 1;
        renderTaskTokenStats({ force: true });
        const emptyHtml = U.taskTokenContent.innerHTML;

        console.log(JSON.stringify({
          withoutQuery,
          byModel,
          byNode,
          emptyStateShown: emptyHtml.includes("未找到匹配的记录。"),
        }));
        """
    )

    assert result["withoutQuery"]["rowCount"] == 100
    assert result["withoutQuery"]["summary"] is True
    assert result["byModel"]["indexes"] == [5]
    assert result["byModel"]["summary"] is True
    assert result["byModel"]["searchValuePreserved"] is True
    assert result["byNode"]["indexes"] == [79, 78, 77, 76, 75, 74, 73, 72, 71, 70, 7]
    assert result["byNode"]["summary"] is True
    assert result["emptyStateShown"] is True


def test_render_task_token_stats_search_supports_call_index() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        // 节点 ID 与模型名均不含数字：纯数字查询只能命中序号。
        const makeCall = (idx) => ({
          call_index: idx,
          node_id: "node:alpha",
          created_at: new Date(Date.UTC(2026, 8, 14, 0, 0, idx)).toISOString(),
          prepared_message_count: 1,
          prepared_message_chars: 10,
          response_tool_call_count: 0,
          delta_usage: { tracked: true, input_tokens: 10, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
          delta_usage_by_model: [{ model_key: "alpha-model" }],
        });
        global.S = {
          currentTask: {
            token_usage: {
              tracked: true,
              input_tokens: 30,
              output_tokens: 3,
              cache_hit_tokens: 0,
              call_count: 3,
              calls_with_usage: 3,
              calls_without_usage: 0,
              is_partial: false,
            },
          },
          taskSummary: { token_usage_by_model: [] },
          recentModelCalls: [makeCall(1), makeCall(2), makeCall(12)],
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStart = appCode.indexOf("const EMPTY_TOKEN_USAGE");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStatsStart = tasksCode.indexOf("function renderTasks");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        S.taskModelCallsQuery = "2";
        renderTaskTokenStats();
        const html = U.taskTokenContent.innerHTML;
        const indexes = Array.from(html.matchAll(/data-task-call-index>([\\d,]+)<\\/td>/g))
          .map((match) => Number(String(match[1] || "").replaceAll(",", "")));

        console.log(JSON.stringify({ indexes, summary: html.includes("共 2 条") }));
        """
    )

    # "2" 按序号子串匹配 call 2 与 call 12（节点/模型均无数字，证明序号搜索生效）。
    assert result["indexes"] == [12, 2]
    assert result["summary"] is True


def test_render_task_token_stats_labels_models_with_user_config_names() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        const usage = (input, output) => ({
          tracked: true,
          input_tokens: input,
          output_tokens: output,
          cache_hit_tokens: 0,
          call_count: 1,
          calls_with_usage: 1,
          calls_without_usage: 0,
          is_partial: false,
        });
        global.S = {
          currentTask: { token_usage: usage(1200, 120) },
          modelCatalog: {
            catalog: [
              { key: "glm-5.2", name: "glm 主力", provider_model: "zhipu:glm-5.2" },
              { key: "glm-5.2-2", name: "glm 5.21", provider_model: "zhipu:glm-5.2" },
            ],
          },
          taskSummary: {
            token_usage_by_model: [
              { ...usage(700, 70), model_key: "glm-5.2-2", provider_id: "zhipu", provider_model: "glm-5.2" },
              { ...usage(500, 50), model_key: "orphan-key", provider_id: "zhipu", provider_model: "glm-5.2" },
            ],
          },
          recentModelCalls: [
            {
              call_index: 1,
              node_id: "node:alpha",
              created_at: "2026-09-14T00:00:01.000Z",
              prepared_message_count: 1,
              prepared_message_chars: 10,
              response_tool_call_count: 0,
              delta_usage: usage(700, 70),
              delta_usage_by_model: [{ ...usage(700, 70), model_key: "glm-5.2-2", provider_id: "zhipu", provider_model: "glm-5.2" }],
            },
            {
              call_index: 2,
              node_id: "node:beta",
              created_at: "2026-09-14T00:00:02.000Z",
              prepared_message_count: 1,
              prepared_message_chars: 10,
              response_tool_call_count: 0,
              delta_usage: usage(500, 50),
              delta_usage_by_model: [{ ...usage(500, 50), model_key: "orphan-key", provider_id: "zhipu", provider_model: "glm-5.2" }],
            },
          ],
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStart = appCode.indexOf("const EMPTY_TOKEN_USAGE");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        // modelDisplayTitle 定义在 ceoModelDisplayTitle 之前：起点必须含它，否则桩里缺被依赖函数。
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStatsStart = tasksCode.indexOf("function renderTasks");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        const readModelCells = (markup) => {
          const tableBody = markup.match(/<tbody>([\\s\\S]*?)<\\/tbody>/)?.[1] || "";
          return Array.from(tableBody.matchAll(/<td class="task-token-call-model" title="([^"]*)">([^<]*)<\\/td>/g))
            .map((match) => ({ title: match[1], text: match[2] }));
        };

        renderTaskTokenStats();
        const html = U.taskTokenContent.innerHTML;

        S.taskModelCallsQuery = "glm 5.21";
        S.taskModelCallsPage = 1;
        renderTaskTokenStats({ force: true });
        const byConfigName = readModelCells(U.taskTokenContent.innerHTML);

        console.log(JSON.stringify({
          html,
          modelCells: readModelCells(html),
          byConfigName,
        }));
        """
    )

    html = result["html"]
    # 汇总行与明细列都显示用户写的配置名，而不是账本里的裸 key
    assert "<h3>glm 5.21</h3>" in html
    assert result["modelCells"] == [
        {"title": "orphan-key", "text": "orphan-key"},
        {"title": "glm-5.2-2", "text": "glm 5.21"},
    ]
    # 配置名之外的 key 仍留在副标题与悬停标题里，改名/删配置后可据此定位
    assert "glm-5.2-2 · zhipu · glm-5.2" in html
    # 目录里查不到（配置已删）时退回 key 本身，不显示空白
    assert "<h3>orphan-key</h3>" in html
    # 搜索命中的是配置名：该串不出现在任何 key / provider_model 里
    assert result["byConfigName"] == [{"title": "glm-5.2-2", "text": "glm 5.21"}]


def test_render_task_token_stats_labels_call_kind_and_filters_by_it() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        const usage = (input, output) => ({
          tracked: true,
          input_tokens: input,
          output_tokens: output,
          cache_hit_tokens: 0,
          call_count: 1,
          calls_with_usage: 1,
          calls_without_usage: 0,
          is_partial: false,
        });
        global.S = {
          currentTask: { token_usage: usage(1400, 90) },
          modelCatalog: { catalog: [] },
          taskSummary: { token_usage_by_model: [] },
          recentModelCalls: [
            {
              call_index: 7,
              node_id: "node:normal",
              created_at: "2026-09-14T00:00:01.000Z",
              prepared_message_count: 1,
              prepared_message_chars: 10,
              response_tool_call_count: 1,
              delta_usage: usage(500, 50),
              delta_usage_by_model: [{ ...usage(500, 50), model_key: "glm-5.2" }],
            },
            {
              call_index: 1,
              node_id: "node:root",
              created_at: "2026-09-14T00:00:02.000Z",
              call_kind: "message_distribution",
              prepared_message_count: 1,
              prepared_message_chars: 10,
              response_tool_call_count: 1,
              delta_usage: usage(900, 40),
              delta_usage_by_model: [{ ...usage(900, 40), model_key: "glm-5.2" }],
            },
          ],
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(appCode.slice(appCode.indexOf("const EMPTY_TOKEN_USAGE"), appCode.indexOf("function ensureTaskTokenUi")));
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(tasksCode.slice(tasksCode.indexOf("function renderTasks"), tasksCode.indexOf("async function loadTaskDetail")));

        const readKindCells = (markup) => {
          const tableBody = markup.match(/<tbody>([\\s\\S]*?)<\\/tbody>/)?.[1] || "";
          return Array.from(tableBody.matchAll(/<td class="task-token-call-kind" title="([^"]*)">([^<]*)<\\/td>/g))
            .map((match) => ({ title: match[1], text: match[2] }));
        };

        renderTaskTokenStats();
        const html = U.taskTokenContent.innerHTML;

        S.taskModelCallsQuery = "分发";
        S.taskModelCallsPage = 1;
        renderTaskTokenStats({ force: true });
        const filtered = readKindCells(U.taskTokenContent.innerHTML);

        S.taskModelCallsQuery = "message_distribution";
        renderTaskTokenStats({ force: true });
        const byRawKind = readKindCells(U.taskTokenContent.innerHTML);

        console.log(JSON.stringify({
          hasHeader: html.includes("<th>类型</th>"),
          placeholder: html.includes("搜索序号 / 节点 ID / 类型 / 模型名称"),
          kindCells: readKindCells(html),
          filtered,
          byRawKind,
        }));
        """
    )

    assert result["hasHeader"] is True
    assert result["placeholder"] is True
    # 空 call_kind（含历史行）读成普通回合，分发控制回合单独一类，悬停保留账本原值
    assert result["kindCells"] == [
        {"title": "message_distribution", "text": "分发消息"},
        {"title": "普通回合", "text": "普通回合"},
    ]
    # 中文标签与账本 key 都能筛：明细表混着两类请求时靠这一列定位分发期调用
    assert result["filtered"] == [{"title": "message_distribution", "text": "分发消息"}]
    assert result["byRawKind"] == [{"title": "message_distribution", "text": "分发消息"}]


def test_render_task_token_stats_freezes_auto_refresh_while_open() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        global.S = {
          currentTask: {
            token_usage: {
              tracked: true,
              input_tokens: 10,
              output_tokens: 1,
              cache_hit_tokens: 0,
              call_count: 1,
              calls_with_usage: 1,
              calls_without_usage: 0,
              is_partial: false,
            },
          },
          taskSummary: { token_usage_by_model: [] },
          recentModelCalls: [
            {
              call_index: 1,
              node_id: "node:a",
              created_at: "2026-09-13T10:00:00+08:00",
              prepared_message_count: 1,
              prepared_message_chars: 10,
              response_tool_call_count: 0,
              delta_usage: { tracked: true, input_tokens: 10, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
              delta_usage_by_model: [{ model_key: "m" }],
            },
          ],
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
          taskModelCallsQuery: "preserved-query",
          taskTokenStatsOpen: true,
        };
        global.U = {
          taskTokenContent: { innerHTML: "<sentinel>" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStart = appCode.indexOf("const EMPTY_TOKEN_USAGE");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStatsStart = tasksCode.indexOf("function renderTasks");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        // 窗口打开时，实时事件触发的非强制渲染不得重建表格（保留哨兵内容）。
        renderTaskTokenStats();
        const frozenHtml = U.taskTokenContent.innerHTML;

        // 「刷新」按钮等强制渲染仍可更新，且保留搜索框内容。
        renderTaskTokenStats({ force: true });
        const forcedHtml = U.taskTokenContent.innerHTML;

        console.log(JSON.stringify({
          frozenHtml,
          forcedRendered: forcedHtml.includes("模型调用明细"),
          searchValuePreserved: forcedHtml.includes('value="preserved-query"'),
        }));
        """
    )

    assert result["frozenHtml"] == "<sentinel>"
    assert result["forcedRendered"] is True
    assert result["searchValuePreserved"] is True


def test_refresh_task_token_call_table_rerenders_only_table_region() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        const makeCall = (idx) => ({
          call_index: idx,
          node_id: `node:${idx % 2 === 0 ? "even" : "odd"}:${idx}`,
          created_at: new Date(Date.UTC(2026, 8, 14, 0, 0, idx)).toISOString(),
          prepared_message_count: 1,
          prepared_message_chars: 10,
          response_tool_call_count: 0,
          delta_usage: { tracked: true, input_tokens: 10, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
          delta_usage_by_model: [{ model_key: `model-${idx}` }],
        });
        global.S = {
          currentTask: {
            token_usage: {
              tracked: true,
              input_tokens: 50,
              output_tokens: 5,
              cache_hit_tokens: 0,
              call_count: 5,
              calls_with_usage: 5,
              calls_without_usage: 0,
              is_partial: false,
            },
          },
          taskSummary: { token_usage_by_model: [] },
          recentModelCalls: Array.from({ length: 5 }, (_, idx) => makeCall(idx + 1)),
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
          taskModelCallsQuery: "",
        };
        // 模拟真实容器：带 querySelector 的区域节点。
        const region = { innerHTML: "" };
        const shellMarker = '<div class="task-token-call-tools">search-box-stays</div>';
        global.U = {
          taskTokenContent: {
            innerHTML: "",
            querySelector: (selector) => (selector === "[data-task-model-call-region]" ? region : null),
          },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStart = appCode.indexOf("const EMPTY_TOKEN_USAGE");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStatsStart = tasksCode.indexOf("function renderTasks");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        renderTaskTokenStats();
        // 用哨兵标记容器级内容：表格区域增量刷新不得触碰它（搜索框/焦点不丢）。
        U.taskTokenContent.innerHTML = shellMarker;

        S.taskModelCallsQuery = "node:even";
        S.taskModelCallsPage = 1;
        refreshTaskTokenCallTable();

        const indexes = Array.from(region.innerHTML.matchAll(/data-task-call-index>([\\d,]+)<\\/td>/g))
          .map((match) => Number(String(match[1] || "").replaceAll(",", "")));

        console.log(JSON.stringify({
          containerUntouched: U.taskTokenContent.innerHTML === shellMarker,
          regionIndexes: indexes,
          regionSummary: region.innerHTML.includes("共 2 条"),
        }));
        """
    )

    assert result["containerUntouched"] is True
    # node:even 匹配 call 2、4；时间倒序 → 4 在前。
    assert result["regionIndexes"] == [4, 2]
    assert result["regionSummary"] is True


def test_task_token_ledger_refresh_refetches_and_keys_rows_by_node_and_index() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        // 三个节点各自把 call_index 计到 5：真实任务里序号是每节点独立的（实盘单任务
        // 8071 行只覆盖 259 个不同序号），它们必须作为不同行共存。
        const makeRow = (nodeId, idx, seconds, input) => ({
          call_index: idx,
          node_id: nodeId,
          created_at: new Date(Date.UTC(2026, 8, 14, 0, 0, seconds)).toISOString(),
          prepared_message_count: 1,
          prepared_message_chars: 10,
          response_tool_call_count: 0,
          duration_ms: 900,
          first_token_ms: 200,
          thinking_tokens: 3,
          delta_usage: { tracked: true, input_tokens: input, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
          delta_usage_by_model: [{ model_key: "model-a" }],
        });
        global.S = {
          currentTaskId: "task:test",
          currentTask: {
            token_usage: { tracked: true, input_tokens: 100, output_tokens: 10, cache_hit_tokens: 0, call_count: 2, calls_with_usage: 2, calls_without_usage: 0, is_partial: false },
          },
          taskSummary: { token_usage_by_model: [] },
          recentModelCalls: [makeRow("node:a", 5, 10, 100), makeRow("node:b", 5, 11, 200)],
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
          taskModelCallsQuery: "",
          taskTokenStatsOpen: true,
          taskTokenLedgerRefreshing: false,
        };
        const refreshButton = { disabled: false, textContent: "刷新" };
        global.U = {
          taskTokenContent: {
            innerHTML: "",
            querySelector: (selector) => (selector === "[data-task-model-call-refresh]" ? refreshButton : null),
          },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };
        const requests = [];
        const toasts = [];
        global.ApiClient = {
          getTaskTokenLedger: async (taskId) => {
            requests.push(taskId);
            return {
              token_usage: { tracked: true, input_tokens: 999, output_tokens: 20, cache_hit_tokens: 5, call_count: 3, calls_with_usage: 3, calls_without_usage: 0, is_partial: false },
              token_usage_by_model: [],
              model_calls: [makeRow("node:a", 5, 12, 300), makeRow("node:c", 5, 13, 400)],
            };
          },
        };
        global.showToast = (options) => toasts.push(options);

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(appCode.slice(appCode.indexOf("const EMPTY_TOKEN_USAGE"), appCode.indexOf("function ensureTaskTokenUi")));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(tasksCode.slice(tasksCode.indexOf("function renderTasks"), tasksCode.indexOf("async function loadTaskDetail")));

        (async () => {
          // 同序号不同节点共存，同节点同序号只被替换一次。
          const mergedSize = mergeTaskModelCallRows(S.recentModelCalls, [makeRow("node:b", 5, 99, 500)]).length;

          await refreshTaskTokenLedger();
          const afterSuccess = {
            requests: requests.slice(),
            rowKeys: S.recentModelCalls.map((row) => `${row.node_id}#${row.call_index}`),
            totalsInput: S.currentTask.token_usage.input_tokens,
            buttonLabel: refreshButton.textContent,
            buttonDisabled: refreshButton.disabled,
            successToast: toasts.some((item) => item.kind === "success"),
            summaryText: U.taskTokenSummaryText.textContent,
            toplineInput: (U.taskTokenContent.innerHTML.match(/<strong>([\\d,]+)<\\/strong><span>总输入/)||[])[1],
            stillFlagged: S.taskTokenLedgerRefreshing,
          };

          // 在飞期间切走任务：旧任务的取数结果不得应用到新任务上。
          let resolveFetch = null;
          global.ApiClient.getTaskTokenLedger = () => new Promise((resolve) => { resolveFetch = resolve; });
          const pending = refreshTaskTokenLedger();
          S.currentTaskId = "task:other";
          resolveFetch({
            token_usage: { tracked: true, input_tokens: 1234, output_tokens: 1, cache_hit_tokens: 0, call_count: 1, calls_with_usage: 1, calls_without_usage: 0, is_partial: false },
            token_usage_by_model: [],
            model_calls: [],
          });
          await pending;
          const staleTotals = S.currentTask.token_usage.input_tokens;
          const staleFlagged = S.taskTokenLedgerRefreshing;

          // 失败侧：中文 toast + 按钮回到可用态，不能停在「刷新中」。
          S.currentTaskId = "task:test";
          global.ApiClient.getTaskTokenLedger = async (taskId) => {
            requests.push(taskId);
            throw new Error("gateway down");
          };
          await refreshTaskTokenLedger();

          console.log(JSON.stringify({
            mergedSize,
            afterSuccess,
            staleTotals,
            staleFlagged,
            failure: {
              errorToast: toasts[toasts.length - 1]?.kind,
              errorText: toasts[toasts.length - 1]?.text,
              buttonLabel: refreshButton.textContent,
              buttonDisabled: refreshButton.disabled,
              stillFlagged: S.taskTokenLedgerRefreshing,
              requestCount: requests.length,
            },
          }));
        })();
        """
    )

    assert result["mergedSize"] == 2
    assert result["afterSuccess"]["requests"] == ["task:test"]
    # node:a#5 被服务端版本替换，node:b#5 没被同序号顶掉，node:c#5 新增。
    assert result["afterSuccess"]["rowKeys"] == ["node:b#5", "node:a#5", "node:c#5"]
    assert result["afterSuccess"]["totalsInput"] == 999
    # 副标题按 effective 口径 = 输入 999 + 缓存命中 5；不得把缓存命中再加第二次。
    assert result["afterSuccess"]["summaryText"] == "总输入 1,004 · 总输出 20 · 缓存命中 5"
    assert result["afterSuccess"]["buttonLabel"] == "刷新"
    assert result["afterSuccess"]["buttonDisabled"] is False
    assert result["afterSuccess"]["successToast"] is True
    assert result["afterSuccess"]["stillFlagged"] is False
    assert result["staleTotals"] == 999
    assert result["staleFlagged"] is False
    assert result["failure"]["errorToast"] == "error"
    assert result["failure"]["errorText"] == "gateway down"
    assert result["failure"]["buttonLabel"] == "刷新"
    assert result["failure"]["buttonDisabled"] is False
    assert result["failure"]["stillFlagged"] is False
    assert result["failure"]["requestCount"] == 2


def test_render_tasks_uses_effective_input_tokens_for_task_card_metric() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        class StubClassList {
          constructor(owner) {
            this.owner = owner;
            this.tokens = new Set();
          }
          add(...tokens) {
            tokens.forEach((token) => {
              const normalized = String(token || "").trim();
              if (normalized) this.tokens.add(normalized);
            });
            this.owner.className = [...this.tokens].join(" ");
          }
          remove(...tokens) {
            tokens.forEach((token) => this.tokens.delete(String(token || "").trim()));
            this.owner.className = [...this.tokens].join(" ");
          }
          contains(token) {
            return this.tokens.has(String(token || "").trim());
          }
          toggle(token, force) {
            const normalized = String(token || "").trim();
            const shouldAdd = force == null ? !this.tokens.has(normalized) : !!force;
            if (shouldAdd) this.tokens.add(normalized);
            else this.tokens.delete(normalized);
            this.owner.className = [...this.tokens].join(" ");
            return shouldAdd;
          }
        }

        class StubElement {
          constructor(tagName = "div") {
            this.tagName = String(tagName || "div").toUpperCase();
            this.children = [];
            this.dataset = {};
            this.style = {};
            this.hidden = false;
            this.disabled = false;
            this.className = "";
            this.classList = new StubClassList(this);
            this.attributes = {};
            this.innerHTML = "";
            this.textContent = "";
            this.parentNode = null;
            this.title = "";
          }
          appendChild(child) {
            if (child && typeof child === "object") child.parentNode = this;
            this.children.push(child);
            return child;
          }
          setAttribute(name, value) {
            this.attributes[String(name)] = String(value);
          }
          addEventListener() {}
          querySelector() { return null; }
          querySelectorAll() { return []; }
          closest() { return null; }
        }

        global.window = global;
        global.HTMLElement = StubElement;
        global.Element = StubElement;
        global.HTMLButtonElement = StubElement;
        global.HTMLInputElement = StubElement;
        global.HTMLSelectElement = StubElement;
        global.DocumentFragment = StubElement;
        global.document = {
          createElement(tagName) { return new StubElement(tagName); },
        };
        global.CSS = { escape: (value) => String(value || "") };
        global.pStatus = (value) => String(value || "").trim().toLowerCase();
        global.esc = (value) => String(value ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        global.S = {
          tasks: [
            {
              task_id: "task:demo",
              title: "Demo task",
              status: "in_progress",
              token_usage: {
                tracked: true,
                input_tokens: 120,
                output_tokens: 30,
                cache_hit_tokens: 40,
                call_count: 2,
                calls_with_usage: 2,
                calls_without_usage: 0,
                is_partial: false,
              },
            },
          ],
          selectedTaskIds: new Set(),
          multiSelectMode: false,
          taskBusy: false,
          taskPage: 1,
          taskPageSize: 20,
          taskGridSignature: "",
          taskMetricSnapshot: {},
          taskMetricAnimationTaskIds: new Set(),
          taskHallStats: {},
          tasksWorkerState: "online",
          tasksWorkerReportedState: "online",
          tasksWorkerLastSeenAt: "",
          tasksWorkerControlAvailable: true,
          tasksWorkerStatusPayload: null,
          tasksWorker: null,
          visibleTaskIds: [],
          pendingTaskCardPatchIds: new Set(),
          taskCardPatchQueuedAt: {},
          taskListDirtyWhileHidden: false,
        };
        global.U = {
          taskGrid: new StubElement("div"),
        };
        global.orderedTasks = (items) => Array.isArray(items) ? items : [];
        global.paginateResources = (items, currentPage, pageSize) => ({
          total: Array.isArray(items) ? items.length : 0,
          items: Array.isArray(items) ? items : [],
          currentPage: Number(currentPage || 1),
          pageSize: Number(pageSize || 20),
        });
        global.syncTaskPagination = () => {};
        global.renderTaskPerformanceBar = () => {};
        global.updateTaskToolbar = () => {};
        global.icons = () => {};
        global.copyTaskId = async () => {};
        global.openTask = async () => {};
        global.setTaskCardMenuOpen = () => {};
        global.runTaskAction = async () => {};

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const appStart = appCode.indexOf("const canPause");
        const appEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(appStart, appEnd));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(tasksCode);

        renderTasks();
        const card = U.taskGrid.children[0];
        console.log(JSON.stringify({
          html: card?.innerHTML || "",
          snapshot: S.taskMetricSnapshot["task:demo"] || null,
        }));
        """
    )

    assert 'data-task-metric-value="input_tokens">160<' in result["html"]
    assert result["snapshot"]["input_tokens"] == 160
    assert result["snapshot"]["cache_hit_tokens"] == 40


def test_render_task_token_stats_uses_effective_input_tokens_for_hit_rate() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.pStatus = (value) => String(value || "").trim().toLowerCase();
        global.esc = (value) => String(value ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        global.S = {
          currentTask: {
            token_usage: {
              tracked: true,
              input_tokens: 100,
              output_tokens: 12,
              cache_hit_tokens: 40,
              call_count: 1,
              calls_with_usage: 1,
              calls_without_usage: 0,
              is_partial: false,
            },
          },
          taskSummary: {
            token_usage_by_model: [],
          },
          recentModelCalls: [
            {
              call_index: 1,
              prepared_message_count: 3,
              prepared_message_chars: 200,
              response_tool_call_count: 0,
              delta_usage: {
                tracked: true,
                input_tokens: 100,
                output_tokens: 5,
                cache_hit_tokens: 40,
                call_count: 1,
                calls_with_usage: 1,
                calls_without_usage: 0,
                is_partial: false,
              },
              delta_usage_by_model: [{ model_key: "demo-model" }],
            },
          ],
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStart = appCode.indexOf("const canPause");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const tokenStatsStart = tasksCode.indexOf("function renderTasks");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        renderTaskTokenStats();
        console.log(JSON.stringify({
          html: U.taskTokenContent.innerHTML,
        }));
        """
    )

    assert "28.6%" in result["html"]


def test_format_node_detail_heading_prefixes_node_id_before_title() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          liveFrameMap: {},
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const sample = {
          node_id: "node:pressure:0001:root",
          title: "Analyze local path `D:\\\\NewProjects\\\\G3KU` flow",
        };

        console.log(JSON.stringify({
          heading: formatNodeDetailHeading(sample),
          tooltip: formatNodeDetailHeading(sample, { compact: false }),
          fallback: formatNodeDetailHeading({ node_id: "node:root" }),
        }));
        """
    )

    assert result["heading"] == "node:pressure:0001:root | Analyze local path `D:\\NewProjects\\G3KU` flow"
    assert result["tooltip"] == "node:pressure:0001:root | Analyze local path `D:\\NewProjects\\G3KU` flow"
    assert result["fallback"] == "node:root"


def test_build_node_execution_trace_uses_summary_execution_trace_when_full_trace_missing() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = { liveFrameMap: {} };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const trace = buildNodeExecutionTrace(
          { node_id: "node:test", goal: "inspect repository" },
          {
            prompt: "inspect repository",
            final_output: "done",
            execution_trace_summary: {
              turns: [
                {
                  seq: 1,
                  text: "inspect repository",
                  status: "completed",
                  tools: [
                    {
                      tool_name: "filesystem",
                      arguments_text: "{\\"path\\": \\".\\"}",
                      output_text: "repo listing",
                    },
                  ],
                },
              ],
            },
          },
        );

        console.log(JSON.stringify({
          turnCount: trace.turns.length,
          turnText: trace.turns[0]?.text || "",
          toolName: trace.turns[0]?.tools?.[0]?.tool_name || "",
          outputText: trace.turns[0]?.tools?.[0]?.output_text || "",
        }));
        """
    )

    assert result["turnCount"] == 1
    assert result["turnText"] == "inspect repository"
    assert result["toolName"] == "filesystem"
    assert result["outputText"] == "repo listing"







def test_build_node_execution_trace_prefers_detail_final_output_when_full_trace_output_is_blank() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          liveFrameMap: {},
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const trace = buildNodeExecutionTrace(
          {
            node_id: "node:test",
            final_output: "",
          },
          {
            final_output: "Externalized final-output:node:test ref=artifact:artifact:123",
            execution_trace: {
              final_output: "",
              stages: [],
            },
          },
        );

        console.log(JSON.stringify({
          finalOutput: trace.final_output,
        }));
        """
    )

    assert result["finalOutput"] == "Externalized final-output:node:test ref=artifact:artifact:123"


def test_build_node_execution_trace_falls_back_to_acceptance_final_output_when_check_result_missing() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          liveFrameMap: {},
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const trace = buildNodeExecutionTrace(
          {
            node_id: "node:acceptance",
            node_kind: "acceptance",
            final_output: "## 验收裁定：拒绝交付",
          },
          {
            node_kind: "acceptance",
            check_result: "",
            final_output: "## 验收裁定：拒绝交付",
          },
        );

        console.log(JSON.stringify({
          acceptanceResult: trace.acceptance_result,
          finalOutput: trace.final_output,
        }));
        """
    )

    assert result["acceptanceResult"] == "## 验收裁定：拒绝交付"
    assert result["finalOutput"] == "## 验收裁定：拒绝交付"


def test_build_node_execution_trace_falls_back_to_failure_reason_when_failed_without_final_output() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          liveFrameMap: {},
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const trace = buildNodeExecutionTrace(
          {
            node_id: "node:failed",
            status: "failed",
            final_output: "",
            failure_reason: "root failed hard",
          },
          {
            status: "failed",
            final_output: "",
            failure_reason: "root failed hard",
            execution_trace: {
              final_output: "",
              stages: [],
            },
          },
        );

        console.log(JSON.stringify({
          finalOutput: trace.final_output,
        }));
        """
    )

    assert result["finalOutput"] == "root failed hard"




_LOADER_CHIP_NODE_STUBS = """
        global.window = global;
        global.S = { liveFrameMap: {} };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        // 会话提醒车道那四个 helper 的真实形状；节点详情页复用它们，这里按同形 stub。
        global.ceoContextLoaderKind = (name) => {
          const normalized = String(name || "").trim().toLowerCase();
          if (normalized === "load_tool_context" || normalized === "load_tool_context_v2") return "tool";
          if (normalized === "load_skill_context" || normalized === "load_skill_context_v2") return "skill";
          return "";
        };
        global.extractCeoContextLoadTarget = (name, raw) => {
          const key = global.ceoContextLoaderKind(name) === "skill" ? "skill_id" : "tool_id";
          try {
            return String(JSON.parse(raw)?.[key] || "");
          } catch {
            return "";
          }
        };
        global.resolveCeoContextLoadNoticeRiskLevel = (kind, id) => {
          const table = kind === "skill"
            ? { "demo-skill": "low" }
            : { "filesystem_edit": "high" };
          return table[String(id)] || "medium";
        };
        global.ceoContextLoadNoticeIconName = (kind) => (kind === "skill" ? "sparkles" : "wrench");
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
"""


def test_node_detail_renders_context_load_chips_without_tool_names() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
"""
        + _LOADER_CHIP_NODE_STUBS
        + r"""
        const toolLoad = {
          tool_call_id: "call:load:tool",
          tool_name: "load_tool_context",
          arguments_text: '{"tool_id": "filesystem_edit"}',
          output_text: JSON.stringify({ ok: true, tool_id: "filesystem_edit", content: "TOOLS-BODY-MARKER" }),
          status: "success",
        };
        const skillLoad = {
          tool_call_id: "call:load:skill",
          tool_name: "load_skill_context",
          arguments_text: '{"skill_id": "demo-skill"}',
          output_text: JSON.stringify({ ok: true, skill_id: "demo-skill", content: "SKILL-BODY-MARKER" }),
          status: "success",
        };
        const ordinary = {
          tool_call_id: "call:exec",
          tool_name: "exec",
          arguments_text: '{"command": "ls"}',
          output_text: "listing",
          status: "success",
        };

        function turnHtmlOf(tools) {
          const trace = buildNodeExecutionTrace(
            { node_id: "node:test", goal: "inspect repository" },
            {
              execution_trace: {
                turns: [{ seq: 1, text: "加载上下文并执行", status: "completed", tools }],
              },
            },
            null,
          );
          return String(buildExecutionTraceSteps(trace, { state: "in_progress" })[1].bodyHtml || "");
        }

        const mixed = turnHtmlOf([toolLoad, skillLoad, ordinary]);
        const failed = turnHtmlOf([
          {
            tool_call_id: "call:load:fail",
            tool_name: "load_skill_context",
            arguments_text: '{"skill_id": "missing"}',
            output_text: "当前运行时技能未包含 missing",
            status: "error",
          },
        ]);

        const flatTrace = buildNodeExecutionTrace(
          { node_id: "node:flat", goal: "inspect repository" },
          { execution_trace: { tool_steps: [skillLoad] } },
          null,
        );
        const flatStep = buildExecutionTraceSteps(flatTrace, { state: "in_progress" })[1];

        console.log(JSON.stringify({
          showsToolLoadLabel: mixed.includes("加载 tool"),
          showsSkillLoadLabel: mixed.includes("加载 skill"),
          leaksLoaderName: mixed.includes("load_tool_context") || mixed.includes("load_skill_context"),
          highRiskWrench: /context-load-icon risk-high"[^>]*><i data-lucide="wrench"/.test(mixed),
          lowRiskSparkles: /context-load-icon risk-low"[^>]*><i data-lucide="sparkles"/.test(mixed),
          showsToolBody: mixed.includes("TOOLS-BODY-MARKER"),
          showsSkillBody: mixed.includes("SKILL-BODY-MARKER"),
          paramFieldCount: (mixed.match(/>参数</g) || []).length,
          keepsOrdinaryOutput: mixed.includes("listing"),
          keepsTargetTooltip: mixed.includes('title="filesystem_edit"'),
          failedKeepsName: failed.includes("load_skill_context"),
          failedKeepsErrorText: failed.includes("当前运行时技能未包含 missing"),
          failedSkipsContentField: !failed.includes("暂无内容"),
          flatTitle: flatStep.title,
          flatShowsBody: String(flatStep.bodyHtml || "").includes("SKILL-BODY-MARKER"),
          flatSkipsArgs: !String(flatStep.bodyHtml || "").includes("Arguments"),
        }));
        """
    )

    assert result["showsToolLoadLabel"] is True
    assert result["showsSkillLoadLabel"] is True
    assert result["leaksLoaderName"] is False
    assert result["highRiskWrench"] is True
    assert result["lowRiskSparkles"] is True
    assert result["showsToolBody"] is True
    assert result["showsSkillBody"] is True
    assert result["paramFieldCount"] == 1
    assert result["keepsOrdinaryOutput"] is True
    assert result["keepsTargetTooltip"] is True
    assert result["failedKeepsName"] is True
    assert result["failedKeepsErrorText"] is True
    assert result["failedSkipsContentField"] is True
    assert result["flatTitle"] == "加载 skill"
    assert result["flatShowsBody"] is True
    assert result["flatSkipsArgs"] is True







def test_summary_execution_trace_defaults_running_when_tool_lacks_completion_signal() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = { liveFrameMap: {} };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const trace = buildNodeExecutionTrace(
          { node_id: "node:test", goal: "inspect repository" },
          {
            execution_trace_summary: {
              turns: [
                {
                  seq: 1,
                  text: "launch child researchers",
                  tools: [
                    {
                      tool_name: "spawn_child_nodes",
                      arguments_text: "{\\"children\\": 3}",
                      output_text: "",
                      started_at: "2026-04-04T19:37:42+08:00",
                      finished_at: "",
                    },
                  ],
                },
              ],
            },
          },
        );

        console.log(JSON.stringify({
          toolStatus: trace.turns[0]?.tools?.[0]?.status || "",
        }));
        """
    )

    assert result["toolStatus"] == "running"



def test_render_trace_step_status_label_override_preserves_success_color() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = { liveFrameMap: {} };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const html = renderTraceStep({
          title: "round",
          status: "success",
          statusLabel: "完成",
          bodyHtml: "",
          open: false,
        });
        const firstClass = (html.match(/task-trace-step\\s+([^\"\\s]+)/) || [null, ""])[1];
        const firstLabel = (html.match(/interaction-step-status\">([^<]+)</) || [null, ""])[1];

        console.log(JSON.stringify({
          successLabel: traceStatusLabel("success"),
          firstClass,
          firstLabel,
        }));
        """
    )

    assert result["successLabel"] == "成功"
    assert result["firstClass"] == "success"
    assert result["firstLabel"] == "完成"












def test_execution_trace_round_status_supports_warning_and_interrupted() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = { liveFrameMap: {} };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        console.log(JSON.stringify({
          warningLabel: traceStatusLabel("warning"),
          interruptedLabel: traceStatusLabel("interrupted"),
          roundStatus: roundTraceStatus({
            tools: [
              { tool_name: "recovery_check", status: "warning" },
              { tool_name: "exec", status: "interrupted" },
            ],
          }),
        }));
        """
    )

    assert result["warningLabel"] == "需处理"
    assert result["interruptedLabel"] == "已中断"
    assert result["roundStatus"] == "warning"




def test_load_selected_node_latest_context_preserves_detail_and_context_scroll() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        const detail = { scrollTop: 184 };
        const artifactContent = {
          _text: "old context\\n".repeat(120),
          scrollTop: 92,
          get textContent() {
            return this._text;
          },
          set textContent(value) {
            this._text = String(value ?? "");
            this.scrollTop = 0;
            detail.scrollTop = 0;
          },
        };
        global.S = {
          currentTaskId: "task:test",
          selectedNodeId: "node:1",
          taskNodeLatestContexts: {},
          taskNodeLatestContextRequests: {},
        };
        global.U = {
          detail,
          artifactContent,
        };
        global.ApiClient = {
          getTaskNodeLatestContext: async () => ({
            content: "fresh context\\n".repeat(120),
          }),
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.readableText = (value, options = {}) => {
          const text = String(value ?? "");
          return text || String(options.emptyText || "");
        };
        global.captureTaskDetailViewState = () => ({
          detailScrollTop: detail.scrollTop,
          traceScrollTop: 0,
          artifactListScrollTop: 0,
          artifactContentScrollTop: artifactContent.scrollTop,
          traceItems: [],
        });
        global.restoreTaskDetailViewState = (state, options = {}) => {
          if (!state || typeof state !== "object") return;
          if (options.detail !== false) detail.scrollTop = Number(state.detailScrollTop || 0);
          if (options.artifactContent !== false) artifactContent.scrollTop = Number(state.artifactContentScrollTop || 0);
        };
        global.scheduleTaskDetailSessionPersist = () => {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        loadSelectedNodeLatestContext({ force: true }).then((payload) => {
          console.log(JSON.stringify({
            detailScrollTop: detail.scrollTop,
            artifactContentScrollTop: artifactContent.scrollTop,
            contentLoaded: artifactContent.textContent.startsWith("fresh context"),
            payloadLength: String(payload?.content || "").length,
          }));
        });
        """
    )

    assert result["detailScrollTop"] == 184
    assert result["artifactContentScrollTop"] == 92
    assert result["contentLoaded"] is True
    assert result["payloadLength"] > 0


def test_show_agent_does_not_auto_refresh_latest_context_when_disclosure_is_open() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        let latestContextLoads = 0;
        global.S = {
          currentTaskId: "task:test",
          selectedNodeId: "node:1",
          currentNodeDetail: {
            node_id: "node:1",
            execution_trace_summary: { stages: [] },
          },
          taskDetailRenderToken: 0,
          taskNodeDetails: {
            "node:1": {
              node_id: "node:1",
              updated_at: "2026-04-20T00:00:00Z",
              detail_level: "full",
              execution_trace_summary: { stages: [] },
            },
          },
          taskNodeDetailRequests: {},
          taskNodeLatestContexts: {},
          taskNodeLatestContextRequests: {},
        };
        global.U = {
          detail: { style: { display: "flex" } },
          nodeEmpty: { style: {} },
          adRole: { hidden: false },
          adTokenSummary: { textContent: "" },
          adStatus: { textContent: "", dataset: {} },
          adFlow: { innerHTML: "" },
          adMessages: { innerHTML: "" },
          adSpawnReviews: { innerHTML: "" },
          feedTitle: { textContent: "", title: "" },
          nodeContextDisclosure: { open: true },
          artifactContent: { textContent: "" },
        };
        global.ApiClient = {
          getTaskNodeDetail: async () => ({
            node_id: "node:1",
            updated_at: "2026-04-20T00:00:01Z",
            detail_level: "full",
            execution_trace_summary: { stages: [] },
          }),
          getTaskNodeLatestContext: async () => {
            latestContextLoads += 1;
            return { content: "fresh context" };
          },
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.setTaskSelectionEmptyVisible = () => {};
        global.renderFlowHeading = () => {};
        global.renderMessageHeading = () => {};
        global.renderSpawnReviewHeading = () => {};
        global.renderExecutionTrace = () => false;
        global.renderMessageList = () => false;
        global.renderSpawnReviewTrace = () => false;
        global.renderFinalOutput = () => false;
        global.renderAcceptanceResult = () => false;
        global.formatNodeDetailHeading = () => "Node Details";
        global.setTaskDetailOpen = () => {};
        global.icons = () => {};
        global.refreshTaskDetailScrollRegions = () => {};
        global.captureTaskDetailViewState = () => null;
        global.consumePendingTaskDetailRestore = () => null;
        global.getStoredTaskDetailViewState = () => null;
        global.restoreTaskDetailViewState = () => {};
        global.stashTaskDetailViewState = () => {};
        global.syncArtifactsForSelectedNode = () => {};
        global.liveFramesByNodeId = () => new Map();
        global.buildNodeExecutionTrace = () => ({ final_output: "", acceptance_result: "" });
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderExecutionTrace = () => false;
        global.renderMessageList = () => false;
        global.renderSpawnReviewTrace = () => false;
        global.renderFinalOutput = () => false;
        global.renderAcceptanceResult = () => false;
        global.formatNodeDetailHeading = () => "Node Details";
        global.setTaskDetailOpen = () => {};
        global.icons = () => {};
        global.restoreTaskDetailViewState = () => {};
        global.stashTaskDetailViewState = () => {};
        global.syncArtifactsForSelectedNode = () => {};
        // buildTokenUsageParts 住在 app bundle：这一只跑 task_view bundle 的夹具里
        // 只需要它不抛，头部文本不是本用例的断言对象。
        global.buildTokenUsageParts = () => [];

        showAgent(
          {
            node_id: "node:1",
            status: "in_progress",
            visual_state: "in_progress",
          },
          { forceRefresh: true }
        ).then(() => {
          console.log(JSON.stringify({
            latestContextLoads,
            disclosureOpen: Boolean(global.U.nodeContextDisclosure.open),
          }));
        });
        """
    )

    assert result["latestContextLoads"] == 0
    assert result["disclosureOpen"] is True


def test_render_artifacts_uses_status_icons_instead_of_plain_change_text() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.HTMLElement = function HTMLElement() {};
        global.window.requestAnimationFrame = (callback) => {
          callback();
          return 1;
        };
        const artifactList = {
          innerHTML: "",
          children: [],
          appendChild(node) {
            this.children.push(node);
          },
        };
        global.document = {
          createElement: () => ({
            className: "",
            dataset: {},
            innerHTML: "",
          }),
        };
        global.S = {
          selectedNodeId: "node:1",
          currentNodeDetail: {
            node_id: "node:1",
            tool_file_changes: [
              { path: "D:/tmp/created.txt", change_type: "created" },
              { path: "D:/tmp/updated.txt", change_type: "modified" },
              { path: "D:/tmp/deleted.txt", change_type: "deleted" },
            ],
          },
          taskNodeDetails: {},
        };
        global.U = {
          artifactList,
        };
        global.esc = (value) => String(value ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        global.renderArtifactHeading = () => {};
        global.refreshTaskDetailScrollRegions = () => {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        renderArtifacts();

        const createdHtml = artifactList.children[0]?.innerHTML || "";
        const modifiedHtml = artifactList.children[1]?.innerHTML || "";
        const deletedHtml = artifactList.children[2]?.innerHTML || "";
        console.log(JSON.stringify({
          count: artifactList.children.length,
          createdType: artifactList.children[0]?.dataset?.changeType || "",
          modifiedType: artifactList.children[1]?.dataset?.changeType || "",
          deletedType: artifactList.children[2]?.dataset?.changeType || "",
          createdHasPathClass: createdHtml.includes("artifact-item-path"),
          createdHasIconClass: createdHtml.includes("artifact-item-state artifact-item-state--created"),
          modifiedHasIconClass: modifiedHtml.includes("artifact-item-state artifact-item-state--modified"),
          deletedHasIconClass: deletedHtml.includes("artifact-item-state artifact-item-state--deleted"),
          createdHasSvg: createdHtml.includes("<svg"),
          modifiedHasSvg: modifiedHtml.includes("<svg"),
          deletedHasSvg: deletedHtml.includes("<svg"),
          createdPathBeforeIcon: createdHtml.indexOf("artifact-item-path") < createdHtml.indexOf("artifact-item-state"),
          noPlainCreatedText: !createdHtml.includes(">created<"),
          noPlainModifiedText: !modifiedHtml.includes(">modified<"),
          noPlainDeletedText: !deletedHtml.includes(">deleted<"),
        }));
        """
    )

    assert result["count"] == 3
    assert result["createdType"] == "created"
    assert result["modifiedType"] == "modified"
    assert result["deletedType"] == "deleted"
    assert result["createdHasPathClass"] is True
    assert result["createdHasIconClass"] is True
    assert result["modifiedHasIconClass"] is True
    assert result["deletedHasIconClass"] is True
    assert result["createdHasSvg"] is True
    assert result["modifiedHasSvg"] is True
    assert result["deletedHasSvg"] is True
    assert result["createdPathBeforeIcon"] is True
    assert result["noPlainCreatedText"] is True
    assert result["noPlainModifiedText"] is True
    assert result["noPlainDeletedText"] is True


def test_ensure_task_node_detail_refetches_stale_flattened_summary_cache() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        let fetchCount = 0;
        const staleDetail = {
          node_id: "node:1",
          execution_trace_summary: {
            stages: [
              {
                stage_goal: "stale flattened stage",
                tool_calls: [
                  { tool_name: "filesystem", arguments_text: "{}", output_text: "old" },
                ],
              },
            ],
          },
        };
        const freshDetail = {
          node_id: "node:1",
          execution_trace_summary: {
            stages: [
              {
                stage_goal: "fresh rounded stage",
                rounds: [
                  {
                    round_id: "round:1",
                    round_index: 1,
                    tools: [
                      { tool_name: "filesystem", arguments_text: "{}", output_text: "new" },
                    ],
                  },
                ],
                tool_calls: [
                  { tool_name: "filesystem", arguments_text: "{}", output_text: "new" },
                ],
              },
            ],
          },
        };
        global.S = {
          currentTaskId: "task:test",
          taskNodeDetails: { "node:1": staleDetail },
          taskNodeDetailRequests: {},
          currentNodeDetail: staleDetail,
        };
        global.U = {};
        global.ApiClient = {
          getTaskNodeDetail: async () => {
            fetchCount += 1;
            return freshDetail;
          },
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        ensureTaskNodeDetail("node:1").then((detail) => {
          console.log(JSON.stringify({
            fetchCount,
            roundCount: detail?.execution_trace_summary?.stages?.[0]?.rounds?.length || 0,
            cachedStageGoal: S.taskNodeDetails["node:1"]?.execution_trace_summary?.stages?.[0]?.stage_goal || "",
          }));
        });
        """
    )

    assert result["fetchCount"] == 1
    assert result["roundCount"] == 1
    assert result["cachedStageGoal"] == "fresh rounded stage"


def test_api_client_get_task_node_detail_requests_full_payload_with_distinct_cache_key() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.location = { origin: "http://localhost" };
        global.fetch = () => {
          throw new Error("fetch should not be called in this test");
        };
        const code = ["g3ku/web/frontend/client_api/core.js", "g3ku/web/frontend/client_api/models.js", "g3ku/web/frontend/client_api/tasks.js", "g3ku/web/frontend/api_client.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        let captured = null;
        ApiClient._request = async (method, path, options = {}) => {
          captured = {
            method,
            path,
            params: options.params || {},
            requestKey: options.requestKey || "",
          };
          return {
            item: {
              node_id: "node:1",
              detail_level: String(options?.params?.detail_level || "summary"),
            },
          };
        };

        ApiClient.getTaskNodeDetail("task:test", "node:1", { detailLevel: "full" }).then((item) => {
          console.log(JSON.stringify({
            detailLevel: item?.detail_level || "",
            params: captured?.params || {},
            requestKey: captured?.requestKey || "",
          }));
        });
        """
    )

    assert result["detailLevel"] == "full"
    assert result["params"]["detail_level"] == "full"
    assert result["requestKey"] == "tasks:node:task:test:node:1:full"


def test_ensure_task_node_detail_upgrades_summary_cache_to_full_detail() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        let fetchCount = 0;
        let requestedDetailLevel = "";
        const cachedDetail = {
          node_id: "node:1",
          detail_level: "summary",
          final_output: "summary output only",
          check_result: "summary acceptance only",
          execution_trace_summary: {
            stages: [],
          },
        };
        const fullDetail = {
          node_id: "node:1",
          detail_level: "full",
          final_output: "full deliverable\\nline 2",
          check_result: "full acceptance\\nline 2",
          execution_trace: {
            final_output: "full deliverable\\nline 2",
            acceptance_result: "full acceptance\\nline 2",
            stages: [],
          },
        };
        global.S = {
          currentTaskId: "task:test",
          taskNodeDetails: { "node:1": cachedDetail },
          taskNodeDetailRequests: {},
          currentNodeDetail: cachedDetail,
        };
        global.U = {};
        global.ApiClient = {
          getTaskNodeDetail: async (_taskId, _nodeId, options = {}) => {
            fetchCount += 1;
            requestedDetailLevel = String(options?.detailLevel || "");
            return fullDetail;
          },
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        ensureTaskNodeDetail("node:1").then((detail) => {
          console.log(JSON.stringify({
            fetchCount,
            requestedDetailLevel,
            detailLevel: detail?.detail_level || "",
            finalOutput: detail?.final_output || "",
            cachedDetailLevel: S.taskNodeDetails["node:1"]?.detail_level || "",
          }));
        });
        """
    )

    assert result["fetchCount"] == 1
    assert result["requestedDetailLevel"] == "full"
    assert result["detailLevel"] == "full"
    assert result["finalOutput"] == "full deliverable\nline 2"
    assert result["cachedDetailLevel"] == "full"


def test_ensure_task_node_detail_refreshes_terminal_cache_when_patch_summary_is_newer() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        let fetchCount = 0;
        const cachedDetail = {
          node_id: "node:1",
          detail_level: "full",
          status: "failed",
          final_output: "",
          failure_reason: "",
          check_result: "",
          updated_at: "2026-04-12T10:00:00Z",
          execution_trace_summary: {
            stages: [],
          },
        };
        const fullDetail = {
          node_id: "node:1",
          detail_level: "full",
          status: "failed",
          final_output: "",
          failure_reason: "root failed",
          check_result: "",
          updated_at: "2026-04-12T10:05:00Z",
          execution_trace: {
            final_output: "",
            stages: [],
          },
        };
        global.S = {
          currentTaskId: "task:test",
          taskNodeDetails: { "node:1": cachedDetail },
          taskNodeDetailRequests: {},
          taskNodePatchSummaries: {
            "node:1": {
              node_id: "node:1",
              status: "failed",
              final_output: "",
              failure_reason: "root failed",
              check_result: "",
              updated_at: "2026-04-12T10:05:00Z",
            },
          },
          currentNodeDetail: cachedDetail,
        };
        global.U = {};
        global.ApiClient = {
          getTaskNodeDetail: async () => {
            fetchCount += 1;
            return fullDetail;
          },
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        ensureTaskNodeDetail("node:1").then((detail) => {
          console.log(JSON.stringify({
            fetchCount,
            failureReason: detail?.failure_reason || "",
            cachedFailureReason: S.taskNodeDetails["node:1"]?.failure_reason || "",
          }));
        });
        """
    )

    assert result["fetchCount"] == 1
    assert result["failureReason"] == "root failed"
    assert result["cachedFailureReason"] == "root failed"


def test_handle_task_terminal_refreshes_selected_node_detail() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        const showAgentCalls = [];
        global.S = {
          currentTaskId: "task:test",
          currentTask: { task_id: "task:test", status: "in_progress" },
          taskSummary: { task_id: "task:test", status: "in_progress" },
          selectedNodeId: "node:1",
          rootNode: { node_id: "node:1" },
          treeView: { node_id: "node:1", children: [] },
          taskNodeDetails: {},
          liveFrameMap: {},
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTaskDetailHeader = () => {};
        global.renderTaskTokenStats = () => {};
        global.patchTaskListItem = () => {};
        global.removeTaskListItem = () => {};
        global.renderTaskGovernancePanel = () => {};
        global.mergeTaskGovernance = (next) => next;
        global.indexTaskLiveFrames = (frames) => frames || {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const taskViewCode = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(taskViewCode);
        global.captureTaskDetailViewState = () => ({ scrollTop: 12 });
        global.stashTaskDetailViewState = () => {};
        global.findTreeNode = () => ({ node_id: "node:1", title: "Node 1", state: "failed" });
        global.showAgent = (node, options) => {
          showAgentCalls.push({ nodeId: String(node?.node_id || ""), forceRefresh: !!options?.forceRefresh });
          return Promise.resolve();
        };
        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(tasksCode);

        handleTaskEvent({
          type: "task.terminal",
          data: {
            task: {
              task_id: "task:test",
              status: "failed",
            },
          },
        });

        Promise.resolve().then(() => {
          console.log(JSON.stringify({
            callCount: showAgentCalls.length,
            nodeId: showAgentCalls[0]?.nodeId || "",
            forceRefresh: !!showAgentCalls[0]?.forceRefresh,
          }));
        });
        """
    )

    assert result["callCount"] == 1
    assert result["nodeId"] == "node:1"
    assert result["forceRefresh"] is True


def test_build_node_message_list_steps_keeps_only_time_status_and_preview() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        global.treeNormalizeInt = global.normalizeInt;
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        global.formatCompactTime = (value) => String(value || "");
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const steps = buildNodeMessageListSteps({
          message_list: [
            {
              notification_id: "notif:1",
              source_node_id: "node:parent-1",
              message: "改成男性角色Top20",
              received_at: "2026-04-19T20:28:17+08:00",
              status: "pending",
              deliveries: [
                {
                  target_node_id: "node:child-1",
                  target_title: "child one",
                  message: "改成男性角色Top20并补充证据",
                  status: "delivered",
                  decision: "distributed",
                },
              ],
            },
          ],
        });

        console.log(JSON.stringify({
          traceKeys: steps.map((item) => item.traceKey),
          titles: steps.map((item) => item.title),
          leadHtml: steps[0]?.leadHtml || "",
          status: steps[0]?.status || "",
          bodyHtml: steps[0]?.bodyHtml || "",
        }));
        """
    )

    assert result["traceKeys"] == ["message:notif:1"]
    # 折叠态：状态图标在最左，然后是时间、来自节点药丸、一行内容预览，不用 · 分隔。
    assert result["titles"][0] == "改成男性角色Top20"
    assert "task-message-status" in result["leadHtml"]
    assert 'data-status="pending"' in result["leadHtml"]
    assert "待处理" in result["leadHtml"]
    assert "2026-04-19T20:28:17+08:00" in result["leadHtml"]
    assert "来自" not in result["leadHtml"]
    assert 'data-node-jump="node:parent-1"' in result["leadHtml"]
    # 药丸内先关系文字（预置占位「节点」），再去 node: 前缀的 id。
    assert '<span class="task-node-ref-type">节点</span>' in result["leadHtml"]
    assert '<span class="task-node-ref-id">parent-1</span>' in result["leadHtml"]
    assert ">node:parent-1<" not in result["leadHtml"]
    assert " · " not in result["leadHtml"]
    assert result["status"] == "warning"
    # 展开态只留消息正文：状态/接收时间/消费时间/并入时间/分发情况全部下掉。
    assert result["bodyHtml"].strip() == '<div class="task-trace-message-body">改成男性角色Top20</div>'
    for removed in ["child one", "未下发", "分发情况", "接收时间", "消费时间", "已并入上下文", "task-trace-label"]:
        assert removed not in result["bodyHtml"]


def test_build_node_sent_message_list_steps_shows_target_and_message_only() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        global.treeNormalizeInt = global.normalizeInt;
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        global.formatCompactTime = (value) => String(value || "");
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const steps = buildNodeSentMessageListSteps({
          sent_message_list: [
            {
              notification_id: "notif:sent-1",
              target_node_id: "node:child-1",
              target_title: "child one",
              message: "给子节点的要求",
              received_at: "2026-04-19T20:30:00+08:00",
              consumed_at: "2026-04-19T20:31:00+08:00",
              merged_at: "2026-04-19T20:31:00+08:00",
              status: "merged",
            },
          ],
        });

        console.log(JSON.stringify({
          traceKeys: steps.map((item) => item.traceKey),
          title: steps[0]?.title || "",
          leadHtml: steps[0]?.leadHtml || "",
          bodyHtml: steps[0]?.bodyHtml || "",
        }));
        """
    )

    assert result["traceKeys"] == ["sent-message:notif:sent-1"]
    # merged 与 consumed 同显示「已消费」；状态图标在最左，随后时间、发送至节点药丸、预览。
    assert result["title"] == "给子节点的要求"
    assert "已消费" in result["leadHtml"]
    assert "已并入上下文" not in result["leadHtml"]
    assert 'data-status="merged"' in result["leadHtml"]
    assert 'data-lucide="circle-check"' in result["leadHtml"]
    assert "2026-04-19T20:30:00+08:00" in result["leadHtml"]
    assert "发送至" not in result["leadHtml"]
    assert 'data-node-jump="node:child-1"' in result["leadHtml"]
    assert '<span class="task-node-ref-type">节点</span>' in result["leadHtml"]
    assert '<span class="task-node-ref-id">child-1</span>' in result["leadHtml"]
    assert ">node:child-1<" not in result["leadHtml"]
    # 节点标题不再出现在消息行里，只用 id 药丸指路。
    assert "child one" not in result["leadHtml"]
    assert " · " not in result["leadHtml"]
    assert result["bodyHtml"].strip() == '<div class="task-trace-message-body">给子节点的要求</div>'




def test_build_spawn_review_trace_steps_lists_nodes_with_mode_and_details() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        global.formatCompactTime = (value) => String(value || "");
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const steps = buildSpawnReviewTraceSteps([
          {
            round_id: "call:spawn-1",
            reviewed_at: "2026-04-06T12:00:00+08:00",
            requested_specs: [
              {
                index: 0,
                goal: "blocked branch",
                prompt: "blocked prompt",
                execution_policy: { mode: "focus" },
                requires_acceptance: false,
                runtime_nodes: [{ node_kind: "execution" }],
              },
              {
                index: 1,
                goal: "coverage branch",
                prompt: "coverage prompt",
                execution_policy: { mode: "coverage" },
                requires_acceptance: true,
                runtime_nodes: [
                  { node_kind: "execution" },
                  { node_kind: "acceptance", goal: "accept:coverage branch", acceptance_prompt: "独立核磁盘" },
                ],
              },
              {
                index: 2,
                goal: "focus branch",
                prompt: "focus prompt",
                execution_policy: { mode: "focus" },
                requires_acceptance: false,
              },
            ],
            allowed_indexes: [1, 2],
            blocked_specs: [
              {
                index: 0,
                reason: "拆分过细，偏离当前父节点目标",
                suggestion: "请由父节点直接执行，或收缩为更聚焦的单一派生",
              },
            ],
            entries: [
              {
                index: 0,
                goal: "blocked branch",
                review_decision: "blocked",
                blocked_reason: "拆分过细，偏离当前父节点目标",
                blocked_suggestion: "请由父节点直接执行，或收缩为更聚焦的单一派生",
              },
              {
                index: 1,
                goal: "coverage branch",
                review_decision: "allowed",
                child_node_id: "node:child-1",
              },
              {
                index: 2,
                goal: "focus branch",
                review_decision: "allowed",
                child_node_id: "node:child-2",
              },
            ],
          },
        ], [
          { child_node_id: "node:child-1", check_result: "验收通过：四个角色全部接好" },
        ]);
        const html = renderTraceStep({
          ...steps[0],
          open: false,
        });
        console.log(JSON.stringify({
          count: steps.length,
          title: steps[0]?.title || "",
          body: steps[0]?.bodyHtml || "",
          showStatus: steps[0]?.showStatus ?? null,
          hasStatusBadge: html.includes("interaction-step-status"),
        }));
        """
    )

    assert result["count"] == 1
    assert "派生记录" in result["title"]
    assert "2 个节点" in result["title"]
    # 轮次体内只列派生出来的节点；被拦截候选不占节点列表。
    assert "coverage branch" in result["body"]
    assert "focus branch" in result["body"]
    assert "blocked branch" not in result["body"]
    assert "拆分过细" not in result["body"]
    # 节点用图标标记专注 / 全面。
    assert "全面" in result["body"]
    assert "专注" in result["body"]
    assert 'data-lucide="layers"' in result["body"]
    assert 'data-lucide="target"' in result["body"]
    # 节点 id 药丸就是跳转入口（去 node: 前缀），不再有单独的跳转按钮。
    assert 'data-node-jump="node:child-1"' in result["body"]
    assert 'data-node-jump="node:child-2"' in result["body"]
    assert '<span class="task-node-ref-type">节点</span>' in result["body"]
    assert '<span class="task-node-ref-id">child-1</span>' in result["body"]
    assert '<span class="task-node-ref-id">child-2</span>' in result["body"]
    assert ">node:child-1<" not in result["body"]
    assert "跳转到节点详情" not in result["body"]
    assert result["body"].count('class="task-node-ref"') == 2
    # 点开节点后给提示词与验收结果。
    assert "coverage prompt" in result["body"]
    assert "focus prompt" in result["body"]
    assert "验收通过：四个角色全部接好" in result["body"]
    assert "暂无验收结果" in result["body"]
    assert result["body"].count('class="spawn-node"') == 2
    assert 'class="task-trace-label"' not in result["body"]
    assert result["showStatus"] is False
    assert result["hasStatusBadge"] is False


def test_build_execution_tree_from_snapshot_keeps_undispatched_acceptance_visible_as_waiting() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          treeRootNodeId: "node:root",
          treeSelectedRoundByNodeId: {},
          treeNodesById: {
            "node:root": {
              node_id: "node:root",
              node_kind: "execution",
              status: "in_progress",
              title: "root",
              rounds: [{ round_id: "round-1", is_latest: true, child_ids: ["node:child"] }],
              auxiliary_child_ids: ["node:acceptance"],
              all_child_ids: ["node:child", "node:acceptance"],
              parent_visible: true,
            },
            "node:child": {
              node_id: "node:child",
              parent_node_id: "node:root",
              node_kind: "execution",
              status: "in_progress",
              title: "child",
              rounds: [],
              auxiliary_child_ids: [],
              parent_visible: false,
              acceptance_handshake_state: "waiting_acceptance",
            },
            "node:acceptance": {
              node_id: "node:acceptance",
              parent_node_id: "node:child",
              node_kind: "acceptance",
              status: "in_progress",
              title: "acceptance",
              rounds: [],
              auxiliary_child_ids: [],
              parent_visible: true,
            },
          },
          liveFrameMap: {},
          taskRuntimeSummary: null,
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        const tree = buildExecutionTreeFromSnapshot("node:root");
        const child = (tree?.children || []).find((item) => item.node_id === "node:child");
        console.log(JSON.stringify({
          childIds: Array.isArray(tree?.children) ? tree.children.map((item) => item.node_id) : [],
          inspectionIds: Array.isArray(tree?.inspectionNodes) ? tree.inspectionNodes.map((item) => item.node_id) : [],
          states: [
            child?.display_state,
            (tree?.inspectionNodes || [])[0]?.display_state,
            child?.visual_state,
            (tree?.inspectionNodes || [])[0]?.visual_state,
          ],
        }));
        """
    )

    assert result["childIds"] == ["node:child"]
    # 检验节点不再隐藏：执行节点只要挂着检验节点就一直显示。
    assert result["inspectionIds"] == ["node:acceptance"]
    # 未派发的检验节点与刚提交、正等检验的执行节点都是「等待中」，不共用「检验中」。
    assert result["states"] == ["等待中", "等待中", "waiting", "waiting"]


def test_build_execution_tree_from_snapshot_keeps_activated_acceptance_visible_after_rejection() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          treeRootNodeId: "node:root",
          treeSelectedRoundByNodeId: {},
          treeNodesById: {
            "node:root": {
              node_id: "node:root",
              node_kind: "execution",
              status: "in_progress",
              title: "root",
              rounds: [{ round_id: "round-1", is_latest: true, child_ids: ["node:child"] }],
              auxiliary_child_ids: [],
              all_child_ids: ["node:child"],
              parent_visible: true,
            },
            "node:child": {
              node_id: "node:child",
              parent_node_id: "node:root",
              node_kind: "execution",
              status: "in_progress",
              title: "child",
              rounds: [],
              auxiliary_child_ids: ["node:acceptance"],
              all_child_ids: ["node:acceptance"],
              parent_visible: false,
              acceptance_handshake_state: "waiting_execution_retry",
            },
            "node:acceptance": {
              node_id: "node:acceptance",
              parent_node_id: "node:child",
              node_kind: "acceptance",
              status: "in_progress",
              title: "acceptance",
              rounds: [],
              auxiliary_child_ids: [],
              parent_visible: false,
            },
          },
          liveFrameMap: {},
          taskRuntimeSummary: null,
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        const tree = buildExecutionTreeFromSnapshot("node:root");
        const child = Array.isArray(tree?.children) ? tree.children.find((item) => item.node_id === "node:child") : null;
        console.log(JSON.stringify({
          childIds: Array.isArray(tree?.children) ? tree.children.map((item) => item.node_id) : [],
          inspectionIds: Array.isArray(child?.inspectionNodes) ? child.inspectionNodes.map((item) => item.node_id) : [],
          inspectionStates: Array.isArray(child?.inspectionNodes) ? child.inspectionNodes.map((item) => item.display_state) : [],
        }));
        """
    )

    assert result["childIds"] == ["node:child"]
    assert result["inspectionIds"] == ["node:acceptance"]
    # 打回执行节点后，检验节点在等待下一次派发：等待中，不是检验中。
    assert result["inspectionStates"] == ["等待中"]


def test_build_execution_tree_from_snapshot_labels_nodes_by_live_turn_activity() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        const node = (id, extra) => ({
          node_id: id,
          parent_node_id: "node:root",
          node_kind: "execution",
          status: "in_progress",
          title: id,
          rounds: [],
          auxiliary_child_ids: [],
          parent_visible: true,
          ...extra,
        });
        global.S = {
          treeRootNodeId: "node:root",
          treeSelectedRoundByNodeId: {},
          treeNodesById: {
            "node:root": node("node:root", {
              parent_node_id: null,
              rounds: [{ round_id: "r1", is_latest: true, child_ids: ["node:submitted", "node:busy", "node:idle", "node:spawn", "node:stalled", "node:requesting", "node:postprocess"] }],
              all_child_ids: ["node:submitted", "node:busy", "node:idle", "node:spawn", "node:stalled", "node:requesting", "node:postprocess"],
            }),
            "node:submitted": node("node:submitted", {
              status: "success",
              auxiliary_child_ids: ["node:acc-running", "node:acc-queued", "node:acc-requesting"],
              all_child_ids: ["node:acc-running", "node:acc-queued", "node:acc-requesting"],
            }),
            "node:acc-running": node("node:acc-running", {
              parent_node_id: "node:submitted",
              node_kind: "acceptance",
            }),
            "node:acc-requesting": node("node:acc-requesting", {
              parent_node_id: "node:submitted",
              node_kind: "acceptance",
            }),
            "node:acc-queued": node("node:acc-queued", {
              parent_node_id: "node:submitted",
              node_kind: "acceptance",
            }),
            "node:busy": node("node:busy"),
            "node:idle": node("node:idle"),
            "node:spawn": node("node:spawn"),
            "node:stalled": node("node:stalled"),
            "node:requesting": node("node:requesting"),
            "node:postprocess": node("node:postprocess"),
          },
          liveFrameMap: {
            "node:root": { node_id: "node:root", phase: "waiting_children", tool_calls: [], child_pipelines: [{ index: 1, status: "running" }] },
            "node:acc-running": { node_id: "node:acc-running", phase: "before_model", tool_calls: [], child_pipelines: [] },
            "node:acc-requesting": { node_id: "node:acc-requesting", phase: "before_model", await_marker: "model.chat.await_response", tool_calls: [], child_pipelines: [] },
            "node:busy": { node_id: "node:busy", phase: "after_model", tool_calls: [{ tool_call_id: "t1", tool_name: "exec", status: "running" }], child_pipelines: [] },
            "node:spawn": {
              node_id: "node:spawn",
              phase: "waiting_tool_results",
              tool_calls: [{ tool_call_id: "t2", tool_name: "spawn_child_nodes", status: "running" }],
              child_pipelines: [{ index: 0, status: "queued" }],
            },
            "node:stalled": {
              node_id: "node:stalled",
              phase: "before_model",
              stale: true,
              await_marker: "model.chat.await_response",
              tool_calls: [],
              child_pipelines: [],
            },
            "node:requesting": {
              node_id: "node:requesting",
              phase: "before_model",
              await_marker: "model.chat.await_response",
              tool_calls: [],
              child_pipelines: [],
            },
            "node:postprocess": {
              node_id: "node:postprocess",
              phase: "before_model",
              await_marker: "model.chat.response_postprocess",
              tool_calls: [],
              child_pipelines: [],
            },
          },
          taskRuntimeSummary: null,
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        const tree = buildExecutionTreeFromSnapshot("node:root");
        const byId = new Map();
        const walk = (item) => {
          if (!item) return;
          byId.set(item.node_id, [item.display_state, item.visual_state]);
          (item.inspectionNodes || []).forEach(walk);
          (item.children || []).forEach(walk);
        };
        walk(tree);
        console.log(JSON.stringify(Object.fromEntries(byId)));
        """
    )

    assert result == {
        # 派生工具还没返回：父节点是等待中，不是运行中。
        "node:root": ["等待中", "waiting"],
        # 已提交交付、检验还没结论：等待中。
        "node:submitted": ["等待中", "waiting"],
        "node:busy": ["运行中", "running"],
        "node:idle": ["等待中", "waiting"],
        # 派生工具在飞、子节点还没返回：等子节点的结果，不是运行中。
        "node:spawn": ["等待中", "waiting"],
        # 后端标记的陈旧帧不再证明有人在跑：带着在途 await_marker 也一起退到等待中。
        "node:stalled": ["等待中", "waiting"],
        # 模型请求在途：文字换成请求中，底色沿用执行蓝（visual_state 仍是 running）。
        "node:requesting": ["请求中", "running"],
        # response_postprocess 时流已经收尾，不算还在等 API。
        "node:postprocess": ["运行中", "running"],
        "node:acc-running": ["检验中", "inspecting"],
        # 检验节点也在等 API 回完：请求中盖过检验中，角色仍在标题前缀里。
        "node:acc-requesting": ["请求中", "running"],
        "node:acc-queued": ["等待中", "waiting"],
    }


def test_tree_round_select_id_is_stable_and_selector_safe() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        console.log(JSON.stringify({
          first: treeRoundSelectId("node:1a2b/c d"),
          again: treeRoundSelectId("node:1a2b/c d"),
          other: treeRoundSelectId("node:other"),
          empty: treeRoundSelectId("  "),
        }));
        """
    )

    # 重绘后要按这个 id 找回展开中的下拉框：同一节点必须得到同一个值，且能安全
    # 放进 [data-select-id="..."] 选择器里。
    assert result["first"] == result["again"]
    assert result["first"] != result["other"]
    assert result["empty"] == ""
    assert result["first"] == "tree-round-node-1a2b-c-d"


def test_sync_selected_task_node_detail_status_follows_live_frames() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          treeRootNodeId: "node:root",
          treeSelectedRoundByNodeId: {},
          currentTask: { task_id: "task:test", status: "in_progress" },
          selectedNodeId: "node:acc",
          currentNodeDetail: { node_id: "node:acc", status: "in_progress" },
          treeNodesById: {
            "node:root": {
              node_id: "node:root",
              node_kind: "execution",
              status: "in_progress",
              title: "root",
              rounds: [{ round_id: "r1", is_latest: true, child_ids: ["node:acc"] }],
              auxiliary_child_ids: [],
              all_child_ids: ["node:acc"],
              parent_visible: true,
            },
            "node:acc": {
              node_id: "node:acc",
              parent_node_id: "node:root",
              node_kind: "acceptance",
              status: "in_progress",
              title: "acc",
              rounds: [],
              auxiliary_child_ids: [],
              parent_visible: true,
            },
          },
          liveFrameMap: { "node:acc": { node_id: "node:acc", phase: "before_model", tool_calls: [], child_pipelines: [] } },
          taskRuntimeSummary: null,
        };
        global.U = { adStatus: { textContent: "", dataset: {} } };
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        const snapshot = () => [U.adStatus.textContent, U.adStatus.dataset.status];
        syncSelectedTaskNodeDetailStatus(buildExecutionTreeFromSnapshot());
        const inspecting = snapshot();
        S.liveFrameMap = { "node:acc": { node_id: "node:acc", phase: "before_model", stale: true, tool_calls: [], child_pipelines: [] } };
        syncSelectedTaskNodeDetailStatus(buildExecutionTreeFromSnapshot());
        const stalled = snapshot();
        S.selectedNodeId = "node:other";
        syncSelectedTaskNodeDetailStatus(buildExecutionTreeFromSnapshot());
        console.log(JSON.stringify({ inspecting, stalled, untouched: snapshot() }));
        """
    )

    assert result["inspecting"] == ["检验中", "inspecting"]
    # 帧被后端判为陈旧后，抽屉那行与树徽标一起退到等待中，不需要重开抽屉。
    assert result["stalled"] == ["等待中", "waiting"]
    assert result["untouched"] == result["stalled"]






def test_execution_trace_round_strip_uses_horizontal_scroller() -> None:
    css_text = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")
    match = re.search(
        r"\.task-trace-round-strip\s*\{(?P<body>[^}]+)\}",
        css_text,
        flags=re.MULTILINE,
    )

    assert match is not None
    block = match.group("body")
    assert "display: flex;" in block
    assert "flex-wrap: nowrap;" in block
    assert "overflow-x: auto;" in block


def test_execution_trace_round_panel_is_full_width_block() -> None:
    css_text = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")
    match = re.search(
        r"\.task-trace-round-panel\s*\{(?P<body>[^}]+)\}",
        css_text,
        flags=re.MULTILINE,
    )

    assert match is not None
    block = match.group("body")
    assert "width: 100%;" in block
    assert "display: grid;" in block


def test_set_trace_round_active_tool_prefetches_full_output_for_active_panel() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        class HTMLElementStub {
          constructor() {
            this.dataset = {};
            this.hidden = false;
            this.textContent = "";
            this._selectors = {};
            this._selectorLists = {};
            this.classList = { toggle() {}, add() {}, remove() {} };
          }
          querySelector(selector) { return this._selectors[selector] || null; }
          querySelectorAll(selector) { return this._selectorLists[selector] || []; }
          setAttribute() {}
        }
        global.HTMLElement = HTMLElementStub;
        const calls = [];
        global.ensureTraceOutputCodeBlockContent = async (element) => {
          calls.push({ ref: element.dataset.outputRef || "", before: element.textContent || "" });
          element.textContent = "FULL OUTPUT";
          return element.textContent;
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const chip = new HTMLElementStub();
        chip.dataset.toolKey = "round:tool:1";

        const output = new HTMLElementStub();
        output.dataset.outputRef = "artifact:artifact:tool-output";
        output.textContent = "preview";

        const panel = new HTMLElementStub();
        panel.dataset.toolKey = "round:tool:1";
        panel.hidden = true;
        panel._selectors[".task-trace-output-value[data-output-ref]"] = output;

        const placeholder = new HTMLElementStub();
        placeholder.hidden = false;

        const roundHost = new HTMLElementStub();
        roundHost._selectorLists[".task-trace-round-chip"] = [chip];
        roundHost._selectorLists[".task-trace-round-panel"] = [panel];
        roundHost._selectors[".task-trace-round-panel-placeholder"] = placeholder;

        setTraceRoundActiveTool(roundHost, "round:tool:1");
        setTimeout(() => {
          console.log(JSON.stringify({
            callCount: calls.length,
            firstRef: calls[0]?.ref || "",
            text: output.textContent,
            activeToolKey: roundHost.dataset.activeToolKey || "",
            panelHidden: panel.hidden,
            placeholderHidden: placeholder.hidden,
          }));
        }, 0);
        """
    )

    assert result["callCount"] == 1
    assert result["firstRef"] == "artifact:artifact:tool-output"
    assert result["text"] == "FULL OUTPUT"
    assert result["activeToolKey"] == "round:tool:1"
    assert result["panelHidden"] is False
    assert result["placeholderHidden"] is False


def test_ceo_composer_html_includes_brain_long_press_controls() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert 'class="ceo-context-usage-brain-ring"' in html
    assert 'id="ceo-context-usage-brain-hint"' in html
    assert "长按压缩上下文" in html


def test_model_retry_toasts_are_wired_for_ceo_and_task_node_views() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    task_view_js = "\n".join(
        (REPO_ROOT / name).read_text(encoding="utf-8")
        for name in (
            "g3ku/web/frontend/org_graph_task_view.js",
            "g3ku/web/frontend/tasks/node-detail.js",
        )
    )
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert 'id="ceo-model-retry-toast"' in html
    assert 'id="ceo-model-retry-toast-text"' in html
    assert 'id="task-node-model-retry-toast"' in html
    assert 'id="task-node-model-retry-toast-text"' in html
    assert "function syncCeoModelRetryToast" in app_js
    assert "model_retry_status" in app_js
    assert "function renderTaskNodeModelRetryToast" in task_view_js
    assert ".model-retry-toast" in css








def test_tree_node_search_matches_id_or_goal_and_ranks_exact_prefix_above_contains() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = { treeNodesById: {} };
        global.U = {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        S.treeNodesById = {
          "node:a": { node_id: "node:a", title: "alpha" },
          "alpha-child": { node_id: "alpha-child", title: "z" },
          "node:b": { node_id: "node:b", title: "an Alpha goal" },
          "x": { node_id: "x", title: "unrelated" },
        };
        console.log(JSON.stringify({
          ranked: searchTaskNodes("alpha").map((match) => match.nodeId),
          caseInsensitiveId: searchTaskNodes("NODE:B").map((match) => match.nodeId),
          blankQuery: searchTaskNodes("   "),
          noHit: searchTaskNodes("不存在的关键词"),
          limited: searchTaskNodes("alpha", { limit: 2 }).length,
        }));
        """
    )

    assert result["ranked"] == ["node:a", "alpha-child", "node:b"]
    assert result["caseInsensitiveId"] == ["node:b"]
    assert result["blankQuery"] == []
    assert result["noHit"] == []
    assert result["limited"] == 2


def test_tree_node_search_result_rows_carry_goal_node_id_and_status() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = { treeNodesById: {} };
        global.U = {};
        global.esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
        const created = [];
        global.document = {
          createElement: (tag) => {
            const element = {
              tagName: String(tag).toUpperCase(),
              className: "",
              dataset: {},
              innerHTML: "",
              type: "",
              listeners: {},
              setAttribute() {},
              addEventListener(type, handler) { this.listeners[type] = handler; },
            };
            created.push(element);
            return element;
          },
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const match = {
          node: { node_id: "node:目标", title: "整理<周报>", status: "in_progress" },
          nodeId: "node:目标",
          title: "整理<周报>",
          score: 3,
        };
        const item = createTaskTreeSearchItem(match);
        const click = item.listeners.click;
        console.log(JSON.stringify({
          goalShown: item.innerHTML.includes("整理&lt;周报&gt;"),
          nodeIdShown: item.innerHTML.includes("node:目标"),
          statusShown: item.innerHTML.includes('data-status="in_progress"'),
          hasClickHandler: typeof click === "function",
          mousedownPrevented: typeof item.listeners.mousedown === "function",
        }));
        """
    )

    assert result["goalShown"] is True
    assert result["nodeIdShown"] is True
    assert result["statusShown"] is True
    assert result["hasClickHandler"] is True
    assert result["mousedownPrevented"] is True


def test_task_tree_snapshot_chunked_load_merges_all_chunks_and_renders_once() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        const noticeTexts = [];
        const noticeStates = [];
        const notice = {
          _hidden: true,
          className: "",
          get hidden() { return this._hidden; },
          set hidden(value) { this._hidden = !!value; noticeStates.push(this._hidden ? "hide" : "show"); },
        };
        const noticeText = {
          _value: "",
          get textContent() { return this._value; },
          set textContent(value) { this._value = String(value); noticeTexts.push(this._value); },
        };
        global.isAbortLike = () => false;
        global.refreshTaskTreeSearchResultsIfVisible = () => {};
        let renderCount = 0;
        let chunkCount = 0;
        global.S = {
          currentTaskId: "task:test",
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
          treeBulkLoadingTaskId: "",
          treeBulkLoadToken: 0,
          treeLoadNoticeTaskId: "",
          treeLoadNoticeTimer: null,
          treeLoadNoticeTimerTaskId: "",
          taskNodeDetails: {},
          liveFrameMap: {},
        };
        global.U = { tree: { innerHTML: "" }, taskLoadNotice: notice, taskLoadNoticeText: noticeText };
        global.ApiClient = {
          getTaskTreeSnapshot: async (taskId, { afterNodeId = "" } = {}) => {
            chunkCount += 1;
            if (!afterNodeId) {
              return {
                task_id: "task:test",
                root_node_id: "root",
                snapshot_version: "7",
                truncated: true,
                total_node_count: 5,
                next_after_node_id: "b",
                nodes_by_id: {
                  root: { node_id: "root", title: "root", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
                  a: { node_id: "a", parent_node_id: "root", title: "a", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
                  b: { node_id: "b", parent_node_id: "root", title: "b", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
                },
              };
            }
            return {
              task_id: "task:test",
              root_node_id: "root",
              snapshot_version: "7",
              truncated: false,
              total_node_count: 5,
              next_after_node_id: "",
              nodes_by_id: {
                c: { node_id: "c", parent_node_id: "a", title: "c", status: "success", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
                d: { node_id: "d", parent_node_id: "b", title: "d", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
              },
            };
          },
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderTree = () => { renderCount += 1; };
        (async () => {
          await loadTaskTreeSnapshot("task:test", { announceLoad: true });
          await new Promise((resolve) => { setTimeout(resolve, 1000); });
          console.log(JSON.stringify({
            renderCount,
            chunkCount,
            nodeCount: Object.keys(S.treeNodesById).length,
            rootId: S.treeRootNodeId,
            hasC: !!S.treeNodesById.c,
            hasD: !!S.treeNodesById.d,
            bulkFlag: S.treeBulkLoadingTaskId,
            noticeTexts,
            noticeStates,
            noticeHidden: notice.hidden,
            noticeClass: notice.className,
            owner: S.treeLoadNoticeTaskId,
          }));
        })();
        """
    )

    assert result["chunkCount"] == 2
    assert result["nodeCount"] == 5
    assert result["rootId"] == "root"
    assert result["hasC"] is True
    assert result["hasD"] is True
    # 全部块落位后才渲染一次；渲染后门闩清空。
    assert result["renderCount"] == 1
    assert result["bulkFlag"] == ""
    # 小树（总数未超单块）不亮加载提示条：既不写文本也不显示，连延迟定时器
    # 都要被首个分块取消掉，否则小任务打开时提示条会一闪而过。
    assert result["noticeTexts"] == []
    assert result["noticeStates"] == []
    assert result["noticeHidden"] is True
    assert result["noticeClass"] == ""
    assert result["owner"] == ""


def test_task_tree_snapshot_chunked_load_shows_progress_notice_for_large_tree() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        const noticeTexts = [];
        const noticeStates = [];
        const notice = {
          _hidden: true,
          className: "",
          get hidden() { return this._hidden; },
          set hidden(value) { this._hidden = !!value; noticeStates.push(this._hidden ? "hide" : "show"); },
        };
        const noticeText = {
          _value: "",
          get textContent() { return this._value; },
          set textContent(value) { this._value = String(value); noticeTexts.push(this._value); },
        };
        global.isAbortLike = () => false;
        global.refreshTaskTreeSearchResultsIfVisible = () => {};
        global.S = {
          currentTaskId: "task:test",
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
          treeBulkLoadingTaskId: "",
          treeBulkLoadToken: 0,
          treeLoadNoticeTaskId: "",
          treeLoadNoticeTimer: null,
          treeLoadNoticeTimerTaskId: "",
          taskNodeDetails: {},
          liveFrameMap: {},
        };
        global.U = { tree: { innerHTML: "" }, taskLoadNotice: notice, taskLoadNoticeText: noticeText };
        const chunkNodes = [
          { n1: "root", n2: "a1" },
          { n3: "a2", n4: "a3" },
          { n5: "a4" },
        ];
        let chunkIndex = 0;
        global.ApiClient = {
          getTaskTreeSnapshot: async (taskId) => {
            const nodes = chunkNodes[Math.min(chunkIndex, chunkNodes.length - 1)];
            const resultPayload = {
              task_id: "task:test",
              root_node_id: "root",
              snapshot_version: "9",
              truncated: chunkIndex < 2,
              total_node_count: 600,
              next_after_node_id: chunkIndex < 2 ? "cursor-" + chunkIndex : "",
              nodes_by_id: Object.fromEntries(
                Object.entries(nodes).map(([key, nodeId]) => [
                  nodeId,
                  { node_id: nodeId, parent_node_id: "root", title: key, status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
                ])
              ),
            };
            chunkIndex += 1;
            return resultPayload;
          },
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderTree = () => {};
        (async () => {
          await loadTaskTreeSnapshot("task:test", { announceLoad: true });
          const visibleRightAfterLoad = notice.hidden === false;
          await new Promise((resolve) => { setTimeout(resolve, 1000); });
          console.log(JSON.stringify({
            nodeCount: Object.keys(S.treeNodesById).length,
            texts: noticeTexts,
            allProgress: noticeTexts.every((text) => text.startsWith("正在打开任务：加载中 (")),
            noticeStates,
            visibleRightAfterLoad,
            noticeHidden: notice.hidden,
            owner: S.treeLoadNoticeTaskId,
            bulkFlag: S.treeBulkLoadingTaskId,
          }));
        })();
        """
    )

    assert result["nodeCount"] == 5
    # 大任务（总数超过单块上限）在首块返回后就亮出进度提示条，并逐块更新数字。
    assert result["texts"] == [
        "正在打开任务：加载中 (2/600)",
        "正在打开任务：加载中 (4/600)",
        "正在打开任务：加载中 (5/600)",
    ]
    assert result["allProgress"] is True
    # 最后一段数字保留可见窗口，不做同帧关闭；800ms 收尾窗口结束后才隐藏。
    assert result["visibleRightAfterLoad"] is True
    assert result["noticeStates"] == ["show", "hide"]
    assert result["noticeHidden"] is True
    assert result["owner"] == ""
    assert result["bulkFlag"] == ""


def test_task_tree_load_notice_updates_numbers_for_single_chunk_when_visible() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        const noticeTexts = [];
        const noticeStates = [];
        const notice = {
          _hidden: false,
          className: "task-load-notice is-open",
          get hidden() { return this._hidden; },
          set hidden(value) { this._hidden = !!value; noticeStates.push(this._hidden ? "hide" : "show"); },
        };
        const noticeText = {
          _value: "正在打开任务：加载中 (0/…)",
          get textContent() { return this._value; },
          set textContent(value) { this._value = String(value); noticeTexts.push(this._value); },
        };
        global.isAbortLike = () => false;
        global.refreshTaskTreeSearchResultsIfVisible = () => {};
        global.S = {
          currentTaskId: "task:test",
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
          treeBulkLoadingTaskId: "",
          treeBulkLoadToken: 0,
          // 模拟详情阶段较慢：400ms 定时器已弹出 加载中 (0/…)，提示条已在显示。
          treeLoadNoticeTaskId: "task:test",
          treeLoadNoticeTimer: null,
          treeLoadNoticeTimerTaskId: "",
          taskNodeDetails: {},
          liveFrameMap: {},
        };
        global.U = { tree: { innerHTML: "" }, taskLoadNotice: notice, taskLoadNoticeText: noticeText };
        global.ApiClient = {
          getTaskTreeSnapshot: async (taskId) => ({
            task_id: "task:test",
            root_node_id: "root",
            snapshot_version: "10",
            truncated: false,
            total_node_count: 3,
            next_after_node_id: "",
            nodes_by_id: {
              root: { node_id: "root", title: "root", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
              a: { node_id: "a", parent_node_id: "root", title: "a", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
              b: { node_id: "b", parent_node_id: "root", title: "b", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
            },
          }),
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderTree = () => {};
        (async () => {
          await loadTaskTreeSnapshot("task:test", { announceLoad: true });
          const visibleRightAfterLoad = notice.hidden === false;
          await new Promise((resolve) => { setTimeout(resolve, 1000); });
          console.log(JSON.stringify({
            nodeCount: Object.keys(S.treeNodesById).length,
            texts: noticeTexts,
            noticeStates,
            visibleRightAfterLoad,
            noticeHidden: notice.hidden,
            owner: S.treeLoadNoticeTaskId,
            bulkFlag: S.treeBulkLoadingTaskId,
          }));
        })();
        """
    )

    assert result["nodeCount"] == 3
    # 单块小树：提示条已显示时数字也必须更新，不能因防闪烁闸门停在 0。
    assert result["texts"] == ["正在打开任务：加载中 (3/3)"]
    # 收尾窗口内不隐藏（已在显示，不再重复写显示状态），窗口结束后隐藏一次。
    assert result["visibleRightAfterLoad"] is True
    assert result["noticeStates"] == ["hide"]
    assert result["noticeHidden"] is True
    assert result["owner"] == ""
    assert result["bulkFlag"] == ""


def test_task_tree_in_place_refresh_never_shows_load_notice() -> None:
    """暂停/恢复、定向通知、快照自愈走的原地刷新不报「正在打开任务」。

    报告的问题：暂停任务时弹出「正在打开任务 加载中 (0/…)」，让人以为任务被
    重新打开了一次。提示条只属于 loadTaskDetail 的打开引导路径（announceLoad）。
    """
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        const noticeTexts = [];
        const notice = {
          _hidden: true,
          className: "task-load-notice",
          get hidden() { return this._hidden; },
          set hidden(value) { this._hidden = !!value; },
        };
        const noticeText = {
          _value: "",
          get textContent() { return this._value; },
          set textContent(value) { this._value = String(value); noticeTexts.push(this._value); },
        };
        global.isAbortLike = () => false;
        global.refreshTaskTreeSearchResultsIfVisible = () => {};
        global.S = {
          currentTaskId: "task:test",
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
          treeBulkLoadingTaskId: "",
          treeBulkLoadToken: 0,
          treeLoadNoticeTaskId: "",
          treeLoadNoticeTimer: null,
          treeLoadNoticeTimerTaskId: "",
          taskNodeDetails: {},
          liveFrameMap: {},
        };
        global.U = { tree: { innerHTML: "" }, taskLoadNotice: notice, taskLoadNoticeText: noticeText };
        global.ApiClient = {
          getTaskTreeSnapshot: async () => ({
            task_id: "task:test",
            root_node_id: "root",
            snapshot_version: "11",
            truncated: false,
            // 大任务：总数远超单块上限，旧实现会在这里无条件亮出加载 toast。
            total_node_count: 600,
            next_after_node_id: "",
            nodes_by_id: {
              root: { node_id: "root", title: "root", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
            },
          }),
        };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        global.renderTree = () => {};
        (async () => {
          await loadTaskTreeSnapshot("task:test");
          await new Promise((resolve) => { setTimeout(resolve, 1000); });
          const inPlaceTexts = noticeTexts.slice();
          const inPlaceHidden = notice.hidden;
          const inPlaceOwner = S.treeLoadNoticeTaskId;
          // 打开流程的提示条还在显示时被原地刷新抢先：它留下的提示条已失去意义，
          // 必须立刻收掉；打开流程失效后的收尾也不能再把它带回来。
          S.treeLoadNoticeTaskId = "task:test";
          notice.hidden = false;
          noticeText.textContent = "正在打开任务：加载中 (0/…)";
          noticeTexts.length = 0;
          await loadTaskTreeSnapshot("task:test");
          const cancelledHidden = notice.hidden;
          const cancelledOwner = S.treeLoadNoticeTaskId;
          finishTaskTreeLoadNotice("task:test");
          await new Promise((resolve) => { setTimeout(resolve, 1000); });
          console.log(JSON.stringify({
            inPlaceTexts,
            inPlaceHidden,
            inPlaceOwner,
            cancelledHidden,
            cancelledOwner,
            textsAfterCancel: noticeTexts,
            finalHidden: notice.hidden,
          }));
        })();
        """
    )

    # 原地刷新即便加载的是超大任务也不写文本、不显示。
    assert result["inPlaceTexts"] == []
    assert result["inPlaceHidden"] is True
    assert result["inPlaceOwner"] == ""
    # 在途打开流程留下的提示条被原地刷新就地收掉，归属同时清空。
    assert result["cancelledHidden"] is True
    assert result["cancelledOwner"] == ""
    assert result["textsAfterCancel"] == []
    assert result["finalHidden"] is True


def test_task_tree_load_notice_aligns_to_tree_search_box() -> None:
    """提示条落在任务树左上角搜索框的高度上，按实测几何对齐（顶栏折行会改 y）。"""
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          currentTaskId: "task:test",
          treeLoadNoticeTaskId: "",
          treeLoadNoticeTimer: null,
          treeLoadNoticeTimerTaskId: "",
        };
        const rect = (top, height) => ({ top, height, bottom: top + height, left: 0, right: 0, width: 0 });
        const viewport = { getBoundingClientRect: () => rect(20, 0) };
        let noticeMeasureCalls = 0;
        const notice = {
          hidden: true,
          className: "",
          style: {},
          parentElement: viewport,
          getBoundingClientRect: () => { noticeMeasureCalls += 1; return rect(20, 35); },
        };
        // 窄窗口：详情页顶栏折成两行，搜索框落在 y=148（宽窗口单行时约 y=68）。
        let anchorRect = rect(148, 40);
        const anchor = { getBoundingClientRect: () => anchorRect };
        global.document = { querySelector: (selector) => (anchorRect && selector === ".task-tree-search-input" ? anchor : null) };
        global.U = { taskLoadNotice: notice, taskLoadNoticeText: { textContent: "" } };
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);
        showTaskTreeLoadNotice("正在打开任务：加载中 (3/600)");
        const firstMargin = notice.style.marginTop;
        const firstText = U.taskLoadNoticeText.textContent;
        const measuresAfterFirst = noticeMeasureCalls;
        // 逐块更新数字（已在显示）：只改文本，不重复量几何。
        showTaskTreeLoadNotice("正在打开任务：加载中 (4/600)");
        const measuresAfterUpdate = noticeMeasureCalls;
        // 换到宽窗口（顶栏单行）：下次显示按新的搜索框位置重新对齐。
        hideTaskTreeLoadNotice();
        anchorRect = rect(68, 40);
        showTaskTreeLoadNotice("正在打开任务：加载中 (0/…)");
        const wrappedMargin = notice.style.marginTop;
        // 取不到锚点：退回 CSS 默认偏移（清掉行内值）。
        hideTaskTreeLoadNotice();
        anchorRect = null;
        showTaskTreeLoadNotice("正在打开任务：加载中 (0/…)");
        console.log(JSON.stringify({
          firstMargin,
          firstText,
          measuresAfterFirst,
          measuresAfterUpdate,
          wrappedMargin,
          fallbackMargin: notice.style.marginTop,
        }));
        """
    )

    # 搜索框中心线 168：168 - 视口顶 20 - 提示条半高 17.5 → 131px。
    assert result["firstMargin"] == "131px"
    assert result["firstText"] == "正在打开任务：加载中 (3/600)"
    assert result["measuresAfterFirst"] == 1
    # 已显示时逐块更新不再量几何。
    assert result["measuresAfterUpdate"] == 1
    # 单行顶栏（搜索框 y=68）→ 68 + 20 - 20 - 17.5 → 51px。
    assert result["wrappedMargin"] == "51px"
    # 无锚点时清掉行内偏移，回落到 CSS。
    assert result["fallbackMargin"] == ""


def test_load_task_detail_announces_tree_load_only_for_real_opens() -> None:
    """只有真正打开任务（preserveView 为假）才起「正在打开任务」提示条。

    暂停/恢复、批量操作、断线重连对账走的都是 loadTaskDetail({preserveView:true})，
    它们发生在已经打开的任务里，不能报"正在打开任务"。
    """
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        const source = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        const start = source.indexOf("async function loadTaskDetail");
        if (start < 0) throw new Error("loadTaskDetail not found");
        const tail = source.slice(start);
        const end = tail.search(/\\n(async function |function )/);
        if (end < 0) throw new Error("loadTaskDetail end not found");
        const scheduleCalls = [];
        const snapshotCalls = [];
        const context = {
          ApiClient: { getTask: async () => ({ task_id: "task:test" }) },
          switchView: () => {},
          resetTaskView: () => {},
          openTaskDetailWs: () => {},
          taskDetailViewVisible: () => true,
          applyTaskPayload: () => {},
          scheduleTaskTreeLoadNotice: (taskId) => { scheduleCalls.push(taskId); },
          cancelTaskTreeLoadNotice: () => {},
          loadTaskTreeSnapshot: async (taskId, options) => {
            snapshotCalls.push({ taskId, announceLoad: options?.announceLoad });
            return null;
          },
          S: { currentTaskId: "", treeFitOnNextRender: false },
        };
        const loadTaskDetail = vm.runInNewContext(`(${tail.slice(0, end)})`, context);
        (async () => {
          await loadTaskDetail("task:test");
          const openSchedule = scheduleCalls.slice();
          const openAnnounce = snapshotCalls[0] || null;
          scheduleCalls.length = 0;
          snapshotCalls.length = 0;
          await loadTaskDetail("task:test", { preserveView: true, reopenSocket: false });
          console.log(JSON.stringify({
            openSchedule,
            openAnnounce,
            refreshSchedule: scheduleCalls,
            refreshAnnounce: snapshotCalls[0] || null,
          }));
        })();
        """
    )

    # 打开任务：起提示条，并把 announceLoad 传给整树加载。
    assert result["openSchedule"] == ["task:test"]
    assert result["openAnnounce"] == {"taskId": "task:test", "announceLoad": True}
    # 原地刷新（暂停/恢复、批量操作、重连对账）：既不起提示条，也不传 announceLoad。
    assert result["refreshSchedule"] == []
    assert result["refreshAnnounce"] == {"taskId": "task:test", "announceLoad": False}


def test_task_recovery_notice_dismissal_survives_page_reload() -> None:
    """关闭「任务自动恢复」toast 必须跨刷新/重启生效。

    整树每次渲染都会重新评估该提示，所以 dismissed 记录只放在内存里时，
    刷新页面或重启项目后用户点过叉号的任务仍会反复弹出。
    """
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        const code = ["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(code);

        const NOTICE = "本任务遇到异常停止，已回退到稳定步骤继续。";
        const STORAGE_KEY = "g3ku.taskRecoveryNotice.dismissed.v1";
        // 浏览器 localStorage：跨页面加载存活，只有它能让关闭动作持久下来。
        const backing = new Map();
        const localStorage = {
          getItem: (key) => (backing.has(key) ? backing.get(key) : null),
          setItem: (key, value) => backing.set(key, String(value)),
        };

        function loadPage(taskId, notice) {
          const handlers = [];
          const textEl = { textContent: "" };
          global.window = { localStorage };
          global.document = {
            getElementById: (id) => {
              if (id === "app-toast") {
                return {
                  classList: { contains: () => true },
                  addEventListener: (_type, fn) => handlers.push(fn),
                };
              }
              return id === "app-toast-text" ? textEl : null;
            },
          };
          global.showToast = ({ text }) => { textEl.textContent = text; };
          global.closeToast = () => { textEl.textContent = ""; };
          global.S = { currentTask: { task_id: taskId, metadata: { recovery_notice: notice } } };
          maybeShowTaskRecoveryNoticeToast();
          return {
            shown: textEl.textContent === notice,
            dismiss: () => handlers.forEach((fn) => fn()),
          };
        }

        const first = loadPage("task:crashed", NOTICE);
        first.dismiss();
        const reopened = loadPage("task:crashed", NOTICE);
        const otherTask = loadPage("task:other", NOTICE);
        const reworded = loadPage("task:crashed", "另一条恢复提示");
        console.log(JSON.stringify({
          firstShown: first.shown,
          reopenedShown: reopened.shown,
          otherTaskShown: otherTask.shown,
          rewordedShown: reworded.shown,
          stored: JSON.parse(backing.get(STORAGE_KEY) || "{}"),
        }));
        """
    )

    assert result["firstShown"] is True
    # 点过叉号后重新加载页面：同一任务不再弹出。
    assert result["reopenedShown"] is False
    # 关闭只作用于产生提示的任务，其他任务的同类提示照常弹出。
    assert result["otherTaskShown"] is True
    # 记账按提示文本，文案换版后老任务会再提示一次。
    assert result["rewordedShown"] is True
    assert result["stored"] == {"task:crashed": "本任务遇到异常停止，已回退到稳定步骤继续。"}


def test_fit_task_tree_anchors_root_when_tree_cannot_fit() -> None:
    """树大到 TREE_SCALE_MIN 也放不下时，fit 必须把根节点摆进视野。

    回归：几百节点的宽幅组织图（实测内容 40488×2347）需要 scale 0.013，被夹到 0.12，
    旧实现仍居中"内容盒"——内容中心是分支之间的空白，用户看到整片空树。
    """
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;

        const pan = { offsetX: 0, offsetY: 0, baseOffsetX: 0, baseOffsetY: 0, baseScale: 1, scale: 1 };
        global.S = { treeRootNodeId: "node:root", treePan: pan };
        class Element {}
        class HTMLElement extends Element {}
        global.Element = Element;
        global.HTMLElement = HTMLElement;
        // clamp 与两个缩放常量定义在 org_graph_app.js，本用例只加载 task_view.js
        global.TREE_SCALE_MIN = 0.12;
        global.TREE_SCALE_MAX = 3.5;
        global.clamp = (value, min, max) => Math.min(Math.max(value, min), max);

        const VIEW = { left: 0, top: 0, width: 600, height: 120 };
        const LAYOUT = { treeW: 40000, treeH: 2000, rootX: 100, rootY: 20, rootW: 26, rootH: 14 };
        const rect = (left, top, width, height) => ({
          left, top, width, height, right: left + width, bottom: top + height,
        });

        const rootEl = new HTMLElement();
        rootEl.getBoundingClientRect = () => rect(
          pan.offsetX + pan.scale * LAYOUT.rootX,
          pan.offsetY + pan.scale * LAYOUT.rootY,
          pan.scale * LAYOUT.rootW,
          pan.scale * LAYOUT.rootH,
        );
        const content = new HTMLElement();
        content.getBoundingClientRect = () => rect(
          pan.offsetX, pan.offsetY, pan.scale * LAYOUT.treeW, pan.scale * LAYOUT.treeH,
        );
        const wrapper = new HTMLElement();
        wrapper.style = {};
        wrapper.firstElementChild = content;
        wrapper.getBoundingClientRect = () => rect(
          pan.offsetX, pan.offsetY, pan.scale * LAYOUT.treeW, pan.scale * LAYOUT.treeH,
        );
        global.U = {
          tree: {
            getBoundingClientRect: () => rect(VIEW.left, VIEW.top, VIEW.width, VIEW.height),
            querySelector: (selector) => {
              if (String(selector).includes("node:root")) return rootEl;
              if (String(selector) === ".execution-tree") return wrapper;
              return null;
            },
          },
        };
        global.executionTreeNodeSelector = (nodeId) => `.execution-tree-node[data-id="${nodeId}"]`;

        vm.runInThisContext(["g3ku/web/frontend/tasks/tree-store.js", "g3ku/web/frontend/tasks/tree-view.js", "g3ku/web/frontend/tasks/node-detail-store.js", "g3ku/web/frontend/tasks/node-detail.js", "g3ku/web/frontend/org_graph_task_view.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n"));
        const applied = fitTaskTreeToView();
        const rootRect = rootEl.getBoundingClientRect();
        console.log(JSON.stringify({
          applied,
          scale: pan.scale,
          transform: wrapper.style.transform,
          rootCenterX: rootRect.left + rootRect.width / 2,
          rootCenterY: rootRect.top + rootRect.height / 2,
          viewCenter: [VIEW.left + VIEW.width / 2, VIEW.top + VIEW.height / 2],
        }));
        """
    )

    assert result["applied"] is True
    assert abs(float(result["scale"]) - 0.12) < 1e-6, "放不下时应停在 TREE_SCALE_MIN 下限"
    assert abs(float(result["rootCenterX"]) - float(result["viewCenter"][0])) < 1.0
    assert abs(float(result["rootCenterY"]) - float(result["viewCenter"][1])) < 1.0


def test_render_task_token_stats_marks_aborted_stream_rows_as_no_usage() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.esc = (v) => String(v ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
        const usage = (input, cache) => ({
          tracked: true,
          input_tokens: input,
          output_tokens: 0,
          cache_hit_tokens: cache,
          call_count: 1,
          calls_with_usage: input || cache ? 1 : 0,
          calls_without_usage: input || cache ? 0 : 1,
          is_partial: !(input || cache),
        });
        global.S = {
          currentTask: {
            token_usage: {
              tracked: true,
              input_tokens: 4200,
              output_tokens: 900,
              cache_hit_tokens: 3300,
              call_count: 2,
              calls_with_usage: 1,
              calls_without_usage: 1,
              is_partial: true,
            },
          },
          taskSummary: { token_usage_by_model: [] },
          recentModelCalls: [
            {
              call_index: 2,
              node_id: "node:aborted",
              created_at: new Date(Date.UTC(2026, 9, 4, 5, 23, 20)).toISOString(),
              prepared_message_count: 41,
              prepared_message_chars: 143113,
              response_tool_call_count: 0,
              duration_ms: 37773,
              first_token_ms: null,
              thinking_tokens: null,
              stream_incomplete: true,
              delta_usage: usage(0, 0),
              delta_usage_by_model: [{ model_key: "deepseek-v4-flash-4" }],
            },
            {
              call_index: 1,
              node_id: "node:normal",
              created_at: new Date(Date.UTC(2026, 9, 4, 5, 22, 3)).toISOString(),
              prepared_message_count: 40,
              prepared_message_chars: 140000,
              response_tool_call_count: 1,
              duration_ms: 41990,
              first_token_ms: 300,
              thinking_tokens: 2811,
              stream_incomplete: false,
              delta_usage: usage(4200, 3300),
              delta_usage_by_model: [{ model_key: "deepseek-v4-flash-4" }],
            },
          ],
          taskModelCallPage: 1,
          taskModelCallsPage: 1,
          taskModelCallsPageSize: 100,
          taskModelCallsQuery: "",
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = ["g3ku/web/frontend/ui/toast.js", "g3ku/web/frontend/ui/confirm.js", "g3ku/web/frontend/ceo/model-panel.js", "g3ku/web/frontend/ceo/composer.js", "g3ku/web/frontend/ceo/compression.js", "g3ku/web/frontend/ceo/feed-model.js", "g3ku/web/frontend/ceo/feed-view.js", "g3ku/web/frontend/ceo/sessions-panel.js", "g3ku/web/frontend/ceo/session-store.js", "g3ku/web/frontend/ceo/ws-router.js", "g3ku/web/frontend/memory/store.js", "g3ku/web/frontend/memory/actions.js", "g3ku/web/frontend/memory/view.js", "g3ku/web/frontend/models/role-store.js", "g3ku/web/frontend/models/role-view.js", "g3ku/web/frontend/models/actions.js", "g3ku/web/frontend/org_graph_app.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(appCode.slice(appCode.indexOf("const EMPTY_TOKEN_USAGE"), appCode.indexOf("function ensureTaskTokenUi")));
        global.S.modelCatalog = global.S.modelCatalog || { catalog: [] };
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/ceo/model-panel.js", "utf8"));

        const tasksCode = ["g3ku/web/frontend/tasks/hall-store.js", "g3ku/web/frontend/tasks/hall-view.js", "g3ku/web/frontend/tasks/ws-router.js", "g3ku/web/frontend/org_graph_tasks.js"].map((name) => fs.readFileSync(name, "utf8")).join("\\n");
        vm.runInThisContext(tasksCode.slice(tasksCode.indexOf("function renderTasks"), tasksCode.indexOf("async function loadTaskDetail")));

        renderTaskTokenStats();
        const html = U.taskTokenContent.innerHTML;
        const tableBody = html.match(/<tbody>([\\s\\S]*?)<\\/tbody>/)?.[1] || "";
        const rows = tableBody.split("<tr>").slice(1);
        const abortedRow = rows.find((row) => row.includes("node:aborted")) || "";
        const normalRow = rows.find((row) => row.includes("node:normal")) || "";

        console.log(JSON.stringify({
          abortedInputCellDashed: /<td class="task-token-call-token"[^>]*>--<\\/td>/.test(abortedRow),
          abortedCellCarriesReason: abortedRow.includes("上游未回终止分片"),
          normalRowStillNumbers: normalRow.includes("4,200") && normalRow.includes("3,300"),
          normalHasNoAbortReason: !normalRow.includes("上游未回终止分片"),
        }));
        """
    )

    assert result["abortedInputCellDashed"] is True
    assert result["abortedCellCarriesReason"] is True
    assert result["normalRowStillNumbers"] is True
    assert result["normalHasNoAbortReason"] is True


def test_node_patch_merge_keeps_children_from_prior_normalized_node() -> None:
    # task.node.patch 只带状态字段、不带子节点列表：合并进缓存后子节点不能丢。
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = { currentTaskId: "task:test", currentTask: { status: "in_progress" }, treeRootNodeId: "root", treeNodesById: {}, liveFrameMap: {} };
        global.U = {};
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/tasks/tree-store.js", "utf8"));

        const snapshotNode = normalizeTaskTreeSnapshotNode(
          { node_id: "root", node_kind: "execution", status: "in_progress", title: "root", auxiliary_child_ids: ["a", "b"] },
          null,
        );
        // 补丁帧形状：只有 node_id/status/title 等，没有 auxiliary_child_ids / rounds。
        const patchNode = { node_id: "root", node_kind: "execution", status: "success", title: "root" };
        const merged = normalizeTaskTreeSnapshotNode({ ...snapshotNode, ...patchNode }, snapshotNode);
        // 快照显式给出空列表时以快照为准。
        const cleared = normalizeTaskTreeSnapshotNode({ ...snapshotNode, auxiliary_child_ids: [] }, snapshotNode);
        console.log(JSON.stringify({
          kept: merged.all_child_ids,
          keptStatus: merged.status,
          explicitEmpty: cleared.all_child_ids,
        }));
        """
    )

    assert result["kept"] == ["a", "b"]
    assert result["keptStatus"] == "success"
    assert result["explicitEmpty"] == []


def test_terminal_task_shows_whole_tree_and_hidden_children_stay_reachable() -> None:
    # 任务结束后默认过滤不能再把整棵树藏成一根节点；运行中藏起来的终态子节点
    # 必须留展开入口，终态节点下的存活后代必须连同祖先链一起显示。
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          currentTaskId: "task:test",
          currentTask: { task_id: "task:test", status: "success" },
          treeRootNodeId: "root",
          treeNodesById: {},
          liveFrameMap: {},
        };
        global.U = {};
        vm.runInThisContext(fs.readFileSync("g3ku/web/frontend/tasks/tree-store.js", "utf8"));

        const raw = {
          root: { node_id: "root", node_kind: "execution", status: "success", title: "root", auxiliary_child_ids: ["work", "root-acc"], pending_notice_count: 2 },
          work: { node_id: "work", parent_node_id: "root", node_kind: "execution", status: "success", title: "work", auxiliary_child_ids: ["leaf", "work-acc"] },
          leaf: { node_id: "leaf", parent_node_id: "work", node_kind: "execution", status: "success", title: "leaf", auxiliary_child_ids: [] },
          "work-acc": { node_id: "work-acc", parent_node_id: "work", node_kind: "acceptance", status: "success", title: "acc", auxiliary_child_ids: [] },
          "root-acc": { node_id: "root-acc", parent_node_id: "root", node_kind: "acceptance", status: "success", title: "acc", auxiliary_child_ids: [] },
        };
        const load = () => {
          S.treeNodesById = {};
          Object.entries(raw).forEach(([id, value]) => {
            S.treeNodesById[id] = normalizeTaskTreeSnapshotNode(value, null);
          });
          return buildExecutionTreeFromSnapshot();
        };
        const walk = (node, visit) => {
          if (!node) return;
          visit(node);
          (node.inspectionNodes || []).forEach((child) => walk(child, visit));
          (node.children || []).forEach((child) => walk(child, visit));
        };
        const ids = (tree) => {
          const out = [];
          walk(tree, (node) => out.push(node.node_id));
          return out.sort();
        };
        const find = (tree, id) => {
          let hit = null;
          walk(tree, (node) => { if (node.node_id === id) hit = node; });
          return hit;
        };

        const terminalIds = ids(load());

        S.currentTask = { task_id: "task:test", status: "in_progress" };
        S.treeShowTerminal = null;
        S.treeExpandedNodeIds = new Set();
        const runningTree = load();
        const runningIds = ids(runningTree);
        const runningRoot = find(runningTree, "root");

        S.treeExpandedNodeIds = new Set(["root"]);
        const expandedIds = ids(load());

        raw.leaf.status = "in_progress";
        S.treeExpandedNodeIds = new Set();
        const liveIds = ids(load());

        console.log(JSON.stringify({
          terminalIds,
          runningIds,
          runningRootHiddenCount: runningRoot.hidden_child_count,
          runningRootExpandable: runningRoot.expandable,
          expandedIds,
          liveIds,
          runningRootUnread: runningRoot.pending_notice_count,
        }));
        """
    )

    all_ids = ["leaf", "root", "root-acc", "work", "work-acc"]
    assert result["terminalIds"] == all_ids
    # 运行中：终态分支折叠，验收节点无条件附着（root-acc 随根一起显示）。
    assert result["runningIds"] == ["root", "root-acc"]
    assert result["runningRootHiddenCount"] == 1
    assert result["runningRootExpandable"] is True
    # 节点行上的展开钮放出该节点的全部直属子节点（逐层展开，不越级）。
    assert result["expandedIds"] == ["root", "root-acc", "work", "work-acc"]
    # 终态 work 底下还有存活的 leaf：祖先链必须一起可见。
    assert result["liveIds"] == ["leaf", "root", "root-acc", "work", "work-acc"]
    assert result["runningRootUnread"] == 2


def test_detail_boxes_are_line_capped_and_outputs_have_copy_buttons() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")
    # 消息列表 / 派生记录：10.5 条封顶；max-height 让不足 11 条时不预占空白。
    assert "max-height: calc(10.5 * 48px + 10 * var(--space-3))" in css
    # 提交输出 / 验收结果：15 行封顶。
    assert "max-height: calc(15 * 1.65em)" in css
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    assert 'id="ad-output-copy"' in html
    assert 'id="ad-check-copy"' in html
