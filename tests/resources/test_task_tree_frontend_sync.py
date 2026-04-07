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


def test_build_node_execution_trace_preserves_summary_stage_budget() -> None:
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
          },
          {
            execution_trace_summary: {
              stages: [
                {
                  stage_goal: "inspect repository",
                  tool_round_budget: 5,
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
          stageTotalSteps: trace.stages[0]?.stage_total_steps ?? null,
        }));
        """
    )

    assert result["stageTotalSteps"] == 5


def test_build_node_execution_trace_preserves_summary_round_boundaries() -> None:
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
          },
          {
            execution_trace_summary: {
              stages: [
                {
                  stage_goal: "inspect repository",
                  tool_round_budget: 4,
                  tool_rounds_used: 2,
                  rounds: [
                    {
                      round_id: "round-1",
                      round_index: 1,
                      budget_counted: true,
                      tools: [
                        {
                          tool_name: "filesystem",
                          arguments_text: "{\\"path\\": \\".\\"}",
                          output_text: "repo listing",
                          status: "success",
                        },
                      ],
                    },
                    {
                      round_id: "round-2",
                      round_index: 2,
                      budget_counted: true,
                      tools: [
                        {
                          tool_name: "content",
                          arguments_text: "{\\"ref\\": \\"artifact:1\\"}",
                          output_text: "file contents",
                          status: "success",
                        },
                      ],
                    },
                  ],
                },
              ],
            },
          },
        );

        console.log(JSON.stringify({
          roundCount: trace.stages[0]?.rounds?.length || 0,
          firstTool: trace.stages[0]?.rounds?.[0]?.tools?.[0]?.tool_name || "",
          secondTool: trace.stages[0]?.rounds?.[1]?.tools?.[0]?.tool_name || "",
          roundsUsed: trace.stages[0]?.tool_rounds_used ?? null,
        }));
        """
    )

    assert result["roundCount"] == 2
    assert result["firstTool"] == "filesystem"
    assert result["secondTool"] == "content"
    assert result["roundsUsed"] == 2


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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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


def test_build_execution_trace_steps_use_stage_goal_as_stage_title_without_duplicate_goal_or_status_field() -> None:
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
    assert result["containsStatusField"] is False
    assert result["containsToolOutput"] is True


def test_build_execution_trace_steps_label_summary_rounds_by_spawn_presence() -> None:
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
                  stage_goal: "normal stage",
                  mode: "自主执行",
                  tool_calls: [
                    {
                      tool_name: "filesystem",
                      arguments_text: "{\\"path\\": \\".\\"}",
                      output_text: "repo listing",
                    },
                  ],
                },
                {
                  stage_goal: "spawn stage",
                  mode: "包含派生",
                  tool_calls: [
                    {
                      tool_name: "spawn_child_nodes",
                      arguments_text: "{\\"children\\": 3}",
                      output_text: "spawned",
                    },
                  ],
                },
              ],
            },
          },
        );
        const steps = buildExecutionTraceSteps(trace, { state: "in_progress" });

        console.log(JSON.stringify({
          normalStageHasSelfMode: String(steps[1]?.bodyHtml || "").includes("\\u81ea\\u4e3b\\u6267\\u884c"),
          normalStageHasDerivedLabel: String(steps[1]?.bodyHtml || "").includes("\\u6d3e\\u751f\\u8282\\u70b9"),
          spawnStageHasWithChildrenMode: String(steps[2]?.bodyHtml || "").includes("\\u5305\\u542b\\u6d3e\\u751f"),
          spawnStageHasDerivedLabel: String(steps[2]?.bodyHtml || "").includes("\\u6d3e\\u751f\\u8282\\u70b9"),
        }));
        """
    )

    assert result["normalStageHasSelfMode"] is True
    assert result["normalStageHasDerivedLabel"] is False
    assert result["spawnStageHasWithChildrenMode"] is True
    assert result["spawnStageHasDerivedLabel"] is False


def test_build_execution_trace_steps_label_mixed_full_round_as_with_children() -> None:
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
            execution_trace: {
              stages: [
                {
                  stage_id: "stage:test",
                  stage_index: 1,
                  mode: "包含派生",
                  status: "完成",
                  stage_goal: "mixed stage",
                  tool_round_budget: 3,
                  tool_rounds_used: 1,
                  rounds: [
                    {
                      round_id: "round:1",
                      round_index: 1,
                      budget_counted: true,
                      tools: [
                        {
                          tool_name: "filesystem",
                          arguments_text: "{\\"path\\": \\".\\"}",
                          output_text: "repo listing",
                          status: "success",
                        },
                        {
                          tool_name: "spawn_child_nodes",
                          arguments_text: "{\\"children\\": 2}",
                          output_text: "spawned",
                          status: "success",
                        },
                      ],
                    },
                  ],
                },
              ],
            },
          },
        );
        const steps = buildExecutionTraceSteps(trace, { state: "completed" });

        console.log(JSON.stringify({
          stageHasWithChildrenMode: String(steps[1]?.bodyHtml || "").includes("\\u5305\\u542b\\u6d3e\\u751f"),
          stageHasRoundIndexLabel: String(steps[1]?.bodyHtml || "").includes("\\u7b2c 1 \\u8f6e"),
          stageHasDerivedLabel: String(steps[1]?.bodyHtml || "").includes("\\u6d3e\\u751f\\u8282\\u70b9"),
        }));
        """
    )

    assert result["stageHasWithChildrenMode"] is True
    assert result["stageHasRoundIndexLabel"] is False
    assert result["stageHasDerivedLabel"] is False


