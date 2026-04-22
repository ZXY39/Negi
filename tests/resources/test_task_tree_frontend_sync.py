from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

from main.monitoring.query_service import TaskQueryService

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


def test_task_status_helpers_treat_unpassed_as_non_failed_without_continue_action() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        const appCode = fs.readFileSync("g3ku/web/frontend/org_graph_app.js", "utf8");
        const pStatusStart = appCode.indexOf("const pStatus");
        const helperStart = appCode.indexOf("const canPause");
        const helperEnd = appCode.indexOf("function normalizeTokenUsage");
        vm.runInThisContext(appCode.slice(pStatusStart, helperStart));
        vm.runInThisContext(appCode.slice(helperStart, helperEnd));

        global.taskWorkerControlsAvailable = () => true;

        const tasksCode = fs.readFileSync("g3ku/web/frontend/org_graph_tasks.js", "utf8");
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
    assert result["actions"] == ["delete"]


def test_task_status_helpers_ignore_legacy_continuation_metadata() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {};
        global.U = {};
        const appCode = fs.readFileSync("g3ku/web/frontend/org_graph_app.js", "utf8");
        const pStatusStart = appCode.indexOf("const pStatus");
        const helperStart = appCode.indexOf("const canPause");
        const helperEnd = appCode.indexOf("function normalizeTokenUsage");
        vm.runInThisContext(appCode.slice(pStatusStart, helperStart));
        vm.runInThisContext(appCode.slice(helperStart, helperEnd));

        global.taskWorkerControlsAvailable = () => true;

        const tasksCode = fs.readFileSync("g3ku/web/frontend/org_graph_tasks.js", "utf8");
        const labelStart = tasksCode.indexOf("function taskStatusLabel");
        const labelEnd = tasksCode.indexOf("function getSelectedTasks");
        const actionStart = tasksCode.indexOf("function taskActionTone");
        const actionEnd = tasksCode.indexOf("function taskActionSuccessTitle");
        vm.runInThisContext(tasksCode.slice(labelStart, labelEnd));
        vm.runInThisContext(tasksCode.slice(actionStart, actionEnd));

        const taskViewCode = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
    assert result["recreatedLabel"] == "Failed"
    assert result["recreatedSummary"] == ""
    assert result["recreatedActions"] == ["delete"]
    assert result["recreatedPrimary"] is None
    assert result["recreatedDetailLabel"] == "失败"
    assert result["retriedStatus"] == "in_progress"
    assert result["retriedLabel"] == "Running"
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
            prepared_message_count: idx + 2,
            prepared_message_chars: (idx + 1) * 100,
            response_tool_call_count: idx % 4,
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
        };
        global.U = {
          taskTokenContent: { innerHTML: "" },
          taskTokenSummaryText: { textContent: "" },
          taskTokenButton: { title: "" },
        };

        const appCode = fs.readFileSync("g3ku/web/frontend/org_graph_app.js", "utf8");
        const tokenStart = appCode.indexOf("const EMPTY_TOKEN_USAGE");
        const tokenEnd = appCode.indexOf("function ensureTaskTokenUi");
        vm.runInThisContext(appCode.slice(tokenStart, tokenEnd));

        const tasksCode = fs.readFileSync("g3ku/web/frontend/org_graph_tasks.js", "utf8");
        const tokenStatsStart = tasksCode.indexOf("function renderTaskTokenStats");
        const tokenStatsEnd = tasksCode.indexOf("async function loadTaskDetail");
        vm.runInThisContext(tasksCode.slice(tokenStatsStart, tokenStatsEnd));

        renderTaskTokenStats();
        const html = U.taskTokenContent.innerHTML;
        const tableBody = html.match(/<tbody>([\\s\\S]*?)<\\/tbody>/)?.[1] || "";
        const firstColumnValues = Array.from(tableBody.matchAll(/<tr>\\s*<td>([\\d,]+)<\\/td>/g))
          .map((match) => Number(String(match[1] || "").replaceAll(",", "")));

        console.log(JSON.stringify({
          headingLocalized: html.includes("模型调用明细"),
          paginationLocalized: html.includes("第 2/2 页") && html.includes("显示 101-135 / 共 135 条"),
          columnsLocalized: [
            "调用序号",
            "预处理字符数",
            "消息数",
            "新增输入 Token",
            "新增缓存命中",
            "命中率",
            "工具调用数",
            "模型",
          ].every((label) => html.includes(label)),
          rowCount: firstColumnValues.length,
          firstCallIndex: firstColumnValues[0],
          lastCallIndex: firstColumnValues[firstColumnValues.length - 1],
        }));
        """
    )

    assert result["headingLocalized"] is True
    assert result["paginationLocalized"] is True
    assert result["columnsLocalized"] is True
    assert result["rowCount"] == 35
    assert result["firstCallIndex"] == 35
    assert result["lastCallIndex"] == 1


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
                  tool_round_budget: 8,
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


def test_execution_stage_progress_ignores_non_budget_rounds_in_frontend_formatting() -> None:
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

        const progress = formatExecutionStageProgress({
          stage_total_steps: 5,
          tool_rounds_used: 1,
          rounds: [
            { round_id: "round-loader", budget_counted: false, tools: [{ tool_name: "load_skill_context" }] },
            { round_id: "round-budgeted", budget_counted: true, tools: [{ tool_name: "memory_note" }] },
          ],
        });

        console.log(JSON.stringify({ progress }));
        """
    )

    assert result["progress"] == "1/5"


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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
          normalStageTitle: String(steps[1]?.title || ""),
          spawnStageTitle: String(steps[2]?.title || ""),
          normalStageHasSelfMode: String(steps[1]?.bodyHtml || "").includes("\\u81ea\\u4e3b\\u6267\\u884c"),
          spawnStageHasWithChildrenMode: String(steps[2]?.bodyHtml || "").includes("\\u5305\\u542b\\u6d3e\\u751f"),
        }));
        """
    )

    assert "normal stage" in result["normalStageTitle"]
    assert "spawn stage" in result["spawnStageTitle"]
    assert result["normalStageHasSelfMode"] is False
    assert result["spawnStageHasWithChildrenMode"] is False


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
                  tool_round_budget: 7,
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
          stageTitle: String(steps[1]?.title || ""),
          stageHasWithChildrenMode: String(steps[1]?.bodyHtml || "").includes("\\u5305\\u542b\\u6d3e\\u751f"),
          stageHasRoundIndexLabel: String(steps[1]?.bodyHtml || "").includes("\\u7b2c 1 \\u8f6e"),
        }));
        """
    )

    assert "mixed stage" in result["stageTitle"]
    assert result["stageHasWithChildrenMode"] is False
    assert result["stageHasRoundIndexLabel"] is False


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

    assert result["roundClasses"] == []
    assert result["labels"][:2] == ["成功", "失败"]
    assert result["classes"][:2] == ["success", "error"]


