const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 前门执行流程头部：本轮总时长（首个请求 → 最终文本输出），整秒自增；圆点与引导线同轴；在飞行不画轨道点。

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
    const probe = {
        fontSize: "13.6px",
        metrics: { fontAsc: 13, fontDesc: 4, inkAsc: 10, inkDesc: 0 },
    };
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
        getComputedStyle: () => ({
            fontStyle: "normal",
            fontWeight: "600",
            fontSize: probe.fontSize,
            fontFamily: "Fira Sans",
        }),
        document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener: () => {},
            createElement: (tag) => {
                if (String(tag).toLowerCase() === "canvas") {
                    return {
                        getContext: () => ({
                            font: "",
                            measureText: () => ({
                                fontBoundingBoxAscent: probe.metrics.fontAsc,
                                fontBoundingBoxDescent: probe.metrics.fontDesc,
                                actualBoundingBoxAscent: probe.metrics.inkAsc,
                                actualBoundingBoxDescent: probe.metrics.inkDesc,
                            }),
                        }),
                    };
                }
                return new StubHTMLElement();
            },
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
        `${APP_CODE}\nthis.__testExports = { formatCeoExecutionSeconds, ceoTurnElapsedSeconds, refreshCeoFlowUsage, refreshLiveDurationBadges, buildCeoRequestedAtByTurn, ceoFlowDotLiftPx, syncCeoFlowDotGeometry, setCeoTurnUsage, S, HTMLElement };`,
        context
    );
    return { ...context.__testExports, __probe: probe, __document: context.document };
}

const ISO = (ms) => new Date(ms).toISOString();

function makeTurn({ requestedAt = "", completedAt = "", finalized = false, source = "user", tools = [] } = {}) {
    return {
        source,
        finalized,
        flowEl: { hidden: false },
        flowUsageEl: { textContent: "" },
        requestedAt,
        completedAt,
        liveToolStepsEl: null,
        lastExecutionTraceSummary: tools.length ? { turns: [{ tools }] } : null,
    };
}

test("flow header duration stays whole seconds across minutes and hours", () => {
    const { formatCeoExecutionSeconds } = loadApp();

    assert.equal(formatCeoExecutionSeconds(0.4), "0s");
    assert.equal(formatCeoExecutionSeconds(59.9), "59s");
    assert.equal(formatCeoExecutionSeconds(172.4), "172s");
    assert.equal(formatCeoExecutionSeconds(3661), "3661s");
    assert.equal(formatCeoExecutionSeconds(0), "");
});

test("turn duration runs from the first request to the final output", () => {
    const { ceoTurnElapsedSeconds } = loadApp();
    const nowMs = Date.parse("2026-10-25T10:00:00+08:00");

    // 在飞：从请求时刻算到"现在"。
    const live = makeTurn({ requestedAt: ISO(nowMs - 65000) });
    assert.equal(ceoTurnElapsedSeconds(live, { nowMs }), 65);

    // 收尾：终点落在 completedAt，不再跟着时钟走。
    const done = makeTurn({
        requestedAt: ISO(nowMs - 65000),
        completedAt: ISO(nowMs - 23000),
        finalized: true,
    });
    assert.equal(ceoTurnElapsedSeconds(done, { nowMs }), 42);

    // 历史行没有完成时间：不给"到现在"的假读数。
    const stale = makeTurn({ requestedAt: ISO(nowMs - 65000), finalized: true, source: "history" });
    assert.equal(ceoTurnElapsedSeconds(stale, { nowMs }), null);

    // 在飞回合切出的头段：收不到 final 帧，按"到现在"继续走，与本轮续跑段同读数。
    const head = makeTurn({ requestedAt: ISO(nowMs - 65000), finalized: true });
    assert.equal(ceoTurnElapsedSeconds(head, { nowMs }), 65);
});

test("turn duration falls back to the earliest tool start when there is no request row", () => {
    const { ceoTurnElapsedSeconds } = loadApp();
    const nowMs = Date.parse("2026-10-25T10:00:00+08:00");
    const turn = makeTurn({
        tools: [
            { started_at: ISO(nowMs - 30000), finished_at: ISO(nowMs - 20000) },
            { started_at: ISO(nowMs - 50000), finished_at: ISO(nowMs - 45000) },
        ],
    });

    assert.equal(ceoTurnElapsedSeconds(turn, { nowMs }), 50);
});

