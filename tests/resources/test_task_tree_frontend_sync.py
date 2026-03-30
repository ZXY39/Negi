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
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(completed.stdout.strip())


def test_children_snapshot_merge_preserves_existing_descendants() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.S = {
          currentTaskId: "task:test",
          taskNodeChildrenCache: {},
          taskNodeChildrenRequests: {},
          taskTreeHasFullSnapshot: true,
          dirtyParentsByNodeId: {},
          branchSyncInFlightByNodeId: {},
          branchSyncQueuedByNodeId: {},
          branchSyncTokenByNodeId: {},
          treeRoundSelectionsByNodeId: {},
          tree: null,
        };
        global.U = {};
        global.ApiClient = { getTaskNodeChildren: async () => ({}) };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        S.tree = buildTaskTreeNodeFromDetail({
          node_id: "root",
          title: "root",
          children: [
            {
              node_id: "a",
              title: "a",
              children: [
                { node_id: "a1", title: "a1", children: [{ node_id: "a1x", title: "a1x" }] },
                { node_id: "a2", title: "a2" },
              ],
            },
            {
              node_id: "b",
              title: "b",
              children: [{ node_id: "b1", title: "b1" }],
            },
          ],
        });

        applyTaskNodeChildrenSnapshot({
          parent_node_id: "root",
          round_id: "",
          default_round_id: "",
          rounds: [],
          items: [
            { node_id: "a", title: "a patched" },
            { node_id: "b", title: "b patched" },
            { node_id: "c", title: "c new" },
          ],
        }, { render: false });

        const a = findRawTaskTreeNode(S.tree, "a");
        const a1x = findRawTaskTreeNode(S.tree, "a1x");
        const b = findRawTaskTreeNode(S.tree, "b");
        console.log(JSON.stringify({
          rootChildren: rawTreeDirectChildren(S.tree).map((node) => node.node_id),
          aChildren: rawTreeDirectChildren(a).map((node) => node.node_id),
          bChildren: rawTreeDirectChildren(b).map((node) => node.node_id),
          a1xExists: !!a1x,
        }));
        """
    )

    assert result["rootChildren"] == ["a", "b", "c"]
    assert result["aChildren"] == ["a1", "a2"]
    assert result["bChildren"] == ["b1"]
    assert result["a1xExists"] is True


def test_dirty_parent_forces_children_request_instead_of_local_snapshot() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        let requestCount = 0;
        global.S = {
          currentTaskId: "task:test",
          taskNodeChildrenCache: {
            "root::default": {
              task_id: "task:test",
              parent_node_id: "root",
              round_id: "",
              default_round_id: "",
              rounds: [],
              items: [{ node_id: "stale-child", title: "stale child" }],
            },
          },
          taskNodeChildrenRequests: {},
          taskTreeHasFullSnapshot: true,
          dirtyParentsByNodeId: { root: true },
          branchSyncInFlightByNodeId: {},
          branchSyncQueuedByNodeId: {},
          branchSyncTokenByNodeId: {},
          treeRoundSelectionsByNodeId: {},
          tree: null,
        };
        global.U = {};
        global.ApiClient = {
          getTaskNodeChildren: async () => {
            requestCount += 1;
            return {
              task_id: "task:test",
              parent_node_id: "root",
              round_id: "",
              default_round_id: "",
              rounds: [],
              items: [{ node_id: "fresh-child", title: "fresh child", children_fingerprint: "" }],
            };
          },
        };
        global.showToast = () => {};
        global.isAbortLike = () => false;
        global.renderTree = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_task_view.js", "utf8");
        vm.runInThisContext(code);
        global.renderTree = () => {};

        S.tree = buildTaskTreeNodeFromDetail({
          node_id: "root",
          title: "root",
          children: [{ node_id: "stale-child", title: "stale child" }],
        });

        ensureTaskNodeChildren("root").then((payload) => {
          console.log(JSON.stringify({
            requestCount,
            dirtyCleared: taskTreeParentIsDirty("root") === false,
            itemIds: (payload.items || []).map((item) => item.node_id),
          }));
        });
        """
    )

    assert result["requestCount"] == 1
    assert result["dirtyCleared"] is True
    assert result["itemIds"] == ["fresh-child"]