def test_summary_execution_trace_preview_fields_render_tool_arguments_and_output() -> None:
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

        const trace = buildNodeExecutionTrace(
          { node_id: "node:test", goal: "remember preference" },
          {
            execution_trace_summary: {
              stages: [
                {
                  stage_goal: "remember preference",
                  mode: "自主执行",
                  status: "active",
                  rounds: [
                    {
                      round_id: "round:1",
                      round_index: 1,
                      tools: [
                        {
                          tool_call_id: "call-1",
                          tool_name: "memory_write",
                          arguments_preview: '{"facts":[{"attribute":"default_document_save_location"}]}',
                          output_preview: 'Error: facts[0] should be object',
                          status: "error",
                        },
                      ],
                    },
                  ],
                },
              ],
            },
          },
        );
        const html = renderExecutionStageRounds(trace.stages[0]);

        console.log(JSON.stringify({
          hasArgumentsPreview: html.includes("default_document_save_location"),
          hasOutputPreview: html.includes("facts[0] should be object"),
        }));
        """
    )

    assert result["hasArgumentsPreview"] is True
    assert result["hasOutputPreview"] is True


def test_summary_execution_trace_round_with_tool_names_only_renders_placeholder_tool_chip() -> None:
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

        const trace = buildNodeExecutionTrace(
          { node_id: "node:test", goal: "remember preference" },
          {
            execution_trace_summary: {
              stages: [
                {
                  stage_goal: "remember preference",
                  mode: "自主执行",
                  status: "active",
                  rounds: [
                    {
                      round_id: "round:2",
                      round_index: 2,
                      tool_names: ["memory_write"],
                      tool_call_ids: ["call-2"],
                      tools: [],
                    },
                  ],
                },
              ],
            },
          },
        );
        const html = renderExecutionStageRounds(trace.stages[0]);

        console.log(JSON.stringify({
          showsEmptyRoundPlaceholder: html.includes("本轮暂无工具记录"),
          hasToolChip: html.includes("memory_write"),
        }));
        """
    )

    assert result["showsEmptyRoundPlaceholder"] is False
    assert result["hasToolChip"] is True


