const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const viewCode = fs.readFileSync(VIEW_PATH, "utf8");

// org_graph_task_view.js 是经典脚本（挂全局），这里用自动补全的 Proxy 桩喂给它，
// 只为把纯函数取出来断言标签合同；不模拟渲染，渲染契约由 test_task_tree_frontend_sync.py 覆盖。
function autoStub() {
  const target = function () {};
  return new Proxy(target, {
    get: (_t, prop) => {
      if (prop === Symbol.toPrimitive || prop === "toString") return () => "";
      if (prop === "then") return undefined;
      return autoStub();
    },
    set: () => true,
    has: () => true,
    apply: () => autoStub(),
    construct: () => autoStub(),
  });
}

function loadExports(names) {
  // S 给空 treeNodesById：聚合读数会读渲染态剔除已终态的应冻节点，空表即退回快照分母
  // （终态剔除那条合同由 test_task_tree_frontend_sync.py 覆盖，这里不重演）。
  const context = { console, JSON, Math, Number, String, Boolean, Array, Object, Date, RegExp, Set, Map, isNaN, parseInt, parseFloat, S: { treeNodesById: {} } };
  context.window = context;
  context.document = autoStub();
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(
    `${viewCode}\nthis.__testExports = { ${names.join(", ")} };\n`,
    context,
  );
  return context.__testExports;
}

test("消息状态标签覆盖分发两档且不外泄原始 token", () => {
  const { messageStatusDescriptor } = loadExports(["messageStatusDescriptor"]);

  assert.equal(messageStatusDescriptor("received").label, "已接收");
  assert.equal(messageStatusDescriptor("frozen").label, "已冻结待释放");
  assert.equal(messageStatusDescriptor("pending").label, "待处理");
  assert.equal(messageStatusDescriptor("consumed").label, "已消费");
  assert.equal(messageStatusDescriptor("merged").label, "已并入上下文");
  assert.equal(messageStatusDescriptor("skipped").label, "未下发");
  assert.equal(messageStatusDescriptor("weird-token").label, "待确认");
  assert.equal(messageStatusDescriptor("").label, "待确认");
});

test("已接收不再充当未知值的兜底标签", () => {
  const { messageStatusDescriptor, messageListStatusDescriptor } = loadExports([
    "messageStatusDescriptor",
    "messageListStatusDescriptor",
  ]);

  // 兜底道曾是 `label: normalized || "已接收"`，会把原始账本 token 直接画成中文标签。
  assert.notEqual(messageStatusDescriptor("weird-token").label, "已接收");
  assert.notEqual(messageListStatusDescriptor("weird-token").label, "已接收");
  // delivered 是真实账本态，显示成语义标签而不是原文。
  assert.equal(messageStatusDescriptor("delivered").label, "已接收");
});

test("列表条目的 key 仍是视觉档位而不是语义名", () => {
  const { messageListStatusDescriptor } = loadExports(["messageListStatusDescriptor"]);

  // 分发情况行用的是语义 key（delivered/consumed/...），条目徽标用的是配色档位。
  // 两套词表被并成一套会静默改掉徽标配色合同。
  assert.equal(messageListStatusDescriptor("pending").key, "warning");
  assert.equal(messageListStatusDescriptor("received").key, "warning");
  assert.equal(messageListStatusDescriptor("frozen").key, "warning");
  assert.equal(messageListStatusDescriptor("merged").key, "info");
});

test("横幅聚合计数按应冻集给分母、按已停步给分子", () => {
  const { summarizeDistributionProgress } = loadExports(["summarizeDistributionProgress"]);

  const partial = summarizeDistributionProgress({
    state: "barrier_draining",
    blocked_node_ids: ["node:a", "node:b", "node:c"],
    frozen_node_ids: ["node:a", "node:b"],
  });
  assert.equal(partial.frozenCount, 2);
  assert.equal(partial.blockedCount, 3);
  assert.match(partial.text, /已停步 2\/3/);

  const settled = summarizeDistributionProgress({
    state: "distributing",
    blocked_node_ids: ["node:a", "node:b"],
    frozen_node_ids: ["node:a", "node:b"],
  });
  assert.equal(settled.frozenCount, 2);
  assert.match(settled.text, /已停步 2\/2/);
});

test("陈旧账本项不得让分子超过分母", () => {
  const { summarizeDistributionProgress } = loadExports(["summarizeDistributionProgress"]);

  // 排空账本可能留下已不在应冻集的节点（重推进、子树变化），计数必须取交集。
  const result = summarizeDistributionProgress({
    state: "barrier_draining",
    blocked_node_ids: ["node:a"],
    frozen_node_ids: ["node:a", "node:gone"],
  });
  assert.equal(result.frozenCount, 1);
  assert.equal(result.blockedCount, 1);
  assert.match(result.text, /已停步 1\/1/);
});

test("无应冻集时交回原文案", () => {
  const { summarizeDistributionProgress } = loadExports(["summarizeDistributionProgress"]);

  assert.equal(summarizeDistributionProgress({ state: "barrier_requested", blocked_node_ids: [] }).text, "");
  assert.equal(summarizeDistributionProgress(null).text, "");
});