def test_summary_execution_trace_defaults_running_when_stage_or_tool_lacks_completion_signal() -> None:
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
                  stage_goal: "launch child researchers",
                  tool_calls: [
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
          stageStatus: trace.stages[0]?.status || "",
          toolStatus: trace.stages[0]?.rounds?.[0]?.tools?.[0]?.status || "",
        }));
        """
    )

    assert result["stageStatus"] == "\u8fdb\u884c\u4e2d"
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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


def test_render_execution_stage_rounds_show_completed_round_and_tool_result_labels() -> None:
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const html = renderExecutionStageRounds({
          stage_id: "stage:test",
          mode: "自主执行",
          rounds: [
            {
              round_id: "round:1",
              round_index: 1,
              tools: [
                {
                  tool_name: "filesystem",
                  status: "success",
                  arguments_text: "{\\"path\\": \\".\\"}",
                  output_text: "repo listing",
                },
                {
                  tool_name: "web_fetch",
                  status: "error",
                  arguments_text: "{\\"url\\": \\"https://example.com\\"}",
                  output_text: "fetch failed",
                },
              ],
            },
          ],
        });
        const labels = [...html.matchAll(/task-trace-round-chip-status\">([^<]+)</g)].map((match) => match[1]);
        const classes = [...html.matchAll(/task-trace-round-chip\\s+([^\"\\s]+)/g)].map((match) => match[1]);
        const roundClasses = [...html.matchAll(/task-trace-step\\s+([^\"\\s]+)/g)].map((match) => match[1]);

        console.log(JSON.stringify({
          labels,
          classes,
          roundClasses,
        }));
        """
    )

    assert result["roundClasses"][:1] == ["success"]
    assert result["labels"][:2] == ["成功", "失败"]
    assert result["classes"][:2] == ["success", "error"]
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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