def test_execution_trace_summary_drops_empty_round_shells_before_ui() -> None:
    summary = TaskQueryService._execution_trace_summary(
        {
            "stages": [
                {
                    "stage_id": "stage:1",
                    "stage_goal": "remember preference",
                    "tool_rounds_used": 2,
                    "rounds": [
                        {
                            "round_id": "round:phantom",
                            "round_index": 1,
                            "tool_names": ["memory_write"],
                            "tool_call_ids": ["call-phantom"],
                            "tools": [],
                        },
                        {
                            "round_id": "round:real",
                            "round_index": 2,
                            "tools": [
                                {
                                    "tool_call_id": "call-real",
                                    "tool_name": "filesystem",
                                    "arguments_text": "{\"path\":\".\"}",
                                    "output_text": "repo listing",
                                    "status": "success",
                                },
                            ],
                        },
                    ],
                },
            ],
        }
    )

    rounds = summary["stages"][0]["rounds"]

    assert [round_item["round_id"] for round_item in rounds] == ["round:real"]
    assert summary["stages"][0]["tool_calls"] == [
        {
            "tool_call_id": "call-real",
            "tool_name": "filesystem",
            "arguments_text": "{\"path\":\".\"}",
            "output_text": "repo listing",
            "output_ref": "",
            "status": "success",
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": None,
            "recovery_decision": "",
            "related_tool_call_ids": [],
            "attempted_tools": [],
            "evidence": [],
            "lost_result_summary": "",
        }
    ]


def test_summary_execution_trace_no_tool_records_skips_empty_round_shells() -> None:
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

        const trace = buildNodeExecutionTrace(
          { node_id: "node:test", goal: "remember preference" },
          {
            execution_trace: {
              stages: [
                {
                  stage_id: "stage:1",
                  stage_goal: "remember preference",
                  mode: "鑷富鎵ц",
                  status: "active",
                  rounds: [
                    {
                      round_id: "round:phantom",
                      round_index: 1,
                      tool_names: ["memory_write"],
                      tool_call_ids: ["call-phantom"],
                      tools: [],
                    },
                  ],
                },
              ],
            },
          },
        );
        const html = renderExecutionStageRounds(trace.stages[0]);

        console.log(JSON.stringify({
          roundCount: trace.stages[0]?.rounds?.length || 0,
          showsEmptyRoundPlaceholder: html.includes("鏈疆鏆傛棤宸ュ叿璁板綍"),
          hasToolChip: html.includes("memory_write"),
        }));
        """
    )

    assert result["roundCount"] == 0
    assert result["showsEmptyRoundPlaceholder"] is False
    assert result["hasToolChip"] is False


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
          adRoundSummary: { textContent: "" },
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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

        showAgent(
          {
            node_id: "node:1",
            status: "in_progress",
            visual_state: "in_progress",
            roundSummary: "",
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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
        const taskViewCode = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(taskViewCode);
        global.captureTaskDetailViewState = () => ({ scrollTop: 12 });
        global.stashTaskDetailViewState = () => {};
        global.findTreeNode = () => ({ node_id: "node:1", title: "Node 1", state: "failed" });
        global.showAgent = (node, options) => {
          showAgentCalls.push({ nodeId: String(node?.node_id || ""), forceRefresh: !!options?.forceRefresh });
          return Promise.resolve();
        };
        const tasksCode = fs.readFileSync("g3ku/web/frontend/org_graph_tasks.js", "utf8");
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


def test_render_tree_shows_distribution_notice_and_forces_yellow_connector_mode() -> None:
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
            tokens.filter(Boolean).forEach((token) => this.tokens.add(String(token)));
            this.owner.className = [...this.tokens].join(" ");
          }
          remove(...tokens) {
            tokens.filter(Boolean).forEach((token) => this.tokens.delete(String(token)));
            this.owner.className = [...this.tokens].join(" ");
          }
          contains(token) {
            return this.tokens.has(String(token));
          }
          toggle(token, force) {
            const normalized = String(token);
            const shouldAdd = force === undefined ? !this.tokens.has(normalized) : !!force;
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
        global.S = {
          currentTaskId: "task:test",
          currentTask: { metadata: {} },
          taskSummary: { active_node_count: 0, runnable_node_count: 0, waiting_node_count: 0 },
          taskRuntimeSummary: {
            distribution: {
              active_epoch_id: "epoch:demo",
              state: "distributing",
              frontier_node_ids: ["root"],
              queued_epoch_count: 0,
              pending_mailbox_count: 0,
            },
          },
          treeRootNodeId: "root",
          treeNodesById: {
            root: {
              node_id: "root",
              title: "Root",
              status: "in_progress",
              node_kind: "execution",
              rounds: [],
              auxiliary_child_ids: [],
              default_round_id: "",
            },
          },
          treeView: null,
          treeSelectedRoundByNodeId: {},
          treePan: {
            offsetX: 0,
            offsetY: 0,
            scale: 1,
            suppressClickNodeId: null,
          },
          selectedNodeId: null,
          taskNodeDetails: {},
          treeLargeMode: false,
        };
        global.U = {
          tree: new StubElement("div"),
          tdActiveCount: new StubElement("span"),
          taskTreeResetRounds: new StubElement("button"),
          taskSelectionEmpty: new StubElement("div"),
          detail: new StubElement("div"),
          nodeEmpty: new StubElement("div"),
        };
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        global.treeNormalizeInt = global.normalizeInt;
        global.esc = (value) => String(value ?? "");
        global.icons = () => {};
        global.setTaskDetailOpen = () => {};
        global.captureTaskDetailViewState = () => ({});
        global.stashTaskDetailViewState = () => {};
        global.scheduleTaskDetailSessionPersist = () => {};
        global.findTreeNode = () => null;
        global.resolveExecutionTreeDensity = () => ({ mode: "default", stats: { totalItems: 1, maxBreadth: 1 } });
        global.hasManualTreeRoundSelections = () => false;
        global.showAgent = () => Promise.resolve();
        global.enhanceResourceSelects = () => {};
        global.formatTokenCount = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        renderTree();

        const wrapper = U.tree.children.find((item) => item instanceof StubElement && String(item.className || "").includes("execution-tree"));
        const notice = U.tree.children.find((item) => item instanceof StubElement && String(item.className || "").includes("task-tree-distribution-bubble"));
        console.log(JSON.stringify({
          hasWrapper: !!wrapper,
          wrapperClassName: wrapper?.className || "",
          noticeText: notice?.textContent || "",
          childCount: U.tree.children.length,
        }));
        """
    )

    assert result["hasWrapper"] is True
    assert "execution-tree--distribution-active" in result["wrapperClassName"]
    assert result["noticeText"] == "接收到新消息，分发中"


