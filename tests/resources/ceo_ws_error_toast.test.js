"use strict";

// CEO websocket 的 error 帧 → toast 这一条显示车道。服务端在这条道上只印 `code`
// （不带 message），所以文案必须长在显示侧：兜底句一旦与标题同源，操作者会读到
// 「连接失败 / 连接失败」这种既不说明发生了什么、也不说明该做什么的提示。

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { FRONTEND_DIR, readFrontendSources } = require("./support/frontend_sources.js");

const SOURCE = [
    ...readFrontendSources(["api_client"]),
    fs.readFileSync(path.join(FRONTEND_DIR, "ceo", "ws-router.js"), "utf8"),
].join("\n");

function buildHarness() {
    const toasts = [];
    const context = {
        console,
        URL,
        DOMException,
        FormData,
        AbortController,
        Map,
        Math,
        Date,
        JSON,
        Promise,
        Number,
        String,
        Object,
        Array,
        Error,
        RegExp,
        Set,
        encodeURIComponent,
        decodeURIComponent,
        fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }),
        setTimeout: () => 1,
        clearTimeout: () => {},
        setInterval: () => 1,
        clearInterval: () => {},
        showToast: (options) => { toasts.push(options); },
        activeSessionId: () => "web:shared",
    };
    context.window = context;
    context.window.location = { origin: "http://127.0.0.1:8123" };
    vm.createContext(context);
    vm.runInContext(SOURCE, context);
    context.S = { ceoWsLastErrorCode: "", ceoWsToken: 0 };
    return { context, toasts };
}

function frame(data) {
    return { type: "error", session_id: "web:shared", data };
}

test("session_not_found 报的是会话没了，不是连接失败", () => {
    const { context, toasts } = buildHarness();
    context.handleCeoWsErrorFrame(frame({ code: "session_not_found" }));
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].title, "会话已不存在");
    assert.equal(toasts[0].kind, "warn");
    assert.equal(toasts[0].text, "这条会话已经不在了（被删除，或作为渠道会话隐了身）。");
    assert.notEqual(toasts[0].text, toasts[0].title);
});

test("没见过的码仍落到连接失败这一档", () => {
    const { context, toasts } = buildHarness();
    context.handleCeoWsErrorFrame(frame({ code: "brand_new_fault" }));
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].title, "连接失败");
    assert.equal(toasts[0].kind, "error");
    assert.equal(toasts[0].text, "连接失败");
});

test("已配置的码自带文案时以服务端那句为准", () => {
    const { context, toasts } = buildHarness();
    context.handleCeoWsErrorFrame(frame({ code: "no_model_configured" }));
    assert.equal(toasts[0].title, "未配置模型");
    assert.match(toasts[0].text, /模型配置/);
    context.handleCeoWsErrorFrame(frame({ code: "session_not_found", message: "服务端写的那句" }));
    assert.equal(toasts[1].text, "服务端写的那句");
});

test("同一个码连到两次只报一次，换码再报", () => {
    const { context, toasts } = buildHarness();
    context.handleCeoWsErrorFrame(frame({ code: "session_not_found" }));
    context.handleCeoWsErrorFrame(frame({ code: "session_not_found" }));
    assert.equal(toasts.length, 1);
    context.handleCeoWsErrorFrame(frame({ code: "task_service_unavailable" }));
    assert.equal(toasts.length, 2);
});

test("没有码的 error 帧不弹提示", () => {
    const { context, toasts } = buildHarness();
    context.handleCeoWsErrorFrame(frame({ detail: "随手写的一句话" }));
    assert.equal(toasts.length, 0);
});