def test_render_execution_stage_rounds_show_recovery_check_panel_fields() -> None:
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const html = renderExecutionStageRounds({
          stage_id: "stage:test",
          mode: "自主执行",
          rounds: [
            {
              round_id: "round:1",
              round_index: 1,
              tools: [
                {
                  tool_call_id: "recovery_check:round:1",
                  tool_name: "recovery_check",
                  status: "warning",
                  output_text: "Recovery check executed before resuming interrupted tool round.",
                  recovery_decision: "model_decide",
                  attempted_tools: ["exec"],
                  lost_result_summary: "The previous exec attempt may have already produced side effects.",
                  evidence: [
                    { kind: "file", path: "D:/tmp/demo.txt", note: "file still exists" },
                  ],
                },
                {
                  tool_call_id: "call:exec",
                  tool_name: "exec",
                  status: "interrupted",
                  arguments_text: "{\\"command\\": \\"git apply patch.diff\\"}",
                  output_text: "Recovery check: the previous exec attempt may have already produced side effects.",
                },
              ],
            },
          ],
        });

        console.log(JSON.stringify({
          hasWarningChip: html.includes('task-trace-round-chip warning'),
          hasInterruptedChip: html.includes('task-trace-round-chip interrupted'),
          hasRecoveryDecision: html.includes("恢复检查结论"),
          hasAttemptedTools: html.includes("之前尝试执行了"),
          hasEvidence: html.includes("证据摘要"),
        }));
        """
    )

    assert result["hasWarningChip"] is True
    assert result["hasInterruptedChip"] is True
    assert result["hasRecoveryDecision"] is True
    assert result["hasAttemptedTools"] is True
    assert result["hasEvidence"] is True


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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
        const code = fs.readFileSync("g3ku/web/frontend/api_client.js", "utf8");
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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


def test_task_detail_html_does_not_render_governance_panel() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert "task-governance-panel" not in html
    assert "task-tree-floating-governance" not in html


def test_build_spawn_review_trace_steps_formats_blocked_and_allowed_results() -> None:
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const steps = buildSpawnReviewTraceSteps([
          {
            round_id: "call:spawn-1",
            reviewed_at: "2026-04-06T12:00:00+08:00",
            requested_specs: [
              { goal: "blocked branch", prompt: "blocked prompt", execution_policy: { mode: "focus" } },
              { goal: "allowed branch", prompt: "allowed prompt", execution_policy: { mode: "coverage" } },
            ],
            allowed_indexes: [1],
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
                synthetic_result_summary: "派生已被拦截：拆分过细，偏离当前父节点目标",
              },
              {
                index: 1,
                goal: "allowed branch",
                review_decision: "allowed",
                child_node_id: "node:child-1",
              },
            ],
          },
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
    assert "blocked branch" in result["body"]
    assert "allowed branch" in result["body"]
    assert "拆分过细，偏离当前父节点目标" in result["body"]
    assert "请由父节点直接执行" in result["body"]


    assert "杩斿洖鎽樿" not in result["body"]
    assert "娲剧敓宸茶鎷︽埅锛氭媶鍒嗚繃缁嗭紝鍋忕褰撳墠鐖惰妭鐐圭洰鏍?" not in result["body"]
    assert result["body"].count('class="task-trace-label"') == 4
    assert result["showStatus"] is False
    assert result["hasStatusBadge"] is False


def test_build_execution_trace_steps_excludes_spawn_review_rounds() -> None:
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
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const steps = buildExecutionTraceSteps({
          initial_prompt: "root prompt",
          tool_steps: [],
          stages: [
            {
              stage_id: "stage:1",
              stage_index: 1,
              mode: "自主执行",
              status: "完成",
              stage_goal: "阶段目标",
              rounds: [],
            },
          ],
        }, {
          spawn_review_rounds: [
            {
              round_id: "call:spawn-1",
              reviewed_at: "2026-04-06T12:00:00+08:00",
              entries: [],
            },
          ],
        });
        console.log(JSON.stringify({
          count: steps.length,
          titles: steps.map((item) => item?.title || ""),
        }));
        """
    )

    assert result["count"] == 2
    assert result["titles"][0] == "初始提示词"
    assert "派生记录" not in "\n".join(result["titles"])


def test_render_execution_stage_rounds_use_horizontal_strip_and_full_width_panel() -> None:
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const html = renderExecutionStageRounds({
          stage_id: "stage:test",
          mode: "鑷富鎵ц",
          rounds: [
            {
              round_id: "round:1",
              round_index: 1,
              tools: [
                {
                  tool_name: "filesystem",
                  status: "success",
                  arguments_text: "{\\"path\\": \\".\\"}",
                  output_text: "repo listing",
                },
                {
                  tool_name: "web_fetch",
                  status: "success",
                  arguments_text: "{\\"url\\": \\"https://example.com\\"}",
                  output_text: "fetch result",
                },
              ],
            },
          ],
        });

        console.log(JSON.stringify({
          hasStripContainer: html.includes("task-trace-round-strip"),
          chipCount: (html.match(/class=\\"task-trace-round-chip\\s/g) || []).length,
          hasDetailPanel: html.includes("task-trace-round-panel"),
          hasFilesystemTitle: html.includes("工具 · filesystem"),
          hasWebFetchTitle: html.includes("工具 · web_fetch"),
        }));
        """
    )

    assert result["hasStripContainer"] is True
    assert result["chipCount"] == 2
    assert result["hasDetailPanel"] is True
    assert result["hasFilesystemTitle"] is True
    assert result["hasWebFetchTitle"] is True


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