def test_render_tree_shows_pending_notice_banner_when_distribution_already_completed() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        class StubElement {
          constructor(tag = "div") {
            this.tagName = tag.toUpperCase();
            this.children = [];
            this.className = "";
            this.dataset = {};
            this.style = {};
            this.hidden = false;
            this.attributes = {};
            this.parentNode = null;
            this.textContent = "";
            this.classList = {
              add: (...tokens) => {
                const set = new Set(String(this.className || "").split(/\\s+/).filter(Boolean));
                tokens.forEach((token) => set.add(String(token || "")));
                this.className = [...set].join(" ");
              },
              remove: (...tokens) => {
                const blocked = new Set(tokens.map((token) => String(token || "")));
                this.className = String(this.className || "")
                  .split(/\\s+/)
                  .filter((token) => token && !blocked.has(token))
                  .join(" ");
              },
              contains: (token) => String(this.className || "").split(/\\s+/).includes(String(token || "")),
              toggle: (token, force) => {
                const shouldAdd = force == null ? !this.classList.contains(token) : !!force;
                if (shouldAdd) this.classList.add(token);
                else this.classList.remove(token);
                return shouldAdd;
              },
            };
          }
          appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
          setAttribute(name, value) { this.attributes[name] = String(value); }
          querySelector() { return null; }
          querySelectorAll() { return []; }
          addEventListener() {}
          closest() { return null; }
        }
        global.Element = StubElement;
        global.HTMLElement = StubElement;
        global.document = {
          createElement: (tag) => new StubElement(tag),
        };
        global.S = {
          treeRootNodeId: "root",
          treeNodesById: {
            root: {
              node_id: "root",
              title: "root",
              status: "in_progress",
              node_kind: "execution",
              default_round_id: "",
              rounds: [],
              auxiliary_child_ids: [],
              pending_notice_count: 1,
            },
          },
          treeSelectedRoundByNodeId: {},
          treeView: {
            node_id: "root",
            title: "root",
            fullTitle: "root",
            state: "in_progress",
            visual_state: "in_progress",
            display_state: "进行中",
            rounds: [],
            children: [],
            selectedRoundId: "",
          },
          taskRuntimeSummary: { distribution: { active_epoch_id: "", state: "" } },
          treePan: { offsetX: 0, offsetY: 0, scale: 1 },
          selectedNodeId: "",
        };
        global.U = { tree: new StubElement("div") };
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.normalizeInt = (value, fallback = 0) => {
          const parsed = Number.parseInt(String(value ?? ""), 10);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        global.treeNormalizeInt = global.normalizeInt;
        global.esc = (value) => String(value ?? "");
        global.icons = () => {};
        global.setTaskDetailOpen = () => {};
        global.captureTaskDetailViewState = () => ({});
        global.stashTaskDetailViewState = () => {};
        global.scheduleTaskDetailSessionPersist = () => {};
        global.findTreeNode = () => null;
        global.resolveExecutionTreeDensity = () => ({ mode: "default", stats: { totalItems: 1, maxBreadth: 1 } });
        global.hasManualTreeRoundSelections = () => false;
        global.showAgent = () => Promise.resolve();
        global.enhanceResourceSelects = () => {};
        global.formatTokenCount = (value) => String(value ?? "");
        global.readableText = (value, { emptyText = "" } = {}) => {
          const text = String(value ?? "").trim();
          return text || emptyText;
        };
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        renderTree();

        const notice = U.tree.children.find((item) => item instanceof StubElement && String(item.className || "").includes("task-tree-distribution-bubble"));
        console.log(JSON.stringify({
          noticeText: notice?.textContent || "",
        }));
        """
    )

    assert result["noticeText"] == "接收到新消息，等待节点处理"


def test_build_execution_trace_steps_no_longer_inserts_notice_pseudo_stage() -> None:
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
        global.displayTaskStageStatus = (value) => String(value || "");
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const steps = buildExecutionTraceSteps(
          {
            initial_prompt: "root prompt",
            stages: [
              {
                stage_id: "stage:1",
                stage_index: 1,
                stage_goal: "collect sources",
                status: "completed",
                tool_round_budget: 3,
                tool_rounds_used: 1,
                rounds: [],
                tool_calls: [],
              },
            ],
          },
          {}
        );

        console.log(JSON.stringify({
          traceKeys: steps.map((item) => item.traceKey),
          titles: steps.map((item) => item.title),
        }));
        """
    )

    assert result["traceKeys"][0] == "initial_prompt"
    assert result["traceKeys"][1] == "stage:stage:1"
    assert "消息通知" not in result["titles"]