test("flow header renders the turn duration and the live ticker refreshes it", () => {
    const api = loadApp();
    const turn = makeTurn({ requestedAt: ISO(Date.now() - 65000) });

    api.refreshCeoFlowUsage(turn);
    assert.equal(turn.flowUsageEl.textContent, "65s");

    turn.flowUsageEl.textContent = "";
    api.S.ceoPendingTurns = [turn];
    api.refreshLiveDurationBadges();
    assert.equal(turn.flowUsageEl.textContent, "65s");
});

test("completion time freezes the header at the final duration", () => {
    const api = loadApp();
    const turn = makeTurn({ requestedAt: ISO(Date.now() - 65000) });

    api.refreshCeoFlowUsage(turn);
    assert.equal(turn.flowUsageEl.textContent, "65s");

    api.setCeoTurnUsage(turn, null, { completedAt: ISO(Date.now() - 23000) });
    assert.equal(turn.flowUsageEl.textContent, "42s");
});

test("history pairing takes the earliest request row of a turn", () => {
    const { buildCeoRequestedAtByTurn } = loadApp();
    const map = buildCeoRequestedAtByTurn([
        { role: "user", turn_id: "t1", timestamp: "2026-10-25T10:00:09+08:00" },
        { role: "user", turn_id: "t1", timestamp: "2026-10-25T10:00:03+08:00" },
        { role: "assistant", turn_id: "t1", timestamp: "2026-10-25T10:01:00+08:00", turns: [] },
        { role: "user", turn_id: "t2", timestamp: "2026-10-25T11:00:00+08:00" },
    ]);

    assert.equal(map.get("t1"), Date.parse("2026-10-25T10:00:03+08:00"));
    assert.equal(map.get("t2"), Date.parse("2026-10-25T11:00:00+08:00"));
});

test("dot lift follows the measured font metrics", () => {
    const api = loadApp();
    const usage = new api.HTMLElement();

    // Fira Sans：墨迹中心比行框中心高半像素。
    assert.equal(api.ceoFlowDotLiftPx(usage), 0.5);

    // 换一个下伸部更深的字体：同样的数字墨高需要抬 1.5px。
    api.__probe.fontSize = "20px";
    api.__probe.metrics = { fontAsc: 16, fontDesc: 8, inkAsc: 12, inkDesc: 1 };
    assert.equal(api.ceoFlowDotLiftPx(usage), 1.5);
});

test("live tool rows carry no rail status dot", () => {
    const match = /\.task-trace-live-steps\s+\.interaction-step::before\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(match, "live-step dot rule not found");
    assert.match(match[1], /display:\s*none/);
});

test("flow dot and guide line are painted from one box", () => {
    const usage = /\.interaction-flow-usage\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(usage, "usage rule not found");
    assert.match(usage[1], /padding-left:\s*42px/);

    const guide = /\.interaction-flow-guide\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(guide, "guide rule not found");
    assert.match(guide[1], /left:\s*calc\(var\(--ceo-flow-dot-x, 8\.5px\) - 4\.5px\)/);
    assert.match(guide[1], /top:\s*calc\(var\(--ceo-flow-dot-y, 23\.5px\) - 4\.5px\)/);
    assert.match(guide[1], /width:\s*9px/);

    const dot = /\.interaction-flow-guide::before\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(dot, "dot rule not found");
    assert.match(dot[1], /width:\s*9px/);
    assert.match(dot[1], /height:\s*9px/);

    const line = /\.interaction-flow-guide::after\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(line, "line rule not found");
    assert.match(line[1], /left:\s*4px/);
    assert.match(line[1], /width:\s*1px/);
    assert.match(line[1], /transform:\s*scaleY\(0\)/);
    assert.match(CSS, /\.interaction-flow\[open\] \.interaction-flow-guide::after\s*\{[\s\S]*?scaleY\(1\)/);
    assert.doesNotMatch(CSS, /\.interaction-flow::before\s*\{/);
    assert.doesNotMatch(CSS, /\.interaction-flow-chevron/);
});

test("dot geometry derives its center from the seconds box and the measured lift", () => {
    const api = loadApp();
    const props = {};
    api.__document.documentElement = {
        style: {
            setProperty: (name, value) => { props[name] = value; },
            removeProperty: (name) => { delete props[name]; },
        },
    };
    const flow = { offsetParent: {}, getBoundingClientRect: () => ({ top: 60, height: 47 }) };
    const usage = new api.HTMLElement();
    usage.closest = () => flow;
    usage.getBoundingClientRect = () => ({ top: 100, height: 23 });
    api.__document.querySelectorAll = () => [usage];

    api.syncCeoFlowDotGeometry();

    // 圆心 = 秒数盒中心 (100-60) + 23/2 = 51.5，再减 Fira Sans 的 0.5px 抬升。
    assert.equal(props["--ceo-flow-dot-y"], "51px");
});