const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const VIEW_CODE = fs.readFileSync(VIEW_PATH, "utf8");

// 只切 indexTaskLiveFrames → mergeTaskLiveFrameDelta 这一段纯函数，不拖 DOM 桩。
// 锚点移动就报错，避免切片悄悄变成空串把用例跑成假绿。
const START_ANCHOR = "function indexTaskLiveFrames(";
const END_ANCHOR = "function liveFramesByNodeId(";

function loadFrameMerge() {
    const start = VIEW_CODE.indexOf(START_ANCHOR);
    const end = VIEW_CODE.indexOf(END_ANCHOR);
    assert.ok(start >= 0 && end > start, "frame-merge slice anchors moved");
    const slice = VIEW_CODE.slice(start, end);
    const S = { frontier: [], liveFrameMap: {} };
    const context = { S, Object, Array, Set, String, globalThis: {} };
    vm.createContext(context);
    vm.runInContext(`${slice}\nglobalThis.mergeTaskLiveFrameDelta = mergeTaskLiveFrameDelta;`, context, { filename: VIEW_PATH });
    return { S, merge: context.globalThis.mergeTaskLiveFrameDelta };
}

function frame(nodeId, extra = {}) {
    return { node_id: nodeId, phase: "before_model", stale: false, ...extra };
}

// 合并函数跑在 vm 上下文里，它返回的数组带着另一个 realm 的原型，
// deepEqual 会比原型而失败——所以断言只用身份比较与 Array.from 出来的原始值。
function ids(list) {
    return Array.from(list || []).map((item) => item?.node_id);
}

test("full summary still replaces the whole frame table", () => {
    const { S, merge } = loadFrameMerge();
    S.frontier = [frame("node:a")];
    S.liveFrameMap = { "node:a": S.frontier[0] };

    const frames = [frame("node:b"), frame("node:c")];
    const result = merge({ frames });

    assert.equal(result, frames);
    assert.equal(S.frontier, frames);
    assert.deepEqual(Object.keys(S.liveFrameMap).sort(), ["node:b", "node:c"]);
});

test("delta replaces one frame in place and keeps the order", () => {
    const { S, merge } = loadFrameMerge();
    const nodeA = frame("node:a", { stage_goal: "a" });
    const nodeB = frame("node:b", { stage_goal: "old" });
    S.frontier = [nodeA, nodeB];
    S.liveFrameMap = { "node:a": nodeA, "node:b": nodeB };

    const changed = frame("node:b", { stage_goal: "new" });
    const result = merge({ frames: null, frame: changed, staleNodeIds: [] });

    assert.equal(result.length, 2);
    assert.deepEqual(ids(result), ["node:a", "node:b"]);
    assert.equal(result[1], changed);
    assert.equal(S.liveFrameMap["node:b"], changed);
});

test("delta refreshes stale flags from the reported list", () => {
    const { S, merge } = loadFrameMerge();
    const nodeA = frame("node:a");
    const nodeB = frame("node:b");
    S.frontier = [nodeA, nodeB];
    S.liveFrameMap = { "node:a": nodeA, "node:b": nodeB };

    const changed = frame("node:b", { stale: false });
    merge({ frames: null, frame: changed, staleNodeIds: ["node:a"] });

    assert.equal(nodeA.stale, true);
    assert.equal(changed.stale, false);
});

test("delta for an unknown node appends instead of dropping it", () => {
    const { S, merge } = loadFrameMerge();
    const nodeA = frame("node:a");
    S.frontier = [nodeA];
    S.liveFrameMap = { "node:a": nodeA };

    const fresh = frame("node:new");
    const result = merge({ frames: null, frame: fresh, staleNodeIds: [] });

    assert.deepEqual(ids(result), ["node:a", "node:new"]);
    assert.equal(S.liveFrameMap["node:new"], fresh);
});

test("delta without a usable node id leaves the table untouched", () => {
    const { S, merge } = loadFrameMerge();
    const nodeA = frame("node:a");
    S.frontier = [nodeA];
    S.liveFrameMap = { "node:a": nodeA };

    const result = merge({ frames: null, frame: null, staleNodeIds: ["node:a"] });

    assert.equal(result.length, 1);
    assert.equal(nodeA.stale, true);
});