def test_build_node_message_list_steps_renders_message_and_distribution_details() -> None:
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        const steps = buildNodeMessageListSteps({
          message_list: [
            {
              notification_id: "notif:1",
              message: "改成男性角色Top20",
              received_at: "2026-04-19T20:28:17+08:00",
              status: "pending",
              deliveries: [
                {
                  target_node_id: "node:child-1",
                  target_title: "child one",
                  message: "改成男性角色Top20并补充证据",
                  status: "delivered",
                },
              ],
            },
          ],
        });

        console.log(JSON.stringify({
          traceKeys: steps.map((item) => item.traceKey),
          titles: steps.map((item) => item.title),
          status: steps[0]?.status || "",
          bodyHtml: steps[0]?.bodyHtml || "",
        }));
        """
    )

    assert result["traceKeys"] == ["message:notif:1"]
    assert "2026-04-19T20:28:17+08:00" in result["titles"][0]
    assert result["status"] == "warning"
    assert "改成男性角色Top20" in result["bodyHtml"]
    assert "child one" in result["bodyHtml"]
    assert "改成男性角色Top20并补充证据" in result["bodyHtml"]


def test_task_detail_html_renders_governance_panel() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert "task-governance-panel" in html
    assert "task-tree-floating-governance" in html


def test_governance_panel_collapsed_summary_only_shows_record_title_and_count() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          taskGovernance: {
            enabled: true,
            frozen: true,
            review_inflight: false,
            hard_limited_depth: null,
            history: [
              {
                triggered_at: "2026-04-12T10:00:00+08:00",
                trigger_reason: "depth threshold",
                trigger_snapshot: { max_depth: 4, total_nodes: 18 },
                decision: "cap_current_depth",
                decision_reason: "too deep",
                decision_evidence: ["depth=4"],
                limited_depth: 4,
                error_text: "",
              },
            ],
          },
          taskGovernanceExpanded: false,
        };
        const panelClasses = new Set();
        global.U = {
          taskGovernancePanel: {
            hidden: false,
            classList: {
              toggle(name, enabled) {
                if (enabled) panelClasses.add(name);
                else panelClasses.delete(name);
              },
            },
          },
          taskGovernanceSummary: { textContent: "" },
          taskGovernanceCount: { textContent: "" },
          taskGovernanceStatus: { textContent: "initial-status" },
          taskGovernanceDecision: { textContent: "initial-decision" },
          taskGovernanceHistory: {
            hidden: false,
            innerHTML: "",
            classList: { toggle() {} },
          },
        };
        global.ApiClient = {};
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        global.esc = (value) => String(value ?? "");
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);

        renderTaskGovernancePanel();

        console.log(JSON.stringify({
          summary: global.U.taskGovernanceSummary.textContent,
          count: global.U.taskGovernanceCount.textContent,
          status: global.U.taskGovernanceStatus.textContent,
          decision: global.U.taskGovernanceDecision.textContent,
          historyHidden: global.U.taskGovernanceHistory.hidden,
          isBreathing: panelClasses.has("is-breathing"),
          isExpanded: panelClasses.has("is-expanded"),
        }));
        """
    )

    assert result["summary"] == "监管记录"
    assert result["count"] == "1次"
    assert result["status"] == ""
    assert result["decision"] == ""
    assert result["historyHidden"] is True
    assert result["isBreathing"] is True
    assert result["isExpanded"] is False


