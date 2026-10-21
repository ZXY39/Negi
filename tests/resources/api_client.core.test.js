"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const CLIENT_CODE = readFrontendSources(["api_client"]).join("\n");

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
            timers.push({ id, ms, fn, cleared: false });
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
    return { ApiClient: context.ApiClient, timers };
}

function jsonResponse(payload, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => payload };
}

test("默认请求在 10 秒排超时，超时后按 Request timeout 拒绝", async () => {
    let seenSignal = null;
    const { ApiClient, timers } = buildHarness({
        fetchImpl: (_url, init) => new Promise((_resolve, reject) => {
            seenSignal = init.signal;
            seenSignal.addEventListener("abort", () => reject(seenSignal.reason));
        }),
    });
    const pending = ApiClient.get("/api/example");
    assert.equal(timers.length, 1);
    assert.equal(timers[0].ms, 10000);
    timers[0].fn();
    await assert.rejects(pending, (error) => {
        assert.equal(error.name, "AbortError");
        assert.equal(error.message, "Request timeout");
        assert.equal(error.isTimeout, true);
        return true;
    });
    assert.equal(timers[0].cleared, true);
});

test("timeoutMs: 0 表示不排客户端超时", async () => {
    const { ApiClient, timers } = buildHarness({
        fetchImpl: async () => jsonResponse({ ok: true }),
    });
    const payload = await ApiClient._request("DELETE", "/api/example", { timeoutMs: 0 });
    assert.deepEqual(payload, { ok: true });
    assert.equal(timers.length, 0);
});

test("同一 requestKey 的新请求会中止在途旧请求", async () => {
    const signals = [];
    const { ApiClient } = buildHarness({
        fetchImpl: (_url, init) => new Promise((resolve, reject) => {
            signals.push(init.signal);
            init.signal.addEventListener("abort", () => reject(init.signal.reason));
            if (signals.length === 2) resolve(jsonResponse({ ok: true, seq: 2 }));
        }),
    });
    const first = ApiClient._request("GET", "/api/example", { requestKey: "same" });
    const second = ApiClient._request("GET", "/api/example", { requestKey: "same" });
    await assert.rejects(first, (error) => {
        assert.equal(error.name, "AbortError");
        assert.equal(error.message, "Request aborted");
        assert.equal(error.isTimeout, false);
        return true;
    });
    assert.deepEqual(await second, { ok: true, seq: 2 });
});

test("非 2xx 响应把 detail 与错误码归一成 Error", async () => {
    const { ApiClient } = buildHarness({
        fetchImpl: async () => jsonResponse({ detail: { code: "session_not_found", message: "会话不存在" } }, 404),
    });
    await assert.rejects(ApiClient.get("/api/example"), (error) => {
        assert.equal(error.code, "session_not_found");
        assert.equal(error.status, 404);
        assert.equal(error.message, "会话不存在");
        assert.deepEqual(error.data, { code: "session_not_found", message: "会话不存在" });
        return true;
    });
});
