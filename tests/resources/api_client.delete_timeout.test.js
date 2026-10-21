const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const CLIENT_PATH = "g3ku/web/frontend/api_client.js";
const CLIENT_CODE = readFrontendSources(["api_client"]).join("\n");

// 删除请求不允许带客户端超时：服务端要先 rmtree 目录再重建资源树，耗时不可预估。
// 10s 默认超时会把"已经删成功"的请求报成 Request timeout（实盘症状），
// 所以这里直接钉住"删除请求不排超时定时器"这条形状。
function buildHarness({ fetchImpl }) {
    const timers = [];
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
        fetch: fetchImpl,
        setTimeout: (fn, ms) => {
            const id = timers.length + 1;
            timers.push({ id, ms, fn });
            return id;
        },
        clearTimeout: (id) => {
            const entry = timers.find((item) => item.id === id);
            if (entry) entry.cleared = true;
        },
        setInterval: () => 1,
        clearInterval: () => {},
    };
    context.window = context;
    context.window.location = { origin: "http://127.0.0.1:8123" };
    vm.createContext(context);
    vm.runInContext(CLIENT_CODE, context);
    return { ApiClient: context.window.ApiClient, timers };
}

function jsonResponse(payload) {
    return {
        ok: true,
        status: 200,
        json: async () => payload,
    };
}

test("deleteTool does not arm a client-side timeout", async () => {
    const harness = buildHarness({ fetchImpl: async () => jsonResponse({ ok: true, item: {} }) });

    const pending = harness.ApiClient.deleteTool("demo_tool");

    // 定时器在 fetch 之前同步排上；删除车道必须一个都没有。
    assert.equal(harness.timers.length, 0);
    await pending;
    assert.equal(harness.timers.length, 0);
});

test("deleteSkill does not arm a client-side timeout", async () => {
    const harness = buildHarness({ fetchImpl: async () => jsonResponse({ ok: true, item: {} }) });

    const pending = harness.ApiClient.deleteSkill("demo_skill");

    assert.equal(harness.timers.length, 0);
    await pending;
    assert.equal(harness.timers.length, 0);
});

test("ordinary requests still time out at the default 10s", async () => {
    let abortSignal = null;
    const harness = buildHarness({
        fetchImpl: (_url, init) => new Promise((_resolve, reject) => {
            abortSignal = init.signal;
            init.signal.addEventListener("abort", () => {
                const error = new Error("aborted");
                error.name = "AbortError";
                reject(error);
            });
        }),
    });

    const pending = harness.ApiClient.getTools(0, 10);
    assert.equal(harness.timers.length, 1);
    assert.equal(harness.timers[0].ms, 10000);

    harness.timers[0].fn();
    await assert.rejects(pending, (error) => {
        assert.equal(error.name, "AbortError");
        assert.equal(error.isTimeout, true);
        assert.equal(error.message, "Request timeout");
        return true;
    });
    assert.ok(abortSignal && abortSignal.aborted);
});

test("deleteTool forwards the server-side failure instead of aborting it", async () => {
    const harness = buildHarness({
        fetchImpl: async () => ({
            ok: false,
            status: 409,
            json: async () => ({ detail: { code: "tool_busy", message: "工具正在执行，稍后再试" } }),
        }),
    });

    await assert.rejects(harness.ApiClient.deleteTool("demo_tool"), (error) => {
        assert.equal(error.code, "tool_busy");
        assert.equal(error.status, 409);
        return true;
    });
    assert.equal(harness.timers.length, 0);
});
