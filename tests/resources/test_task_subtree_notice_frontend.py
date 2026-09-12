"""定向通知前端契约回归：消息三态标签、subtree_barrier 分发 UI、通知输入框。"""

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


_COMMON_PRELUDE = """
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
  selectedNodeId: "",
  currentNodeDetail: null,
  currentTask: null,
};
global.U = { tree: {}, adMessages: {} };
global.ApiClient = {};
global.showToast = () => {};
global.isAbortLike = () => false;
const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
vm.runInThisContext(code);
global.renderTree = () => {};
"""


def test_message_list_status_descriptor_three_states() -> None:
    result = _run_node_script(
        _COMMON_PRELUDE
        + """
        console.log(JSON.stringify({
          pending: messageListStatusDescriptor("pending"),
          consumed: messageListStatusDescriptor("consumed"),
          merged: messageListStatusDescriptor("merged"),
          unknown: messageListStatusDescriptor("weird"),
        }));
        """
    )
    assert result["pending"] == {"key": "warning", "label": "待处理"}
    assert result["consumed"] == {"key": "info", "label": "已消费"}
    assert result["merged"] == {"key": "info", "label": "已并入上下文"}
    assert result["unknown"]["label"] == "weird"


def test_message_delivery_status_descriptor_semantic_labels() -> None:
    result = _run_node_script(
        _COMMON_PRELUDE
        + """
        console.log(JSON.stringify({
          delivered: messageDeliveryStatusDescriptor({ decision: "distributed", status: "delivered" }),
          consumed: messageDeliveryStatusDescriptor({ decision: "distributed", status: "consumed" }),
          merged: messageDeliveryStatusDescriptor({ decision: "distributed", status: "consumed", merged_at: "2026-09-12T10:00:00+08:00" }),
          skipped: messageDeliveryStatusDescriptor({ decision: "skipped", status: "" }),
          unknown: messageDeliveryStatusDescriptor({ decision: "distributed", status: "weird" }),
          empty: messageDeliveryStatusDescriptor({}),
        }));
        """
    )
    assert result["delivered"] == {"key": "delivered", "label": "已分发·待处理", "icon": "inbox"}
    assert result["consumed"] == {"key": "consumed", "label": "已消费", "icon": "circle-check"}
    assert result["merged"] == {"key": "merged", "label": "已并入上下文", "icon": "merge"}
    assert result["skipped"] == {"key": "skipped", "label": "未下发", "icon": "circle-slash"}
    assert result["unknown"]["label"] == "weird"
    assert result["empty"]["label"] == "已接收"


def test_render_message_deliveries_field_replaces_raw_status_tokens() -> None:
    result = _run_node_script(
        _COMMON_PRELUDE
        + """
        global.esc = (value) => String(value ?? "");
        const html = renderMessageDeliveriesField([
          { decision: "distributed", status: "delivered", target_title: "分支A", target_node_id: "node:a", message: "补充证据" },
          { decision: "distributed", status: "consumed", target_title: "分支B", target_node_id: "node:b", message: "同步口径" },
          { decision: "distributed", status: "consumed", merged_at: "2026-09-12T10:00:00+08:00", target_title: "分支C", target_node_id: "node:c", message: "并入演示" },
          { decision: "skipped", status: "", target_title: "分支D", target_node_id: "node:d", reason: "不受影响" },
        ]);
        const empty = renderMessageDeliveriesField([]);
        console.log(JSON.stringify({ html, empty }));
        """
    )
    html = result["html"]
    assert "已分发·待处理" in html
    assert "已消费" in html
    assert "已并入上下文" in html
    assert "未下发" in html
    assert "不受影响" in html
    # 旧实现会把账本原始状态文本直接拼成 [delivered]/[consumed]，必须不再出现。
    assert "[delivered]" not in html
    assert "[consumed]" not in html
    assert 'data-lucide="inbox"' in html
    assert 'data-lucide="circle-check"' in html
    assert 'data-lucide="merge"' in html
    assert 'data-lucide="circle-slash"' in html
    assert "无" in result["empty"]


