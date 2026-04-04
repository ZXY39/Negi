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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        ensureTaskTreeSubtree("root", { force: true }).then((payload) => {
          console.log(JSON.stringify({
            requestCount,
            dirtyCleared: taskTreeParentIsDirty("root") === false,
            childIds: S.treeNodesById.root.auxiliary_child_ids,
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const trace = buildNodeExecutionTrace(
          {
            node_id: "node:test",
            goal: "inspect repository",
            final_output: "done",
          },
          {
            prompt: "inspect repository",
            final_output: "done",
            execution_trace_summary: {
              stages: [
                {
                  stage_goal: "inspect repository",
                  tool_calls: [
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
          stageCount: trace.stages.length,
          stageGoal: trace.stages[0]?.stage_goal || "",
          roundCount: trace.stages[0]?.rounds?.length || 0,
          toolName: trace.stages[0]?.rounds?.[0]?.tools?.[0]?.tool_name || "",
          outputText: trace.stages[0]?.rounds?.[0]?.tools?.[0]?.output_text || "",
        }));
        """
    )

    assert result["stageCount"] == 1
    assert result["stageGoal"] == "inspect repository"
    assert result["roundCount"] == 1
    assert result["toolName"] == "filesystem"
    assert result["outputText"] == "repo listing"


def test_build_execution_trace_steps_use_stage_goal_as_stage_title_without_duplicate_goal_field() -> None:
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
        global.esc = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const trace = buildNodeExecutionTrace(
          {
            node_id: "node:test",
            goal: "inspect repository",
          },
          {
            execution_trace_summary: {
              stages: [
                {
                  stage_goal: "full stage goal: locate entry, read context, organize evidence",
                  tool_calls: [
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
        const steps = buildExecutionTraceSteps(trace, { state: "in_progress" });
        const stageStep = steps[1];

        console.log(JSON.stringify({
          title: stageStep?.title || "",
          containsStageGoalField: String(stageStep?.bodyHtml || "").includes("\\u9636\\u6bb5\\u76ee\\u6807"),
          containsStatusField: String(stageStep?.bodyHtml || "").includes("\\u72b6\\u6001"),
          containsToolOutput: String(stageStep?.bodyHtml || "").includes("repo listing"),
        }));
        """
    )

    assert result["title"] == "full stage goal: locate entry, read context, organize evidence"
    assert result["containsStageGoalField"] is False
    assert result["containsStatusField"] is True
    assert result["containsToolOutput"] is True


def test_task_governance_view_model_marks_breathing_and_formats_history() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          taskGovernance: null,
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const governance = normalizeTaskGovernanceState({
          frozen: true,
          review_inflight: true,
          history: [
            {
              triggered_at: "2026-04-04T07:00:00+08:00",
              trigger_reason: "depth+1",
              trigger_snapshot: { max_depth: 2, total_nodes: 18 },
              decision: "cap_current_depth",
              decision_reason: "depth runaway",
              limited_depth: 2,
            },
            {
              triggered_at: "2026-04-04T06:30:00+08:00",
              trigger_reason: "node_count_double",
              trigger_snapshot: { max_depth: 1, total_nodes: 16 },
              decision: "allow",
              decision_reason: "breadth only",
            },
          ],
        });
        const view = buildTaskGovernanceViewModel(governance);
        console.log(JSON.stringify({
          breathing: view.breathing,
          statusLabel: view.statusLabel,
          historyCount: view.historyCount,
          firstDecision: view.items[0].decisionLabel,
          secondReason: view.items[1].decisionReason,
        }));
        """
    )

    assert result["breathing"] is True
    assert result["statusLabel"] == "监管中"
    assert result["historyCount"] == 2
    assert result["firstDecision"] == "限制深度"
    assert result["secondReason"] == "breadth only"