def test_governance_panel_css_uses_edge_ring_for_collapsed_and_expanded_breathing() -> None:
    css_text = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert ".task-tree-floating-governance::after" in css_text
    assert "animation: ceo-session-breathe 2.4s cubic-bezier(0.4, 0, 0.2, 1) infinite;" in css_text
    assert ".task-tree-floating-governance.is-breathing::after" in css_text


def test_governance_panel_css_keeps_collapsed_summary_on_one_row_and_expands_wider_only_when_open() -> None:
    css_text = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    governance_match = re.search(
        r"\.task-tree-floating-governance\s*\{(?P<body>[^}]+)\}",
        css_text,
        flags=re.MULTILINE,
    )
    expanded_match = re.search(
        r"\.task-tree-floating-governance\.is-expanded\s*\{(?P<body>[^}]+)\}",
        css_text,
        flags=re.MULTILINE,
    )
    toggle_match = re.search(
        r"\.task-governance-toggle\s*\{(?P<body>[^}]+)\}",
        css_text,
        flags=re.MULTILINE,
    )

    assert governance_match is not None
    assert expanded_match is not None
    assert toggle_match is not None

    governance_block = governance_match.group("body")
    expanded_block = expanded_match.group("body")
    toggle_block = toggle_match.group("body")

    assert "width: fit-content;" in governance_block
    assert "max-width: calc(100% - 24px);" in governance_block
    assert "width: min(420px, calc(100% - 24px));" in expanded_block
    assert "grid-template-columns: minmax(0, 1fr) auto;" in toggle_block
    assert "white-space: nowrap;" in toggle_block


def test_governance_panel_css_optically_centers_count_chip_with_summary() -> None:
    css_text = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")
    count_match = re.search(
        r"\.task-governance-count\s*\{(?P<body>[^}]+)\}",
        css_text,
        flags=re.MULTILINE,
    )

    assert count_match is not None
    count_block = count_match.group("body")
    assert "align-self: center;" in count_block
    assert "transform: translateY(1px);" in count_block


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
          hasFilesystemTitle: html.includes(">filesystem<"),
          hasWebFetchTitle: html.includes(">web_fetch<"),
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
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
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


def test_ceo_composer_html_includes_local_compression_toast() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert 'id="ceo-compression-toast"' in html
    assert 'id="ceo-compression-toast-text"' in html
    assert "上下文压缩中" in html


def test_ceo_execution_trace_reuses_stage_round_helpers() -> None:
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    assert "function renderCeoStageTraceIntoTurn" in app_js
    assert "normalizeExecutionStageTrace(" in app_js
    assert "renderExecutionStageRounds(" in app_js