def test_active_distribution_state_accepts_subtree_barrier_and_legacy_mode() -> None:
    result = _run_node_script(
        _COMMON_PRELUDE
        + """
        const base = {
          active_epoch_id: "epoch:1",
          state: "distributing",
          target_node_ids: ["node:t"],
          frontier_node_ids: ["node:t"],
          blocked_node_ids: ["node:t", "node:c"],
          pending_notice_node_ids: [],
          queued_epoch_count: 0,
          pending_mailbox_count: 0,
        };
        S.taskRuntimeSummary = { distribution: { ...base, mode: "subtree_barrier" } };
        const subtree = activeTaskDistributionState();
        S.taskRuntimeSummary = { distribution: { ...base, mode: "task_wide_barrier" } };
        const legacy = activeTaskDistributionState();
        S.taskRuntimeSummary = { distribution: { ...base, mode: "", blocked_node_ids: [], active_epoch_id: "" , state: ""} };
        const inactive = activeTaskDistributionState();
        console.log(JSON.stringify({
          subtreeUiMode: subtree?.ui_mode || "",
          legacyUiMode: legacy?.ui_mode || "",
          inactive: inactive,
        }));
        """
    )
    assert result["subtreeUiMode"] == "distribution"
    assert result["legacyUiMode"] == "distribution", "旧持久化 meta 的 task_wide_barrier 必须继续被识别"
    assert result["inactive"] is None


def test_notice_composer_usability_follows_node_state() -> None:
    result = _run_node_script(
        _COMMON_PRELUDE
        + """
        U.adNoticeComposer = { hidden: false };
        U.adNoticeInput = { disabled: false, placeholder: "" };
        U.adNoticeSend = { disabled: false, dataset: {} };
        S.currentTask = { status: "in_progress" };
        S.treeNodesById = {
          "node:exec": { node_id: "node:exec", node_kind: "execution", status: "in_progress" },
          "node:done": { node_id: "node:done", node_kind: "execution", status: "success" },
          "node:acc": { node_id: "node:acc", node_kind: "acceptance", status: "in_progress" },
        };

        const stateFor = (nodeId) => {
          S.selectedNodeId = nodeId;
          syncNoticeComposerState();
          return {
            hidden: U.adNoticeComposer.hidden,
            inputDisabled: U.adNoticeInput.disabled,
            sendDisabled: U.adNoticeSend.disabled,
            placeholder: U.adNoticeInput.placeholder,
          };
        };

        console.log(JSON.stringify({
          exec: stateFor("node:exec"),
          done: stateFor("node:done"),
          acc: stateFor("node:acc"),
          none: stateFor(""),
        }));
        """
    )
    assert result["exec"] == {
        "hidden": False,
        "inputDisabled": False,
        "sendDisabled": False,
        "placeholder": "向该节点子树追加定向通知…",
    }
    assert result["done"]["inputDisabled"] is True
    assert result["done"]["placeholder"] == "终态节点不接收通知"
    assert result["acc"]["inputDisabled"] is True
    assert result["acc"]["placeholder"] == "验收节点不能作为通知目标"
    assert result["none"]["hidden"] is True


def test_notice_composer_submit_posts_and_refreshes() -> None:
    result = _run_node_script(
        _COMMON_PRELUDE
        + """
        const calls = { notice: [], toasts: [], snapshotLoads: 0, agentRefresh: [] };
        U.adNoticeComposer = { hidden: false };
        U.adNoticeInput = { disabled: false, value: "补充验收口径", placeholder: "" };
        U.adNoticeSend = { disabled: false, dataset: {} };
        S.currentTask = { status: "in_progress" };
        S.currentTaskId = "task:test";
        S.selectedNodeId = "node:exec";
        S.treeNodesById = {
          "node:exec": { node_id: "node:exec", node_kind: "execution", status: "in_progress", title: "exec" },
        };
        global.showToast = (payload) => calls.toasts.push(payload);
        global.ApiClient = {
          appendTaskNodeNotice: async (taskId, nodeId, message) => {
            calls.notice.push({ taskId, nodeId, message });
            return { ok: true };
          },
        };
        global.loadTaskTreeSnapshot = async () => { calls.snapshotLoads += 1; return null; };
        global.findTreeNode = () => ({ node_id: "node:exec" });
        global.showAgent = async (node, options) => { calls.agentRefresh.push({ node: node.node_id, options }); };

        (async () => {
          await submitNodeNoticeComposer();
          console.log(JSON.stringify({
            notice: calls.notice,
            successToast: calls.toasts[0]?.title,
            snapshotLoads: calls.snapshotLoads,
            agentRefresh: calls.agentRefresh,
            inputValueCleared: U.adNoticeInput.value === "",
            busyCleared: U.adNoticeSend.dataset.busy === "",
          }));
        })();
        """
    )
    assert result["notice"] == [{"taskId": "task:test", "nodeId": "node:exec", "message": "补充验收口径"}]
    assert result["successToast"] == "定向通知已提交"
    assert result["snapshotLoads"] == 1
    assert result["agentRefresh"] == [{"node": "node:exec", "options": {"preserveViewState": True, "forceRefresh": True}}]
    assert result["inputValueCleared"] is True
    assert result["busyCleared"] is True
