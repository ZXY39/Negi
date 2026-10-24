const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 前门执行流程头部：裸秒数、每秒自增、圆点与引导线同轴、在飞行不画轨道点。

const APP_CODE = readFrontendSources(["app"]).join("\n");
const CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8").replace(/\r\n/g, "\n");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.dataset = {};
    }
}

function loadApp() {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener: () => {},
            createElement: () => new StubHTMLElement(),
        },
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener: () => {},
        removeEventListener: () => {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = { formatCeoExecutionSeconds, refreshCeoFlowUsage, refreshLiveDurationBadges, S, HTMLElement };`,
        context
    );
    return context.__testExports;
}

function ledgerTurn() {
    return {
        tools: [{
            started_at: "2026-10-24T12:00:00+08:00",
            finished_at: "2026-10-24T12:01:00+08:00",
        }],
    };
}

function makeTurn(HTMLElementCtor, { liveRows = [] } = {}) {
    return {
        flowEl: { hidden: false },
        flowUsageEl: { textContent: "" },
        liveToolStepsEl: { querySelectorAll: () => liveRows },
        lastExecutionTraceSummary: { turns: [ledgerTurn()] },
    };
}

test("flow header duration stays in seconds across minutes and hours", () => {
    const { formatCeoExecutionSeconds } = loadApp();

    assert.equal(formatCeoExecutionSeconds(9.42), "9.4s");
    assert.equal(formatCeoExecutionSeconds(59.6), "60s");
    assert.equal(formatCeoExecutionSeconds(172), "172s");
    assert.equal(formatCeoExecutionSeconds(3661), "3661s");
    assert.equal(formatCeoExecutionSeconds(0), "");
});

test("flow header adds the in-flight tool's seconds without a full re-render", () => {
    const api = loadApp();
    const startedAt = new Date(Date.now() - 5000).toISOString();
    const liveRow = new api.HTMLElement();
    liveRow.dataset = { stepState: "running", startedAt, elapsedSeconds: "5" };
    const turn = makeTurn(api.HTMLElement, { liveRows: [liveRow] });
    const { flowUsageEl } = turn;

    api.refreshCeoFlowUsage(turn);
    assert.equal(flowUsageEl.textContent, "65s");

    api.S.ceoPendingTurns = [turn];
    api.refreshLiveDurationBadges();
    assert.equal(flowUsageEl.textContent, "65s");
});

test("live tool rows carry no rail status dot", () => {
    const match = /\.task-trace-live-steps\s+\.interaction-step::before\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(match, "live-step dot rule not found");
    assert.match(match[1], /display:\s*none/);
});

test("flow chevron dot shares the guide line's center", () => {
    const dot = /\.interaction-flow-chevron::before\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(dot, "chevron dot rule not found");
    assert.match(dot[1], /transform:\s*translateX\(0\.5px\)/);

    const guide = /\.interaction-flow-guide\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(guide, "guide rule not found");
    assert.match(guide[1], /left:\s*8px/);
    assert.match(guide[1], /width:\s*1px/);
});
