const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { readFrontendSources } = require("./support/frontend_sources.js");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = readFrontendSources(["app"]).join("\n");

// CEO 车道上会改写「当前可见回合」的帧：这些帧必须自己划清会话归属，要么在
// dispatch 条件上比 effectiveSessionId === activeSessionId()，要么把
// effectiveSessionId 作为参数交给处理函数。少了这一层，后台会话的帧会拆掉
// 正在看的那个会话的回合（handleCeoError 会 finalizeCeoTurn 到 active session）。
const VISIBLE_TURN_FRAMES = [
    "ceo.turn.interrupt",
    "ceo.edit_fork.gates",
    "ceo.agent.tool",
    "ceo.error",
    "ceo.internal.ack",
    "ceo.reasoning.delta",
    "ceo.reply.delta",
    "ceo.reply.final",
    "ceo.turn.discard",
];

function dispatchSlice(typeName) {
    const marker = `if (payload.type === "${typeName}"`;
    const start = APP_CODE.indexOf(marker);
    assert.notEqual(start, -1, `${typeName} is no longer dispatched on the ceo ws lane`);
    const next = APP_CODE.indexOf("if (payload.type === ", start + marker.length);
    return APP_CODE.slice(start, next === -1 ? start + 600 : next);
}

test("every visible-turn CEO frame is scoped to one session", () => {
    const unscoped = VISIBLE_TURN_FRAMES.filter((typeName) => !dispatchSlice(typeName).includes("effectiveSessionId"));
    assert.deepEqual(unscoped, [], "these frames can rewrite the visible turn of another session");
});

test("handleCeoError tears down the turn it was called for", () => {
    const start = APP_CODE.indexOf("function handleCeoError(");
    const body = APP_CODE.slice(start, APP_CODE.indexOf("\nfunction ", start + 10));
    assert.match(body, /S\.ceoTurnActive = false;/);
    assert.match(body, /finalizeCeoTurn\(/);
});
