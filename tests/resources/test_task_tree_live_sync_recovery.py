"""任务树实时同步自愈回归测试。

覆盖 2026-09-12 分发事故暴露的两个前端破坏路径：

1. `applyTaskTreeSubtreePayload` 收到缺失自身根节点的退化子树响应时，
   不能再执行"先删旧子树再重建"的合并（整树根被删会导致树整体消失、
   但搜索仍命中旧缓存），必须丢弃响应并回退整表快照重载；
2. `renderTree` 发现根节点不在缓存中时，除空态外还要去抖触发整表自愈，
   且自愈重试有预算上限；
3. 任务详情 WS 断线后必须自动重连（对齐任务列表 WS 策略），重连成功
   后必须重拉任务详情对齐状态；主动关闭（切换视图/换任务）不得触发重连。
"""

from __future__ import annotations

import json
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
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(completed.stdout.strip())


def test_degenerate_subtree_payload_does_not_destroy_tree_and_falls_back_to_full_snapshot() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        let snapshotFetches = 0;
        const fullSnapshot = () => ({
          root_node_id: "root",
          snapshot_version: "1",
          nodes_by_id: {
            root: {
              node_id: "root",
              title: "root",
              status: "in_progress",
              node_kind: "execution",
              default_round_id: "r1",
              rounds: [{ round_id: "r1", label: "Round 1", is_latest: true, child_ids: ["a"] }],
              auxiliary_child_ids: [],
            },
            a: {
              node_id: "a",
              parent_node_id: "root",
              title: "a",
              status: "in_progress",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
            },
          },
        });
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
          treeSnapshotSelfHealToken: null,
          treeSnapshotSelfHealAttempts: 0,
          taskNodeDetails: {},
          liveFrameMap: {},
          taskRuntimeSummary: null,
        };
        global.U = { tree: {} };
        global.ApiClient = {
          getTaskTreeSnapshot: async () => {
            snapshotFetches += 1;
            return fullSnapshot();
          },
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        (async () => {
          applyTaskTreeSnapshotPayload(fullSnapshot());

          // 退化响应：root_node_id 指向 root，但 nodes_by_id 里没有 root。
          applyTaskTreeSubtreePayload({
            root_node_id: "root",
            snapshot_version: "2",
            nodes_by_id: {
              a: { node_id: "a", parent_node_id: "root", title: "a", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
            },
          });

          // 合并必须被拒绝：根节点仍在缓存中，整树仍可构建。
          const rootStillCached = !!S.treeNodesById.root;
          const tree = buildExecutionTreeFromSnapshot();
          const buildable = !!tree && tree.node_id === "root" && tree.children.map((node) => node.node_id).join(",") === "a";

          // 拒绝后回退整表快照重载（异步），等它落位。
          await new Promise((resolve) => window.setTimeout(resolve, 10));

          console.log(JSON.stringify({
            rootStillCached,
            buildable,
            snapshotFetches,
            rootRestored: !!S.treeNodesById.root,
          }));
        })();
        """
    )

    assert result["rootStillCached"] is True, "退化子树响应不得删除整树根节点"
    assert result["buildable"] is True, "拒绝退化响应后树必须仍可构建"
    assert result["snapshotFetches"] == 1, "拒绝退化响应后必须回退一次整表快照重载"
    assert result["rootRestored"] is True


def test_well_formed_subtree_payload_still_merges() -> None:
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
          treeSnapshotSelfHealToken: null,
          treeSnapshotSelfHealAttempts: 0,
          taskNodeDetails: {},
          liveFrameMap: {},
          taskRuntimeSummary: null,
        };
        global.U = { tree: {} };
        global.ApiClient = { getTaskTreeSnapshot: async () => ({}) };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        applyTaskTreeSnapshotPayload({
          root_node_id: "root",
          snapshot_version: "1",
          nodes_by_id: {
            root: {
              node_id: "root",
              title: "root",
              status: "in_progress",
              node_kind: "execution",
              default_round_id: "r1",
              rounds: [{ round_id: "r1", label: "Round 1", is_latest: true, child_ids: ["a"] }],
              auxiliary_child_ids: [],
            },
            a: {
              node_id: "a",
              parent_node_id: "root",
              title: "a",
              status: "in_progress",
              node_kind: "execution",
              rounds: [{ round_id: "r1", label: "Round 1", is_latest: true, child_ids: ["a1"] }],
              auxiliary_child_ids: [],
            },
            a1: { node_id: "a1", parent_node_id: "a", title: "a1", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
          },
        });

        // 正常子树响应：根在 payload 内，合并按旧路径执行。
        applyTaskTreeSubtreePayload({
          root_node_id: "a",
          snapshot_version: "2",
          nodes_by_id: {
            a: {
              node_id: "a",
              parent_node_id: "root",
              title: "a-updated",
              status: "in_progress",
              node_kind: "execution",
              rounds: [{ round_id: "r1", label: "Round 1", is_latest: true, child_ids: ["a1", "a2"] }],
              auxiliary_child_ids: [],
            },
            a1: { node_id: "a1", parent_node_id: "a", title: "a1", status: "success", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
            a2: { node_id: "a2", parent_node_id: "a", title: "a2", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
          },
        });

        console.log(JSON.stringify({
          rootKept: !!S.treeNodesById.root,
          updatedTitle: S.treeNodesById.a?.title,
          newChildMerged: !!S.treeNodesById.a2,
        }));
        """
    )

    assert result["rootKept"] is True
    assert result["updatedTitle"] == "a-updated"
    assert result["newChildMerged"] is True, "正常子树响应必须照常合并"


def test_render_tree_self_heal_reloads_snapshot_with_retry_budget() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        let snapshotFetches = 0;
        const timers = [];
        global.window = global;
        global.setTimeout = (callback) => { timers.push(callback); return timers.length; };
        global.clearTimeout = () => {};
        global.S = {
          currentTaskId: "task:test",
          treeRootNodeId: "root",
          treeNodesById: { a: { node_id: "a", parent_node_id: "root", title: "a", rounds: [] } },
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
          liveFrameMap: {},
          taskRuntimeSummary: null,
        };
        global.U = { tree: {} };
        global.ApiClient = {
          getTaskTreeSnapshot: async () => {
            snapshotFetches += 1;
            return {
              root_node_id: "root",
              snapshot_version: "9",
              nodes_by_id: {
                root: {
                  node_id: "root",
                  title: "root",
                  status: "in_progress",
                  node_kind: "execution",
                  rounds: [{ round_id: "r1", label: "Round 1", is_latest: true, child_ids: ["a"] }],
                  auxiliary_child_ids: [],
                },
                a: { node_id: "a", parent_node_id: "root", title: "a", status: "in_progress", node_kind: "execution", rounds: [], auxiliary_child_ids: [] },
              },
            };
          },
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        (async () => {
          // 根节点缺失的残缺缓存：第一次调度落一个定时器。
          scheduleTaskTreeSnapshotSelfHeal();
          const firstScheduled = timers.length;
          // 预算内第二次调度被去抖挡掉。
          scheduleTaskTreeSnapshotSelfHeal();
          const secondScheduled = timers.length;
          // 烧掉定时器预算后不再调度。
          S.treeSnapshotSelfHealAttempts = 2;
          scheduleTaskTreeSnapshotSelfHeal();
          const thirdScheduled = timers.length;

          // 触发第一个定时器：根仍缺失 → 整表重载，快照落位后自愈预算清零。
          await timers[0]();
          await new Promise((resolve) => setImmediate(resolve));

          console.log(JSON.stringify({
            firstScheduled,
            secondScheduled,
            thirdScheduled,
            snapshotFetches,
            rootRestored: !!S.treeNodesById.root,
            attemptsReset: S.treeSnapshotSelfHealAttempts,
          }));
        })();
        """
    )

    assert result["firstScheduled"] == 1, "残缺缓存必须调度一次整表自愈"
    assert result["secondScheduled"] == 1, "去抖窗口内不得重复调度"
    assert result["thirdScheduled"] == 1, "自愈预算耗尽后不得再调度"
    assert result["snapshotFetches"] == 1
    assert result["rootRestored"] is True
    assert result["attemptsReset"] == 0, "成功快照落位后必须重置自愈预算"


def test_task_detail_ws_reconnects_on_drop_and_skips_reconnect_on_intentional_close() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        const context = {
          console,
          Promise,
          S: {
            currentTaskId: "task:test",
            taskWs: null,
            taskWsReconnectTimer: null,
            treeSelectedRoundByNodeId: {},
            treeRootNodeId: "root",
          },
          U: { viewTaskDetails: { classList: { contains: () => true } } },
          __sockets: [],
          __timerCallbacks: [],
          __loadTaskDetailCalls: [],
          __renderCalls: 0,
        };
        context.setTimeout = (callback) => {
          context.__timerCallbacks.push(callback);
          return context.__timerCallbacks.length;
        };
        context.clearTimeout = () => {};
        context.window = context;
        class FakeWebSocket {
          constructor(url) {
            this.url = url;
            this.closed = false;
            this.onmessage = null;
            this.onopen = null;
            this.onclose = null;
            context.__sockets.push(this);
          }
          close() { this.closed = true; }
        }
        context.WebSocket = FakeWebSocket;
        context.ApiClient = { getTaskWsUrl: (taskId) => `ws://test/api/ws/tasks/${taskId}` };
        context.handleTaskEvent = () => {};
        context.normalizeTreeRoundSelections = (value) => ({ ...(value || {}) });
        context.renderTree = () => { context.__renderCalls += 1; };
        context.loadTaskDetail = async (taskId, options) => {
          context.__loadTaskDetailCalls.push({ taskId, options });
        };
        context.TASK_DETAIL_WS_RECONNECT_MS = 1000;
        vm.createContext(context);

        const code = fs.readFileSync("g3ku/web/frontend/org_graph_tasks.js", "utf8");
        const start = code.indexOf("function taskDetailViewVisible");
        const end = code.indexOf("function resetTaskView");
        vm.runInContext(code.slice(start, end), context);

        (async () => {
          // 1) 首次打开：建立 WS，不触发对齐重载。
          context.openTaskDetailWs("task:test");
          const socket1 = context.__sockets.at(-1);
          socket1.onopen();
          const reconcileAfterFirstOpen = context.__loadTaskDetailCalls.length;

          // 2) 意外断线：调度重连定时器。
          socket1.onclose();
          const timersAfterDrop = context.__timerCallbacks.length;

          // 3) 定时器到期：重建 WS（isReconnect），open 后触发对齐重载。
          await context.__timerCallbacks.at(-1)();
          const socket2 = context.__sockets.at(-1);
          socket2.onopen();
          await new Promise((resolve) => setImmediate(resolve));

          // 4) 主动关闭（切视图/换任务）：不得再调度重连。
          const timersBeforeIntentionalClose = context.__timerCallbacks.length;
          context.closeTaskDetailWs();
          const timersAfterIntentionalClose = context.__timerCallbacks.length;
          if (socket2.onclose) socket2.onclose();

          console.log(JSON.stringify({
            firstUrl: socket1.url,
            reconnectUrl: socket2.url,
            reconnectIsNewSocket: socket2 !== socket1,
            reconcileAfterFirstOpen,
            timersAfterDrop,
            reconcileCalls: context.__loadTaskDetailCalls,
            renderCallsAfterReconnect: context.__renderCalls,
            timersBeforeIntentionalClose,
            timersAfterIntentionalClose,
            socketAfterClose: context.S.taskWs,
          }));
        })();
        """
    )

    assert result["firstUrl"] == "ws://test/api/ws/tasks/task:test"
    assert result["reconnectUrl"] == "ws://test/api/ws/tasks/task:test"
    assert result["reconnectIsNewSocket"] is True
    assert result["reconcileAfterFirstOpen"] == 0, "首次连接不得触发对齐重载"
    assert result["timersAfterDrop"] == 1, "意外断线必须调度重连"
    assert result["reconcileCalls"] == [
        {"taskId": "task:test", "options": {"preserveView": True, "reopenSocket": False}}
    ], "重连成功后必须重拉任务详情对齐状态"
    assert result["renderCallsAfterReconnect"] == 1
    assert result["timersBeforeIntentionalClose"] == result["timersAfterIntentionalClose"], (
        "主动关闭不得调度重连"
    )
    assert result["socketAfterClose"] is None
